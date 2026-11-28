# Installing Lara Pet

Lara Pet is two parts that share one home directory (`~/.dsh`):

| Part | What it is | How it ships |
|---|---|---|
| **lara** (Python) | Lara's brain (pygent runtime), the `lara` CLI, the resident dsh daemon and the pet sidecar (`lara-pet-sidecar`) | a Python wheel (`lara-<version>-py3-none-any.whl`) |
| **Lara Pet** (desktop) | the transparent always-on-top pet window, global hotkey, speech bubbles | a Windows installer (`Lara Pet_<version>_x64-setup.exe`) |

The desktop app resolves the sidecar in this order: `LARA_PET_SIDECAR`, the
`lara-dsh` package's own `bin/lara-pet-sidecar.exe` (a frozen, Python-free
build), the wheel's console script on `PATH`, the dev checkout's venv. Install
the wheel for the `lara` CLI in your own shell; the bundled copy is what runs
inside the pet unless `LARA_PET_SIDECAR` points elsewhere — so refreshing the
wheel alone does not update an installed pet (see the update flow below).

## 0. The dsh-plugin way (one command, preferred)

The `lara-dsh` npm package bundles the control plugin **and** both
executables (pet + a frozen, Python-free sidecar). With dsh installed:

```powershell
dsh plugin --profile web add lara-dsh        # or: dsh plugin ... add file:<path to the package>
```

Starting dsh now launches the pet automatically — the plugin spawns it
through dsh's managed subprocess seam, and the pet lives with the dsh that
started it. Nothing else to install: the bundled `bin/lara.exe` is on the
pet's own PATH, so the pet's agent runs `lara dsh ...` without a Python
wheel. (The wheel below remains the way to get the `lara` CLI in your own
shell.)

Build the package locally with `uv run python scripts/build_npm_package.py`
(see the script's docstring for the two build steps it expects).

### Updating an installed pet (rebuild + reinstall)

The pet keeps running the **bundled** sidecar, so refreshing the wheel is not
enough — rebuild the frozen executables and reinstall the package:

```powershell
# 1. freeze the sidecar from the current tree; VENV_SITE must point at the
#    project venv so PyInstaller collects pygent + lara from it
$env:VENV_SITE = "E:\Projects\lara-dsh\.venv\Lib\site-packages"
uv run --with pyinstaller pyinstaller pet/sidecar-freeze.spec `
      --noconfirm --distpath .tmp/freeze --workpath .tmp/freeze/_build

# 2. pet UI unchanged? skip cargo. Assemble the npm package from
#    .tmp/freeze exes + pet/src-tauri/target/release/lara-pet.exe
uv run python scripts/build_npm_package.py

# 3. close the running pet first — the process locks the bin/ exes — then
#    reinstall into dsh's web profile
dsh plugin --profile web add file:E:\Projects\lara-dsh\dist\npm\lara-dsh

# 4. restart the dsh desktop; the plugin respawns the pet on the new sidecar
```

Gotchas seen in the field:

- `dsh plugin add` shells out to **pnpm**; it must be on the `PATH` of the
  shell that runs dsh. `npm i -g pnpm` lands wherever `npm prefix -g` points —
  verify with `pnpm --version` if dsh reports pnpm "not found".
- The deployed sidecar announces itself in `<lara root>/pet/sidecar.log`; the
  `context budget <n> tokens (model declares <n>)` line appears only on builds
  since the catalog-based capability lookup. If the log stays empty, the pet
  spawned nothing — check the resolution order above.

## 1. Install the Python side

Requires Python 3.13+. With [uv](https://docs.astral.sh/uv/):

```powershell
uv tool install .\lara-0.1.0-py3-none-any.whl
```

(or `pipx install …`, or `pip install …` into a venv whose `Scripts` directory is on `PATH`).

This puts two commands on `PATH`: `lara` and `lara-pet-sidecar`. Check:

```powershell
lara-pet-sidecar --help   # or just: where lara-pet-sidecar
```

The first `lara …` command downloads the DeepSeek Harness runtime resources
(~80 MB, once) into `%USERPROFILE%\.cache\deepseek-harness`.

## 2. Sign in to dsh

Open dsh, sign in to your DeepSeek account, and select an account model in
dsh's model picker. The pet uses the model selected by the running dsh.
`lara dsh models` lists the models the running instance exposes.

Sanity check (talks to the model):

```powershell
lara dsh run "只回答：ok"
```

## 3. Install the pet

Run `Lara Pet_0.1.0_x64-setup.exe`. It installs to `%LOCALAPPDATA%\Lara Pet`
and adds a Start Menu entry. On launch the pet:

- starts `lara-pet-sidecar` (which starts the dsh daemon),
- shows the pixel rabbit, always on top, click-through outside the sprite,
- opens the input bar on **triple Shift** (configurable in the settings panel).

If the sidecar is not found, the bubble says so; make sure step 1's `PATH`
change applies to the session the pet starts from (log out/in after
installing the wheel if needed), or set the environment variable
`LARA_PET_SIDECAR` to the full path of `lara-pet-sidecar.exe`.

## Where things are

| | |
|---|---|
| dsh home (shared with any dsh front end) | `~/.dsh` (`DSH_HOME` overrides) |
| Lara's data | `~/.dsh/lara/` — `config.yaml`, `pet-config.json`, `projects/` |
| pet workspace (where Lara works by default) | `~/.dsh/lara/pet` (`LARA_PET_WORKSPACE` overrides) |
| runtime resources cache | `~/.cache/deepseek-harness` |

Removing everything: uninstall Lara Pet from Apps, `uv tool uninstall lara`,
delete `~/.dsh` (this also deletes dsh's own sessions and credentials).

## Building the artifacts yourself

```powershell
uv build --wheel                 # dist/lara-*.whl
cd pet; npm install; npx tauri build   # pet/src-tauri/target/release/bundle/nsis/*.exe
```
