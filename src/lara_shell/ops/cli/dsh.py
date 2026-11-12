"""lara dsh：通过 DshGateway 驱动 DeepSeek Harness（dsh）会话。

凭据沿用 DshGatewayConfig.from_env 的环境变量约定：
DEEPSEEK_BASE_URL / DEEPSEEK_API_KEY / DSH_MODEL。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from lara.dsh_gateway import DshGateway, DshGatewayConfig

DEFAULT_DSH_HOME = Path.home() / ".lara-dsh" / "dsh-home"


def register_dsh_parser(subparsers: Any) -> None:
    dsh = subparsers.add_parser(
        "dsh", help="Run DeepSeek Harness (dsh) agent sessions"
    )
    commands = dsh.add_subparsers(dest="dsh_command", required=True)

    run = commands.add_parser(
        "run", help="Run one prompt in a new or chosen durable dsh session"
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
        "sessions", help="List durable dsh sessions available for resume"
    )
    sessions.add_argument(
        "--home",
        default=None,
        metavar="DIR",
        help="dsh home directory holding durable sessions "
        f"(default: {DEFAULT_DSH_HOME})",
    )
    sessions.set_defaults(handler=_dsh_sessions)


def _add_dsh_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--home",
        default=None,
        metavar="DIR",
        help="dsh home directory holding durable sessions "
        f"(default: {DEFAULT_DSH_HOME})",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        metavar="DIR",
        help="Working directory the dsh agent operates in "
        "(default: current directory)",
    )


def _dsh_config(args: argparse.Namespace) -> DshGatewayConfig:
    home = Path(args.home) if args.home else DEFAULT_DSH_HOME
    cwd = Path(args.cwd) if args.cwd else Path.cwd()
    return DshGatewayConfig.from_env(home, cwd)


def _dsh_run(args: argparse.Namespace) -> dict[str, Any]:
    config = _dsh_config(args)
    with DshGateway(config) as gateway:
        response, session_id = gateway.run(args.prompt, session_id=args.session_id)
    return _dsh_result(session_id, response, config)


def _dsh_resume(args: argparse.Namespace) -> dict[str, Any]:
    config = _dsh_config(args)
    with DshGateway(config) as gateway:
        response, session_id = gateway.resume_run(args.session_id, args.prompt)
    return _dsh_result(session_id, response, config)


def _dsh_sessions(args: argparse.Namespace) -> dict[str, Any]:
    home = Path(args.home) if args.home else DEFAULT_DSH_HOME
    store = home / "storages" / "session_projcache" / "sessions"
    durable_dir = home / "sessions"

    sessions = []
    for path in sorted(store.glob("*.json")):
        try:
            record = (json.loads(path.read_text(encoding="utf-8")) or {}).get(
                "record"
            ) or {}
        except (OSError, ValueError):
            continue
        identity = record.get("identity") or {}
        rows = record.get("rows") or {}
        title = (rows.get("title") or {}).get("val")
        created = identity.get("createdAt")
        resumable = any(
            (workspace / path.stem).is_dir()
            for workspace in (durable_dir.iterdir() if durable_dir.is_dir() else ())
            if workspace.is_dir()
        )
        sessions.append(
            {
                "session_id": path.stem,
                "title": str(title) if title else "",
                "cwd": str(identity.get("cwd") or ""),
                "created_at": created,
                "resumable": resumable,
            }
        )

    sessions.sort(key=lambda item: item["created_at"] or 0, reverse=True)
    return {"home": str(home), "sessions": sessions}


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
