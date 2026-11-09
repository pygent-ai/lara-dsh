"""导入完整性冒烟测试：抽取后的分层包必须全模块可导入。

评估 / 修复 / 自动化 / 工作流子系统已从本仓裁剪；任何残留的悬空引用
（悬空 import）都会在这里暴露。同时固定依赖方向：``lara_sdk`` ->
``lara_shell`` -> ``lara``，内核 ``lara`` 不得导入外壳或对外契约层。
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import pkgutil
from pathlib import Path

import lara
import lara_sdk
import lara_shell


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
            elif node.level > 0:
                roots.add("lara")
    return roots


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


def test_lara_kernel_does_not_import_shell_or_sdk() -> None:
    offenders: dict[str, set[str]] = {}
    for path in Path(lara.__path__[0]).rglob("*.py"):
        # ``python -m lara`` is the process entry shim; it composes the shell
        # CLI and is intentionally the only kernel file that names lara_shell.
        if path.name == "__main__.py":
            continue
        forbidden = _import_roots(path) & {"lara_shell", "lara_sdk"}
        if forbidden:
            offenders[str(path.relative_to(Path(lara.__path__[0])))] = forbidden
    assert not offenders, f"kernel imports shell/sdk: {offenders}"


def test_every_lara_shell_module_imports() -> None:
    module_names = {
        name
        for _, name, _ in pkgutil.walk_packages(
            lara_shell.__path__, prefix="lara_shell."
        )
        if not name.endswith(".__main__")
    }
    assert module_names, "lara_shell package resolved to an empty module set"
    failures: dict[str, str] = {}
    for name in sorted(module_names):
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - smoke test reports all failures
            failures[name] = f"{type(exc).__name__}: {exc}"
    assert not failures, f"dangling imports in lara_shell: {failures}"


def test_lara_sdk_modules_import() -> None:
    from lara_sdk import agent, interfaces, lara_rt  # noqa: F401

    # SDK 只保留使用契约、运行时适配器与完整门面，不再暴露构造/试点模块。
    modules = {name for _, name, _ in pkgutil.iter_modules(lara_sdk.__path__)}
    assert modules == {"agent", "interfaces", "lara_rt"}
    for removed in ("engine", "simple_agent"):
        assert importlib.util.find_spec(f"lara_sdk.{removed}") is None


def test_pruned_subsystems_absent() -> None:
    for pruned in ("evaluation", "repair", "automations", "workflows"):
        assert not (Path(lara.__path__[0]) / pruned).exists(), pruned
