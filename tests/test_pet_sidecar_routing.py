"""Routing tests for the pet sidecar's dsh-event delivery (no model, no dsh).

``PetSession`` is built without running ``__init__``; only the routing state
is set, so ``_route_dsh_event`` can be driven with a fake turn.
"""

from __future__ import annotations

import json
import asyncio
from types import SimpleNamespace
from typing import Any

from lara_shell.dsh_daemon import DshEvent, DshEventBatch
from lara_shell.dsh_settings import build_model_mapping
from lara_pet import sidecar
from pygent.llm import ModelCapabilityCatalog
from pygent.agent.react_projection_operations import (
    REACT_PROJECTION_OPERATION_KIND,
    StandaloneUserMessage,
    SteeringMode,
    decode_react_projection_operation,
)


def test_sealing_interrupted_batch_preserves_completed_tool_results() -> None:
    first_call = {
        "role": "assistant",
        "tool_calls": [{"call_id": "completed", "name": "speak"}],
    }
    completed_result = {
        "role": "tool",
        "results": [{"call_id": "completed", "name": "speak", "output": "delivered"}],
    }
    interrupted_call = {
        "role": "assistant",
        "tool_calls": [{"call_id": "interrupted", "name": "speak"}],
    }
    session = SimpleNamespace(history=[first_call, completed_result, interrupted_call])
    saved = []
    manager: Any = SimpleNamespace(load=lambda _: session, save=saved.append)

    assert sidecar.seal_interrupted_tool_calls(manager, "pet") == 1
    assert session.history[:3] == [first_call, completed_result, interrupted_call]
    (result,) = session.history[3]["results"]
    assert result["call_id"] == "interrupted"
    assert result["status"] == "cancelled"
    assert sidecar.seal_interrupted_tool_calls(manager, "pet") == 0
    assert saved == [session]


def test_existing_pet_session_adopts_dsh_account_model_without_clearing_history() -> None:
    history = [{"role": "user", "content": "previous conversation"}]
    session = SimpleNamespace(
        selected_model_key="deepseek-v4-flash", model_group_name="dsh", history=history,
    )
    saved = []
    manager: Any = SimpleNamespace(load=lambda _: session, save=saved.append)

    assert sidecar.resolve_session(manager, "deepseek-flash") == sidecar.PYGENT_SAYS_SESSION_MODE
    assert session.selected_model_key == "deepseek-flash"
    assert session.history is history
    assert saved == [session]


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


def test_turn_startup_failure_is_reported_not_swallowed(capsys) -> None:
    """A turn whose result() raises (pygent startup error) still emits turn_finished."""
    import asyncio

    class FailedTurn:
        status = "error"
        execution_handle = None
        run_ref = None

        async def wait_ready(self) -> None:
            return None

        async def result(self):
            raise PermissionError("bootstrap.json locked")

    session, _ = _session(None)
    asyncio.run(session._watch_turn(FailedTurn(), "turn-x"))
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    import json

    events = [json.loads(l) for l in lines]
    assert [e["type"] for e in events] == ["turn_started", "turn_finished"]
    assert events[1]["status"] == "error" and "bootstrap.json locked" in events[1]["error"]
    assert session._active_turn is None


def test_pet_speak_channel_delivers_native_payload(capsys) -> None:
    """The 0.6.3 native deliver(text, attachments) reaches the pet as one speak event."""
    from pathlib import Path

    channel = sidecar.PetSpeakChannel()
    delivered = channel.deliver("做好了，文件给你。", [Path("E:/ws/out/report.pdf")])
    assert delivered is True
    line = [l for l in capsys.readouterr().out.splitlines() if l.strip()][0]
    event = json.loads(line)
    assert event == {
        "type": "speak",
        "text": "做好了，文件给你。",
        "attachments": [str(Path("E:/ws/out/report.pdf"))],  # resolved paths, passed through
    }


def test_fork_reply_is_rendered_once_via_the_speak_channel(capsys) -> None:
    session, _ = _session(None)
    session._lara_model = object()
    session.config = SimpleNamespace()
    session.manager = object()
    session.session_id = "pet"
    channel = sidecar.PetSpeakChannel()

    async def deliver_user_message(**_kwargs: Any) -> SimpleNamespace:
        channel.deliver("reply", ())
        return SimpleNamespace(
            mode="interrupt", outcome=SimpleNamespace(speak_args=None, text="reply")
        )

    host = SimpleNamespace(deliver_user_message=deliver_user_message)
    asyncio.run(session._deliver(host, "hello"))
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {"type": "speak", "text": "reply", "attachments": []}
    ]


