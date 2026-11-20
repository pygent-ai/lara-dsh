"""Assemble the `lara-dsh` npm package: the control plugin plus the bundled
pet and sidecar executables, installable with one official command:

    dsh plugin --profile web add lara-dsh            (from npm, once published)
    dsh plugin --profile web add file:dist/npm/lara-dsh   (local build)

Build chain this script expects (run first):
    1. uv run --with pyinstaller pyinstaller pet/sidecar-freeze.spec \
           --noconfirm --distpath .tmp/freeze --workpath .tmp/freeze/_build
       (with VENV_SITE pointing at the project venv's site-packages)
    2. cargo build --release                      (in pet/src-tauri)

usage: uv run python scripts/build_npm_package.py [--out dist/npm]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "src" / "lara_shell" / "dsh_plugin" / "lara-session-control"
FREEZE = ROOT / ".tmp" / "freeze"
SIDECAR = FREEZE / "lara-pet-sidecar.exe"
CLI = FREEZE / "lara.exe"
PET = ROOT / "pet" / "src-tauri" / "target" / "release" / "lara-pet.exe"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "dist" / "npm"))
    args = parser.parse_args()

    for label, path in (
        ("frozen sidecar", SIDECAR),
        ("frozen lara CLI", CLI),
        ("pet executable", PET),
    ):
        if not path.is_file():
            print(f"missing {label}: {path}\nrun the build chain first (see module docstring)", file=sys.stderr)
            return 1

    out = Path(args.out) / "lara-dsh"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # The plugin ships the executables; `files` in package.json covers bin/.
    shutil.copy2(PLUGIN / "index.js", out / "index.js")
    shutil.copy2(PLUGIN / "cordis.patch.yml", out / "cordis.patch.yml")
    bin_dir = out / "bin"
    bin_dir.mkdir()
    shutil.copy2(PET, bin_dir / "lara-pet.exe")
    shutil.copy2(SIDECAR, bin_dir / "lara-pet-sidecar.exe")
    shutil.copy2(CLI, bin_dir / "lara.exe")

    manifest = json.loads((PLUGIN / "package.json").read_text(encoding="utf-8"))
    manifest["name"] = "lara-dsh"
    manifest["description"] = (
        "Lara, the desktop-pet system agent for DeepSeek Harness (dsh): "
        "attaches to this dsh, drives sessions on dsh's own default model, "
        "relays approvals and questions, and launches the pet UI. Ships the "
        "pet and sidecar executables — nothing else to install."
    )
    (out / "package.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # A README helps the npm listing and `pnpm view` consumers.
    (out / "README.md").write_text(
        "# lara-dsh\n\n"
        "Lara, the desktop-pet system agent for DeepSeek Harness (dsh).\n\n"
        "Install into a profile:\n\n"
        "    dsh plugin --profile web add lara-dsh\n\n"
        "Starting dsh launches the pet automatically (kill-on-close job: the pet "
        "lives with the dsh that spawned it). The pet attaches to this dsh and "
        "drives sessions on dsh's own default model; approvals and user "
        "questions are relayed to the pet.\n",
        encoding="utf-8",
    )
    print(f"npm package assembled at {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
