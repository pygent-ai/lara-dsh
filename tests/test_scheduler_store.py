"""Unit tests for the scheduler SQLite store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from lara.scheduler.store import SchedulerStore, prompt_hash


def _store(tmp_path: Path) -> SchedulerStore:
    store = SchedulerStore(tmp_path / "scheduler" / "lara-scheduler.sqlite3")
    store.migrate()
    return store


def test_submission_queue_and_idempotent_insert(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = {"name": "task", "schedule": "every 1h", "prompt": "hello"}
    first = store.submit(
        kind="task",
        source="user",
        payload=payload,
        signature="sig",
        submission_id="sub-1",
    )
    second = store.submit(
        kind="task",
        source="user",
        payload=payload,
        signature="sig",
        submission_id="sub-1",
    )
    assert first.id == second.id == "sub-1"
    assert [item.id for item in store.pending_submissions()] == ["sub-1"]
    store.mark_submission("sub-1", "verified", task_id="task-1")
    assert store.submission("sub-1").status == "verified"  # type: ignore[union-attr]
    assert store.pending_submissions() == []


def test_task_and_run_audit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    task = store.create_task(
        name="daily",
        schedule="daily 09:00",
        prompt="do it",
        session="scheduler-session",
        source="user",
        status="approved",
        next_run_at=now,
    )
    assert task.prompt_hash == prompt_hash("do it")
    assert store.list_tasks()[0].id == task.id
    run = store.append_run(
        task_id=task.id,
        status="success",
        started_at=now,
        finished_at=now + timedelta(seconds=1),
        exit_code=0,
        output="ok",
        error=None,
        missed=True,
    )
    assert run.missed is True
    assert store.list_runs(task_id=task.id)[0].exit_code == 0
    store.set_task_status(task.id, "paused")
    assert store.task(task.id).status == "paused"  # type: ignore[union-attr]


def test_atomic_task_claim_and_release(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    task = store.create_task(
        name="due",
        schedule="every 1h",
        prompt="run",
        session="scheduler-session",
        source="user",
        status="approved",
        next_run_at=now - timedelta(seconds=1),
    )
    first = store.claim_due_tasks(
        now=now, lease_id="daemon-1", lease_seconds=60, limit=10
    )
    assert [item.id for item in first] == [task.id]
    second = store.claim_due_tasks(
        now=now, lease_id="daemon-2", lease_seconds=60, limit=10
    )
    assert second == []
    store.release_task(task.id, lease_id="daemon-1")
    third = store.claim_due_tasks(
        now=now, lease_id="daemon-2", lease_seconds=60, limit=10
    )
    assert [item.id for item in third] == [task.id]


def test_claim_event_task_and_delete(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    task = store.create_task(
        name="event",
        schedule="on build",
        prompt="run",
        session="scheduler-session",
        source="user",
        status="approved",
        next_run_at=None,
    )
    claimed = store.claim_task(
        task.id, now=now, lease_id="daemon", lease_seconds=60
    )
    assert claimed is not None
    store.release_task(task.id, lease_id="daemon")
    store.delete_task(task.id)
    assert store.task(task.id) is None


def test_reads_before_daemon_migration_are_empty(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path / "scheduler" / "lara-scheduler.sqlite3")
    assert store.list_tasks() == []
    assert store.task("missing") is None
    assert store.list_runs() == []
    assert store.submission("missing") is None
