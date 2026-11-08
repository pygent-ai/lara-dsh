"""Shared helpers for the realtime interaction matrix scenarios.

The matrix runs the real ``LaraRuntimeService``, session store, tool pipeline
and orchestrator. Only the remote model is replaced by an in-process
``ScriptedRealtimeInvoker``; every scenario controls timing through explicit
gates, so the suite is deterministic and needs no network or credentials.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pygent import AIMessage, Context, GenerationConfig, Message, ToolCall
from pygent.llm import ModelExecution, ModelProviderResponse

from _e2e_support import content_text, context_text, make_config
from lara.orchestration.runtime_pool import WorkspaceRuntimePool
from lara.runtime.service import LaraRuntimeService
from lara.schema.models import RealtimeInteractionConfig
from lara.sessions import SessionManager

RealtimeResponder = Callable[
    ["RealtimeModelCall"], AIMessage | Awaitable[AIMessage]
]


@dataclass(frozen=True)
class RealtimeModelCall:
    """One model invocation, tagged with the generation that drove it."""

    message: Message
    context: Context
    generation: GenerationConfig
    tools: tuple[Any, ...]

    @property
    def is_fork(self) -> bool:
        """Fork replies force ``tool_choice="speak"``; main turns do not."""
        return self.generation.tool_choice == "speak"

    @property
    def text(self) -> str:
        """Everything the model can see for this call, including tool outputs."""
        parts = [content_text(self.message), context_text(self.context)]
        for item in (self.message, *self.context.messages):
            for result in getattr(item, "results", ()) or ():
                parts.append(str(getattr(result, "output", "")))
        return "\n".join(parts)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(str(getattr(tool, "name", tool)) for tool in self.tools)

    @property
    def message_kind(self) -> str | None:
        return getattr(self.message, "kind", None)


class ScriptedRealtimeInvoker:
    """Record every main/fork model call and answer from a Python responder."""

    def __init__(self, responder: RealtimeResponder) -> None:
        self.responder = responder
        self.calls: list[RealtimeModelCall] = []

    def execute(
        self,
        *,
        model_group: Any,
        retry_policy: Any,
        generation: GenerationConfig,
        message: Message,
        context: Context,
        tools: tuple[Any, ...] = (),
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> ModelExecution:
        del model_group, retry_policy, deadline, cancel_event
        call = RealtimeModelCall(
            message=message,
            context=context,
            generation=generation,
            tools=tools,
        )
        self.calls.append(call)

        async def operation(_emit: Any) -> ModelProviderResponse:
            answer = self.responder(call)
            if inspect.isawaitable(answer):
                answer = await answer
            return ModelProviderResponse(message=answer, finish_reason="stop")

        return ModelExecution(operation)

    @property
    def main_calls(self) -> list[RealtimeModelCall]:
        return [call for call in self.calls if not call.is_fork]

    @property
    def fork_calls(self) -> list[RealtimeModelCall]:
        return [call for call in self.calls if call.is_fork]

    async def aclose(self) -> None:
        pass

    async def close(self) -> None:
        pass


@dataclass
class RealtimeScenario:
    config: Any
    invoker: ScriptedRealtimeInvoker
    service: LaraRuntimeService
    pool: WorkspaceRuntimePool
    manager: SessionManager
    session: Any


def build_scenario(
    tmp_path: Path,
    responder: RealtimeResponder,
    *,
    realtime: bool = True,
    mode: str = "lara-says",
    fork_deadline_seconds: float | None = None,
    nudge_interval: int | None = None,
) -> RealtimeScenario:
    """Create one isolated realtime (or negative-control) scenario."""

    config = make_config(tmp_path, realtime=realtime)
    if fork_deadline_seconds is not None or nudge_interval is not None:
        current = config.realtime_interaction
        config.realtime_interaction = RealtimeInteractionConfig(
            enabled=realtime,
            nudge_interval=nudge_interval or current.nudge_interval,
            fork_deadline_seconds=(
                fork_deadline_seconds or current.fork_deadline_seconds
            ),
        )
    invoker = ScriptedRealtimeInvoker(responder)
    service = LaraRuntimeService(config)
    service._model_invokers["default"] = invoker
    pool = WorkspaceRuntimePool(runtime_factory=lambda _config, **_kwargs: service)
    manager = SessionManager(config)
    session = manager.create("chat", mode=mode)
    return RealtimeScenario(
        config=config,
        invoker=invoker,
        service=service,
        pool=pool,
        manager=manager,
        session=session,
    )


async def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    description: str = "condition",
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"{description} was not reached before timeout")
        await asyncio.sleep(0.01)


def session_history(scenario: RealtimeScenario) -> list[dict[str, Any]]:
    return scenario.manager.load(scenario.session.session_id).history


def finalizer_messages(scenario: RealtimeScenario) -> list[dict[str, Any]]:
    return [
        item
        for item in session_history(scenario)
        if item.get("kind") == "lara.realtime.finalizer"
    ]


def claim_envelopes(
    scenario: RealtimeScenario, *, claim_id: str
) -> list[Any]:
    return scenario.service.reminders.message_store.claim_messages(
        scenario.session.session_id,
        claim_id=claim_id,
        lease_seconds=30,
    )


def release_envelopes(
    scenario: RealtimeScenario, messages: Sequence[Any], *, claim_id: str
) -> None:
    for item in messages:
        scenario.service.reminders.message_store.release_message(
            item.message_id,
            claim_id=claim_id,
        )


async def speak_events(turn: Any) -> list[dict[str, Any]]:
    """Collect the user-visible speak events emitted by a finished turn."""

    events: list[dict[str, Any]] = []
    async with turn.subscribe(after=0) as stream:
        async for event in stream:
            if event.kind == "lara.realtime.speak":
                events.append(dict(event.data))
    return events


def write_workspace_file(
    scenario: RealtimeScenario, name: str, content: str
) -> Path:
    path = Path(scenario.config.workspace_root) / name
    path.write_text(content, encoding="utf-8")
    return path


def speak_call(call_id: str, text: str) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name="speak",
        arguments={"text": text},
    )


def say_nothing_call(call_id: str) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name="speak",
        arguments={"say_nothing": True},
    )


def read_call(call_id: str, file_path: str) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name="read",
        arguments={"file_path": file_path},
    )


def count_occurrences(haystack: str, needle: str) -> int:
    return haystack.count(needle)


async def assert_no_extra_main_call(
    invoker: ScriptedRealtimeInvoker,
    expected: int,
    *,
    timeout: float = 0.75,
) -> None:
    """Give a queued follow-up lane a chance to surface, then assert it has not.

    The session lane releases only after the current turn finalizes, so a
    queued finalizer becomes observable as one more main model call. Waiting
    for that call (and failing if it appears) is stronger than a fixed sleep.
    """

    try:
        await wait_until(
            lambda: len(invoker.main_calls) > expected,
            timeout=timeout,
            description="unexpected follow-up model call",
        )
    except AssertionError:
        pass
    assert len(invoker.main_calls) == expected


__all__ = [
    "RealtimeModelCall",
    "RealtimeResponder",
    "RealtimeScenario",
    "ScriptedRealtimeInvoker",
    "assert_no_extra_main_call",
    "build_scenario",
    "claim_envelopes",
    "count_occurrences",
    "finalizer_messages",
    "read_call",
    "release_envelopes",
    "say_nothing_call",
    "session_history",
    "speak_call",
    "speak_events",
    "wait_until",
    "write_workspace_file",
]
