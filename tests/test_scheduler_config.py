"""Scheduler configuration tests."""

from __future__ import annotations

from pathlib import Path

import lara.config.loader as loader
from lara.config import load_run_config
from lara.scheduler.store import scheduler_db_path
from lara.schema import RunConfig


def test_scheduler_defaults() -> None:
    config = RunConfig(
        workspace_root="C:/workspace",
        user_lara_root="C:/user",
    )
    assert config.scheduler.agent_create_enabled is False
    assert config.scheduler.session == "scheduler"
    assert config.scheduler.max_concurrent == 2
    assert config.scheduler.poll_seconds == 10.0
    assert str(scheduler_db_path(config.lara_root)).endswith(
        "scheduler\\lara-scheduler.sqlite3"
    )


def test_scheduler_overrides(tmp_path: Path) -> None:
    config = RunConfig(
        workspace_root=str(tmp_path / "workspace"),
        user_lara_root=str(tmp_path / "user"),
        scheduler={  # type: ignore[arg-type]
            "agent_create_enabled": True,
            "session": "ops",
            "max_concurrent": 4,
            "poll_seconds": 2.5,
        },
    )
    assert config.scheduler.agent_create_enabled is True
    assert config.scheduler.session == "ops"
    assert config.scheduler.max_concurrent == 4
    assert config.scheduler.poll_seconds == 2.5


def test_loader_reads_scheduler_section(
    tmp_path: Path, monkeypatch
) -> None:
    user_root = tmp_path / "user"
    user_root.mkdir()
    (user_root / "config.yaml").write_text(
        "scheduler:\n"
        "  agent_create_enabled: true\n"
        "  session: ops\n"
        "  max_concurrent: 3\n"
        "  poll_seconds: 5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "user_lara_root", lambda: user_root)
    config = load_run_config(workspace_root=tmp_path / "workspace")
    assert config.scheduler.agent_create_enabled is True
    assert config.scheduler.session == "ops"
    assert config.scheduler.max_concurrent == 3
    assert config.scheduler.poll_seconds == 5.0
