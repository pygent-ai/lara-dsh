# PyInstaller freeze of the two portable executables the `lara-dsh` npm
# package ships in its `bin/` directory:
#
#   lara-pet-sidecar.exe  the pet's sidecar (attach-first: pygent + the dsh
#                         SDK ride along; the 147 MB bundled dsh runtime does
#                         NOT — the portable build attaches to the running
#                         dsh and refuses spawn mode with a clear error)
#   lara.exe              the `lara` CLI, so the pet's agent can run
#                         `lara dsh ...` without a Python wheel on PATH
#
# usage (VENV_SITE = the project venv's site-packages):
#   uv run --with pyinstaller pyinstaller pet/sidecar-freeze.spec \
#         --noconfirm --distpath .tmp/freeze --workpath .tmp/freeze/_build
import os
import sysconfig

from PyInstaller.utils.hooks import collect_all, collect_submodules

site = os.environ.get("VENV_SITE") or sysconfig.get_paths()["purelib"]

def runtime_metadata():
    # bundled_package_dir() requires the package metadata next to the module;
    # the 147 MB runtime/ tree is deliberately left out (attach-only build).
    return [(os.path.join(site, "deepseek_harness_runtime", "deepseek-harness-runtime.json"), "deepseek_harness_runtime")]

def plugin_dir_data():
    # The control plugin is data, not code: `lara dsh plugin status/install`
    # reads its package.json, and the patch layer loads index.js from beside
    # the bundled dsh_gateway module.
    root = os.path.dirname(SPECPATH)  # SPECPATH is the pet/ dir holding this spec
    src = os.path.join(root, "src", "lara_shell", "dsh_plugin", "lara-session-control")
    dest = os.path.join("lara_shell", "dsh_plugin", "lara-session-control")
    return [(os.path.join(src, name), dest) for name in ("index.js", "cordis.patch.yml", "package.json")]

# The sidecar drives the pet's own turns through pygent, and the CLI runs the
# same `lara` operations: both need the full runtime package set.
def full_collection():
    datas, binaries, hidden = [], [], []
    for pkg in ("pygent", "deepseek_harness"):
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hidden += h
    hidden += collect_submodules("deepseek_harness_runtime")
    return datas, binaries, hidden

sd, sb, sh = full_collection()
sidecar = Analysis(
    ["../src/lara_pet/sidecar.py"],
    pathex=["../src"],
    binaries=sb,
    datas=sd + runtime_metadata() + plugin_dir_data(),
    hiddenimports=sh,
    noarchive=False,
)
cd, cb, ch = full_collection()
cli = Analysis(
    ["../scripts/lara_cli_entry.py"],
    pathex=["../src"],
    binaries=cb,
    datas=cd + runtime_metadata() + plugin_dir_data(),
    hiddenimports=ch,
    noarchive=False,
)

EXE(
    PYZ(sidecar.pure),
    sidecar.scripts,
    sidecar.binaries,
    sidecar.datas,
    [],
    name="lara-pet-sidecar",
    console=True,
    upx=False,
)
EXE(
    PYZ(cli.pure),
    cli.scripts,
    cli.binaries,
    cli.datas,
    [],
    name="lara",
    console=True,
    upx=False,
)
