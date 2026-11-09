"""Real-case tests for DshGateway: drive actual dsh sessions end to end.

These tests launch a real dsh runtime and call the real DeepSeek model.
They are skipped when DEEPSEEK_API_KEY is not configured in the process
environment. The bundled runtime resources are downloaded once into the
resource cache shared by ``DSH_RESOURCE_CACHE``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lara.dsh_gateway import DshGateway, DshGatewayConfig, ensure_primary_runtime

REQUIRES_API_KEY = pytest.mark.skipif(
    not os.environ.get("DEEPSEEK_API_KEY"),
    reason="real dsh runtime + DEEPSEEK_API_KEY required",
)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return workspace


@REQUIRES_API_KEY
def test_dsh_session_survives_a_new_gateway_process(tmp_path: Path) -> None:
    """A durable dsh session continues across gateway processes."""
    ensure_primary_runtime()
    home = tmp_path / "dsh-home"
    with DshGateway(DshGatewayConfig.from_env(home, _workspace(tmp_path))) as first:
        _, session_id = first.run(
            "记住数字 42。只回答：收到", session_id="lara-dsh-smoke-001"
        )
    assert session_id

    with DshGateway(DshGatewayConfig.from_env(home, _workspace(tmp_path))) as second:
        reply, _ = second.resume_run(
            session_id, "我让你记住的数字是多少？只回答数字本身"
        )
    assert "42" in reply


@REQUIRES_API_KEY
def test_dsh_agent_performs_real_work_in_the_workspace(tmp_path: Path) -> None:
    """The dsh agent executes real tool work inside the gateway cwd."""
    workspace = _workspace(tmp_path)
    ensure_primary_runtime()
    with DshGateway(DshGatewayConfig.from_env(tmp_path / "dsh-home", workspace)) as gateway:
        reply, _ = gateway.run(
            "在当前工作目录创建文件 dsh-ok.txt，内容写 DSH-OK。完成后只回答：done"
        )
    marker = workspace / "dsh-ok.txt"
    assert marker.is_file(), f"agent did not create the file; reply: {reply[:200]}"
    assert "DSH-OK" in marker.read_text(encoding="utf-8")
