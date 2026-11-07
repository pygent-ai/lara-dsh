"""lara CLI（抽取版）：仅保留会话相关子命令。

原仓的 case / regression / test / repair / optimize / automations 子命令依赖
已裁剪的 evaluation / repair / automations / workflows 子系统，故不在此仓。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from lara.cli.credentials import register_credentials_parser
from lara.cli.projects import register_projects_parser
from lara.cli.sessions import register_session_parser
from lara.config import load_run_config
from lara.sessions import SessionManager


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary should render readable errors.
        print(f"lara: error: {exc}", file=sys.stderr)
        return 2
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lara", description="Standalone agent runtime CLI (extracted from lara)"
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="Workspace root. Defaults to cwd or LARA_WORKSPACE_ROOT.",
    )
    parser.add_argument(
        "--agent", dest="agent_alias", default=None, help="Agent profile alias."
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="Maximum agent steps; -1 means unlimited.",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    register_session_parser(sub)
    register_credentials_parser(sub)
    register_projects_parser(sub)
    return parser


def _manager(args: argparse.Namespace) -> SessionManager:
    return SessionManager(
        load_run_config(
            workspace_root=args.workspace_root,
            agent_alias=args.agent_alias,
            max_steps=args.max_steps,
        )
    )
