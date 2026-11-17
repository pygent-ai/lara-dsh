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
    lara_model: deepseek-v4-flash   # optional; the pet's own model (default: model)

The dsh home itself is ``$DSH_HOME`` or ``~/.dsh`` (see
``lara_shell.dsh_home``); Lara's root lives inside it. ``DSH_MODEL`` and
``DEEPSEEK_BASE_URL`` in the process environment override the file. ``DEEPSEEK_API_KEY`` in the environment is
honoured only when set explicitly for this process by the caller — the
shell's exported credential store is kept away from dsh (see
``lara_shell.dsh_gateway``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from lara_shell import dsh_home
from lara_shell.config.loader import load_mapping_file
from lara_shell.config.yaml_subset import dump_yaml_subset
from lara_shell.dsh_gateway import DEFAULT_DSH_MODEL, DEFAULT_DSH_PROVIDER, DshGatewayConfig

DSH_SETTINGS_FILENAME = "dsh.yaml"
DEEPSEEK_OFFICIAL_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_API_KEY_REF = "DEEPSEEK_API_KEY"
SETTABLE_KEYS = ("provider", "model", "base_url", "workspace", "lara_model")


@dataclass(frozen=True)
class DshSettings:
    dsh_home: Path
    workspace: Path
    provider: str
    model: str
    base_url: str | None
    lara_model: str

    def gateway_config(self, cwd: Path | None = None) -> DshGatewayConfig:
        return DshGatewayConfig(
            dsh_home=self.dsh_home,
            cwd=cwd or self.workspace,
            provider=self.provider,
            model=self.model,
            base_url=self.base_url,
        )

    def lara_model_mapping(self, api_key_env: str) -> dict[str, Any]:
        """Pygent ``connections/models/model_groups`` for the pet's own model.

        The key is referenced through ``api_key_env``, which the host supplies
        transiently from dsh's credential store. Only the DeepSeek route is
        mapped: it is the one OpenAI-compatible endpoint whose location dsh
        and Lara agree on.
        """
        return {
            "connections": {
                "dsh": {
                    "provider": "dsh",
                    "credential": {"env": api_key_env},
                    "protocols": {
                        "openai_chat_completions": {
                            "base_url": self.base_url or DEEPSEEK_OFFICIAL_BASE_URL
                        }
                    },
                    "verify_ssl": True,
                }
            },
            "models": {
                self.lara_model: {
                    "connection": "dsh",
                    "model_id": self.lara_model,
                    "protocol": "openai_chat_completions",
                    "capabilities": {
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "streaming": {"output": ["text"]},
                        "tools": {"call": True, "choice": ["none", "auto", "required", "named"], "parallel": True},
                        "structured_output": {"json_object": True, "json_schema": False},
                        "reasoning": {"supported": True, "controllable": True},
                        "limits": {"context_tokens": 128000, "max_output_tokens": 32000},
                    },
                }
            },
            "model_groups": {"dsh": {"models": [self.lara_model]}},
        }


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
        workspace=_resolve(root, workspace_value) if workspace_value else root / "pet",
        provider=_non_empty(settings.get("provider")) or DEFAULT_DSH_PROVIDER,
        model=model,
        base_url=_non_empty(env.get("DEEPSEEK_BASE_URL")) or _non_empty(settings.get("base_url")),
        lara_model=_non_empty(settings.get("lara_model")) or model,
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
    "DEEPSEEK_API_KEY_REF",
    "DEEPSEEK_OFFICIAL_BASE_URL",
    "DSH_SETTINGS_FILENAME",
    "SETTABLE_KEYS",
    "DshSettings",
    "load_dsh_settings",
    "read_dsh_settings_file",
    "write_dsh_settings_file",
]
