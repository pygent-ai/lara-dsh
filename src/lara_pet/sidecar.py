"""Lara Pet sidecar: owns THE single realtime lara session for the pet.

Installed as the ``lara-pet-sidecar`` console script; the Tauri shell spawns
it (``lara-pet-sidecar --workspace <dir>``) and talks JSON lines over stdio.

Protocol (JSON lines over stdio):
  in:  {"type": "send", "text": "...", "attachments": [...]}
  out: {"type": "ready", "session_id": ..., "workspace": ...}
       {"type": "turn_started", "turn_id": ...}
       {"type": "speak", "text": ..., "attachments": [...]}
       {"type": "turn_finished", "turn_id": ..., "status": ..., "error": ...}
       {"type": "error", "msg": ..., "fatal": bool}   (fatal: the session is gone —
                 bootstrap failure; delivery/request glitches are transient)
       {"type": "log", "msg": ...}

The session is the kernel's singleton ``lara-says`` realtime session (the
session id IS ``"lara-says"`` by design): the agent works silently and
``speak`` is the only user-visible channel; the pet renders those events as
bubbles. The session is resumed across sidecar restarts.

Final ``speak`` messages come from a ``SpeakChannel`` registered on the
runtime (``lara_sdk.execution_host``), which hears every speak the runtime
delivers — including the finalizer round pygent appends when a turn ends in
plain text, and dsh-event turns. The per-turn event stream is used only for
``speak_delta`` previews; its terminal speak event is not re-emitted.

dsh reports reach Lara as wait-mode steering user messages when a turn is
running (one more model step inside that turn) and as turns of their own
otherwise; see ``PetSession._route_dsh_event``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import sysconfig
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Any

from lara_sdk.execution_host import LaraExecutionHost
from lara_sdk.lara_profile import lara_prompt_modules
from lara_shell import default_pet_workspace, user_lara_root
from lara_shell.config.loader import load_run_config
from lara_shell.credentials import build_credential_environment
from lara_shell.dsh_daemon import DshDaemon, DshEvent, DshEventBatch
from lara_shell.dsh_gateway import DshGatewayConfig, PluginEndpoint
from lara_shell.dsh_settings import build_model_mapping, dsh_target
from pygent.agent.pygent_v1.schema import ResolvedAgentConfig
from pygent.llm import ModelConfig
from pygent.agent.pygent_v1.core.io import plain_object
from pygent.agent.pygent_v1.graph.prompt_models import PromptRenderContext
from pygent.agent.pygent_v1.orchestration import ManagedSessionTurn
from pygent.agent.pygent_v1.orchestration.models import TurnCommand
from pygent.agent.pygent_v1.runtime.realtime.speak import render_reply_instruction
from pygent.agent.react_projection_operations import (
    REACT_PROJECTION_OPERATION_KIND,
    StandaloneUserMessage,
    SteeringMode,
    encode_react_projection_operation,
)
from pygent.core import UserMessage
from pygent.agent.pygent_v1.schema import RuntimeApprovalConfig
from pygent.agent.pygent_v1.schema.models import RealtimeInteractionConfig
from pygent.agent.pygent_v1.sessions import PYGENT_SAYS_SESSION_MODE, SessionManager
from lara_pet.speak_stream import SpeakStreamBridge

CASE_ID = "pet"

# Env name under which dsh's DeepSeek key is handed to pygent, transiently.

# Harness-originated messages carry their own framing; the runtime leaves this
# kind out of the <user-context> wrapper and logs it with origin "automation".
# Used both for steered mid-turn reports and for report turns of their own.
DSH_EVENT_MESSAGE_KIND = "pygent.v1.automation.trigger"

# The pet's persona comes from the stock prompt profile; the speak channel is
# the only user-visible surface, so the sidecar appends its channel rules to
# the rendered profile (the runtime's per-turn reply-instruction alone does
# not keep the model off plain text).
SPEAK_CHANNEL_RULES = """Output channel
- The user sees only messages sent with the speak tool. Text in your reply —
  including a reply that reads like a message to the user — is never shown to
  them. If you have not called speak(text=...) in this turn, you have said
  nothing, no matter what you wrote.
- Do not answer the user in plain text. When a task arrives, speak a short
  acknowledgment first, then work; speak progress and questions while you work;
  speak the result when you finish.
- Keep each speak to one or two sentences, in the user's language (Chinese by
  default).
