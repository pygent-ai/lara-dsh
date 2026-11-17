"""Shared setup for tests that drive the real dsh runtime.

The real-runtime tests run in throwaway dsh homes but need a working
DeepSeek route. The key comes from ``DEEPSEEK_API_KEY`` when the caller
exports it, otherwise from the user's own dsh credential store (the same
place the pet reads it from); the endpoint comes from ``dsh.yaml``. Tests
are skipped when neither source has a key.
"""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path

import pytest

from lara_shell import user_lara_root
from lara_shell.dsh_gateway import DshGateway, DshGatewayConfig
from lara_shell.dsh_settings import DEEPSEEK_API_KEY_REF, load_dsh_settings


@lru_cache(maxsize=1)
def deepseek_route() -> tuple[str, str | None] | None:
    """``(api_key, base_url)`` for the real-runtime tests, or None when unavailable."""
    settings = load_dsh_settings(user_lara_root())
    key = os.environ.get("DEEPSEEK_API_KEY") or None
    if key is None and (settings.dsh_home / ".credentials.yaml").is_file():
        with tempfile.TemporaryDirectory() as scratch:
            try:
                with DshGateway(settings.gateway_config(Path(scratch))) as gateway:
                    key = gateway.resolve_credential(DEEPSEEK_API_KEY_REF)
            except Exception:
                key = None
    return None if key is None else (key, settings.base_url)


def real_gateway_config(dsh_home: Path, cwd: Path) -> DshGatewayConfig:
    """A config for a throwaway dsh home that still reaches the real model."""
    route = deepseek_route()
    if route is None:
        pytest.skip("real dsh runtime needs DEEPSEEK_API_KEY or a key in dsh's credential store")
    api_key, base_url = route
    settings = load_dsh_settings(user_lara_root())
    return DshGatewayConfig(
        dsh_home=dsh_home, cwd=cwd, provider=settings.provider, model=settings.model,
        api_key=api_key, base_url=base_url,
    )
