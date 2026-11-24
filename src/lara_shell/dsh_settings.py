"""How Lara's dsh runtime connects, and how Lara borrows dsh's model setup.

dsh (DeepSeek Harness) owns provider configuration: API keys live in its
credential store (``$DSH_HOME/.credentials.yaml``, managed through dsh's own
UI or ``lara dsh credentials``), and its llm service lists the providers and
models that are actually available. Lara keeps no second copy of any key.

``<dsh home>/lara/dsh.yaml`` holds only routing (no secrets)::

    provider: deepseek-official # dsh provider route for worker sessions
    model: deepseek-v4-flash    # model id the route resolves
    base_url: https://…/v1      # optional OpenAI-compatible endpoint override
    workspace: E:/work          # default cwd for new dsh sessions
    lara_model: deepseek-flash  # optional; the pet's own model (default: dsh's route model)

The dsh home itself is ``$DSH_HOME`` or ``~/.dsh`` (see
``lara_shell.dsh_home``); Lara's root lives inside it. ``DSH_MODEL`` and
``DEEPSEEK_BASE_URL`` in the process environment override the file. ``DEEPSEEK_API_KEY`` in the environment is
honoured only when set explicitly for this process by the caller — the
shell's exported credential store is kept away from dsh (see
``lara_shell.dsh_gateway``).
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pygent.llm import ModelCapabilityCatalog

from lara_shell import dsh_home
from lara_shell.config.loader import load_mapping_file
from lara_shell.config.yaml_subset import dump_yaml_subset
from lara_shell.dsh_gateway import DEFAULT_DSH_MODEL, DEFAULT_DSH_PROVIDER, OFFICIAL_BASE_URL, DshGatewayConfig

DSH_SETTINGS_FILENAME = "dsh.yaml"
DEEPSEEK_OFFICIAL_BASE_URL = OFFICIAL_BASE_URL
DEEPSEEK_MESSAGES_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_API_KEY_REF = "DEEPSEEK_API_KEY"
SETTABLE_KEYS = ("provider", "model", "base_url", "workspace", "lara_model")


@dataclass(frozen=True)
class DshSettings:
    dsh_home: Path
    workspace: Path
    provider: str
    model: str
    base_url: str | None
    # Explicit pet-side model override; ``None`` follows dsh's route model.
    # Defaulting this to ``model`` used to shadow the model id dsh actually
    # serves (``deepseek-flash``) with the local placeholder, so the pet asked
    # the account endpoint for a model that does not exist.
    lara_model: str | None = None

    def lara_model_mapping(self, api_key_env: str) -> dict[str, Any]:
        """Pygent ``connections/models/model_groups`` for the pet's own model."""
        return build_model_mapping(
            self.lara_model or self.model,
            self.base_url or DEEPSEEK_OFFICIAL_BASE_URL,
            api_key_env,
        )

    def gateway_config(self, cwd: Path | None = None) -> DshGatewayConfig:
        return DshGatewayConfig(
            dsh_home=self.dsh_home,
            cwd=cwd or self.workspace,
            provider=self.provider,
            model=self.model,
            base_url=self.base_url,
        )


