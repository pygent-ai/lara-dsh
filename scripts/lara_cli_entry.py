"""Frozen-entry launcher for the `lara` CLI (PyInstaller builds this file).

The wheel's console script (`lara = lara_shell.ops.cli.main:main`) is not
available to a frozen build, so the freeze runs this module instead; it only
routes to the real CLI entry point.
"""

from lara_shell.ops.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
