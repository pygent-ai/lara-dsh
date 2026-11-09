"""Shared CLI run-config assembly."""

from __future__ import annotations

import argparse
from collections.abc import Callable

from lara.config import load_run_config
from lara.schema import RunConfig


def config_from_args(
    args: argparse.Namespace,
    *,
    session_id: str | None = None,
    loader: Callable[..., RunConfig] = load_run_config,
) -> RunConfig:
    return loader(
        workspace_root=args.workspace_root,
        session_id=session_id,
        agent_alias=args.agent_alias,
        max_steps=args.max_steps,
    )