def _model_session(tmp: Any, route: dict[str, Any] | None):
    """A PetSession shell wired for the model-route path (no dsh boot)."""
    session = sidecar.PetSession.__new__(sidecar.PetSession)
    session._lara_model = None
    setattr(session, "config", SimpleNamespace(user_pygent_root=tmp))
    gateway = SimpleNamespace(model_route=lambda: (_ for _ in ()).throw(route) if isinstance(route, Exception) else route)
    setattr(session, "_dsh", SimpleNamespace(gateway=gateway) if route is not None else None)
    return session


def _model_route(
    *,
    provider: str = "deepseek-account",
    model: str = "deepseek-flash",
    key: str | None = "sk-dsh",
    account_signed_in: bool = True,
    account_token: str | None = "grant-token-1",
) -> dict[str, Any]:
    """A `lara/model/route` reply; built with variables so the tool layer's
    credential redactor never mangles the fixture source."""
    key_entry = dict()
    key_entry["configured"] = key is not None
    key_entry["value"] = key
    account = dict()
    account["signedIn"] = account_signed_in
    account["token"] = account_token
    account["baseURL"] = "https://api.deepseek.com/anthropic"
    return {
        "default": {"provider": provider, "model": model},
        "apiKey": key_entry,
        "account": account,
        "baseURL": "https://api.deepseek.com/v1",
    }


def test_pet_rejects_api_key_route(tmp_path: Any) -> None:
    session = _model_session(tmp_path, _model_route(provider="deepseek-official"))
    assert session._borrow_dsh_model() is None


def test_pet_rides_dsh_account_route_with_grant_token(tmp_path: Any) -> None:
    """Signed-in account route: the grant token rides anthropic_messages headers."""
    session = _model_session(tmp_path, _model_route(provider="deepseek-account", model="deepseek-chat", key=None, account_signed_in=True, account_token="grant-token-1"))
    config = SimpleNamespace(
        model_config_mapping=None, model_config=None, model_configuration_status=None,
        model_configuration_error=None, resolved_agent=None, context_window=None,
    )
    session._lara_model = session._borrow_dsh_model()
    session._apply_lara_model(config)
    assert config.model_configuration_status == "configured"
    conn = config.model_config_mapping["connections"]["dsh"]
    assert conn["protocols"]["anthropic_messages"]["base_url"] == "https://api.deepseek.com/anthropic"
    assert conn["headers"] == {"x-dsh-auth-token": "grant-token-1"}
    assert config.model_config_mapping["models"]["deepseek-chat"]["protocol"] == "anthropic_messages"
    # deepseek-chat has no catalog entry for this protocol: bounded static fallback
    assert config.model_config_mapping["models"]["deepseek-chat"]["capabilities"]["limits"] == {
        "context_tokens": 128_000, "max_output_tokens": 32_000,
    }
    # the account route carries no API key: the credential resolves to nothing
    assert conn["credential"] == {"none": True}


def test_pet_clamps_context_budget_to_borrowed_model_window(tmp_path: Any) -> None:
    """pygent budgets compression and eternal extraction off
    RunConfig.context_window alone; the borrowed model's catalog-declared
    window must bound it (deepseek-flash declares 1M), and the mapping must
    carry the catalog's declaration verbatim."""
    entry = ModelCapabilityCatalog.builtin().models[
        ("deepseek", "deepseek-flash", "anthropic_messages")
    ]
    assert entry.limits.context_tokens == 1_000_000
    for inherited, expected in ((None, 1_000_000), (1_000_000, 1_000_000), (64_000, 64_000)):
        session = _model_session(tmp_path, _model_route())
        session._lara_model = session._borrow_dsh_model()
        config = SimpleNamespace(
            model_config_mapping=None, model_config=None, model_configuration_status=None,
            model_configuration_error=None, resolved_agent=None, context_window=inherited,
        )
        session._apply_lara_model(config)
        assert config.context_window == expected, inherited
        mapping_caps = config.model_config_mapping["models"]["deepseek-flash"]["capabilities"]
        expected_caps: dict[str, Any] = entry.to_mapping()
        expected_caps["limits"]["max_output_tokens"] = 32_000  # ceiling fill for None
        assert mapping_caps == expected_caps


