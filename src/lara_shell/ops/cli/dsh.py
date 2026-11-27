"""lara dsh：Lara 作为 dsh（DeepSeek Harness）系统 agent 的会话控制命令。

常驻的 dsh 运行时由 DshDaemon 持有（pet sidecar 内嵌，或 ``lara dsh daemon``
单独运行），端口发布在 ``<dsh home>/lara/dsh-daemon.json``。``send/wait`` 只在
daemon 存在时可用；``run/resume/sessions/status`` 优先连 daemon，没有时自起一个
临时运行时（命令结束即退出）。

路由来自 ``<dsh home>/lara/dsh.yaml``（见 lara_shell.dsh_settings），凭据在 dsh
自己的凭据库；``DSH_HOME / DSH_MODEL / DEEPSEEK_BASE_URL`` 环境变量可覆盖。
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lara_shell import dsh_home, user_lara_root
from lara_shell.config.loader import resolve_workspace
from lara_shell.dsh_daemon import DaemonUnavailable, DshDaemon, DshDaemonClient, DshEvent, DshEventBatch, SessionView
from lara_shell.dsh_gateway import DshGatewayConfig, DshSessionEntry, open_gateway
from lara_shell.dsh_settings import (
    DEEPSEEK_API_KEY_REF,
    DSH_SETTINGS_FILENAME,
    SETTABLE_KEYS,
    load_dsh_settings,
    read_dsh_settings_file,
    write_dsh_settings_file,
)


def register_dsh_parser(subparsers: Any) -> None:
    dsh = subparsers.add_parser(
        "dsh", help="Manage DeepSeek Harness (dsh) agent sessions"
    )
    commands = dsh.add_subparsers(dest="dsh_command", required=True)

    send = commands.add_parser(
        "send", help="Queue a prompt on a new or existing dsh session and return at once (needs the daemon)"
    )
    send.add_argument("prompt", help="Prompt for the dsh agent")
    send.add_argument("--session-id", default=None, help="Continue this session instead of creating one")
    _add_cwd_arg(send)
    send.set_defaults(handler=_dsh_send)

    wait = commands.add_parser(
        "wait", help="Block until a dsh session is idle and print its last reply (needs the daemon)"
    )
    wait.add_argument("session_id", help="Session id to wait for")
    wait.add_argument("--timeout", type=float, default=None, help="Seconds to wait before giving up")
    wait.set_defaults(handler=_dsh_wait)

    run = commands.add_parser(
        "run", help="Run one prompt and wait for the reply (daemon if running, else the user's dsh or a temporary runtime)"
    )
    run.add_argument("prompt", help="Prompt for the dsh agent")
    run.add_argument(
        "--session-id",
        default=None,
        help="Run inside this durable session id instead of a fresh one",
    )
    _add_cwd_arg(run)
    run.set_defaults(handler=_dsh_run)

    resume = commands.add_parser(
        "resume", help="Resume a persisted dsh session and run one prompt"
    )
    resume.add_argument("session_id", help="Persisted session id to resume")
    resume.add_argument("prompt", help="Prompt for the resumed session")
    resume.set_defaults(handler=_dsh_resume)

    sessions = commands.add_parser(
        "sessions", help="List dsh sessions (persisted and live) with status"
    )
    sessions.add_argument(
        "--all", action="store_true",
        help="Include blank sessions (never prompted, e.g. the one dsh web opens on start); hidden by default",
    )
    sessions.set_defaults(handler=_dsh_sessions)

    status = commands.add_parser(
        "status", help="Show one dsh session's inventory entry and live status"
    )
    status.add_argument("session_id", help="Session id to inspect")
    status.set_defaults(handler=_dsh_status)

    artifact = commands.add_parser("artifact", help="Stage a file from a dsh session in the Lara workspace")
    artifact.add_argument("session_id", help="Session that produced the file")
    artifact.add_argument("path", help="File path within the session workspace")
    artifact.set_defaults(handler=_dsh_artifact)

    cancel = commands.add_parser(
        "cancel", help="Stop a running dsh session's current work (needs the daemon)"
    )
    cancel.add_argument("session_id", help="Session id to cancel")
    cancel.set_defaults(handler=_dsh_cancel)

    requests = commands.add_parser(
        "requests", help="List dsh questions and approvals waiting for an answer (needs the daemon)"
    )
    requests.set_defaults(handler=_dsh_requests)

    approve = commands.add_parser(
        "approve", help="Allow a pending dsh approval request once (needs the daemon)"
    )
    approve.add_argument("request_id", help="Request id from the approval_request event")
    approve.set_defaults(handler=_dsh_approve)

    reject = commands.add_parser(
        "reject", help="Reject a pending dsh approval request (needs the daemon)"
    )
    reject.add_argument("request_id", help="Request id from the approval_request event")
    reject.set_defaults(handler=_dsh_reject)

    answer = commands.add_parser(
        "answer", help="Answer a pending dsh question (needs the daemon)"
    )
    answer.add_argument("request_id", help="Request id from the question event")
    answer.add_argument(
        "answers",
        nargs="+",
        help="One answer for every question, or per-question 'question_id=answer' pairs. "
        "An answer equal to an option label selects it; other text is a free-form answer.",
    )
    answer.set_defaults(handler=_dsh_answer)

    decline = commands.add_parser(
        "decline", help="Withdraw a pending dsh question so its tool call fails (needs the daemon)"
    )
    decline.add_argument("request_id", help="Request id from the question event")
    decline.set_defaults(handler=_dsh_decline)

    config = commands.add_parser(
        "config", help=f"Show or change dsh routing ({DSH_SETTINGS_FILENAME}: {', '.join(SETTABLE_KEYS)})"
    )
    config.add_argument(
        "assignments",
        nargs="*",
        metavar="KEY=VALUE",
        help="Settings to change, e.g. model=deepseek-v4-flash base_url=https://host/v1; "
        f"an empty value unsets the key. Keys: {', '.join(SETTABLE_KEYS)}. "
        "The running daemon keeps its current settings until restarted.",
    )
    config.set_defaults(handler=_dsh_config_cmd)

    models = commands.add_parser(
        "models", help="List the providers and models dsh can use (from its own configuration)"
    )
    models.set_defaults(handler=_dsh_models)

    credentials = commands.add_parser(
        "credentials", help="Show or store a secret in dsh's credential store (never prints values)"
    )
    credentials.add_argument(
        "ref", nargs="?", default=DEEPSEEK_API_KEY_REF,
        help=f"Credential reference, e.g. {DEEPSEEK_API_KEY_REF} (default)",
    )
    credentials.add_argument(
        "--set", dest="set_value", action="store_true",
        help="Read the new value from stdin (or an interactive prompt) and store it",
    )
    credentials.add_argument("--unset", action="store_true", help="Remove the stored value")
    credentials.set_defaults(handler=_dsh_credentials)

    open_cmd = commands.add_parser(
        "open", help="Open dsh's own web UI on the shared home so the user can browse sessions"
    )
    open_cmd.add_argument("session_id", nargs="?", default=None, help="Session to point the user at (listed, not deep-linked)")
    open_cmd.set_defaults(handler=_dsh_open)

    daemon = commands.add_parser(
        "daemon", help="Run a standalone dsh daemon in the foreground (Ctrl+C to stop)"
    )
    daemon.set_defaults(handler=_dsh_daemon)

    plugin = commands.add_parser(
        "plugin", help="Install or inspect Lara's plugin inside dsh (needed to attach to the user's own dsh)"
    )
    plugin.add_argument(
        "action", choices=("install", "status"),
        help="install: `dsh plugin add` the bundled plugin into a dsh profile; status: installed version and live endpoint",
    )
    plugin.add_argument("--profile", default="web", help="dsh profile to install into (default: web, the desktop/web UI)")
    plugin.set_defaults(handler=_dsh_plugin)

    for command in commands.choices.values():
        command.add_argument(
            "--home",
            default=None,
            metavar="DIR",
            help="dsh home directory holding durable sessions (default: $DSH_HOME or ~/.dsh)",
        )


def _add_cwd_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cwd",
        default=None,
        metavar="DIR",
        help="Working directory for a new session (default: configured dsh workspace)",
    )


def _cwd_arg(args: argparse.Namespace) -> Path | None:
    cwd = getattr(args, "cwd", None)
    if cwd and getattr(args, "session_id", None):
        raise ValueError("--cwd applies only to new sessions")
    return Path(cwd).expanduser().resolve() if cwd else None


def _dsh_config(args: argparse.Namespace) -> DshGatewayConfig:
    return load_dsh_settings(args.home).gateway_config(_cwd_arg(args))


def _daemon_client(home: str | Path | None) -> DshDaemonClient | None:
    home = dsh_home(home)
    try:
        return DshDaemonClient.connect(user_lara_root(home), home=home)
    except DaemonUnavailable:
        return None


def _require_daemon(home: str | Path | None) -> DshDaemonClient:
    client = _daemon_client(home)
    if client is None:
        raise RuntimeError(
            "no dsh daemon is running; start the pet or `lara dsh daemon` first"
        )
    return client


def _dsh_send(args: argparse.Namespace) -> dict[str, Any]:
    config = _dsh_config(args)
    with _require_daemon(config.dsh_home) as client:
        return client.send(
            args.prompt, session_id=args.session_id,
            cwd=None if args.session_id else config.cwd,
        )


def _dsh_wait(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon(args.home) as client:
        view, timed_out = client.wait(args.session_id, timeout=args.timeout)
    return {"session": None if view is None else _view_payload(view), "timed_out": timed_out}


def _dsh_cancel(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon(args.home) as client:
        return {"session_id": args.session_id, "cancelled": client.cancel(args.session_id)}


def _dsh_requests(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon(args.home) as client:
        return {"requests": client.requests()}


def _dsh_approve(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon(args.home) as client:
        return {"outcome": "allowed-once", "request": client.approve(args.request_id, "allowed-once")}


def _dsh_reject(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon(args.home) as client:
        return {"outcome": "rejected", "request": client.approve(args.request_id, "rejected")}


def _dsh_answer(args: argparse.Namespace) -> dict[str, Any]:
    answers: dict[str, str] | str
    if len(args.answers) == 1 and "=" not in args.answers[0]:
        answers = args.answers[0]
    else:
        pairs: dict[str, str] = {}
        for item in args.answers:
            key, sep, value = item.partition("=")
            if not sep or not key.strip():
                raise ValueError(f"expected 'question_id=answer', got {item!r}")
            pairs[key.strip()] = value
        answers = pairs
    with _require_daemon(args.home) as client:
        return {"request": client.answer(args.request_id, answers)}


def _dsh_decline(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon(args.home) as client:
        return {"request": client.decline(args.request_id)}


def _dsh_run(args: argparse.Namespace) -> dict[str, Any]:
    config = _dsh_config(args)
    client = _daemon_client(config.dsh_home)
    if client is not None:
        with client:
            sent = client.send(
                args.prompt, session_id=args.session_id,
                cwd=None if args.session_id else config.cwd,
            )
            view, _ = client.wait(sent["session_id"])
        assert view is not None
        return _daemon_result(view, config.dsh_home)
    with open_gateway(config) as gateway:
        response, session_id = gateway.run(args.prompt, session_id=args.session_id)
        entry = gateway.stat_session(session_id)
    return _dsh_result(session_id, response, config.dsh_home, entry, gateway.mode)


def _dsh_resume(args: argparse.Namespace) -> dict[str, Any]:
    client = _daemon_client(args.home)
    if client is not None:
        with client:
            client.resume(args.session_id)
            client.send(args.prompt, session_id=args.session_id)
            view, _ = client.wait(args.session_id)
        assert view is not None
        return _daemon_result(view, dsh_home(args.home))
    config = _dsh_config(args)
    with open_gateway(config) as gateway:
        response, session_id = gateway.resume_run(args.session_id, args.prompt)
        entry = gateway.stat_session(session_id)
    return _dsh_result(session_id, response, config.dsh_home, entry, gateway.mode)


# A root session whose log holds only its opening records (policy, model
# selection), i.e. nothing was ever prompted; the dsh web UI opens one such
# session on every start. Subagent children are born from a prompt, and a
# persisted-only entry reports no count: neither is ever hidden.
BLANK_SESSION_MAX_EVENTS = 3


def _is_blank_session(payload: dict[str, Any]) -> bool:
    event_count = payload.get("event_count")
    return (
        payload.get("origin") == "root"
        and not payload.get("title")
        and payload.get("status") != "running"
        and not payload.get("turns")
        and isinstance(event_count, int)
        and event_count <= BLANK_SESSION_MAX_EVENTS
    )


def _dsh_sessions(args: argparse.Namespace) -> dict[str, Any]:
    show_all = bool(getattr(args, "all", False))
    client = _daemon_client(args.home)
    if client is not None:
        with client:
            views = client.sessions()
        payloads = [_view_payload(view) for view in views]
        hidden = sum(_is_blank_session(p) for p in payloads)
        if not show_all:
            payloads = [p for p in payloads if not _is_blank_session(p)]
        return {"via": "daemon", "home": str(dsh_home(args.home)), "sessions": payloads, "hidden_blank": 0 if show_all else hidden}
    config = _dsh_config(args)
    with open_gateway(config) as gateway:
        entries = gateway.list_sessions()
    payloads = [_session_payload(entry) for entry in entries]
    hidden = sum(_is_blank_session(p) for p in payloads)
    if not show_all:
        payloads = [p for p in payloads if not _is_blank_session(p)]
    return {
        "via": gateway.mode,
        "home": str(config.dsh_home),
        "sessions": payloads,
        "hidden_blank": 0 if show_all else hidden,
    }


def _dsh_status(args: argparse.Namespace) -> dict[str, Any]:
    client = _daemon_client(args.home)
    if client is not None:
        with client:
            view = client.status(args.session_id)
        return {"via": "daemon", "home": str(dsh_home(args.home)), "session": None if view is None else _view_payload(view)}
    config = _dsh_config(args)
    with open_gateway(config) as gateway:
        entry = gateway.stat_session(args.session_id)
    return {
        "via": gateway.mode,
        "home": str(config.dsh_home),
        "session": None if entry is None else _session_payload(entry),
    }


def _dsh_artifact(args: argparse.Namespace) -> dict[str, str]:
    session = _dsh_status(args)["session"]
    if session is None or not session.get("cwd"):
        raise ValueError(f"session {args.session_id!r} has no recorded workspace")
    session_root = Path(session["cwd"])
    if not session_root.is_absolute():
        raise ValueError("the session workspace must be an absolute path")
    session_root = session_root.resolve(strict=True)
    source = (session_root / args.path).resolve(strict=True)
    if not source.is_relative_to(session_root) or not source.is_file():
        raise ValueError("artifact must be a file inside the dsh session workspace")

    workspace = resolve_workspace(args.workspace_root)
    if source.is_relative_to(workspace):
        return {"path": source.relative_to(workspace).as_posix()}

    staging = (workspace / "dsh-artifacts").resolve()
    if not staging.is_relative_to(workspace):
        raise ValueError("the artifact staging directory must be inside the Lara workspace")
    staging.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(dir=staging))
    destination = directory / source.name
    try:
        shutil.copyfile(source, destination)
    except OSError:
        destination.unlink(missing_ok=True)
        directory.rmdir()
        raise
    return {"path": destination.relative_to(workspace).as_posix()}


def _with_runtime(home, call_daemon, call_gateway):
    """Run against the daemon when one is up, else attach to the user's dsh or spawn a temporary runtime."""
    client = _daemon_client(home)
    if client is not None:
        with client:
            return call_daemon(client)
    with open_gateway(load_dsh_settings(home).gateway_config()) as gateway:
        return call_gateway(gateway)


