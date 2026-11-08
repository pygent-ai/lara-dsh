"""E6: an ``on <event>`` task fires only for its matching emitted event.

Anchors: agent-signed event submissions enter the trigger bus, the daemon
consumes them, and only the matching ``on build`` task executes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from _e2e_support import make_config
from lara.scheduler.daemon import SchedulerDaemon, TaskResult
from lara.scheduler.keys import init_keys
from lara.scheduler.store import SchedulerStore, scheduler_db_path
from lara.scheduler.tools import EMIT_EVENT_TOOL_SPEC, SchedulerToolExecutor


class _Runner:
    def __init__(self) -> None:
        self.tasks: list[object] = []

    def run(self, task: object) -> TaskResult:
        self.tasks.append(task)
        return TaskResult(exit_code=0, output="event runner")


def _event_call(name: str, call_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        arguments={"event": name, "data": {"ok": True}},
        idempotency_key=call_id,
        call_id=call_id,
    )


@pytest.mark.e2e
def test_emit_event_triggers_only_matching_on_task(tmp_path: Path) -> None:
    config = make_config(tmp_path, scheduler_agent_create=True)
    init_keys(config.user_lara_root)
    store = SchedulerStore(scheduler_db_path(config.lara_root))
    store.migrate()
    task = store.create_task(
        name="on-build",
        schedule="on build",
        prompt="run for build",
        session="scheduler",
        source="user",
        status="approved",
        next_run_at=None,
    )
    executor = SchedulerToolExecutor(config, store=store)
    runner = _Runner()
    daemon = SchedulerDaemon(
        config, store=store, runner=runner, session_id="scheduler"
    )
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    other = asyncio.run(
        executor.execute(
            EMIT_EVENT_TOOL_SPEC,
            _event_call("deploy", "event-other"),
            None,  # type: ignore[arg-type]
        )
    )
    assert isinstance(other, dict)
    daemon.run_once(now=now)
    assert runner.tasks == []
    assert store.list_runs(task_id=task.id) == []

    matching = asyncio.run(
        executor.execute(
            EMIT_EVENT_TOOL_SPEC,
            _event_call("build", "event-build"),
            None,  # type: ignore[arg-type]
        )
    )
    assert isinstance(matching, dict)
    daemon.run_once(now=now)

    assert store.submission(str(other["id"])).status == "verified"  # type: ignore[union-attr]
    assert store.submission(str(matching["id"])).status == "verified"  # type: ignore[union-attr]
    assert len(runner.tasks) == 1
    runs = store.list_runs(task_id=task.id)
    assert len(runs) == 1
    assert runs[0].status == "success"
