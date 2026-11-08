"""Scheduler daemon promotion, security, and execution tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from lara.scheduler.daemon import SchedulerDaemon, TaskResult
from lara.scheduler.keys import init_keys, load_keys, sign
from lara.scheduler.store import SchedulerStore, prompt_hash, scheduler_db_path
from lara.schema import RunConfig


class _Runner:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.tasks: list[object] = []

    def run(self, task: object) -> TaskResult:
        self.tasks.append(task)
        return TaskResult(exit_code=self.exit_code, output="ok")


def _config(tmp_path: Path, *, agent_create_enabled: bool = False) -> RunConfig:
    return RunConfig(
        workspace_root=str(tmp_path / "workspace"),
        user_lara_root=str(tmp_path / "user"),
        scheduler={  # type: ignore[arg-type]
            "agent_create_enabled": agent_create_enabled,
            "session": "scheduler",
            "max_concurrent": 2,
            "poll_seconds": 10,
        },
    )


def _daemon(
    tmp_path: Path, *, agent_create_enabled: bool = False, runner: _Runner | None = None
) -> tuple[SchedulerDaemon, SchedulerStore, dict[str, bytes]]:
    config = _config(tmp_path, agent_create_enabled=agent_create_enabled)
    store = SchedulerStore(scheduler_db_path(config.lara_root))
    store.migrate()
    init_keys(tmp_path / "keys")
    keys = load_keys(tmp_path / "keys")
    daemon = SchedulerDaemon(
        config,
        store=store,
        keys=keys,
        runner=runner or _Runner(),
        session_id="scheduler-session",
    )
    return daemon, store, keys


def _submit(
    store: SchedulerStore,
    keys: dict[str, bytes],
    *,
    kind: str,
    source: str,
    payload: Mapping[str, object],
    signature: str | None = None,
) -> str:
    signed = (
        signature
        if signature is not None
        else sign(keys, kind=kind, source=source, payload=payload)
    )
    return store.submit(
        kind=kind,
        source=source,
        payload=dict(payload),
        signature=signed,
        submission_id=None,
    ).id


def test_user_task_promotes_to_approved(tmp_path: Path) -> None:
    daemon, store, keys = _daemon(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    submission_id = _submit(
        store,
        keys,
        kind="task",
        source="user",
        payload={
            "name": "daily",
            "schedule": "every 1h",
            "prompt": "do it",
            "session": "scheduler-session",
        },
    )
    daemon.run_once(now=now)
    task = store.list_tasks()[0]
    assert task.status == "approved"
    assert task.next_run_at == now + timedelta(hours=1)
    assert store.submission(submission_id).status == "verified"  # type: ignore[union-attr]


def test_agent_task_defaults_to_needs_review(tmp_path: Path) -> None:
    daemon, store, keys = _daemon(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    _submit(
        store,
        keys,
        kind="task",
        source="agent",
        payload={"name": "agent", "schedule": "daily 09:00", "prompt": "review"},
    )
    daemon.run_once(now=now)
    assert store.list_tasks()[0].status == "needs_review"


def test_agent_task_auto_approves_when_enabled(tmp_path: Path) -> None:
    daemon, store, keys = _daemon(tmp_path, agent_create_enabled=True)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    _submit(
        store,
        keys,
        kind="task",
        source="agent",
        payload={"name": "agent", "schedule": "daily 09:00", "prompt": "auto"},
    )
    daemon.run_once(now=now)
    assert store.list_tasks()[0].status == "approved"


def test_unsigned_tampered_and_forged_submissions_are_quarantined(
    tmp_path: Path,
) -> None:
    daemon, store, keys = _daemon(tmp_path)
    payload = {"name": "bad", "schedule": "every 1h", "prompt": "bad"}
    unsigned = _submit(
        store, keys, kind="task", source="user", payload=payload, signature=""
    )
    tampered = _submit(
        store,
        keys,
        kind="task",
        source="user",
        payload={**payload, "prompt": "changed"},
        signature=sign(keys, kind="task", source="user", payload=payload),
    )
    forged = _submit(
        store,
        keys,
        kind="task",
        source="user",
        payload=payload,
        signature=sign(keys, kind="task", source="agent", payload=payload),
    )
    daemon.run_once(now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert store.submission(unsigned).status == "quarantined"  # type: ignore[union-attr]
    assert store.submission(tampered).status == "quarantined"  # type: ignore[union-attr]
    assert store.submission(forged).status == "quarantined"  # type: ignore[union-attr]
    assert store.list_tasks() == []


def test_missed_due_task_runs_once_and_advances(tmp_path: Path) -> None:
    daemon, store, keys = _daemon(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    task = store.create_task(
        name="missed",
        schedule="every 1h",
        prompt="run me",
        session="scheduler-session",
        source="user",
        status="approved",
        next_run_at=now - timedelta(hours=2),
    )
    daemon.run_once(now=now, startup=True)
    runs = store.list_runs(task_id=task.id)
    assert len(runs) == 1
    assert runs[0].missed is True
    assert store.task(task.id).next_run_at is not None  # type: ignore[union-attr]


def test_single_at_task_terminates_after_run(tmp_path: Path) -> None:
    daemon, store, _keys = _daemon(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    task = store.create_task(
        name="once",
        schedule="at 2026-01-01T11:00:00+00:00",
        prompt="run once",
        session="scheduler-session",
        source="user",
        status="approved",
        next_run_at=now - timedelta(hours=1),
    )
    daemon.run_once(now=now, startup=True)
    assert store.task(task.id).next_run_at is None  # type: ignore[union-attr]
    assert len(store.list_runs(task_id=task.id)) == 1


def test_changed_prompt_hash_rejects_and_alerts(tmp_path: Path) -> None:
    daemon, store, _keys = _daemon(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    task = store.create_task(
        name="tampered",
        schedule="every 1h",
        prompt="original",
        session="scheduler-session",
        source="user",
        status="approved",
        next_run_at=now - timedelta(minutes=1),
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE tasks SET prompt='tampered' WHERE id=?", (task.id,)
        )
    daemon.run_once(now=now)
    assert store.task(task.id).status == "rejected"  # type: ignore[union-attr]
    assert store.list_runs(task_id=task.id)[0].status == "rejected"
    assert any("prompt hash mismatch" in alert for alert in daemon.alerts)
    updated = store.task(task.id)
    assert updated is not None
    assert prompt_hash(updated.prompt) != updated.prompt_hash


def test_control_submission_updates_task_status(tmp_path: Path) -> None:
    daemon, store, keys = _daemon(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    task = store.create_task(
        name="review",
        schedule="daily 09:00",
        prompt="review",
        session="scheduler-session",
        source="agent",
        status="needs_review",
        next_run_at=None,
    )
    _submit(
        store,
        keys,
        kind="control",
        source="user",
        payload={"action": "approve", "task_id": task.id},
    )
    daemon.run_once(now=now)
    assert store.task(task.id).status == "approved"  # type: ignore[union-attr]


def test_event_submission_triggers_on_task(tmp_path: Path) -> None:
    runner = _Runner()
    daemon, store, keys = _daemon(tmp_path, runner=runner)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    task = store.create_task(
        name="event",
        schedule="on build",
        prompt="event run",
        session="scheduler-session",
        source="user",
        status="approved",
        next_run_at=None,
    )
    submission_id = _submit(
        store,
        keys,
        kind="event",
        source="user",
        payload={"event": "build", "data": {"ok": True}},
    )
    daemon.run_once(now=now)
    assert len(runner.tasks) == 1
    assert len(store.list_runs(task_id=task.id)) == 1
    assert store.submission(submission_id).status == "verified"  # type: ignore[union-attr]


def test_run_forever_processes_one_loop_and_stops(tmp_path: Path) -> None:
    runner = _Runner()
    daemon, store, _keys = _daemon(tmp_path, runner=runner)
    now = datetime.now(timezone.utc)
    task = store.create_task(
        name="loop",
        schedule="every 1h",
        prompt="run loop",
        session="scheduler-session",
        source="user",
        status="approved",
        next_run_at=now - timedelta(seconds=1),
    )
    daemon.sleeper = lambda _seconds: daemon.stop()
    daemon.run_forever()
    assert len(store.list_runs(task_id=task.id)) == 1
