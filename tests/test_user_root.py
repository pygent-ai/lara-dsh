"""Where Lara's user root lives: inside dsh's home, migrated once from ~/.lara-dsh."""

from __future__ import annotations

from pathlib import Path

import pytest

import lara_shell
from lara_shell import dsh_home, user_lara_root


def test_root_is_lara_inside_dsh_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lara_shell, "LEGACY_ROOT", tmp_path / "no-legacy")
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh"))
    assert dsh_home() == (tmp_path / "dsh").resolve()
    assert user_lara_root() == (tmp_path / "dsh" / "lara").resolve()
    assert not user_lara_root().exists()  # nothing is created until something is written

    monkeypatch.delenv("DSH_HOME")
    assert dsh_home() == (Path.home() / ".dsh").resolve()
    monkeypatch.setenv("DSH_HOME", "   ")
    assert dsh_home() == (Path.home() / ".dsh").resolve()  # blank counts as unset


def test_legacy_root_is_copied_once_and_left_in_place(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    legacy = tmp_path / ".lara-dsh"
    (legacy / "projects" / "p1").mkdir(parents=True)
    (legacy / "projects" / "p1" / "state.sqlite3").write_bytes(b"db")
    (legacy / "config.yaml").write_text("agent: {}\n", encoding="utf-8")
    (legacy / "dsh-home" / "sessions").mkdir(parents=True)  # the old private dsh home: not Lara's
    (legacy / "dsh-runtime-cwd").mkdir()
    monkeypatch.setattr(lara_shell, "LEGACY_ROOT", legacy)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh"))

    root = user_lara_root()

    assert (root / "config.yaml").read_text(encoding="utf-8") == "agent: {}\n"
    assert (root / "projects" / "p1" / "state.sqlite3").read_bytes() == b"db"
    assert not (root / "dsh-home").exists() and not (root / "dsh-runtime-cwd").exists()
    assert (legacy / "config.yaml").is_file()  # legacy data is never removed
    assert "copied legacy user root" in capsys.readouterr().err

    # Second call: the new root exists, so nothing is copied again even if legacy changes.
    (legacy / "later.txt").write_text("x", encoding="utf-8")
    assert user_lara_root() == root
    assert not (root / "later.txt").exists()
