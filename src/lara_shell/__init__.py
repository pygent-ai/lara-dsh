"""lara_shell — the lara-dsh environment layer on top of ``pygent.agent.pygent_v1``.

Lara lives inside dsh's home: user-owned state (config.yaml, credentials,
project sessions, pet settings, dsh routing) sits under ``<dsh home>/lara``,
where the dsh home is ``$DSH_HOME`` or ``~/.dsh`` — the same directory dsh's
own front ends use, so there is exactly one place for sessions, credentials
and settings. :func:`user_lara_root` is the single source of that decision
and the composition root injects it into ``RunConfig.user_pygent_root`` so
the kernel never hard-codes a brand path.
"""

from __future__ import annotations

import os
from pathlib import Path

LARA_SUBDIR = "lara"


def dsh_home(home: str | Path | None = None) -> Path:
    """Resolve the selected dsh home against this process's launch directory."""
    if home is not None:
        return Path(home).expanduser().resolve()
    configured = os.environ.get("DSH_HOME", "").strip()
    return (Path(configured) if configured else Path.home() / ".dsh").expanduser().resolve()


def user_lara_root(home: str | Path | None = None) -> Path:
    """Return the state directory for the selected dsh home."""
    return dsh_home(home) / LARA_SUBDIR


def default_pet_workspace(home: str | Path | None = None) -> Path:
    return user_lara_root(home) / "pet"


__all__ = ["LARA_SUBDIR", "default_pet_workspace", "dsh_home", "user_lara_root"]
