"""lara dsh：Lara 作为 dsh（DeepSeek Harness）系统 agent 的会话控制命令。

常驻的 dsh 运行时由 DshDaemon 持有（pet sidecar 内嵌，或 ``lara dsh daemon``
单独运行），端口发布在 ``~/.lara-dsh/dsh-daemon.json``。``send/wait`` 只在
daemon 存在时可用；``run/resume/sessions/status`` 优先连 daemon，没有时自起一个
临时运行时（命令结束即退出）。

端点、凭据与模型来自 ``~/.lara-dsh/dsh.yaml``（见 lara_shell.dsh_settings），
``DEEPSEEK_BASE_URL / DEEPSEEK_API_KEY / DSH_MODEL / DSH_HOME`` 环境变量可覆盖。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lara_shell import user_lara_root
from lara_shell.dsh_daemon import DaemonUnavailable, DshDaemon, DshDaemonClient, DshEvent, DshEventBatch, SessionView
from lara_shell.dsh_gateway import DshGateway, DshGatewayConfig, DshSessionEntry
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
    send.add_argument("--cwd", default=None, metavar="DIR", help="Working directory for a new session")
    send.set_defaults(handler=_dsh_send)

    wait = commands.add_parser(
        "wait", help="Block until a dsh session is idle and print its last reply (needs the daemon)"
    )
    wait.add_argument("session_id", help="Session id to wait for")
    wait.add_argument("--timeout", type=float, default=None, help="Seconds to wait before giving up")
    wait.set_defaults(handler=_dsh_wait)

    run = commands.add_parser(
        "run", help="Run one prompt and wait for the reply (daemon if running, else a temporary runtime)"
    )
    run.add_argument("prompt", help="Prompt for the dsh agent")
    run.add_argument(
        "--session-id",
        default=None,
        help="Run inside this durable session id instead of a fresh one",
    )
    _add_dsh_target(run)
    run.set_defaults(handler=_dsh_run)

    resume = commands.add_parser(
        "resume", help="Resume a persisted dsh session and run one prompt"
    )
    resume.add_argument("session_id", help="Persisted session id to resume")
    resume.add_argument("prompt", help="Prompt for the resumed session")
    _add_dsh_target(resume)
    resume.set_defaults(handler=_dsh_resume)

    sessions = commands.add_parser(
        "sessions", help="List dsh sessions (persisted and live) with status"
    )
    _add_dsh_target(sessions)
    sessions.set_defaults(handler=_dsh_sessions)

    status = commands.add_parser(
        "status", help="Show one dsh session's inventory entry and live status"
    )
    status.add_argument("session_id", help="Session id to inspect")
    _add_dsh_target(status)
    status.set_defaults(handler=_dsh_status)

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
    _add_dsh_target(daemon)
    daemon.set_defaults(handler=_dsh_daemon)


def _add_dsh_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--home",
        default=None,
        metavar="DIR",
        help="dsh home directory holding durable sessions (default: from dsh.yaml)",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        metavar="DIR",
        help="Working directory the dsh agent operates in (default: current directory)",
    )


def _dsh_config(args: argparse.Namespace) -> DshGatewayConfig:
    settings = load_dsh_settings(user_lara_root())
    config = settings.gateway_config(Path(args.cwd) if args.cwd else Path.cwd())
    if args.home:
        config = DshGatewayConfig(**{**asdict(config), "dsh_home": Path(args.home)})
    return config


def _daemon_client() -> DshDaemonClient | None:
    try:
        return DshDaemonClient.connect(user_lara_root())
    except DaemonUnavailable:
        return None


def _require_daemon() -> DshDaemonClient:
    client = _daemon_client()
    if client is None:
        raise RuntimeError(
            "no dsh daemon is running; start the pet or `lara dsh daemon` first"
        )
    return client


def _dsh_send(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon() as client:
        return client.send(args.prompt, session_id=args.session_id, cwd=args.cwd)


def _dsh_wait(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon() as client:
        view, timed_out = client.wait(args.session_id, timeout=args.timeout)
    return {"session": None if view is None else _view_payload(view), "timed_out": timed_out}


def _dsh_cancel(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon() as client:
        return {"session_id": args.session_id, "cancelled": client.cancel(args.session_id)}


def _dsh_requests(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon() as client:
        return {"requests": client.requests()}


def _dsh_approve(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon() as client:
        return {"outcome": "allowed-once", "request": client.approve(args.request_id, "allowed-once")}


def _dsh_reject(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon() as client:
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
    with _require_daemon() as client:
        return {"request": client.answer(args.request_id, answers)}


def _dsh_decline(args: argparse.Namespace) -> dict[str, Any]:
    with _require_daemon() as client:
        return {"request": client.decline(args.request_id)}


def _dsh_run(args: argparse.Namespace) -> dict[str, Any]:
    client = _daemon_client()
    if client is not None:
        with client:
            sent = client.send(args.prompt, session_id=args.session_id, cwd=args.cwd)
            view, _ = client.wait(sent["session_id"])
        assert view is not None
        return {"session_id": view.session_id, "response": view.reply, "finish_reason": view.finish_reason, "error": view.error, "via": "daemon"}
    config = _dsh_config(args)
    with DshGateway(config) as gateway:
        response, session_id = gateway.run(args.prompt, session_id=args.session_id)
    return _dsh_result(session_id, response, config)


def _dsh_resume(args: argparse.Namespace) -> dict[str, Any]:
    client = _daemon_client()
    if client is not None:
        with client:
            client.resume(args.session_id)
            client.send(args.prompt, session_id=args.session_id)
            view, _ = client.wait(args.session_id)
        assert view is not None
        return {"session_id": view.session_id, "response": view.reply, "finish_reason": view.finish_reason, "error": view.error, "via": "daemon"}
    config = _dsh_config(args)
    with DshGateway(config) as gateway:
        response, session_id = gateway.resume_run(args.session_id, args.prompt)
    return _dsh_result(session_id, response, config)


def _dsh_sessions(args: argparse.Namespace) -> dict[str, Any]:
    client = _daemon_client()
    if client is not None:
        with client:
            views = client.sessions()
        return {"via": "daemon", "sessions": [_view_payload(view) for view in views]}
    config = _dsh_config(args)
    with DshGateway(config) as gateway:
        entries = gateway.list_sessions()
    return {
        "home": str(config.dsh_home),
        "sessions": [_session_payload(entry) for entry in entries],
    }


def _dsh_status(args: argparse.Namespace) -> dict[str, Any]:
    client = _daemon_client()
    if client is not None:
        with client:
            view = client.status(args.session_id)
        return {"via": "daemon", "session": None if view is None else _view_payload(view)}
    config = _dsh_config(args)
    with DshGateway(config) as gateway:
        entry = gateway.stat_session(args.session_id)
    return {
        "home": str(config.dsh_home),
        "session": None if entry is None else _session_payload(entry),
    }


def _with_runtime(call_daemon, call_gateway):
    """Run against the daemon when one is up, else a temporary runtime."""
    client = _daemon_client()
    if client is not None:
        with client:
            return call_daemon(client)
    with DshGateway(load_dsh_settings(user_lara_root()).gateway_config(Path.cwd())) as gateway:
        return call_gateway(gateway)


def _dsh_models(args: argparse.Namespace) -> dict[str, Any]:
    providers = _with_runtime(lambda c: c.models(), lambda g: g.list_models())
    settings = load_dsh_settings(user_lara_root())
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
            lambda c: c.set_credential(args.ref, value),
            lambda g: g.set_credential(args.ref, value),
        )
    info = _with_runtime(lambda c: c.describe_credential(args.ref), lambda g: g.describe_credential(args.ref))
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

    settings = load_dsh_settings(user_lara_root())
    state_path = user_lara_root() / WEB_STATE_FILENAME
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
        # The web UI is token-gated: only the URL dsh prints opens it, so run
        # it detached with --no-open, read that line, and remember it.
        process = subprocess.Popen(
            [*resolve_bundled_launch_args(), "--profile", "web", "--no-open", "--port", "0"],
            env=dict(os.environ, DSH_HOME=str(settings.dsh_home)),
            cwd=str(settings.workspace) if settings.workspace.is_dir() else None,
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
    root = user_lara_root()
    changed: dict[str, str | None] = {}
    for item in args.assignments:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"expected KEY=VALUE, got {item!r}")
        changed[key.strip()] = value.strip() or None
    if changed:
        write_dsh_settings_file(root, changed)
    settings = load_dsh_settings(root)
    daemon_running = _daemon_client()
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

    with DshDaemon(config, state_dir=user_lara_root(), on_event=announce) as daemon:
        print(f"lara dsh daemon: port {daemon.port}, home {config.dsh_home}", flush=True)
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        while not stop.wait(0.5):
            pass


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
    session_id: str, response: str, config: DshGatewayConfig
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "response": response,
        "home": str(config.dsh_home),
        "cwd": str(config.cwd),
    }


__all__ = ["register_dsh_parser"]