def _dsh_models(args: argparse.Namespace) -> dict[str, Any]:
    providers = _with_runtime(args.home, lambda c: c.models(), lambda g: g.list_models())
    settings = load_dsh_settings(args.home)
    return {
        "selected": {"provider": settings.provider, "model": settings.model, "lara_model": settings.lara_model},
        "providers": providers,
        "note": "`lara dsh config provider=<id> model=<id>` selects; a provider with no models usually lacks its credential",
    }


def _dsh_credentials(args: argparse.Namespace) -> dict[str, Any]:
    if args.set_value and args.unset:
        raise ValueError("--set and --unset are mutually exclusive")
    value: str | None = None
    if args.set_value:
        from lara_shell.credentials import prompt_for_secret

        value = sys.stdin.readline().strip() if not sys.stdin.isatty() else prompt_for_secret(f"{args.ref}: ")
        if not value:
            raise ValueError("no value given")
    if args.set_value or args.unset:
        _with_runtime(
            args.home,
            lambda c: c.set_credential(args.ref, value),
            lambda g: g.set_credential(args.ref, value),
        )
    info = _with_runtime(args.home, lambda c: c.describe_credential(args.ref), lambda g: g.describe_credential(args.ref))
    return {"ref": args.ref, **info, "changed": bool(args.set_value or args.unset)}