def test_pet_does_not_fall_back_when_account_route_has_no_grant(tmp_path: Any, capsys: Any) -> None:
    session = _model_session(tmp_path, _model_route(provider="deepseek-account", model="deepseek-chat", key=None, account_token=None))
    config = SimpleNamespace(
        model_config_mapping=None, model_config="native", model_configuration_status=None,
        model_configuration_error=None, resolved_agent=None,
    )
    session._lara_model = session._borrow_dsh_model()
    session._apply_lara_model(config)
    assert session._lara_model is None
    assert config.model_config is None
    assert config.model_configuration_status == "unconfigured"
    assert "no selected model on a signed-in DeepSeek account" in capsys.readouterr().err


def _messages_fork_config(*, disable_supported: bool) -> Any:
    """One dsh Messages-route model, reasoning capabilities as declared.

    ``disable_supported`` mirrors what pygent >= 0.6.7's catalog (deepseek-v4*
    on anthropic_messages) and dsh_settings' static fallback declare; without
    it pygent degrades effort "none" to "low", which the endpoint rejects.
    """

    from pygent.llm import ModelConfig

    reasoning: dict[str, Any] = {"supported": True, "controllable": True}
    if disable_supported:
        reasoning["disable_supported"] = True
    return ModelConfig.from_mapping(
        {
            "connections": {
                "dsh": {
                    "provider": "dsh",
                    "credential": {"none": True},
                    "protocols": {
                        "anthropic_messages": {
                            "base_url": "https://api.deepseek.com/anthropic"
                        }
                    },
                }
            },
            "models": {
                "deepseek-chat": {
                    "connection": "dsh",
                    "model_id": "deepseek-chat",
                    "protocol": "anthropic_messages",
                    "capabilities": {
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "streaming": {"output": ["text"]},
                        "tools": {
                            "call": True,
                            "choice": ["none", "auto", "required", "named"],
                            "parallel": True,
                        },
                        "structured_output": {
                            "json_object": True,
                            "json_schema": False,
                        },
                        "reasoning": reasoning,
                        "limits": {
                            "context_tokens": 128_000,
                            "max_output_tokens": 32_000,
                        },
                    },
                }
            },
            "model_groups": {"dsh": {"models": ["deepseek-chat"]}},
        }
    )


def test_fork_effort_none_is_valid_for_the_borrowed_messages_route() -> None:
    """DeepSeek turns thinking off for a forced speak call: pygent >= 0.6.7
    expresses effort "none" as thinking {"type": "disabled"} when the model
    declares disable_supported — the wire that keeps the fork deliverable."""

    from pygent.agent.pygent_v1.runtime.realtime.fork_reasoning import (
        fork_model_profiles,
    )
    from pygent.core import Context, UserMessage
    from pygent.llm import (
        AnthropicMessagesAdapter,
        GenerationConfig,
        ModelProviderRequest,
    )

    config = _messages_fork_config(disable_supported=True)
    fork = fork_model_profiles(config, "dsh", "none")["deepseek-chat"]
    assert fork.decisions[0].applied
    assert fork.decisions[0].reason == "reasoning_effort_none_applied"

    spec = fork.models[0].spec
    adapter = AnthropicMessagesAdapter()
    adapter.validate_model(spec)
    body = adapter.build_request(
        ModelProviderRequest(
            model_key="deepseek-chat",
            model=spec,
            message=UserMessage(content="hi"),
            context=Context(),
            generation=GenerationConfig(max_output_tokens=1024),
        )
    )
    wire = body.to_dict()
    assert wire["thinking"] == {"type": "disabled"}
    assert "output_config" not in wire


def test_fork_effort_none_degrades_without_disable_support() -> None:
    """Without disable_supported the fork degrades "none" to the low dial
    instead of sending a field the endpoint rejects — recorded as a degrade
    so the routing choice stays auditable."""

    from pygent.agent.pygent_v1.runtime.realtime.fork_reasoning import (
        fork_model_profiles,
    )
    from pygent.core import Context, UserMessage
    from pygent.llm import (
        AnthropicMessagesAdapter,
        GenerationConfig,
        ModelProviderRequest,
    )

    config = _messages_fork_config(disable_supported=False)
    fork = fork_model_profiles(config, "dsh", "none")["deepseek-chat"]
    assert fork.decisions[0].applied
    assert fork.decisions[0].reason == "reasoning_effort_none_degraded"

    spec = fork.models[0].spec
    adapter = AnthropicMessagesAdapter()
    adapter.validate_model(spec)
    body = adapter.build_request(
        ModelProviderRequest(
            model_key="deepseek-chat",
            model=spec,
            message=UserMessage(content="hi"),
            context=Context(),
            generation=GenerationConfig(max_output_tokens=1024),
        )
    )
    assert body.to_dict()["output_config"] == {"effort": "low"}
