"""Scheduler CLI tests."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import lara_shell.ops.cli.scheduler as scheduler_cli
from lara_shell.ops.cli.main import build_parser
from lara.scheduler.keys import init_keys
from lara.scheduler.store import SchedulerStore, scheduler_db_path
from lara.schema import RunConfig


def _config(tmp_path: Path) -> RunConfig:
    return RunConfig(
        workspace_root=str(tmp_path / "workspace"),
        user_lara_root=str(tmp_path / "user"),
    )


def _args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    values = {
        "workspace_root": str(tmp_path / "workspace"),
        "agent_alias": None,
        "max_steps": None,
        "name": "task",
        "schedule": "every 1h",
        "prompt": "do it",
        "instructions": "",
        "session": None,
        "priority": "normal",
        "delivery": "fire-and-forget",
        "task_id": "task-1",
        "force": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_cli_registers_scheduler_commands() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["scheduler", "add", "--name", "n", "--schedule", "every 1h", "--prompt", "p"]
    )
    assert args.scheduler_command == "add"
    assert args.handler is not None


def test_cli_add_creates_signed_user_submission(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    init_keys(config.user_lara_root)
    monkeypatch.setattr(scheduler_cli, "load_run_config", lambda **_: config)
    result = scheduler_cli._scheduler_add(_args(tmp_path))
    assert result["kind"] == "task"
    assert result["source"] == "user"
    store = SchedulerStore(scheduler_db_path(config.lara_root))
    submission = store.submission(str(result["id"]))
    assert submission is not None
    assert submission.payload["schedule"] == "every 1h"


def test_cli_add_forwards_bus_fields(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    init_keys(config.user_lara_root)
    monkeypatch.setattr(scheduler_cli, "load_run_config", lambda **_: config)
    result = scheduler_cli._scheduler_add(
        _args(tmp_path, priority="high", delivery="speak")
    )
    store = SchedulerStore(scheduler_db_path(config.lara_root))
    submission = store.submission(str(result["id"]))
    assert submission is not None
    assert submission.payload["priority"] == "high"
    assert submission.payload["delivery"] == "speak"


def test_cli_init_keys(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(scheduler_cli, "load_run_config", lambda **_: config)
    result = scheduler_cli._scheduler_init_keys(_args(tmp_path))
    assert result["status"] == "ready"
    assert set(result["keys"]) == {"user", "agent"}


def test_cli_list_and_show(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    store = SchedulerStore(scheduler_db_path(config.lara_root))
    store.migrate()
    task = store.create_task(
        name="task",
        schedule="every 1h",
        prompt="do it",
        session="scheduler",
        source="user",
        status="approved",
        next_run_at=None,
    )
    monkeypatch.setattr(scheduler_cli, "load_run_config", lambda **_: config)
    assert scheduler_cli._scheduler_list(_args(tmp_path))["tasks"][0]["id"] == task.id
    shown = scheduler_cli._scheduler_show(_args(tmp_path, task_id=task.id))
    assert shown["task"]["id"] == task.id
    assert shown["runs"] == []


def test_install_and_uninstall_autostart(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    xml_seen: list[str] = []
    monkeypatch.setattr(scheduler_cli.os, "name", "nt")

    def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(args)
        if "/XML" in args:
            xml_seen.append(
                Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scheduler_cli, "_lara_executable", lambda: tmp_path / "lara.exe")
    monkeypatch.setattr(scheduler_cli.subprocess, "run", fake_run)
    installed = scheduler_cli._scheduler_install_autostart(argparse.Namespace())
    removed = scheduler_cli._scheduler_uninstall_autostart(argparse.Namespace())
    assert installed["task"] == scheduler_cli.AUTOSTART_TASK_NAME
    assert removed["status"] == "removed"
    assert calls[0][:4] == ["schtasks", "/Create", "/TN", scheduler_cli.AUTOSTART_TASK_NAME]
    assert "RestartOnFailure" in xml_seen[0]
    assert calls[1][:3] == ["schtasks", "/Delete", "/TN"]
