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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pet" / "sidecar"))

import main as sidecar  # noqa: E402  (pet/sidecar/main.py)
from lara_shell.dsh_daemon import DshEvent, DshEventBatch  # noqa: E402
from pygent.agent.react_projection_operations import (  # noqa: E402
    REACT_PROJECTION_OPERATION_KIND,
    StandaloneUserMessage,
    SteeringMode,
    decode_react_projection_operation,
)


class _Handle:
    def __init__(self, status: str = "accepted") -> None:
        self.status = status
        self.inputs: list[dict[str, Any]] = []

    async def send_input(self, *, input_id: str, kind: str, value: Any) -> SimpleNamespace:
        self.inputs.append({"input_id": input_id, "kind": kind, "value": value})
        return SimpleNamespace(status=self.status)


def _turn(status: str = "running", handle: Any = None, run_ref: Any = "default") -> SimpleNamespace:
    if run_ref == "default":
        run_ref = SimpleNamespace(case_run_id="run-1")
    return SimpleNamespace(status=status, execution_handle=handle, run_ref=run_ref)


def _session(active_turn: Any) -> tuple[Any, list[Any]]:
    session = sidecar.PetSession.__new__(sidecar.PetSession)
    session._active_turn = active_turn
    session._turn_tasks = set()
    opened: list[Any] = []

    async def start_event_turn(event: Any) -> None:
        opened.append(event)

    session._start_event_turn = start_event_turn  # type: ignore[method-assign]
    return session, opened


def _event() -> DshEvent:
    return DshEvent(kind="turn_finished", session_id="dsh-1", title="t", finish_reason="completed", reply="done")


def test_running_turn_is_steered_with_a_harness_framed_user_message() -> None:
    handle = _Handle()
    session, opened = _session(_turn(handle=handle))

    asyncio.run(session._route_dsh_event(_event()))

    assert opened == []
    (sent,) = handle.inputs
    assert sent["kind"] == REACT_PROJECTION_OPERATION_KIND
    operation = decode_react_projection_operation(sent["value"])
    assert isinstance(operation, StandaloneUserMessage)
    assert operation.mode is SteeringMode.WAIT  # never interrupts the step in flight
    message = operation.message
    assert message.kind == sidecar.DSH_EVENT_MESSAGE_KIND  # not a <user-context> turn
    assert message.content.startswith('<dsh-event kind="turn_finished">')
    assert "<reply>done</reply>" in message.content
    assert dict(message.data) == {"case_run_id": "run-1", "input_id": sent["input_id"]}


def test_batches_are_steered_as_one_message() -> None:
    handle = _Handle()
    session, _ = _session(_turn(handle=handle))
    batch = DshEventBatch((_event(), DshEvent(kind="turn_finished", session_id="dsh-2", finish_reason="completed", reply="also")))
    asyncio.run(session._route_dsh_event(batch))
    (sent,) = handle.inputs
    operation = decode_react_projection_operation(sent["value"])
    assert isinstance(operation, StandaloneUserMessage)
    assert operation.message.content.startswith('<dsh-events count="2">')


def test_idle_session_opens_a_turn_for_the_report() -> None:
    session, opened = _session(None)
    asyncio.run(session._route_dsh_event(_event()))
    assert len(opened) == 1 and opened[0].kind == "turn_finished"


def test_finished_or_unready_turn_opens_a_new_turn() -> None:
    for turn in (
        _turn(status="passed", handle=_Handle()),  # already ended
        _turn(handle=None),  # execution not ready yet
        _turn(handle=_Handle(), run_ref=None),  # no run to bind the message to
    ):
        session, opened = _session(turn)
        asyncio.run(session._route_dsh_event(_event()))
        assert len(opened) == 1


def test_sealed_inbox_falls_back_to_a_new_turn() -> None:
    handle = _Handle(status="sealed")  # the turn ended between our check and the send
    session, opened = _session(_turn(handle=handle))
    asyncio.run(session._route_dsh_event(_event()))
    assert len(handle.inputs) == 1 and len(opened) == 1


def test_send_failure_falls_back_to_a_new_turn() -> None:
    class Broken:
        async def send_input(self, **kwargs: Any) -> SimpleNamespace:
            raise RuntimeError("execution gone")

    session, opened = _session(_turn(handle=Broken()))
    asyncio.run(session._route_dsh_event(_event()))
    assert len(opened) == 1
