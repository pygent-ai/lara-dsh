"""examples 可运行性冒烟测试：离线示例必须能在测试环境直接跑通。"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path


def test_offline_example_runs() -> None:
    path = Path(__file__).resolve().parents[1] / "examples" / "run_simple_agent.py"
    spec = importlib.util.spec_from_file_location("lara_agent_offline_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    asyncio.run(module.main())
