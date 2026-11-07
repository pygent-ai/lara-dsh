"""导入完整性冒烟测试：抽取后的 lara 包必须全模块可导入。

评估 / 修复 / 自动化 / 工作流子系统已从本仓裁剪；任何残留的悬空引用
（悬空 import）都会在这里暴露。
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import lara


def test_every_lara_module_imports() -> None:
    module_names = {
        name
        for _, name, _ in pkgutil.walk_packages(lara.__path__, prefix="lara.")
        if not name.endswith(".__main__")
    }
    assert module_names, "lara package resolved to an empty module set"
    failures: dict[str, str] = {}
    for name in sorted(module_names):
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - smoke test reports all failures
            failures[name] = f"{type(exc).__name__}: {exc}"
    assert not failures, f"dangling imports after extraction: {failures}"


def test_standard_agent_modules_import() -> None:
    from standard_agent import engine, interfaces, simple_agent  # noqa: F401

    from standard_agent import lara_rt  # noqa: F401


def test_pruned_subsystems_absent() -> None:
    for pruned in ("evaluation", "repair", "automations", "workflows"):
        assert not (Path(lara.__path__[0]) / pruned).exists(), pruned
