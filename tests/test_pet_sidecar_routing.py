"""Routing tests for the pet sidecar's dsh-event delivery (no model, no dsh).

``PetSession`` is built without running ``__init__``; only the routing state
is set, so ``_route_dsh_event`` can be driven with a fake turn.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pet" / "sidecar"))

import main as sidecar  # noqa: E402  (pet/sidecar/main.py)
from lara_shell.dsh_daemon import DshEvent  # noqa: E402
from pygent.agent.react_projection_operations import (  # noqa: E402
    REACT_PROJECTION_OPERATION_KIND,
    AppendToolResultContent,
    decode_react_projection_operation,
)


class _Handle:
    def __init__(self, status: str = "accepted") -> None:
        self.status = status
        self.inputs: list[dict[str, Any]] = []

    async def send_input(self, *, input_id: str, kind: str, value: Any) -> SimpleNamespace:
        self.inputs.append({"input_id": input_id, "kind": kind, "value": value})
        return SimpleNamespace(status=self.status)


def _session(active_turn: Any) -> tuple[Any, list[Any]]:
    session = sidecar.PetSession.__new__(sidecar.PetSession)
    session._active_turn = active_turn
    session._pending_appends = {}
    session._turn_tasks = set()
    opened: list[Any] = []

    async def start_event_turn(event: Any) -> None:
        opened.append(event)

    session._start_event_turn = start_event_turn  # type: ignore[method-assign]
    return session, opened


def _event() -> DshEvent:
    return DshEvent(kind="turn_finished", session_id="dsh-1", title="t", finish_reason="completed", reply="done")


def test_running_turn_gets_the_report_appended_to_its_tool_result() -> None:
    handle = _Handle()
    turn = SimpleNamespace(status="running", execution_handle=handle)
    session, opened = _session(turn)

    asyncio.run(session._route_dsh_event(_event()))

    assert opened == []
    (sent,) = handle.inputs
    assert sent["kind"] == REACT_PROJECTION_OPERATION_KIND
    operation = decode_react_projection_operation(sent["value"])
    assert isinstance(operation, AppendToolResultContent)
    assert operation.content.startswith("dsh event: turn_finished — session dsh-1 (t), finish_reason=completed")
    assert "<" not in operation.content  # plain text: pygent escapes appended context
    # Remembered until the react layer applies or rejects it.
    assert list(session._pending_appends) == [sent["input_id"]]


def test_idle_session_opens_a_turn_for_the_report() -> None:
    session, opened = _session(None)
    asyncio.run(session._route_dsh_event(_event()))
    assert len(opened) == 1 and opened[0].kind == "turn_finished"


def test_finished_or_unready_turn_falls_back_to_a_new_turn() -> None:
    for turn in (
        SimpleNamespace(status="passed", execution_handle=_Handle()),  # already ended
        SimpleNamespace(status="running", execution_handle=None),  # not ready yet
    ):
        session, opened = _session(turn)
        asyncio.run(session._route_dsh_event(_event()))
        assert len(opened) == 1
        assert session._pending_appends == {}


def test_refused_append_falls_back_immediately() -> None:
    handle = _Handle(status="sealed")  # inbox closed: no later tool result will come
    session, opened = _session(SimpleNamespace(status="running", execution_handle=handle))
    asyncio.run(session._route_dsh_event(_event()))
    assert len(handle.inputs) == 1
    assert len(opened) == 1 and session._pending_appends == {}


def test_send_failure_falls_back_to_a_new_turn() -> None:
    class Broken:
        async def send_input(self, **kwargs: Any) -> SimpleNamespace:
            raise RuntimeError("execution gone")

    session, opened = _session(SimpleNamespace(status="running", execution_handle=Broken()))
    asyncio.run(session._route_dsh_event(_event()))
    assert len(opened) == 1 and session._pending_appends == {}


def test_react_rejection_during_the_turn_reroutes_the_event() -> None:
    """The watcher sees `react.projection_operation.rejected` and re-delivers."""
    handle = _Handle()
    turn = SimpleNamespace(status="running", execution_handle=handle)
    session, opened = _session(turn)
    asyncio.run(session._route_dsh_event(_event()))
    (input_id,) = session._pending_appends

    # Simulate what _watch_turn does on the rejection event for this input id.
    pending = session._pending_appends.pop(input_id)
    rejected = [pending]
    for event in rejected:
        asyncio.run(session._route_dsh_event(event))  # turn still "running" → appended again
    assert len(handle.inputs) == 2  # a fresh input id, same content
    assert handle.inputs[0]["input_id"] != handle.inputs[1]["input_id"]
    first, second = (decode_react_projection_operation(i["value"]) for i in handle.inputs)
    assert first == second
