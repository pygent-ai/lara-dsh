"""Resolve how Lara's dsh runtime connects: endpoint, credential, model, home.

Lara already owns provider connections in ``~/.lara-dsh/config.yaml``
(``connections.<name>.protocols.openai_chat_completions.base_url`` plus a
credential env name). The dsh runtime's ``deepseek-official`` adapter speaks
the DeepSeek wire protocol against ``DEEPSEEK_BASE_URL`` / ``DEEPSEEK_API_KEY``,
so any OpenAI-compatible connection that serves a DeepSeek model can back it.

An optional ``~/.lara-dsh/dsh.yaml`` selects the connection::

    connection: az              # name under config.yaml connections
    model: deepseek-v4-flash    # model id the dsh adapter resolves
    home: dsh-home              # dsh home, relative to ~/.lara-dsh
    workspace: E:/work          # default cwd for new dsh sessions

``DSH_MODEL`` and ``DSH_HOME`` in the process environment override the file.
``DEEPSEEK_BASE_URL`` redirects the endpoint (taking ``DEEPSEEK_API_KEY`` with
it); without a configured connection, ``DEEPSEEK_API_KEY`` alone is used.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from lara_shell.config.loader import USER_CONFIG_FILENAME, load_mapping_file
from lara_shell.config.yaml_subset import dump_yaml_subset
from lara_shell.credentials import build_credential_environment
from lara_shell.dsh_gateway import DEFAULT_DSH_MODEL, DshGatewayConfig

DSH_SETTINGS_FILENAME = "dsh.yaml"
DEFAULT_DSH_HOME_NAME = "dsh-home"


SETTABLE_KEYS = ("connection", "model", "home", "workspace")


def read_dsh_settings_file(user_lara_root: str | Path) -> dict[str, Any]:
    """The raw ``dsh.yaml`` mapping (empty when absent)."""
    return _read_optional_mapping(Path(user_lara_root).expanduser().resolve() / DSH_SETTINGS_FILENAME)


def write_dsh_settings_file(user_lara_root: str | Path, updates: Mapping[str, str | None]) -> Path:
    """Merge ``updates`` into ``dsh.yaml`` (``None`` removes a key); validate the result."""
    root = Path(user_lara_root).expanduser().resolve()
    data = read_dsh_settings_file(root)
    for key, value in updates.items():
        if key not in SETTABLE_KEYS:
            raise ValueError(f"unknown dsh setting {key!r}; expected one of {', '.join(SETTABLE_KEYS)}")
        if value is None or not str(value).strip():
            data.pop(key, None)
        else:
            data[key] = str(value).strip()
    path = root / DSH_SETTINGS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dump_yaml_subset(data) if data else ""
    # Resolve before writing so a bad connection name never lands on disk.
    previous = path.read_text(encoding="utf-8") if path.is_file() else None
    path.write_text(text, encoding="utf-8")
    try:
        load_dsh_settings(root)
    except ValueError:
        if previous is None:
            path.unlink()
        else:
            path.write_text(previous, encoding="utf-8")
        raise
    return path


@dataclass(frozen=True)
class DshSettings:
    dsh_home: Path
    workspace: Path
    model: str
    base_url: str | None
    api_key: str | None
    connection: str | None

    def gateway_config(self, cwd: Path | None = None) -> DshGatewayConfig:
        return DshGatewayConfig(
            dsh_home=self.dsh_home,
            cwd=cwd or self.workspace,
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
        )


def load_dsh_settings(
    user_lara_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    credentials: Mapping[str, str] | None = None,
) -> DshSettings:
    """Compose settings from dsh.yaml, config.yaml connections, and the environment.

    ``credentials`` defaults to the shell credential store (user file,
    environment, keyring); tests inject a plain mapping.
    """
    root = Path(user_lara_root).expanduser().resolve()
    env = os.environ if environ is None else environ
    settings = _read_optional_mapping(root / DSH_SETTINGS_FILENAME)

    connection_name = _non_empty(settings.get("connection"))
    base_url: str | None = None
    api_key: str | None = None
    if connection_name is not None:
        connection = _connection(root, connection_name)
        base_url = _non_empty(
            _dig(connection, "protocols.openai_chat_completions.base_url")
        )
        if base_url is None:
            raise ValueError(
                f"dsh connection {connection_name!r} has no openai_chat_completions.base_url"
            )
        credential_env = _non_empty(_dig(connection, "credential.env"))
        if credential_env is None:
            raise ValueError(f"dsh connection {connection_name!r} declares no credential.env")
        store = build_credential_environment(root) if credentials is None else credentials
        api_key = store.get(credential_env)
        if not api_key:
            raise ValueError(
                f"credential {credential_env!r} for dsh connection {connection_name!r} is not configured"
            )

    # A connection owns its endpoint+credential pair. The environment only
    # replaces that pair when it redirects the endpoint explicitly: a stray
    # DEEPSEEK_API_KEY (the shell loads every stored credential into the
    # process environment) must not be paired with another connection's URL.
    env_base_url = _non_empty(env.get("DEEPSEEK_BASE_URL"))
    env_api_key = _non_empty(env.get("DEEPSEEK_API_KEY"))
    if env_base_url is not None or connection_name is None:
        base_url = env_base_url or base_url
        api_key = env_api_key or api_key

    home_value = _non_empty(env.get("DSH_HOME")) or _non_empty(settings.get("home")) or DEFAULT_DSH_HOME_NAME
    workspace_value = _non_empty(settings.get("workspace"))
    return DshSettings(
        dsh_home=_resolve(root, home_value),
        workspace=_resolve(root, workspace_value) if workspace_value else root / "pet",
        model=_non_empty(env.get("DSH_MODEL")) or _non_empty(settings.get("model")) or DEFAULT_DSH_MODEL,
        base_url=base_url,
        api_key=api_key,
        connection=connection_name,
    )


def _connection(root: Path, name: str) -> dict[str, Any]:
    config = _read_optional_mapping(root / USER_CONFIG_FILENAME)
    connections = config.get("connections")
    if not isinstance(connections, dict) or not isinstance(connections.get(name), dict):
        raise ValueError(f"dsh connection {name!r} is not defined in {USER_CONFIG_FILENAME}")
    return connections[name]


def _read_optional_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = load_mapping_file(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def _resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _dig(data: Any, dotted: str) -> Any:
    for key in dotted.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _non_empty(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "DSH_SETTINGS_FILENAME",
    "SETTABLE_KEYS",
    "DshSettings",
    "load_dsh_settings",
    "read_dsh_settings_file",
    "write_dsh_settings_file",
]
