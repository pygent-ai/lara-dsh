"""E3: realtime mid-turn interruption, supersede and merged fork reply.

This is the deliberate anti-deletion anchor. The test imports and calls
``LaraRuntimeService.send_interrupt`` through the real service, starts a real
managed main turn with a scripted slow model, and verifies that a second
interrupt cancels the in-flight fork and produces one merged exchange
envelope. Removing the interrupt entry point or the fork-reply chain makes
this test fail at import or at the first ``send_interrupt`` call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from pygent import AIMessage, Context, GenerationConfig, Message, ToolCall
from pygent.llm import ModelExecution, ModelProviderResponse

from _e2e_support import content_text, make_config
from lara.core.io import plain_object
from lara.orchestration import LocalExecutionHost
from lara.orchestration.runtime_pool import WorkspaceRuntimePool
from lara.runtime.service import LaraRuntimeService
from lara.sessions import SessionManager

# Deliberate anti-deletion anchor: collection fails with AttributeError if the
# realtime interrupt entry point disappears from the service.
SEND_INTERRUPT = LaraRuntimeService.send_interrupt


class GatedRealtimeInvoker:
    """Scripted model: the main turn waits until released; forks wait too."""

    def __init__(self) -> None:
        self.main_started = asyncio.Event()
        self.main_release = asyncio.Event()
        self.fork_requests: list[str] = []
        self.fork_gates: list[asyncio.Event] = []
        self.fork_cancelled: list[int] = []

    def execute(
        self,
        *,
        model_group: object,
        retry_policy: object,
        generation: GenerationConfig,
        message: Message,
        context: Context,
        tools: tuple[object, ...] = (),
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> ModelExecution:
        del model_group, retry_policy, tools, deadline, cancel_event
        if generation.tool_choice == "speak":
            index = len(self.fork_requests)
            self.fork_requests.append(content_text(message))
            gate = asyncio.Event()
            self.fork_gates.append(gate)

            async def fork_operation(_emit: object) -> ModelProviderResponse:
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    self.fork_cancelled.append(index)
                    raise
                return ModelProviderResponse(
                    message=AIMessage(
                        content="",
                        tool_calls=(
                            ToolCall(
                                call_id=f"speak-{index}",
                                name="speak",
                                arguments={"text": f"fork reply {index + 1}"},
                            ),
                        ),
                    ),
                    finish_reason="stop",
                )

            return ModelExecution(fork_operation)

        async def main_operation(_emit: object) -> ModelProviderResponse:
            self.main_started.set()
            await self.main_release.wait()
            return ModelProviderResponse(
                message=AIMessage(content="main turn finished"), finish_reason="stop"
            )

        return ModelExecution(main_operation)


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 10.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.e2e
async def test_second_interrupt_supersedes_fork_and_merges_envelope(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, realtime=True)
    invoker = GatedRealtimeInvoker()
    service = LaraRuntimeService(config)
    # LaraAgent supports model_invoker injection through the service cache.
    service._model_invokers["default"] = invoker
    pool = WorkspaceRuntimePool(
        runtime_factory=lambda _config, **_kwargs: service
    )
    manager = SessionManager(config)
    session = manager.create("chat", mode="lara-says")

    async with LocalExecutionHost(runtime_pool=pool) as host:
        turn = await host.turns.submit(
            config=config,
            manager=manager,
            session_id=session.session_id,
            message="开始一个需要较长时间的任务",
            turn_id="turn-1",
            interactive_approvals=False,
        )
        await asyncio.wait_for(invoker.main_started.wait(), timeout=10)
        assert turn.execution_handle is not None
        assert turn.run_ref is not None

        first = asyncio.create_task(
            SEND_INTERRUPT(
                service,
                turn.execution_handle.execution_id,
                manager=manager,
                run_ref=turn.run_ref,
                session_id=session.session_id,
                input_id="user-1",
                message="第一个插话",
            )
        )
        await _wait_until(lambda: len(invoker.fork_requests) == 1)

        second = asyncio.create_task(
            SEND_INTERRUPT(
                service,
                turn.execution_handle.execution_id,
                manager=manager,
                run_ref=turn.run_ref,
                session_id=session.session_id,
                input_id="user-2",
                message="第二个插话",
            )
        )
        await _wait_until(
            lambda: len(invoker.fork_requests) == 2 and len(invoker.fork_cancelled) == 1
        )

        assert invoker.fork_requests == [
            "第一个插话",
            "1. 第一个插话\n2. 第二个插话",
        ]
        assert invoker.fork_cancelled == [0]

        invoker.fork_gates[1].set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result is None
        assert second_result is not None
        assert second_result.via == "speak"
        assert second_result.speak_args is not None
        assert second_result.speak_args.text == "fork reply 2"

        queued = service.reminders.message_store.claim_messages(
            session.session_id,
            claim_id="e2e-interrupt",
            lease_seconds=30,
        )
        assert len(queued) == 1
        envelope = queued[0].content
        assert "<user-asked>\n1. 第一个插话\n2. 第二个插话\n</user-asked>" in envelope
        assert (
            "<your-response-in-another-session>\nfork reply 2\n"
            "</your-response-in-another-session>"
        ) in envelope

        invoker.main_release.set()
        output, _ = await asyncio.wait_for(turn.result(), timeout=10)
        result = plain_object(plain_object(output.data)["result"])
        assert result["status"] == "passed"
        assert result["final_answer"] == "main turn finished"
