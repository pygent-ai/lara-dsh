"""Unit tests for the complete ``lara_sdk.Lara`` facade.

The real session store, orchestration host, tool pipeline and scheduler all
run; only the remote model is replaced by a scripted invoker.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pygent import AIMessage, Context, Message
from pygent.llm import ModelExecution, ModelProviderResponse

from lara.schema import ResolvedAgentConfig, RunConfig
from lara_sdk import AgentResult, Lara, LaraConfig, SchedulerSettings

MODEL_CONFIG_MAPPING: dict[str, Any] = {
    "connections": {
        "facade": {
            "provider": "facade-scripted",
            "credential": {"none": True},
            "protocols": {
                "openai_chat_completions": {"base_url": "http://127.0.0.1:1"}
            },
        }
    },
    "models": {
        "scripted": {
            "connection": "facade",
            "model_id": "scripted",
            "protocol": "openai_chat_completions",
            "capabilities": {
                "modalities": {"input": ["text"], "output": ["text"]},
                "streaming": {"output": []},
                "tools": {
                    "call": True,
                    "choice": ["auto"],
                    "parallel": False,
                },
                "structured_output": {"json_object": False, "json_schema": False},
                "reasoning": {"supported": False, "controllable": False},
                "limits": {"context_tokens": 100_000, "max_output_tokens": 4_096},
            },
        }
    },
    "model_groups": {"e2e": {"models": ["scripted"]}},
}

Responder = Callable[[Message, Context], AIMessage | Awaitable[AIMessage]]


class RecordingInvoker:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.requests: list[tuple[Message, Context, tuple[Any, ...]]] = []

    def execute(
        self,
        *,
        model_group: Any,
        retry_policy: Any,
        generation: Any,
        message: Message,
        context: Context,
        tools: tuple[Any, ...] = (),
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> ModelExecution:
        del model_group, retry_policy, generation, deadline, cancel_event
        self.requests.append((message, context, tools))

        async def operation(_emit: Any) -> ModelProviderResponse:
            answer = self.responder(message, context)
            if inspect.isawaitable(answer):
                answer = await answer
            return ModelProviderResponse(message=answer, finish_reason="stop")

        return ModelExecution(operation)

    async def aclose(self) -> None:
        pass

    async def close(self) -> None:
        pass


def _run_config(tmp_path: Path) -> RunConfig:
    workspace = tmp_path / "workspace"
    user_root = tmp_path / "user"
    workspace.mkdir(parents=True, exist_ok=True)
    user_root.mkdir(parents=True, exist_ok=True)
    return RunConfig(
        workspace_root=str(workspace),
        user_lara_root=str(user_root),
        model_config_mapping=MODEL_CONFIG_MAPPING,
        resolved_agent=ResolvedAgentConfig(
            alias="default", default_model_group="e2e"
        ),
        runtime_approvals={"enabled": False},  # type: ignore[arg-type]
    )


@pytest.fixture()
def configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = _run_config(tmp_path)

    def apply(
        responder: Responder,
        *,
        realtime: bool = False,
        eternal_memory: bool = False,
        scheduler: SchedulerSettings | None = None,
        model_group: str = "e2e",
    ) -> tuple[Lara, RecordingInvoker]:
        invoker = RecordingInvoker(responder)
        monkeypatch.setattr(
            "lara.runtime.agent.core.build_model_invoker",
            lambda *_args, **_kwargs: invoker,
        )
        monkeypatch.setattr(
            "lara_sdk.agent.load_run_config", lambda **_kwargs: config
        )
        facade = Lara(
            LaraConfig(
                workspace_root=config.workspace_root,
                model_group=model_group,
                eternal_memory=eternal_memory,
                realtime=realtime,
                scheduler=scheduler or SchedulerSettings(enabled=False),
                system_prompt="Test agent system prompt for facade suites.",
            )
        )
        return facade, invoker

    return apply


def _message_text(message: Message, context: Context) -> str:
    parts = [str(getattr(message, "content", ""))]
    parts.extend(str(getattr(item, "content", "")) for item in context.messages)
    return "\n".join(parts)


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.01)


def _tool_names(tools: tuple[Any, ...]) -> set[str]:
    return {str(getattr(tool, "name", tool)) for tool in tools}


async def test_start_run_history_and_stop(configure) -> None:
    facade, invoker = configure(lambda _message, _context: AIMessage(content="façade"))

    with pytest.raises(RuntimeError, match="started"):
        await facade.run("too early")
    with pytest.raises(RuntimeError, match="started"):
        facade.history()

    await facade.start()
    with pytest.raises(RuntimeError, match="already started"):
        await facade.start()
    assert facade.history() == ()

    result = await facade.run("hello")

    assert isinstance(result, AgentResult)
    assert result.text == "façade"
    assert result.label.startswith("lara-session.")
    assert facade.history()
    assert "speak" not in _tool_names(invoker.requests[0][2])

    await facade.stop()
    await facade.stop()  # idempotent
    with pytest.raises(RuntimeError, match="started"):
        await facade.run("after stop")


async def test_realtime_switch_adds_speak_tool(configure) -> None:
    facade, invoker = configure(
        lambda _message, _context: AIMessage(content="plain reply"),
        realtime=True,
    )

    await facade.start()
    result = await facade.run("你好")
    await facade.stop()

    assert result.text == "plain reply"
    assert invoker.requests
    assert "speak" in _tool_names(invoker.requests[0][2])


async def test_eternal_memory_switch_enables_session_opt_in(configure) -> None:
    facade, _invoker = configure(
        lambda _message, _context: AIMessage(content="memory"),
        eternal_memory=True,
    )

    await facade.start()
    try:
        assert facade._session_agent is not None
        assert facade._session_agent.eternal_memory_enabled is True
    finally:
        await facade.stop()


async def test_scheduler_gate_approve_and_execution(configure) -> None:
    def responder(message: Message, context: Context) -> AIMessage:
        if "task-context" in _message_text(message, context):
            return AIMessage(content="scheduled result")
        return AIMessage(content="chat result")

    facade, invoker = configure(
        responder,
        scheduler=SchedulerSettings(
            enabled=True, poll_seconds=0.02, max_concurrent=2
        ),
    )
    await facade.start()
    try:
        task = await facade.schedule_task(
            "nightly", "at 2020-01-01T00:00:00+00:00", "do the work", "be terse"
        )
        assert task.status == "needs_review"
        assert task.source == "agent"
        assert facade.tasks().get(task.id) is not None
        assert facade.tasks().runs(task.id) == ()

        approved = await facade.tasks().approve(task.id)
        assert approved.status == "approved"
        await _wait_until(lambda: bool(facade.tasks().runs(task.id)))
        runs = facade.tasks().runs(task.id)
        assert len(runs) == 1
        assert runs[0].status == "success"
        assert "scheduled result" in runs[0].output

        assert any(
            "task-context" in _message_text(message, context)
            for message, context, _tools in invoker.requests
        )

        recurring = await facade.schedule_task("hourly", "every 1h", "check status")
        assert recurring.status == "needs_review"
        assert (await facade.tasks().pause(recurring.id)).status == "paused"
        assert (await facade.tasks().resume(recurring.id)).status == "approved"
        await facade.tasks().reject(recurring.id)
        assert facade.tasks().get(recurring.id).status == "rejected"  # type: ignore[union-attr]
        await facade.tasks().delete(recurring.id)
        assert facade.tasks().get(recurring.id) is None
    finally:
        await facade.stop()


async def test_event_trigger_runs_matching_task(configure) -> None:
    def responder(message: Message, context: Context) -> AIMessage:
        if "task-context" in _message_text(message, context):
            return AIMessage(content="event handled")
        return AIMessage(content="chat")

    facade, _invoker = configure(
        responder,
        scheduler=SchedulerSettings(enabled=True, poll_seconds=0.02),
    )
    await facade.start()
    try:
        task = await facade.schedule_task("on-deploy", "on deploy", "handle deploy")
        assert task.status == "needs_review"
        await facade.tasks().approve(task.id)

        submission = await facade.emit_event("deploy", {"version": 3})
        assert submission.status == "verified"
        runs = facade.tasks().runs(task.id)
        assert len(runs) == 1
        assert runs[0].status == "success"
        assert "event handled" in runs[0].output
    finally:
        await facade.stop()


async def test_scheduler_embedded_loop_processes_shared_store(configure) -> None:
    def responder(message: Message, context: Context) -> AIMessage:
        if "task-context" in _message_text(message, context):
            return AIMessage(content="loop picked it up")
        return AIMessage(content="chat")

    facade, _invoker = configure(
        responder,
        scheduler=SchedulerSettings(enabled=True, poll_seconds=0.02),
    )
    await facade.start()
    try:
        task = await facade.schedule_task(
            "loop-task", "at 2020-01-01T00:00:00+00:00", "run from loop"
        )
        await facade.tasks().approve(task.id)
        # No manual poll: the embedded loop must pick up the approved due task.
        await _wait_until(lambda: bool(facade.tasks().runs(task.id)))
        assert "loop picked it up" in facade.tasks().runs(task.id)[0].output
    finally:
        await facade.stop()


async def test_scheduler_disabled_rejects_scheduler_operations(configure) -> None:
    facade, _invoker = configure(lambda _message, _context: AIMessage(content="chat"))
    await facade.start()
    try:
        with pytest.raises(RuntimeError, match="disabled"):
            await facade.schedule_task("task", "every 1h", "prompt")
        with pytest.raises(RuntimeError, match="disabled"):
            facade.tasks()
    finally:
        await facade.stop()


async def test_unknown_model_group_fails_at_start(configure) -> None:
    facade, _invoker = configure(
        lambda _message, _context: AIMessage(content="chat"),
        model_group="missing",
    )

    with pytest.raises(ValueError, match="unknown model group"):
        await facade.start()


async def test_run_delivers_attachments(configure) -> None:
    facade, invoker = configure(
        lambda _message, _context: AIMessage(content="attached"),
    )

    await facade.start()
    assert facade._run_config is not None
    workspace = Path(facade._run_config.workspace_root)
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")
    try:
        result = await facade.run("check notes", attachments=["notes.txt"])
    finally:
        await facade.stop()

    assert result.text == "attached"
    assert invoker.requests
    model_text = _message_text(*invoker.requests[0][:2])
    assert "<attachments>" in model_text
    assert 'delivery="inline" name="notes.txt"' in model_text
    assert "hello</attachment>" in model_text


async def test_run_rejects_missing_attachment(configure) -> None:
    facade, invoker = configure(
        lambda _message, _context: AIMessage(content="never"),
    )

    await facade.start()
    try:
        with pytest.raises(ValueError, match="attachment file not found"):
            await facade.run("hello", attachments=["missing.txt"])
    finally:
        await facade.stop()

    assert invoker.requests == []
