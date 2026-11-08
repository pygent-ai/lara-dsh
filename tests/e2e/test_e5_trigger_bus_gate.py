"""E5: the trigger-bus approval gate for agent-originated tasks.

Anchors: agent-signed submissions land as ``needs_review``; a user approval
promotes the task so the daemon can execute it; an agent-requested ``high``
priority is normalized to ``normal`` with an audit note.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from _e2e_support import make_config
from lara.scheduler.daemon import SchedulerDaemon, TaskResult
from lara.scheduler.keys import init_keys, load_secret, sign
from lara.scheduler.store import (
    SchedulerStore,
    scheduler_db_path,
    submission_payload,
)


class _Runner:
    def __init__(self) -> None:
        self.tasks: list[object] = []

    def run(self, task: object) -> TaskResult:
        self.tasks.append(task)
        return TaskResult(exit_code=0, output="gate runner")


@pytest.mark.e2e
def test_agent_task_waits_for_review_then_runs_with_normalized_priority(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    init_keys(config.user_lara_root)
    store = SchedulerStore(scheduler_db_path(config.lara_root))
    store.migrate()

    payload: dict[str, object] = {
        "name": "agent-task",
        "schedule": "at 2020-01-01T00:00:00+00:00",
        "prompt": "agent requested work",
        "instructions": "",
        "session": "scheduler",
        "priority": "high",
    }
    submitted = store.submit(
        kind="task",
        source="agent",
        payload=payload,
        signature=sign(
            submission_payload("task", "agent", payload),
            load_secret(config.user_lara_root, "agent"),
        ),
    )
    submission_id = submitted.id

    runner = _Runner()
    daemon = SchedulerDaemon(
        config, store=store, runner=runner, session_id="scheduler"
    )
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    daemon.run_once(now=now)

    task = store.list_tasks()[0]
    assert task.source == "agent"
    assert task.status == "needs_review"
    assert task.priority == "normal"
    submission = store.submission(submission_id)
    assert submission is not None
    assert submission.error == "agent-requested-high normalized"
    assert runner.tasks == []
    assert store.list_runs(task_id=task.id) == []

    control = {"action": "approve", "task_id": task.id}
    control_submission = store.submit(
        kind="control",
        source="user",
        payload=control,
        signature=sign(
            submission_payload("control", "user", control),
            load_secret(config.user_lara_root, "user"),
        ),
    )
    daemon.run_once(now=now)

    assert store.submission(control_submission.id).status == "verified"  # type: ignore[union-attr]
    assert len(runner.tasks) == 1
    runs = store.list_runs(task_id=task.id)
    assert len(runs) == 1
    assert runs[0].status == "success"
