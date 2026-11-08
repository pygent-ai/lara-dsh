"""Scheduler agent-tool tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lara.scheduler.keys import identify_source, init_keys, load_keys
from lara.scheduler.store import SchedulerStore, scheduler_db_path
from lara.scheduler.tools import (
    EMIT_EVENT_TOOL_SPEC,
    SCHEDULE_TASK_TOOL_SPEC,
    SchedulerToolExecutor,
)
from lara.schema import RunConfig


def _config(tmp_path: Path, *, enabled: bool) -> RunConfig:
    return RunConfig(
        workspace_root=str(tmp_path / "workspace"),
        user_lara_root=str(tmp_path / "user"),
        scheduler={"agent_create_enabled": enabled},  # type: ignore[arg-type]
    )


def _call(arguments: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(arguments=arguments, idempotency_key="call-1", call_id="call-1")


async def test_agent_tools_reject_when_disabled(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=False)
    store = SchedulerStore(scheduler_db_path(config.lara_root))
    store.migrate()
    init_keys(tmp_path / "user" / "scheduler" / "keys")
    executor = SchedulerToolExecutor(config, store=store)
    with pytest.raises(PermissionError, match="disabled"):
        await executor.execute(
            SCHEDULE_TASK_TOOL_SPEC,
            _call({"name": "n", "schedule": "every 1h", "prompt": "p"}),
            None,  # type: ignore[arg-type]
        )
    with pytest.raises(PermissionError, match="disabled"):
        await executor.execute(
            EMIT_EVENT_TOOL_SPEC,
            _call({"event": "build"}),
            None,  # type: ignore[arg-type]
        )


async def test_schedule_task_signs_as_agent(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True)
    store = SchedulerStore(scheduler_db_path(config.lara_root))
    store.migrate()
    init_keys(tmp_path / "user" / "scheduler" / "keys")
    keys = load_keys(tmp_path / "user" / "scheduler" / "keys")
    executor = SchedulerToolExecutor(config, store=store, keys=keys)
    result = await executor.execute(
        SCHEDULE_TASK_TOOL_SPEC,
        _call({"name": "agent task", "schedule": "daily 09:00", "prompt": "do it"}),
        None,  # type: ignore[arg-type]
    )
    assert isinstance(result, dict)
    submission = store.submission(str(result["id"]))
    assert submission is not None
    assert submission.source == "agent"
    assert identify_source(
        keys,
        kind="task",
        payload=submission.payload,
        signature=submission.signature,
    ) == "agent"


async def test_emit_event_signs_as_agent(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True)
    store = SchedulerStore(scheduler_db_path(config.lara_root))
    store.migrate()
    init_keys(tmp_path / "user" / "scheduler" / "keys")
    keys = load_keys(tmp_path / "user" / "scheduler" / "keys")
    executor = SchedulerToolExecutor(config, store=store, keys=keys)
    result = await executor.execute(
        EMIT_EVENT_TOOL_SPEC,
        _call({"event": "build.finished", "data": {"ok": True}}),
        None,  # type: ignore[arg-type]
    )
    assert isinstance(result, dict)
    submission = store.submission(str(result["id"]))
    assert submission is not None
    assert submission.source == "agent"
    assert submission.payload["event"] == "build.finished"
