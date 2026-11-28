"""Resolve dsh workspaces and adapt the official account model to pygent."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from pygent.llm import ModelCapabilityCatalog

from lara_shell import default_pet_workspace, dsh_home
from lara_shell.dsh_gateway import DshGatewayConfig

DEEPSEEK_API_KEY_REF = "DEEPSEEK_API_KEY"
_ACCOUNT_PROTOCOL = "anthropic_messages"


def build_model_mapping(model: str, base_url: str, token: str) -> dict[str, Any]:
    return {
        "connections": {
            "dsh": {
                "provider": "dsh",
                "credential": {"none": True},
                "protocols": {_ACCOUNT_PROTOCOL: {"base_url": base_url}},
                "headers": {"x-dsh-auth-token": token},
                "verify_ssl": True,
            },
        },
        "models": {
            model: {
                "connection": "dsh",
                "model_id": model,
                "protocol": _ACCOUNT_PROTOCOL,
                "capabilities": _capabilities_for(model, _ACCOUNT_PROTOCOL),
            },
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


def default_dsh_workspace(home: str | Path | None = None) -> Path | None:
    """The workspace dsh desktop has selected, from its own ``workspace.json``.

    Sessions are filed per working directory and the desktop UI lists only the
    workspaces it has registered, so a session created in an unregistered
    directory is invisible there. Preferring this path keeps Lara's dsh
    sessions in the workspace the user is looking at.
    """
    base = dsh_home(home)
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
    return (base / Path(path.strip()).expanduser()).resolve()


def dsh_target(home: str | Path | None = None, cwd: Path | None = None) -> DshGatewayConfig:
    home = dsh_home(home)
    return DshGatewayConfig(
        dsh_home=home,
        cwd=cwd or default_dsh_workspace(home) or default_pet_workspace(home),
    )
