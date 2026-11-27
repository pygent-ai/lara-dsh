"""Lara state lives under the selected dsh home."""

from __future__ import annotations

from pathlib import Path

import pytest

from lara_shell import dsh_home, user_lara_root


def test_root_is_lara_inside_dsh_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh"))
    assert dsh_home() == (tmp_path / "dsh").resolve()
    assert user_lara_root() == (tmp_path / "dsh" / "lara").resolve()
    assert not user_lara_root().exists()  # nothing is created until something is written

    monkeypatch.delenv("DSH_HOME")
    assert dsh_home() == (Path.home() / ".dsh").resolve()
    monkeypatch.setenv("DSH_HOME", "   ")
    assert dsh_home() == (Path.home() / ".dsh").resolve()  # blank counts as unset


def test_explicit_home_overrides_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DSH_HOME", "ambient")
    assert user_lara_root("other") == tmp_path / "other" / "lara"
    assert user_lara_root() == tmp_path / "ambient" / "lara"
    assert not list(tmp_path.iterdir())
