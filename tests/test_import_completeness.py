"""导入完整性冒烟测试：分层包必须全模块可导入，且不残留内核副本引用。

本仓的 agent 内核是 ``pygent.agent.pygent_v1``（随 pygent-ai 安装）；
``lara_shell``（环境层）与 ``lara_sdk``（对外层）站在它之上。任何对已退役
``src/lara`` 副本的悬空引用都会在这里暴露。依赖方向固定：
``lara_sdk`` -> ``lara_shell`` -> ``pygent.agent.pygent_v1``。
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import lara_sdk
import lara_shell

PET_SIDECAR = Path(__file__).resolve().parents[1] / "pet" / "sidecar"


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _module_names(package) -> set[str]:
    prefix = f"{package.__name__}."
    return {
        name
        for _, name, _ in pkgutil.walk_packages(package.__path__, prefix=prefix)
        if not name.endswith(".__main__")
    }


def test_every_layer_module_imports() -> None:
    module_names = _module_names(lara_shell) | _module_names(lara_sdk)
    assert module_names, "layer packages resolved to an empty module set"
    failures: dict[str, str] = {}
    for name in sorted(module_names):
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - smoke test reports all failures
            failures[name] = f"{type(exc).__name__}: {exc}"
    assert not failures, f"dangling imports: {failures}"


def test_no_module_references_the_retired_kernel_copy() -> None:
    offenders: dict[str, set[str]] = {}
    sources = [
        *Path(lara_shell.__path__[0]).rglob("*.py"),
        *Path(lara_sdk.__path__[0]).rglob("*.py"),
        *PET_SIDECAR.glob("*.py"),
    ]
    for path in sources:
        stale = _import_roots(path) & {"lara"}
        if stale:
            offenders[path.name] = stale
    assert not offenders, f"references to the retired src/lara copy: {offenders}"


def test_shell_does_not_import_sdk() -> None:
    offenders: dict[str, set[str]] = {}
    for path in Path(lara_shell.__path__[0]).rglob("*.py"):
        forbidden = _import_roots(path) & {"lara_sdk"}
        if forbidden:
            offenders[str(path.relative_to(Path(lara_shell.__path__[0])))] = forbidden
    assert not offenders, f"lara_shell must not depend on lara_sdk: {offenders}"


def test_sdk_public_surface() -> None:
    modules = {
        name.rsplit(".", 1)[-1]
        for _, name, _ in pkgutil.iter_modules(lara_sdk.__path__, prefix="lara_sdk.")
    }
    assert modules == {"agent", "execution_host", "interfaces", "lara_profile", "lara_rt"}
