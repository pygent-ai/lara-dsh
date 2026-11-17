"""lara CLI：会话、凭据、调度器与 dsh 子命令，运行在 ``pygent.agent.pygent_v1`` 之上。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from lara_shell.ops.cli.credentials import register_credentials_parser
from lara_shell.ops.cli.dsh import register_dsh_parser
from lara_shell.ops.cli.scheduler import register_scheduler_parser
from lara_shell.ops.cli.sessions import register_session_parser


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
    register_scheduler_parser(sub)
    register_dsh_parser(sub)
    return parser
