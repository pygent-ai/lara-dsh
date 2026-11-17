"""lara_shell — the lara-dsh environment layer on top of ``pygent.agent.pygent_v1``.

The deployment keeps its user-owned state (config.yaml, credentials, project
sessions, dsh home) under ``~/.lara-dsh``; :func:`user_lara_root` is the single
source of that decision and the composition root injects it into
``RunConfig.user_pygent_root`` so the kernel never hard-codes a brand path.
"""

from __future__ import annotations

from pathlib import Path


def user_lara_root() -> Path:
    """Return the lara-dsh user data root shared by all projects."""
    return (Path.home() / ".lara-dsh").expanduser().resolve()


__all__ = ["user_lara_root"]
