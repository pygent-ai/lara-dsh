# Installing Lara Pet

Lara Pet is two parts that share one home directory (`~/.dsh`):

| Part | What it is | How it ships |
|---|---|---|
| **lara** (Python) | Lara's brain (pygent runtime), the `lara` CLI, the resident dsh daemon and the pet sidecar (`lara-pet-sidecar`) | a Python wheel (`lara-<version>-py3-none-any.whl`) |
| **Lara Pet** (desktop) | the transparent always-on-top pet window, global hotkey, speech bubbles | a Windows installer (`Lara Pet_<version>_x64-setup.exe`) |

The desktop app finds the sidecar on `PATH` (or via `LARA_PET_SIDECAR`); it does
not bundle Python. Install the wheel first.

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

## 2. Give dsh an API key

Lara runs on DeepSeek Harness (dsh); dsh owns the provider configuration and
Lara borrows it. Store the key in dsh's credential store (it is read from
stdin / an interactive prompt, never passed on the command line):

```powershell
lara dsh credentials DEEPSEEK_API_KEY --set
```

Using an OpenAI-compatible endpoint other than the official DeepSeek API?
Point dsh at it:

```powershell
lara dsh config base_url=https://your-endpoint/v1 model=deepseek-v4-flash
```

`lara dsh config` shows the effective routing; `lara dsh models` lists what
the runtime can see. Everything lives under `~/.dsh`: the key in
`~/.dsh/.credentials.yaml`, routing in `~/.dsh/lara/dsh.yaml`, Lara's own
sessions and settings under `~/.dsh/lara/`.

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
| Lara's data | `~/.dsh/lara/` — `config.yaml`, `dsh.yaml`, `pet-config.json`, `projects/` |
| pet workspace (where Lara works by default) | `~/.dsh/lara/pet` (`LARA_PET_WORKSPACE` overrides) |
| runtime resources cache | `~/.cache/deepseek-harness` |

Removing everything: uninstall Lara Pet from Apps, `uv tool uninstall lara`,
delete `~/.dsh` (this also deletes dsh's own sessions and credentials).

## Building the artifacts yourself

```powershell
uv build --wheel                 # dist/lara-*.whl
cd pet; npm install; npx tauri build   # pet/src-tauri/target/release/bundle/nsis/*.exe
```