WEB_STATE_FILENAME = "dsh-web.json"


def _dsh_open(args: argparse.Namespace) -> dict[str, Any]:
    """Open dsh's own web UI on the shared home (start it once, then reuse)."""
    import json
    import re
    import socket
    import subprocess
    import webbrowser

    from deepseek_harness_runtime import resolve_bundled_launch_args

    settings = load_dsh_settings(args.home)
    state_path = user_lara_root(settings.dsh_home) / WEB_STATE_FILENAME
    url: str | None = None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        port = int(state["port"])
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0 and state.get("home") == str(settings.dsh_home):
                url = str(state["url"])
    except (OSError, ValueError, KeyError, TypeError):
        url = None
    started = False
    if url is None:
        workspace = settings.gateway_config().prepare_workspace()
        # The web UI is token-gated: only the URL dsh prints opens it, so run
        # it detached with --no-open, read that line, and remember it. The
        # web profile carries the Lara plugin once `lara dsh plugin install`
        # ran, so this runtime is also what the daemon attaches to. It gets
        # the endpoint Lara's dsh.yaml names and dsh's own credential store,
        # never a DEEPSEEK_API_KEY Lara's shell happens to export.
        env = {k: v for k, v in os.environ.items() if k not in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")}
        env["DSH_HOME"] = str(settings.dsh_home)
        if settings.base_url:
            env["DEEPSEEK_BASE_URL"] = settings.base_url
        process = subprocess.Popen(
            [*resolve_bundled_launch_args(), "--profile", "web", "--no-open", "--port", "0"],
            env=env,
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        assert process.stdout is not None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            match = re.search(r"https?://\S+", line)
            if match and "dsh web" in line:
                url = match.group(0)
                break
        if url is None:
            process.kill()
            raise RuntimeError("dsh web did not print its URL; is the web profile available in this runtime?")
        process.stdout.close()  # the server keeps running detached
        port = int(url.split(":")[2].split("/")[0])
        state_path.write_text(json.dumps({"url": url, "port": port, "pid": process.pid, "home": str(settings.dsh_home)}), encoding="utf-8")
        started = True
    webbrowser.open(url)
    return {
        "url": url,
        "home": str(settings.dsh_home),
        "started": started,
        "session_id": args.session_id,
        "note": "dsh web lists every session in this home; pick the session there" if args.session_id else None,
    }


def _dsh_config_cmd(args: argparse.Namespace) -> dict[str, Any]:
    root = user_lara_root(args.home)
    changed: dict[str, str | None] = {}
    for item in args.assignments:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"expected KEY=VALUE, got {item!r}")
        changed[key.strip()] = value.strip() or None
    if changed:
        write_dsh_settings_file(root, changed)
    settings = load_dsh_settings(args.home)
    daemon_running = _daemon_client(args.home)
    if daemon_running is not None:
        daemon_running.close()
    return {
        "file": str(root / DSH_SETTINGS_FILENAME),
        "settings": read_dsh_settings_file(root),
        "effective": {
            "home": str(settings.dsh_home),
            "provider": settings.provider,
            "model": settings.model,
            "base_url": settings.base_url,
            "workspace": str(settings.workspace),
            "lara_model": settings.lara_model,
        },
        "changed": sorted(changed),
        "daemon_running": daemon_running is not None,
        "note": "restart the pet or `lara dsh daemon` for a running daemon to pick up changes" if changed and daemon_running else None,
    }


def _dsh_daemon(args: argparse.Namespace) -> None:
    config = _dsh_config(args)
    stop = threading.Event()

    def announce(event: DshEvent | DshEventBatch) -> None:
        print(event.envelope(), flush=True)

    with DshDaemon(config, state_dir=user_lara_root(config.dsh_home), on_event=announce) as daemon:
        print(f"lara dsh daemon: port {daemon.port}, home {config.dsh_home}, mode {daemon.mode}", flush=True)
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        while not stop.wait(0.5):
            pass


def _dsh_plugin(args: argparse.Namespace) -> dict[str, Any]:
    """Install Lara's dsh plugin into a profile the way any dsh plugin is installed.

    ``dsh plugin --profile <p> add file:<bundled plugin dir>`` makes the user's
    own dsh (desktop / ``dsh web`` on that profile) load the plugin, which
    publishes the endpoint the daemon attaches to. Reinstalling upgrades it.
    """
    import json
    import subprocess

    from deepseek_harness_runtime import resolve_bundled_launch_args

    from lara_shell.dsh_gateway import SESSION_CONTROL_PLUGIN_DIR, probe_plugin_endpoint

    settings = load_dsh_settings(args.home)
    # Prefer the assembled npm package (plugin + bundled pet/sidecar/CLI under
    # `bin/`); the bare source plugin dir is the fallback for a checkout that
    # has not run scripts/build_npm_package.py yet.
    repo_root = Path(__file__).resolve().parents[4]
    packaged = repo_root / "dist" / "npm" / "lara-dsh"
    target = packaged if (packaged / "package.json").is_file() else SESSION_CONTROL_PLUGIN_DIR
    manifest = json.loads((target / "package.json").read_text(encoding="utf-8"))
    profile_manifest = settings.dsh_home / "profiles" / args.profile / "package.json"
    log = ""
    if args.action == "install":
        # pnpm keeps a `file:` dependency it already resolved, so an upgrade
        # of the bundled plugin only lands after a remove; a missing package
        # makes the remove a harmless no-op.
        for pnpm_args in (["remove", manifest["name"]], ["add", f"file:{target}"]):
            completed = subprocess.run(
                [*resolve_bundled_launch_args(), "plugin", "--profile", args.profile, *pnpm_args],
                env=dict(os.environ, DSH_HOME=str(settings.dsh_home)),
                cwd=str(settings.dsh_home),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
            log = (log + "\n" + completed.stdout + completed.stderr).strip()
            if completed.returncode != 0 and pnpm_args[0] == "add":
                raise RuntimeError(f"dsh plugin add failed (exit {completed.returncode}):\n{log}")
    installed: str | None = None
    try:
        dependencies = json.loads(profile_manifest.read_text(encoding="utf-8")).get("dependencies") or {}
        spec = dependencies.get(manifest["name"])
        installed = str(spec) if spec else None
    except (OSError, ValueError):
        installed = None
    endpoint = probe_plugin_endpoint(settings.dsh_home)
    return {
        "action": args.action,
        "plugin": manifest["name"],
        "bundled_version": manifest["version"],
        "profile": args.profile,
        "profile_manifest": str(profile_manifest),
        "installed": installed,
        "live": None if endpoint is None else {"port": endpoint.port, "pid": endpoint.pid, "version": endpoint.version, "attach": endpoint.attach},
        "log": log or None,
        "note": None if installed else f"not installed; run `lara dsh plugin install --profile {args.profile}`",
    }


def _view_payload(view: SessionView) -> dict[str, Any]:
    payload = view.to_wire()
    payload["resumable"] = view.origin != "subagent"
    return payload


def _session_payload(entry: DshSessionEntry) -> dict[str, Any]:
    return {
        "session_id": entry.session_id,
        "title": entry.title or "",
        "cwd": entry.cwd or "",
        "created_at": entry.created_at,
        "origin": entry.origin,
        "parent_session_id": entry.parent_session_id,
        "event_count": entry.event_count,
        "live": entry.live,
        "status": entry.status,
        # The SDK server refuses to resume subagent sessions directly.
        "resumable": entry.origin != "subagent",
    }


def _dsh_result(
    session_id: str, response: str, home: Path, entry: DshSessionEntry | None, via: str
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "response": response,
        "via": via,
        "home": str(home),
        "cwd": None if entry is None else entry.cwd,
    }


def _daemon_result(view: SessionView, home: Path) -> dict[str, Any]:
    return {
        "session_id": view.session_id,
        "response": view.reply,
        "finish_reason": view.finish_reason,
        "error": view.error,
        "via": "daemon",
        "home": str(home),
        "cwd": view.cwd,
    }


__all__ = ["register_dsh_parser"]
