"""Scheduler daemon: verify submissions, promote tasks, and execute runs.

The daemon is the only process allowed to write ``tasks`` and ``runs``. It
polls every ``scheduler.poll_seconds``, claims due tasks with an expiring
lease, and invokes the Lara CLI in a child process with the inherited
environment (no scheduler-specific variables are injected).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from lara.core.io import append_jsonl
from lara.schema import RunConfig

from .keys import identify_source, load_keys, scheduler_keys_dir
from .schedule import matches_event, next_run_at, parse_schedule
from .store import (
    SchedulerStore,
    Task,
    iso,
    prompt_hash,
    scheduler_db_path,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class TaskResult:
    exit_code: int | None
    output: str = ""
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskRunner(Protocol):
    def run(self, task: Task) -> TaskResult: ...


def lara_command() -> tuple[str, ...]:
    executable = shutil.which("lara")
    if executable:
        return (executable,)
    return (sys.executable, "-m", "lara")


class SubprocessTaskRunner:
    """Run one task through the Lara CLI in a child process."""

    def __init__(
        self,
        *,
        command: tuple[str, ...] | None = None,
        timeout: float = 3600.0,
    ) -> None:
        self.command = command or lara_command()
        self.timeout = timeout

    def run(self, task: Task) -> TaskResult:
        started = utc_now()
        command = (*self.command, "session", "run", task.session, "-m", task.prompt)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=None,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return TaskResult(
                exit_code=None,
                output="",
                error=str(exc),
                started_at=started,
                finished_at=utc_now(),
            )
        output = (completed.stdout or "") + (completed.stderr or "")
        return TaskResult(
            exit_code=completed.returncode,
            output=output[-20_000:],
            error=None if completed.returncode == 0 else output[-20_000:],
            started_at=started,
            finished_at=utc_now(),
        )


class SchedulerDaemon:
    """Single-agent scheduler process with durable submissions and runs."""

    def __init__(
        self,
        config: RunConfig,
        *,
        store: SchedulerStore | None = None,
        keys: dict[str, bytes] | None = None,
        runner: TaskRunner | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        alert: Callable[[str], None] | None = None,
        session_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> None:
        self.config = config
        self.store = store or SchedulerStore(scheduler_db_path(config.lara_root))
        self.keys_root = scheduler_keys_dir(config.user_lara_root)
        self.keys = keys
        self.runner = runner or SubprocessTaskRunner()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleeper = sleeper or time.sleep
        self.alert_callback = alert
        self.owner = f"scheduler-{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds or max(
            60.0, float(config.scheduler.poll_seconds) * 3
        )
        self.session_id = session_id or config.scheduler.session
        self.alerts_path = Path(config.lara_root) / "scheduler" / "alerts.jsonl"
        self.alerts: list[str] = []
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        self._startup()
        startup = True
        while not self._stop.is_set():
            self.run_once(startup=startup)
            startup = False
            self.sleeper(float(self.config.scheduler.poll_seconds))

    def run_once(self, *, now: datetime | None = None, startup: bool = False) -> dict[str, int]:
        self._startup()
        current = now or self.clock()
        submissions = self._process_submissions(current)
        tasks = self._run_due_tasks(current, startup=startup)
        return {"submissions": submissions, "tasks": tasks}

    def _startup(self) -> None:
        self.store.migrate()
        if self.keys is None:
            self.keys = load_keys(self.keys_root)
        self.alerts_path.parent.mkdir(parents=True, exist_ok=True)

    def _process_submissions(self, now: datetime) -> int:
        assert self.keys is not None
        submissions = self.store.pending_submissions(limit=1000)
        events = []
        processed = 0
        for submission in submissions:
            source = identify_source(
                self.keys,
                kind=submission.kind,
                payload=submission.payload,
                signature=submission.signature,
            )
            if source is None:
                self._quarantine(submission.id, "missing or invalid signature")
                continue
            if source != submission.source:
                self._quarantine(
                    submission.id,
                    f"signature source {source!r} does not match claimed source "
                    f"{submission.source!r}",
                )
                continue
            try:
                if submission.kind == "task":
                    task = self._promote_task(submission.source, submission.payload, now)
                    self.store.mark_submission(
                        submission.id, "verified", task_id=task.id
                    )
                elif submission.kind == "control":
                    task_id = self._apply_control(submission.payload, now)
                    self.store.mark_submission(
                        submission.id, "verified", task_id=task_id
                    )
                elif submission.kind == "event":
                    events.append(submission)
                else:
                    raise ValueError(f"unsupported submission kind {submission.kind!r}")
                processed += 1
            except Exception as exc:  # noqa: BLE001 - rejected submissions stay auditable
                self.store.mark_submission(
                    submission.id, "rejected", error=f"{type(exc).__name__}: {exc}"
                )
                self._alert(f"rejected submission {submission.id}: {exc}")
        # Events run after task/control submissions so a task created in the
        # same poll can receive an event emitted with it.
        for submission in events:
            try:
                self._consume_event(submission.payload, now)
                self.store.mark_submission(submission.id, "verified")
                processed += 1
            except Exception as exc:  # noqa: BLE001 - keep event evidence
                self.store.mark_submission(
                    submission.id, "rejected", error=f"{type(exc).__name__}: {exc}"
                )
                self._alert(f"rejected event {submission.id}: {exc}")
        return processed

    def _quarantine(self, submission_id: str, reason: str) -> None:
        self.store.mark_submission(submission_id, "quarantined", error=reason)
        self._alert(f"quarantined submission {submission_id}: {reason}")

    def _promote_task(self, source: str, payload: dict[str, Any], now: datetime) -> Task:
        name = str(payload.get("name") or "").strip()
        schedule_text = str(payload.get("schedule") or "").strip()
        prompt = str(payload.get("prompt") or "")
        if not name:
            raise ValueError("task name must be non-empty")
        if not prompt.strip():
            raise ValueError("task prompt must be non-empty")
        spec = parse_schedule(schedule_text)
        next_at = next_run_at(spec, now, allow_past=True)
        status = (
            "approved"
            if source == "user" or self.config.scheduler.agent_create_enabled
            else "needs_review"
        )
        return self.store.create_task(
            name=name,
            schedule=spec.raw,
            prompt=prompt,
            session=str(payload.get("session") or self.session_id),
            source=source,
            status=status,
            next_run_at=next_at,
            created_at=now,
        )

    def _apply_control(self, payload: dict[str, Any], now: datetime) -> str | None:
        action = str(payload.get("action") or "").strip()
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("control submission requires task_id")
        task = self.store.task(task_id)
        if task is None:
            raise ValueError(f"unknown task {task_id!r}")
        if action == "approve":
            self.store.update_task_next_run(
                task_id,
                self._next_for_resume(task, now),
                status="approved",
            )
        elif action == "reject":
            self.store.set_task_status(task_id, "rejected")
        elif action == "pause":
            self.store.set_task_status(task_id, "paused")
        elif action == "resume":
            self.store.update_task_next_run(
                task_id,
                self._next_for_resume(task, now),
                status="approved",
            )
        elif action == "delete":
            self.store.delete_task(task_id)
        else:
            raise ValueError(f"unsupported control action {action!r}")
        return task_id

    @staticmethod
    def _next_for_resume(task: Task, now: datetime) -> datetime | None:
        spec = parse_schedule(task.schedule)
        return next_run_at(spec, now, allow_past=True)

    def _consume_event(self, payload: dict[str, Any], now: datetime) -> None:
        event = str(payload.get("event") or "").strip()
        if not event:
            raise ValueError("event submission requires event")
        for task in self.store.list_tasks(status="approved"):
            try:
                spec = parse_schedule(task.schedule)
            except ValueError:
                continue
            if not matches_event(spec, event):
                continue
            claimed = self.store.claim_task(
                task.id,
                now=now,
                lease_id=self.owner,
                lease_seconds=self.lease_seconds,
            )
            if claimed is not None:
                self._execute_task(claimed, now, missed=False)

    def _run_due_tasks(self, now: datetime, *, startup: bool) -> int:
        tasks = self.store.claim_due_tasks(
            now=now,
            lease_id=self.owner,
            lease_seconds=self.lease_seconds,
            limit=self.config.scheduler.max_concurrent,
        )
        if not tasks:
            return 0
        missed_by_id = {
            task.id: bool(
                startup
                and task.next_run_at is not None
                and task.next_run_at < now
            )
            for task in tasks
        }
        with ThreadPoolExecutor(
            max_workers=self.config.scheduler.max_concurrent
        ) as pool:
            futures = [
                pool.submit(self._execute_task, task, now, missed_by_id[task.id])
                for task in tasks
            ]
            for future in futures:
                future.result()
        return len(tasks)

    def _execute_task(self, task: Task, now: datetime, missed: bool) -> None:
        started = utc_now()
        if task.prompt_hash != prompt_hash(task.prompt):
            message = f"prompt hash mismatch for task {task.id}"
            self.store.set_task_status(task.id, "rejected")
            self.store.update_task_next_run(task.id, None)
            self.store.append_run(
                task_id=task.id,
                status="rejected",
                started_at=started,
                finished_at=utc_now(),
                exit_code=None,
                output="",
                error=message,
                missed=missed,
            )
            self.store.release_task(task.id, lease_id=self.owner)
            self._alert(message)
            return
        try:
            result = self.runner.run(task)
        except Exception as exc:  # noqa: BLE001 - a failed task must become an audit run
            result = TaskResult(
                exit_code=None,
                error=f"{type(exc).__name__}: {exc}",
                started_at=started,
                finished_at=utc_now(),
            )
        status = "success" if result.exit_code == 0 else "failed"
        finished = result.finished_at or utc_now()
        self.store.append_run(
            task_id=task.id,
            status=status,
            started_at=result.started_at or started,
            finished_at=finished,
            exit_code=result.exit_code,
            output=result.output,
            error=result.error,
            missed=missed,
        )
        spec = parse_schedule(task.schedule)
        self.store.update_task_next_run(task.id, next_run_at(spec, finished))
        self.store.release_task(task.id, lease_id=self.owner)
        if status == "failed":
            self._alert(f"task {task.id} failed: {result.error or result.output}")

    def _alert(self, message: str) -> None:
        self.alerts.append(message)
        append_jsonl(
            self.alerts_path,
            {"at": iso(utc_now()), "message": message},
        )
        if self.alert_callback is not None:
            self.alert_callback(message)


__all__ = [
    "SchedulerDaemon",
    "SubprocessTaskRunner",
    "TaskResult",
    "TaskRunner",
    "lara_command",
]