def build_model_mapping(
    model: str,
    base_url: str,
    api_key_env: str | None = None,
    *,
    protocol: str = "openai_chat_completions",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Pygent ``connections/models/model_groups`` for one DeepSeek route.

    ``api_key_env`` names the environment variable the host supplies
    transiently (the key itself travels from dsh's credential store); pass
    ``None`` for routes whose credential travels in ``headers`` instead
    (e.g. the account token's ``x-dsh-auth-token``). The endpoint follows
    dsh's own resolution per route.
    """
    connection: dict[str, Any] = {
        "provider": "dsh",
        # Header-authenticated routes (the signed-in account's grant token)
        # carry no key at all: pygent's ``{"none": true}`` is the ref that
        # resolves to None. An unset env ref would make pygent raise
        # LookupError, which the agent swallows into llm=None — the pet then
        # answers "API key is not configured" and the message vanishes.
        "credential": (
            {"none": True} if api_key_env is None else {"env": api_key_env}
        ),
        # 0.6.4 shape: each protocol endpoint is an object with base_url.
        "protocols": {protocol: {"base_url": base_url or DEEPSEEK_OFFICIAL_BASE_URL}},
        "verify_ssl": True,
    }
    if headers:
        connection["headers"] = dict(headers)
    return {
        "connections": {"dsh": connection},
        "models": {
            model: {
                "connection": "dsh",
                "model_id": model,
                "protocol": protocol,
                "capabilities": _capabilities_for(model, protocol),
            }
        },
        "model_groups": {"dsh": {"models": [model]}},
    }


# Fallback for model ids pygent's catalog does not list; bounded so an
# unknown id still gets a safe compression budget and output ceiling.
_STATIC_CAPABILITIES: dict[str, Any] = {
    "modalities": {"input": ["text"], "output": ["text"]},
    "streaming": {"output": ["text"]},
    "tools": {"call": True, "choice": ["none", "auto", "required", "named"], "parallel": True},
    "structured_output": {"json_object": True, "json_schema": False},
    # disable_supported lets the realtime fork express effort "none" as
    # thinking {"type": "disabled"} on the Messages route (verified against
    # DeepSeek's endpoint); without it pygent degrades "none" to "low", which
    # the endpoint rejects under the fork's forced tool_choice.
    "reasoning": {"supported": True, "controllable": True, "disable_supported": True},
    "limits": {"context_tokens": 128000, "max_output_tokens": 32000},
}

_MODEL_CAPABILITIES = ModelCapabilityCatalog.builtin()


def _capabilities_for(model: str, protocol: str) -> dict[str, Any]:
    """Declared capabilities from pygent's per-model catalog, keyed by
    provider/model/protocol; bounded statics for ids it does not list. pygent
    never reads the catalog at runtime (it budgets compression off
    ``RunConfig.context_window`` alone), so the borrowed mapping must carry
    the limits; an undeclared output ceiling keeps the route's known 32k
    budget (Anthropic requires max_output_tokens on every request)."""
    entry = _MODEL_CAPABILITIES.models.get(("deepseek", model, protocol))
    # pygent >= 0.6.7 types to_mapping() as dict[str, object]; the Any
    # annotation keeps the nested limits fill below type-clean.
    capabilities: dict[str, Any] = (
        entry.to_mapping() if entry is not None else copy.deepcopy(_STATIC_CAPABILITIES)
    )
    if capabilities["limits"]["max_output_tokens"] is None:
        capabilities["limits"]["max_output_tokens"] = 32000
    return capabilities


def migrate_endpoint_to_dsh_env(user_lara_root: str | Path, dsh_home_override: str | Path | None = None) -> bool:
    """Move dsh.yaml's legacy ``base_url`` into dsh's native trusted layer.

    The official way to point dsh at a custom OpenAI-compatible endpoint is
    ``DEEPSEEK_BASE_URL`` in the Harness-home ``.env`` (dsh materializes it
    into its environment at boot); dsh.yaml's ``base_url`` was Lara's
    duplicate. Copies the value once when the ``.env`` does not name it yet;
    returns whether the file changed.
    """
    settings = read_dsh_settings_file(user_lara_root)
    base_url = _non_empty(settings.get("base_url"))
    if base_url is None:
        return False
    home = Path(dsh_home_override).expanduser() if dsh_home_override else dsh_home()
    env_path = home / ".env"
    existing = ""
    if env_path.is_file():
        existing = env_path.read_text(encoding="utf-8")
    for line in existing.splitlines():
        if line.strip().startswith("DEEPSEEK_BASE_URL"):
            return False
    entry = f"DEEPSEEK_BASE_URL={base_url}"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    with env_path.open("a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(entry + "\n")
    return True


def default_dsh_workspace(home: str | Path | None = None) -> Path | None:
    """The workspace dsh desktop has selected, from its own ``workspace.json``.

    Sessions are filed per working directory and the desktop UI lists only the
    workspaces it has registered, so a session created in an unregistered
    directory is invisible there. Preferring this path keeps Lara's dsh
    sessions in the workspace the user is looking at.
    """
    base = Path(home).expanduser() if home is not None else dsh_home()
    store = base / "storages" / "workspace.json"
    try:
        # dsh stores this one as JSON; load_mapping_file only parses the YAML
        # subset, so read it directly.
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    glob = data.get("global")
    tables = data.get("tables")
    if not isinstance(glob, Mapping) or not isinstance(tables, Mapping):
        return None
    workspaces = tables.get("workspaces")
    if not isinstance(workspaces, Mapping):
        return None
    default_id = glob.get("defaultWorkspaceId")
    entry = workspaces.get(default_id) if isinstance(default_id, str) else None
    if not isinstance(entry, Mapping):
        return None
    path = entry.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    return Path(path).expanduser()


def _dsh_session_cwd(root: Path) -> Path:
    """Explicit ``workspace:`` wins; otherwise follow dsh's own default workspace."""
    registered = default_dsh_workspace()
    if registered is not None:
        return registered
    return root / "pet"


def load_dsh_settings(
    user_lara_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> DshSettings:
    root = Path(user_lara_root).expanduser().resolve()
    env = os.environ if environ is None else environ
    settings = read_dsh_settings_file(root)

    workspace_value = _non_empty(settings.get("workspace"))
    model = _non_empty(env.get("DSH_MODEL")) or _non_empty(settings.get("model")) or DEFAULT_DSH_MODEL
    return DshSettings(
        dsh_home=dsh_home(),
        workspace=_resolve(root, workspace_value) if workspace_value else _dsh_session_cwd(root),
        provider=_non_empty(settings.get("provider")) or DEFAULT_DSH_PROVIDER,
        model=model,
        base_url=_non_empty(env.get("DEEPSEEK_BASE_URL")) or _non_empty(settings.get("base_url")),
        lara_model=_non_empty(settings.get("lara_model")),
    )


def read_dsh_settings_file(user_lara_root: str | Path) -> dict[str, Any]:
    """The raw ``dsh.yaml`` mapping (empty when absent)."""
    path = Path(user_lara_root).expanduser().resolve() / DSH_SETTINGS_FILENAME
    if not path.is_file():
        return {}
    data = load_mapping_file(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def write_dsh_settings_file(user_lara_root: str | Path, updates: Mapping[str, str | None]) -> Path:
    """Merge ``updates`` into ``dsh.yaml`` (``None`` removes a key)."""
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
    path.write_text(dump_yaml_subset(data) if data else "", encoding="utf-8")
    return path


def _resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _non_empty(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "default_dsh_workspace",
    "DEEPSEEK_API_KEY_REF",
    "DEEPSEEK_MESSAGES_BASE_URL",
    "DEEPSEEK_OFFICIAL_BASE_URL",
    "DSH_SETTINGS_FILENAME",
    "SETTABLE_KEYS",
    "DshSettings",
    "build_model_mapping",
    "load_dsh_settings",
    "migrate_endpoint_to_dsh_env",
    "read_dsh_settings_file",
    "write_dsh_settings_file",
]
