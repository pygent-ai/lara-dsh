"""lara_shell — the lara-dsh environment layer on top of ``pygent.agent.pygent_v1``.

Lara lives inside dsh's home: user-owned state (config.yaml, credentials,
project sessions, pet settings, dsh routing) sits under ``<dsh home>/lara``,
where the dsh home is ``$DSH_HOME`` or ``~/.dsh`` — the same directory dsh's
own front ends use, so there is exactly one place for sessions, credentials
and settings. :func:`user_lara_root` is the single source of that decision
and the composition root injects it into ``RunConfig.user_pygent_root`` so
the kernel never hard-codes a brand path.

The first run after the move copies a legacy ``~/.lara-dsh`` into place;
the legacy directory is left untouched.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

LARA_SUBDIR = "lara"
LEGACY_ROOT = Path.home() / ".lara-dsh"
# Legacy entries that belonged to the old private dsh home, not to Lara.
_LEGACY_SKIP = {"dsh-home", "dsh-runtime-cwd"}


def dsh_home() -> Path:
    """dsh's home directory: ``$DSH_HOME`` when set, else ``~/.dsh``."""
    configured = os.environ.get("DSH_HOME", "").strip()
    return (Path(configured) if configured else Path.home() / ".dsh").expanduser().resolve()


def user_lara_root() -> Path:
    """Return Lara's user data root, ``<dsh home>/lara``, migrating a legacy root once."""
    root = dsh_home() / LARA_SUBDIR
    if not root.exists() and LEGACY_ROOT.is_dir():
        _copy_legacy_root(LEGACY_ROOT, root)
    return root


def _copy_legacy_root(legacy: Path, root: Path) -> None:
    root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        legacy,
        root,
        ignore=lambda directory, names: [n for n in names if n in _LEGACY_SKIP and Path(directory) == legacy],
    )
    print(f"lara: copied legacy user root {legacy} -> {root} (the legacy directory was left in place)", file=sys.stderr)


__all__ = ["LARA_SUBDIR", "LEGACY_ROOT", "dsh_home", "user_lara_root"]