"""


_stdout_lock = Lock()


def out(payload: dict) -> None:
    with _stdout_lock:
        print(json.dumps(payload, ensure_ascii=False), flush=True)


def log(msg: str) -> None:
    print(f"pet-sidecar: {msg}", file=sys.stderr, flush=True)


def resolve_session(manager: SessionManager, account_model: str | None = None) -> str:
    """The kernel keeps exactly one lara-says session; resume or create it."""
    try:
        session = manager.load(PYGENT_SAYS_SESSION_MODE)
    except FileNotFoundError:
        manager.create(CASE_ID, mode=PYGENT_SAYS_SESSION_MODE)
    else:
        if account_model is not None and (
            session.model_group_name != "dsh" or session.selected_model_key != account_model
        ):
            session.model_group_name = "dsh"
            session.selected_model_key = account_model
            manager.save(session)
    return PYGENT_SAYS_SESSION_MODE


def seal_interrupted_tool_calls(manager: SessionManager, session_id: str) -> int:
    """Pair cancelled tool results with calls a hard kill left unanswered.

    pygent records the tool batch an execution-level cancel killed
    (``_checkpoint_cancelled_batch``), but a killed process skips that path:
    the transcript keeps assistant ``tool_calls`` with no paired tool
    message, and every later turn replays that dangling tail — a shape
    strict providers reject with HTTP 400 ("An assistant message with
    'tool_calls' must be followed by tool messages"), wedging the session
    until it is repaired. Runs once at sidecar boot, before any turn can
    replay the tail; idempotent (nothing dangling, no-op).
    """
    session = manager.load(session_id)
    answered = {
        result.get("call_id")
        for message in session.history
        if message.get("role") == "tool"
        for result in message.get("results") or []
    }
    awaiting: dict[str, str] = {}
    rebuilt: list[dict] = []
    sealed = 0

    def _seal_message() -> dict:
        return {
            "content": "",
            "data": {},
            "kind": None,
            "metadata": {},
            "role": "tool",
            "slot": None,
            "results": [
                {
                    "call_id": call_id,
                    "name": name,
                    "status": "cancelled",
                    "error_kind": "cancelled",
                    "error_code": "interrupted_by_execution_cancel",
                    "error": None,
                    "retryable": False,
                    # The kill makes "did the tool run?" unknowable; the model
                    # may re-run the call (bash reruns fine, speak is benign).
                    "side_effect_committed": None,
                    "tool_id": None,
                    "tool_version": None,
                    "output": "",
                    "content": [],
                    "missing_capabilities": [],
                }
                for call_id, name in awaiting.items()
            ],
        }

    for message in session.history:
        role = message.get("role")
        if role == "tool":
            for result in message.get("results") or []:
                awaiting.pop(result.get("call_id"), None)
            rebuilt.append(message)
        else:
            if awaiting:
                dangling = {k: v for k, v in awaiting.items() if k not in answered}
                if dangling:
                    rebuilt.append(_seal_message())
                    sealed += 1
                awaiting = {}
            rebuilt.append(message)
            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    if call.get("call_id") not in answered:
                        awaiting[call["call_id"]] = call.get("name") or "?"
    if awaiting:
        dangling = {k: v for k, v in awaiting.items() if k not in answered}
        if dangling:
            awaiting = dangling
            rebuilt.append(_seal_message())
            sealed += 1
    if not sealed:
        return 0
    session.history = rebuilt
    manager.save(session)
    log(f"sealed {sealed} interrupted tool batch(es) in session {session_id}")
    return sealed


class PetSpeakChannel:
    """Runtime → pet bubble. ``deliver`` runs on a runtime worker thread."""

    def deliver(self, text: str, attachments: Sequence[Path]) -> bool:
        # pygent records the delivery receipt when this returns True.
        out({"type": "speak", "text": text, "attachments": [str(p) for p in attachments]})
        return True


class PetSession:
    """One runtime host + one lara-says session, turns serialized."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.config = load_run_config(workspace_root=self.workspace, load_local_model=False)
        # The pet runs unattended: realtime speak interaction on, approval
        # prompts off (nobody is watching a dialog box).
        # The fork reply forces tool_choice="speak"; DeepSeek's Anthropic
        # endpoint rejects that while thinking is on ("Thinking mode does not
        # support this tool_choice"), which used to drop every message sent
        # during a running turn. effort "none" turns thinking off for the
        # fork only; pygent >= 0.6.7 sends it as thinking {"type": "disabled"}
        # for models whose catalog entry declares disable_supported, trading a
        # little reasoning depth in the one-line ack for a delivered message.
        self.config.realtime_interaction = RealtimeInteractionConfig(
            enabled=True, fork_reasoning_effort="none"
        )
        self.config.runtime_approvals = RuntimeApprovalConfig(
            enabled=False, timeout_seconds=300.0, preauthorized_tools=()
        )
        # The managed-execution binding does not carry the prompt registry to
        # the executing module copy, so the canonical desktop-pet persona is
        # rendered here into the static base prompt instead.
        self.config.system_prompt = self.config.system_prompt or self._persona()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._host: LaraExecutionHost | None = None
        self._dsh = self._start_dsh(dsh_target())
        self._lara_model = self._borrow_dsh_model()
        self._apply_lara_model(self.config)
        self.manager = SessionManager(self.config)
        account_model = None if self._lara_model is None else next(iter(self._lara_model["models"]))
        self.session_id = resolve_session(self.manager, account_model)
        seal_interrupted_tool_calls(self.manager, self.session_id)
        self._turn_tasks: set[asyncio.Task[None]] = set()
        self._active_turn: ManagedSessionTurn | None = None

    def _persona(self) -> str:
        ctx = PromptRenderContext(
            session_id=PYGENT_SAYS_SESSION_MODE,
            workspace_root=self.workspace,
            session_dir=self.workspace,
            turn_id="static",
            projection={},
            tool_names=[],
            request_id="pet-persona",
            request_type="agent_turn",
        )
        return "\n\n".join(
            [
                *(
                    rendered
                    for module in lara_prompt_modules()
                    if module.phase == "static"
                    if (rendered := module.render(ctx)) is not None
                ),
                SPEAK_CHANNEL_RULES,
            ]
        )

    def _config_loader(self, **kwargs):
        kwargs.setdefault("workspace_root", str(self.workspace))
        config = load_run_config(**kwargs, load_local_model=False)
        self._apply_lara_model(config)
        return config

    def _borrow_dsh_model(self) -> dict[str, Any] | None:
        """Use the signed-in model selected by the running dsh."""
        if self._dsh is None:
            return None
        try:
            route = self._dsh.gateway.model_route()
        except Exception as exc:
            log(f"could not read dsh's model route: {type(exc).__name__}")
            return None
        selection = route.get("default") or {}
        account = route.get("account") or {}
        model = selection.get("model")
        token = account.get("token")
        base_url = account.get("baseURL")
        if (
            selection.get("provider") != "deepseek-account"
            or not account.get("signedIn")
            or not all(isinstance(value, str) and value for value in (model, token, base_url))
        ):
            log("dsh has no selected model on a signed-in DeepSeek account")
            return None
        return build_model_mapping(model, base_url, token)

    def _apply_lara_model(self, config) -> None:
        mapping = self._lara_model
        if mapping is None:
            config.model_config_mapping = {}
            config.model_config = None
            config.model_configuration_status = "unconfigured"
            config.model_configuration_error = None
            config.resolved_agent = None
            return
        config.model_config_mapping = mapping
        config.model_config = ModelConfig.from_mapping(mapping)
        config.model_configuration_status = "configured"
        config.model_configuration_error = None
        config.resolved_agent = ResolvedAgentConfig(
            alias=config.resolved_agent.alias if config.resolved_agent is not None else "default",
            default_model_group="dsh",
        )
        # pygent 0.6.5 never reads the declared context limit at runtime:
        # compression and eternal extraction budget off RunConfig.context_window
        # alone. Clamp the inherited budget to the borrowed model's declared
        # window so compaction fires before the real API window overflows.
        model = next(iter(config.model_config.models.values()))
        limits = model.spec.capabilities.limits
        if limits.context_tokens:
            config.context_window = min(
                v for v in (config.context_window, limits.context_tokens) if v
            )
            log(f"context budget {config.context_window} tokens (model declares {limits.context_tokens})")

    def _start_dsh(self, config: DshGatewayConfig) -> DshDaemon | None:
        """Attach the pet's daemon to the running dsh."""
        try:
            if "LARA_DSH_PLUGIN_PID" in os.environ:
                endpoint = PluginEndpoint(
                    port=int(os.environ["LARA_DSH_PLUGIN_PORT"]),
                    pid=int(os.environ["LARA_DSH_PLUGIN_PID"]),
                    version=None,
                )
                config = replace(config, plugin_endpoint=endpoint)
            daemon = DshDaemon(
                config,
                state_dir=user_lara_root(config.dsh_home),
                on_event=self._on_dsh_event,
                on_activity=self._on_dsh_activity,
            )
            daemon.start()
        except Exception as exc:
            log(f"dsh daemon unavailable: {exc!r}")
            return None
        log(f"dsh daemon on port {daemon.port}, home {config.dsh_home}, mode {daemon.mode}")
        return daemon

    def _on_dsh_activity(self, working: bool) -> None:
        if self._loop is not None and self._host is not None:
            self._loop.call_soon_threadsafe(out, {"type": "dsh_activity", "working": working})

    def _on_dsh_event(self, event: DshEvent | DshEventBatch) -> None:
        """Called on the daemon's notification thread; hop onto the loop."""
        if self._loop is None or self._host is None:
            return
        self._loop.call_soon_threadsafe(
            lambda: self._track(asyncio.create_task(self._route_dsh_event(event)))
        )

    async def _route_dsh_event(self, event: DshEvent | DshEventBatch) -> None:
        """Deliver a dsh report the way a mid-turn user exchange arrives.

        While Lara is mid-turn the report is steered into that turn as a
        wait-mode ``StandaloneUserMessage`` — the same operation pygent uses
        for fork-reply exchanges. The react layer applies it after pending
        tool results and before the next model call, and at a plain-text
        ending it still applies, giving the turn one more model step instead
        of letting the report wait for a new turn. Only a turn whose inbox is
        already sealed refuses it; then (and when Lara is idle) the report
        opens a harness-framed turn of its own.
        """
        turn = self._active_turn
        if (
            turn is not None
            and turn.status == "running"
            and turn.execution_handle is not None
            and turn.run_ref is not None
        ):
            input_id = f"dsh-event-{uuid.uuid4().hex[:12]}"
            message = UserMessage(
                content=event.envelope(),
                kind=DSH_EVENT_MESSAGE_KIND,
                data={"case_run_id": turn.run_ref.case_run_id, "input_id": input_id},
            )
            try:
                delivery = await turn.execution_handle.send_input(
                    input_id=input_id,
                    kind=REACT_PROJECTION_OPERATION_KIND,
                    value=encode_react_projection_operation(
                        StandaloneUserMessage(message, SteeringMode.WAIT)
                    ),
                )
            except Exception as exc:  # execution already sealed or gone
                log(f"dsh event steering failed, opening a turn: {exc!r}")
                delivery = None
            if delivery is not None and delivery.status in {"accepted", "duplicate"}:
                return
        await self._start_event_turn(event)

    async def _start_event_turn(self, event: DshEvent | DshEventBatch) -> None:
        host = self._host
        if host is None or self._lara_model is None:
            return
        input_id = f"dsh-event-{uuid.uuid4().hex[:12]}"
        # The runtime adds the speak reply-instruction only to <user-context>
        # turns; a harness-framed turn supplies it itself.
        message = f"{event.envelope()}\n{render_reply_instruction().strip()}"
        lease = await host.acquire_runtime(config=self.config, manager=self.manager)
        try:
            lease.runtime.reminders.prewarm_session(self.session_id)
            turn = await host.coordinator.submit_turn(
                lease=lease,
                command=TurnCommand(
                    session_id=self.session_id,
                    message=message,
                    case_id=CASE_ID,
                    submission_id=input_id,
                    interactive_approvals=False,
                    message_kind=DSH_EVENT_MESSAGE_KIND,
                ),
            )
        except BaseException:
            await lease.release()
            raise
        await self._watch_turn(turn, input_id)

    def _track(self, task: asyncio.Task[None]) -> None:
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        async with LaraExecutionHost(
            credential_environ=build_credential_environment(self.config.user_pygent_root),
            run_config_loader=self._config_loader,
            speak_channels=[PetSpeakChannel()],
        ) as host:
            self._loop = loop
            self._host = host
            try:
                await self._serve(host, self._dsh)
            finally:
                self._host = None
                if self._dsh is not None:
                    self._dsh.close()

    async def _serve(self, host: LaraExecutionHost, dsh: DshDaemon | None) -> None:
        out({
            "type": "ready",
            "session_id": self.session_id,
            "workspace": str(self.workspace),
            "dsh_port": None if dsh is None else dsh.port,
            "dsh_working": dsh is not None and dsh.working,
        })
        loop = asyncio.get_running_loop()
        while True:
            raw = await loop.run_in_executor(None, sys.stdin.readline)
            if not raw:
                break  # parent died or closed stdin
            line = raw.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                out({"type": "error", "msg": f"bad request line: {line[:120]}", "fatal": False})
                continue
            if request.get("type") == "ping":
                out({"type": "pong"})
                continue
            if request.get("type") == "send":
                text = str(request.get("text") or "").strip()
                if not text:
                    continue
                # The unified entry routes by lane state: an active turn
                # chain gets a fork interrupt (fast speak ack + exchange
                # envelope), otherwise a real turn starts. Deliveries are
                # detached so a long turn never blocks the next message.
                self._track(asyncio.create_task(self._deliver(host, text)))
                continue
            out({"type": "error", "msg": f"unknown request type: {request.get('type')!r}", "fatal": False})

    def _model_unavailable_notice(self) -> str | None:
        """Why the pet cannot answer right now, as one actionable line."""
        if self._lara_model is not None:
            return None
        return "桌宠需要运行中的 dsh。请在 dsh 登录官方账号、选择账号模型，再重启桌宠。"

    async def _deliver(self, host: LaraExecutionHost, text: str) -> None:
        input_id = f"turn-{uuid.uuid4().hex}"
        notice = self._model_unavailable_notice()
        if notice is not None:
            # Without a model the agent still runs and answers in plain text,
            # which the pet never renders: the user sees a message vanish.
            # Say why, in the one channel the pet does show.
            out({"type": "speak", "text": notice, "attachments": []})
            out({"type": "turn_started", "turn_id": input_id})
            out({"type": "turn_finished", "turn_id": input_id, "status": "error",
                 "error": "no usable model route"})
            return
        try:
            delivery = await host.deliver_user_message(
                config=self.config,
                manager=self.manager,
                session_id=self.session_id,
                message=text,
                input_id=input_id,
                interactive_approvals=False,
            )
        except Exception as exc:
            out({"type": "error", "msg": f"delivery failed: {exc!r}", "fatal": False})
            return
        if delivery.mode == "interrupt":
            # pygent publishes the fork reply through PetSpeakChannel.
            return
        assert delivery.turn is not None
        await self._watch_turn(delivery.turn, input_id)

    async def _watch_turn(self, turn: ManagedSessionTurn, turn_id: str) -> None:
        out({"type": "turn_started", "turn_id": turn_id})
        try:
            await self._stream_turn(turn, turn_id)
            output, _ = await turn.result()
        except Exception as exc:
            # A turn that failed before or while running (pygent raises its
            # startup error from result()) must still close out in the UI.
            out({"type": "turn_finished", "turn_id": turn_id, "status": "error", "error": repr(exc)})
            return
        finally:
            if self._active_turn is turn:
                self._active_turn = None
        # A skipped (cancelled) turn finalizes without an output envelope.
        result = (
            plain_object(plain_object(output.data).get("result"))
            if output is not None
            else {}
        )
        out({
            "type": "turn_finished",
            "turn_id": turn_id,
            "status": str(result.get("status") or turn.status),
            "error": str(result.get("error") or "") or None,
        })

    async def _stream_turn(self, turn: ManagedSessionTurn, turn_id: str) -> None:
        await turn.wait_ready()
        if turn.execution_handle is None:
            return  # failed at startup; result() reports why
        self._active_turn = turn
        # Consumer-side streaming bridge: model.tool_call.delta events flow
        # through the turn stream; decode the speak arguments incrementally
        # (pygent whitelists the model-layer event kinds, so the bridge lives
        # here, on the renderer side of the bus).
        speak_bridge = SpeakStreamBridge()
        async with turn.subscribe() as stream:
            async for event in stream:
                if event.kind == "pygent.v1.realtime.speak":
                    # The final text arrives through PetSpeakChannel, which
                    # also covers turns this watcher never sees.
                    continue
                for suffix in speak_bridge.feed(event.kind, plain_object(event.data)):
                    out({"type": "speak_delta", "text": suffix, "turn_id": turn_id})


def main() -> None:
    # Windows pipes default to the locale codec; the Tauri host speaks UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    # Agent shell calls must use the CLI shipped with this sidecar.
    cli_dir = (
        Path(sys.executable).parent
        if getattr(sys, "frozen", False)
        else Path(sysconfig.get_path("scripts"))
    )
    os.environ["PATH"] = str(cli_dir) + os.pathsep + os.environ.get("PATH", "")
    workspace = (
        Path(sys.argv[sys.argv.index("--workspace") + 1])
        if "--workspace" in sys.argv
        else default_pet_workspace()
    )
    try:
        session = PetSession(workspace)
    except Exception as exc:
        out({"type": "error", "msg": f"bootstrap failed: {exc!r}", "fatal": True})
        return
    log(f"session {session.session_id} workspace {session.workspace}")
    try:
        asyncio.run(session.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
