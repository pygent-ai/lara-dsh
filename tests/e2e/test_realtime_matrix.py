"""Realtime interaction matrix (R1-R13).

Every scenario starts from the real ``LaraRuntimeService`` and drives a real
managed turn through ``LocalExecutionHost``. Only the model boundary is
scripted, which lets each test pin the exact timing of tool rounds, fork
replies, interrupts and turn endings. The comments above each test name the
functional behavior the test anchors, so deleting that behavior fails here
even if the implementation's own unit tests are deleted with it.

R2/R3 live in ``test_e3_realtime_supersede.py``: E3 already anchors the
mid-turn fork reply and the supersede/merge chain from the real service.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pygent import AIMessage, ToolCall
from pygent.runtime import ExecutionDeadlineExceeded

from _e2e_support import content_text
from _realtime_support import (
    RealtimeScenario,
    assert_no_extra_main_call,
    build_scenario,
    claim_envelopes,
    count_occurrences,
    finalizer_messages,
    read_call,
    release_envelopes,
    say_nothing_call,
    session_history,
    speak_call,
    speak_events,
    wait_until,
    write_workspace_file,
)
from lara.orchestration import LocalExecutionHost
from lara.runtime.realtime.injection import FIRST_RESULT_SPEAK_REMINDER
from lara.runtime.realtime.nudge import nudge_reminder
from lara.runtime.realtime.speak import SILENT_ACKNOWLEDGEMENT
from lara.runtime.realtime.turn_finalizer import TURN_FINALIZER_REMINDER

NUDGE_MARKER = "You have not spoken to the user"


async def _submit(
    host: LocalExecutionHost,
    scenario: RealtimeScenario,
    message: str,
    turn_id: str,
):
    return await host.turns.submit(
        config=scenario.config,
        manager=scenario.manager,
        session_id=scenario.session.session_id,
        message=message,
        turn_id=turn_id,
        interactive_approvals=False,
    )


async def _interrupt(
    scenario: RealtimeScenario,
    turn,
    *,
    input_id: str,
    message: str,
):
    assert turn.execution_handle is not None
    assert turn.run_ref is not None
    return await scenario.service.send_interrupt(
        turn.execution_handle.execution_id,
        manager=scenario.manager,
        run_ref=turn.run_ref,
        session_id=scenario.session.session_id,
        input_id=input_id,
        message=message,
    )


@pytest.mark.e2e
async def test_r1_idle_realtime_turn_replies_directly_through_speak(
    tmp_path: Path,
) -> None:
    """R1: an idle realtime session takes the ordinary foreground path.

    Anchor: a message arriving while no turn is running goes through
    ``start_turn`` (not ``send_interrupt``); the reply reaches the user as a
    ``lara.realtime.speak`` event and no fork execution is created.
    """

    answers = [
        AIMessage(
            content="",
            tool_calls=(speak_call("speak-1", "idle reply"),),
        ),
        AIMessage(content="plain final"),
    ]
    index = 0

    def responder(call):
        nonlocal index
        assert not call.is_fork
        answer = answers[min(index, len(answers) - 1)]
        index += 1
        return answer

    scenario = build_scenario(tmp_path, responder)
    async with LocalExecutionHost(runtime_pool=scenario.pool) as host:
        turn = await _submit(host, scenario, "空闲时的新消息", "turn-1")
        output, _ = await asyncio.wait_for(turn.result(), timeout=10)
        events = await speak_events(turn)
        await assert_no_extra_main_call(scenario.invoker, 2)

    result = output.data["result"]
    assert result["status"] == "passed"
    assert result["final_answer"] == "plain final"
    assert len(scenario.invoker.fork_calls) == 0
    assert len(scenario.invoker.main_calls) == 2

    assert [event["text"] for event in events] == ["idle reply"]
    # The idle path left no envelope behind and needed no second round.
    assert claim_envelopes(scenario, claim_id="r1") == []


@pytest.mark.e2e
async def test_r4_pure_text_ending_injects_finalizer_reminder(
    tmp_path: Path,
) -> None:
    """R4: a turn ending in pure text gets one bottom-line finalizer round.

    Anchor: ``decide_turn_finalization`` / the session coordinator queues a
    ``lara.realtime.finalizer`` turn carrying ``TURN_FINALIZER_REMINDER``; the
    agent then declines with ``speak(say_nothing=true)`` and the turn ends.
    """

    def responder(call):
        assert not call.is_fork
        if TURN_FINALIZER_REMINDER not in call.text:
            return AIMessage(content="draft answer")
        if SILENT_ACKNOWLEDGEMENT not in call.text:
            return AIMessage(
                content="",
                tool_calls=(say_nothing_call("say-nothing"),),
            )
        return AIMessage(content="silent finish")

    scenario = build_scenario(tmp_path, responder)
    async with LocalExecutionHost(runtime_pool=scenario.pool) as host:
        turn = await _submit(host, scenario, "结束前没有 speak", "turn-1")
        output, _ = await asyncio.wait_for(turn.result(), timeout=10)
        await wait_until(
            lambda: any(
                item.get("role") == "assistant"
                and item.get("content") == "silent finish"
                for item in session_history(scenario)
            ),
            description="finalizer turn finished",
        )

    result = output.data["result"]
    assert result["status"] == "passed"
    assert result["final_answer"] == "draft answer"
    assert len(scenario.invoker.main_calls) == 3

    finalizer_calls = [
        call
        for call in scenario.invoker.main_calls
        if call.message_kind == "lara.realtime.finalizer"
    ]
    assert len(finalizer_calls) == 1
    assert finalizer_calls[0].text.startswith("<system-reminder>")
    # The finalizer reminder names the draft instead of quoting it; the draft
    # itself is already the preceding assistant message.
    assert "draft answer" in finalizer_calls[0].text

    assert len(finalizer_messages(scenario)) == 1
    # say_nothing produced the silent acknowledgement and no user-visible text.
    assert any(
        item.get("role") == "tool"
        and any(
            result.get("name") == "speak"
            and result.get("output") == SILENT_ACKNOWLEDGEMENT
            for result in item.get("results") or ()
        )
        for item in session_history(scenario)
    )


@pytest.mark.e2e
async def test_r5_first_tool_result_injects_speak_reminder_once(
    tmp_path: Path,
) -> None:
    """R5: the first tool result of a silent turn carries the speak reminder.

    Anchor: ``RealtimeNudgeModule.first_result_seen`` fires at the first tool
    boundary when the turn has not spoken, and never repeats in that turn.
    """

    def responder(call):
        assert not call.is_fork
        index = len(scenario.invoker.main_calls) - 1
        if index < 2:
            return AIMessage(
                content="",
                tool_calls=(read_call(f"read-{index}", "note.txt"),),
            )
        if index == 2:
            return AIMessage(
                content="",
                tool_calls=(speak_call("speak-1", "read it"),),
            )
        return AIMessage(content="plain final")

    scenario = build_scenario(tmp_path, responder)
    write_workspace_file(scenario, "note.txt", "hello")
    async with LocalExecutionHost(runtime_pool=scenario.pool) as host:
        turn = await _submit(host, scenario, "先读文件", "turn-1")
        await asyncio.wait_for(turn.result(), timeout=10)
        await assert_no_extra_main_call(scenario.invoker, 4)

    calls = scenario.invoker.main_calls
    assert len(calls) == 4
    assert FIRST_RESULT_SPEAK_REMINDER not in calls[0].text
    assert FIRST_RESULT_SPEAK_REMINDER in calls[1].text
    # The reminder is injected once, not repeated at the second boundary.
    assert count_occurrences(calls[3].text, FIRST_RESULT_SPEAK_REMINDER) == 1
    assert TURN_FINALIZER_REMINDER not in calls[3].text


@pytest.mark.e2e
async def test_r6_five_silent_tool_rounds_inject_periodic_nudge(
    tmp_path: Path,
) -> None:
    """R6: five consecutive tool rounds without speak trigger the nudge.

    Anchor: the default ``nudge_interval=5`` rule injects
    ``nudge_reminder(rounds_since_speak=5)`` at the fifth tool boundary, not
    before it and not on every later boundary.
    """

    def responder(call):
        assert not call.is_fork
        index = len(scenario.invoker.main_calls) - 1
        if index < 5:
            return AIMessage(
                content="",
                tool_calls=(read_call(f"read-{index}", "note.txt"),),
            )
        if index == 5:
            return AIMessage(
                content="",
                tool_calls=(speak_call("speak-1", "five rounds later"),),
            )
        return AIMessage(content="plain final")

    scenario = build_scenario(tmp_path, responder)
    write_workspace_file(scenario, "note.txt", "hello")
    async with LocalExecutionHost(runtime_pool=scenario.pool) as host:
        turn = await _submit(host, scenario, "连续五轮工具调用", "turn-1")
        await asyncio.wait_for(turn.result(), timeout=10)
        await assert_no_extra_main_call(scenario.invoker, 7)

    calls = scenario.invoker.main_calls
    assert len(calls) == 7
    # Four completed rounds are below the threshold.
    assert NUDGE_MARKER not in calls[4].text
    # The fifth boundary carries the measured reminder.
    assert nudge_reminder(rounds_since_speak=5) in calls[5].text
    assert count_occurrences(calls[6].text, NUDGE_MARKER) == 1
    assert FIRST_RESULT_SPEAK_REMINDER in calls[6].text
    assert TURN_FINALIZER_REMINDER not in calls[6].text


@pytest.mark.e2e
async def test_r7_three_interrupts_merge_into_one_fork_envelope(
    tmp_path: Path,
) -> None:
    """R7: a third interrupt supersedes two forks and merges all three asks.

    Anchor: ``SessionForkState`` cancels each in-flight generation and the
    final envelope numbers the whole pending batch under ``<user-asked>``.
    """

    main_started = asyncio.Event()
    main_release = asyncio.Event()
    fork_requests: list[str] = []
    fork_gates: list[asyncio.Event] = []
    fork_cancelled: list[int] = []

    async def responder(call):
        if call.is_fork:
            index = len(fork_requests)
            fork_requests.append(content_text(call.message))
            gate = asyncio.Event()
            fork_gates.append(gate)
            try:
                await gate.wait()
            except asyncio.CancelledError:
                fork_cancelled.append(index)
                raise
            return AIMessage(
                content="",
                tool_calls=(speak_call(f"fork-{index}", f"fork reply {index + 1}"),),
            )
        main_started.set()
        await main_release.wait()
        return AIMessage(content="main done")

    scenario = build_scenario(tmp_path, responder)
    async with LocalExecutionHost(runtime_pool=scenario.pool) as host:
        turn = await _submit(host, scenario, "开始长任务", "turn-1")
        await asyncio.wait_for(main_started.wait(), timeout=10)

        first = asyncio.create_task(
            _interrupt(scenario, turn, input_id="user-1", message="第一条")
        )
        await wait_until(lambda: len(fork_requests) == 1)

        second = asyncio.create_task(
            _interrupt(scenario, turn, input_id="user-2", message="第二条")
        )
        await wait_until(
            lambda: len(fork_requests) == 2 and fork_cancelled == [0]
        )

        third = asyncio.create_task(
            _interrupt(scenario, turn, input_id="user-3", message="第三条")
        )
        await wait_until(
            lambda: len(fork_requests) == 3 and fork_cancelled == [0, 1]
        )

        assert fork_requests == [
            "第一条",
            "1. 第一条\n2. 第二条",
            "1. 第一条\n2. 第二条\n3. 第三条",
        ]
        assert fork_cancelled == [0, 1]

        fork_gates[2].set()
        first_result, second_result, third_result = await asyncio.gather(
            first, second, third
        )
        assert first_result is None
        assert second_result is None
        assert third_result is not None
        assert third_result.via == "speak"
        assert third_result.speak_args is not None
        assert third_result.speak_args.text == "fork reply 3"

        queued = claim_envelopes(scenario, claim_id="r7")
        assert len(queued) == 1
        envelope = queued[0].content
        assert "<user-asked>\n1. 第一条\n2. 第二条\n3. 第三条\n</user-asked>" in envelope
        assert (
            "<your-response-in-another-session>\nfork reply 3\n"
            "</your-response-in-another-session>"
        ) in envelope

        main_release.set()
        output, _ = await asyncio.wait_for(turn.result(), timeout=10)

    result = output.data["result"]
    assert result["status"] == "passed"
    assert result["final_answer"] == "main done"


@pytest.mark.e2e
async def test_r8_main_agent_claims_envelope_at_next_tool_boundary(
    tmp_path: Path,
) -> None:
    """R8: the main agent consumes a fork envelope at its next tool boundary.

    Anchor: ``RuntimeReminderModule`` claims the queued ``AgentMessage`` after
    a tool result and appends the rendered envelope to that result, so the
    model's next request contains the exchange.
    """

    main_started = asyncio.Event()
    main_release = asyncio.Event()

    async def responder(call):
        if call.is_fork:
            return AIMessage(
                content="",
                tool_calls=(speak_call("fork-1", "fork reply"),),
            )
        index = len(scenario.invoker.main_calls) - 1
        if index == 0:
            main_started.set()
            await main_release.wait()
            return AIMessage(
                content="",
                tool_calls=(read_call("read-1", "note.txt"),),
            )
        if index == 1:
            # This request follows the tool result that claimed the envelope.
            assert "user-asked" in call.text
            assert "第一个插话" in call.text
            assert "fork reply" in call.text
            return AIMessage(
                content="",
                tool_calls=(speak_call("speak-1", "main knows"),),
            )
        return AIMessage(content="plain final")

    scenario = build_scenario(tmp_path, responder)
    write_workspace_file(scenario, "note.txt", "hello")
    async with LocalExecutionHost(runtime_pool=scenario.pool) as host:
        turn = await _submit(host, scenario, "开始长任务", "turn-1")
        await asyncio.wait_for(main_started.wait(), timeout=10)

        outcome = await asyncio.wait_for(
            _interrupt(
                scenario,
                turn,
                input_id="user-1",
                message="第一个插话",
            ),
            timeout=10,
        )
        assert outcome is not None
        assert outcome.speak_args is not None
        assert outcome.speak_args.text == "fork reply"

        main_release.set()
        output, _ = await asyncio.wait_for(turn.result(), timeout=10)

    result = output.data["result"]
    assert result["status"] == "passed"
    assert len(scenario.invoker.main_calls) == 3
    # The envelope was delivered, so a fresh claim finds nothing.
    assert claim_envelopes(scenario, claim_id="r8") == []
    events = await speak_events(turn)
    assert [event["text"] for event in events] == ["main knows"]


@pytest.mark.e2e
async def test_r9_say_nothing_acknowledges_silently_without_envelope(
    tmp_path: Path,
) -> None:
    """R9: ``speak(say_nothing=true)`` records silence and posts no envelope.

    Anchor: ``SILENT_ACKNOWLEDGEMENT`` is the tool result, the call emits no
    ``lara.realtime.speak`` event, and no exchange envelope is queued.
    """

    def responder(call):
        assert not call.is_fork
        index = len(scenario.invoker.main_calls) - 1
        if index == 0:
            return AIMessage(
                content="",
                tool_calls=(say_nothing_call("say-nothing"),),
            )
        assert SILENT_ACKNOWLEDGEMENT in call.text
        return AIMessage(content="plain final")

    scenario = build_scenario(tmp_path, responder)
    async with LocalExecutionHost(runtime_pool=scenario.pool) as host:
        turn = await _submit(host, scenario, "决定不说话", "turn-1")
        output, _ = await asyncio.wait_for(turn.result(), timeout=10)
        events = await speak_events(turn)
        await assert_no_extra_main_call(scenario.invoker, 2)

    result = output.data["result"]
    assert result["status"] == "passed"
    assert len(scenario.invoker.main_calls) == 2
    assert claim_envelopes(scenario, claim_id="r9") == []
    assert events == []
    assert TURN_FINALIZER_REMINDER not in scenario.invoker.main_calls[1].text


@pytest.mark.e2e
async def test_r10_speaking_turn_end_skips_finalizer_reminder(
    tmp_path: Path,
) -> None:
    """R10: a turn that already spoke gets no bottom-line finalizer.

    Anchor: the finalizer decision sees the successful speak tool result in
    this turn's history and ends without queuing ``TURN_FINALIZER_REMINDER``.
    Regression guard for a turn that spoke and then ended in plain text.
    """

    def responder(call):
        assert not call.is_fork
        index = len(scenario.invoker.main_calls) - 1
        if index == 0:
            return AIMessage(
                content="",
                tool_calls=(speak_call("speak-1", "final via speak"),),
            )
        return AIMessage(content="plain final")

    scenario = build_scenario(tmp_path, responder)
    async with LocalExecutionHost(runtime_pool=scenario.pool) as host:
        turn = await _submit(host, scenario, "结束时已经有 speak", "turn-1")
        output, _ = await asyncio.wait_for(turn.result(), timeout=10)
        events = await speak_events(turn)
        await assert_no_extra_main_call(scenario.invoker, 2)

    result = output.data["result"]
    assert result["status"] == "passed"
    assert len(scenario.invoker.main_calls) == 2
    assert all(
        TURN_FINALIZER_REMINDER not in call.text
        for call in scenario.invoker.main_calls
    )
    assert finalizer_messages(scenario) == []
    assert [event["text"] for event in events] == ["final via speak"]


@pytest.mark.e2e
async def test_r10b_late_tool_round_without_speak_triggers_finalizer(
    tmp_path: Path,
) -> None:
    """R10b: only the tool round right before the final reply counts.

    Anchor: the agent spoke in an early round, but the LAST tool round before
    the final text had no speak — the trailing answer would otherwise be
    invisible, so the bottom-line finalizer must fire. This pins the
    user-approved preceding-round semantics (not a whole-turn scan).
    """

    def responder(call):
        assert not call.is_fork
        index = len(scenario.invoker.main_calls) - 1
        if index == 0:
            return AIMessage(
                content="",
                tool_calls=(speak_call("speak-1", "progress update"),),
            )
        if index == 1:
            return AIMessage(
                content="",
                tool_calls=(read_call("read-1", "note.txt"),),
            )
        return AIMessage(content="plain final")

    scenario = build_scenario(tmp_path, responder)
    async with LocalExecutionHost(runtime_pool=scenario.pool) as host:
        turn = await _submit(host, scenario, "干活并在结束时汇报", "turn-1")
        output, _ = await asyncio.wait_for(turn.result(), timeout=10)
        await wait_until(
            lambda: len(scenario.invoker.main_calls) == 4,
            description="finalizer turn ran",
        )

    result = output.data["result"]
    assert result["status"] == "passed"
    assert TURN_FINALIZER_REMINDER in scenario.invoker.main_calls[-1].text
    finalizer_calls = [
        call
        for call in scenario.invoker.main_calls
        if call.message_kind == "lara.realtime.finalizer"
    ]
    assert len(finalizer_calls) == 1
    assert "note.txt" in finalizer_calls[0].text or "plain final" in str(
        finalizer_calls[0].text
    )


@pytest.mark.e2e
async def test_r11_non_realtime_session_has_no_speak_or_reminders(
    tmp_path: Path,
) -> None:
    """R11: the realtime gate keeps ordinary sessions untouched.

    Anchor: a normal chat session exposes no ``speak`` tool, injects none of
    the realtime reminders, and never queues a finalizer round.
    """

    def responder(call):
        assert not call.is_fork
        index = len(scenario.invoker.main_calls) - 1
        if index == 0:
            return AIMessage(
                content="",
                tool_calls=(read_call("read-1", "note.txt"),),
            )
        return AIMessage(content="plain final")

    scenario = build_scenario(tmp_path, responder, realtime=False, mode="chat")
    write_workspace_file(scenario, "note.txt", "hello")
    async with LocalExecutionHost(runtime_pool=scenario.pool) as host:
        turn = await _submit(host, scenario, "普通会话", "turn-1")
        output, _ = await asyncio.wait_for(turn.result(), timeout=10)
        events = await speak_events(turn)
        await assert_no_extra_main_call(scenario.invoker, 2)

    result = output.data["result"]
    assert result["status"] == "passed"
    assert len(scenario.invoker.main_calls) == 2
    assert all(
        "speak" not in call.tool_names
        for call in scenario.invoker.main_calls
    )
    assert all(
        FIRST_RESULT_SPEAK_REMINDER not in call.text
        and TURN_FINALIZER_REMINDER not in call.text
        and NUDGE_MARKER not in call.text
        for call in scenario.invoker.main_calls
    )
    assert finalizer_messages(scenario) == []
    assert events == []


@pytest.mark.e2e
async def test_r12_fork_deadline_cancels_fork_without_envelope(
    tmp_path: Path,
) -> None:
    """R12: a fork that exceeds its deadline is cancelled and posts nothing.

    Anchor: ``fork_deadline_seconds`` admits the fork execution with a
    deadline, ``send_interrupt`` surfaces ``ExecutionDeadlineExceeded``,
    cancels the model operation, and queues no exchange envelope.
    """

    main_started = asyncio.Event()
    main_release = asyncio.Event()
    fork_started: list[int] = []
    fork_cancelled: list[int] = []
    fork_gates: list[asyncio.Event] = []

    async def responder(call):
        if call.is_fork:
            index = len(fork_started)
            fork_started.append(index)
            if index == 0:
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    fork_cancelled.append(index)
                    raise
            gate = asyncio.Event()
            fork_gates.append(gate)
            await gate.wait()
            return AIMessage(
                content="",
                tool_calls=(speak_call(f"fork-{index}", "recovered reply"),),
            )
        index = len(scenario.invoker.main_calls) - 1
        if index == 0:
            return AIMessage(
                content="",
                tool_calls=(speak_call("speak-1", "working"),),
            )
        main_started.set()
        await main_release.wait()
        return AIMessage(content="main done")

    scenario = build_scenario(
        tmp_path,
        responder,
        fork_deadline_seconds=0.25,
    )
    async with LocalExecutionHost(runtime_pool=scenario.pool) as host:
        turn = await _submit(host, scenario, "开始长任务", "turn-1")
        await asyncio.wait_for(main_started.wait(), timeout=10)

        with pytest.raises(ExecutionDeadlineExceeded):
            await asyncio.wait_for(
                _interrupt(
                    scenario,
                    turn,
                    input_id="user-1",
                    message="超时插话",
                ),
                timeout=10,
            )
        assert fork_cancelled == [0]
        assert claim_envelopes(scenario, claim_id="r12-timeout") == []

        # The timed-out generation must not leave the session fork state stuck.
        second = asyncio.create_task(
            _interrupt(
                scenario,
                turn,
                input_id="user-2",
                message="重试插话",
            )
        )
        await wait_until(lambda: len(fork_started) == 2 and len(fork_gates) == 1)
        fork_gates[0].set()
        outcome = await asyncio.wait_for(second, timeout=10)
        assert outcome is not None
        assert outcome.speak_args is not None
        assert outcome.speak_args.text == "recovered reply"

        queued = claim_envelopes(scenario, claim_id="r12-retry")
        assert len(queued) == 1
        assert "重试插话" in queued[0].content
        assert "recovered reply" in queued[0].content

        main_release.set()
        output, _ = await asyncio.wait_for(turn.result(), timeout=10)

    result = output.data["result"]
    assert result["status"] == "passed"
    assert result["final_answer"] == "main done"


@pytest.mark.e2e
async def test_r13_pending_envelope_survives_into_next_turn_context(
    tmp_path: Path,
) -> None:
    """R13: an interrupt that loses the race with turn completion is not lost.

    Anchor: the envelope stays queued when the main turn ends without a
    further tool boundary, and the next user turn claims it while assembling
    its opening context (before any new tool result).
    """

    main_started = asyncio.Event()
    main_release = asyncio.Event()
    turn_two_saw_envelope = asyncio.Event()

    async def responder(call):
        if call.is_fork:
            return AIMessage(
                content="",
                tool_calls=(speak_call("fork-1", "fork reply"),),
            )
        index = len(scenario.invoker.main_calls) - 1
        if index == 0:
            # A tool boundary before the interrupt, so this turn already
            # spoke and its plain-text ending needs no finalizer.
            return AIMessage(
                content="",
                tool_calls=(speak_call("speak-1", "working"),),
            )
        if index == 1:
            main_started.set()
            await main_release.wait()
            return AIMessage(content="main done")
        if index == 2:
            # First model call of the next user turn: the pending envelope
            # must already be in the opening context, before any tool result.
            assert "第一个插话" in call.text
            assert "fork reply" in call.text
            turn_two_saw_envelope.set()
            return AIMessage(
                content="",
                tool_calls=(speak_call("speak-2", "got it"),),
            )
        return AIMessage(content="plain final")

    scenario = build_scenario(tmp_path, responder)
    async with LocalExecutionHost(runtime_pool=scenario.pool) as host:
        turn = await _submit(host, scenario, "开始长任务", "turn-1")
        await asyncio.wait_for(main_started.wait(), timeout=10)

        outcome = await asyncio.wait_for(
            _interrupt(
                scenario,
                turn,
                input_id="user-1",
                message="第一个插话",
            ),
            timeout=10,
        )
        assert outcome is not None

        # The fork finished while the main turn was still gated: the envelope
        # is pending and no tool boundary will follow in this turn.
        pending = claim_envelopes(scenario, claim_id="r13-pending")
        assert len(pending) == 1
        assert "第一个插话" in pending[0].content
        assert "fork reply" in pending[0].content
        release_envelopes(scenario, pending, claim_id="r13-pending")

        main_release.set()
        first_output, _ = await asyncio.wait_for(turn.result(), timeout=10)
        await assert_no_extra_main_call(scenario.invoker, 2)

        assert first_output.data["result"]["final_answer"] == "main done"
        assert len(scenario.invoker.main_calls) == 2
        assert finalizer_messages(scenario) == []

        second = await _submit(host, scenario, "下一轮问题", "turn-2")
        second_output, _ = await asyncio.wait_for(second.result(), timeout=10)

    assert turn_two_saw_envelope.is_set()
    assert second_output.data["result"]["status"] == "passed"
    assert len(scenario.invoker.main_calls) == 4
    # Delivery happened at turn start, so no copy remains for a tool boundary.
    assert claim_envelopes(scenario, claim_id="r13-after") == []
    assert count_occurrences(
        scenario.invoker.main_calls[2].text, "fork reply"
    ) == 1
