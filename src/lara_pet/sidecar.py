"""Lara Pet sidecar: owns THE single realtime lara session for the pet.

Installed as the ``lara-pet-sidecar`` console script; the Tauri shell spawns
it (``lara-pet-sidecar --workspace <dir>``) and talks JSON lines over stdio.

Protocol (JSON lines over stdio):
  in:  {"type": "send", "text": "...", "attachments": [...]}
  out: {"type": "ready", "session_id": ..., "workspace": ...}
       {"type": "turn_started", "turn_id": ...}
       {"type": "speak", "text": ..., "attachments": [...]}
       {"type": "turn_finished", "turn_id": ..., "status": ..., "error": ...}
       {"type": "error", "msg": ...}
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
import sys
import time
import uuid
from pathlib import Path

from lara_sdk.execution_host import LaraExecutionHost, split_speak_delivery
from lara_sdk.lara_profile import lara_prompt_modules
from lara_shell import user_lara_root
from lara_shell.config.loader import load_run_config
from lara_shell.credentials import build_credential_environment
from lara_shell.dsh_daemon import DshDaemon, DshEvent, DshEventBatch
from lara_shell.dsh_settings import DEEPSEEK_API_KEY_REF, DshSettings, load_dsh_settings
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
LARA_DSH_KEY_ENV = "LARA_DSH_DEEPSEEK_API_KEY"

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


def out(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def log(msg: str) -> None:
    print(f"pet-sidecar: {msg}", file=sys.stderr, flush=True)


def resolve_session(manager: SessionManager) -> str:
    """The kernel keeps exactly one lara-says session; resume or create it."""
    try:
        manager.load(PYGENT_SAYS_SESSION_MODE)
    except FileNotFoundError:
        manager.create(CASE_ID, mode=PYGENT_SAYS_SESSION_MODE)
    return PYGENT_SAYS_SESSION_MODE


class PetSpeakChannel:
    """Runtime → pet bubble. ``deliver`` runs on a runtime worker thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop, workspace: Path) -> None:
        self._loop = loop
        self._workspace = workspace

    def deliver(self, text: str) -> bool:
        spoken, attachments = split_speak_delivery(text, self._workspace)
        self._loop.call_soon_threadsafe(
            out, {"type": "speak", "text": spoken, "attachments": attachments}
        )
        return True


class PetSession:
    """One runtime host + one lara-says session, turns serialized."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        workspace.mkdir(parents=True, exist_ok=True)
        self.config = load_run_config(workspace_root=str(workspace))
        # The pet runs unattended: realtime speak interaction on, approval
        # prompts off (nobody is watching a dialog box).
        self.config.realtime_interaction = RealtimeInteractionConfig(enabled=True)
        self.config.runtime_approvals = RuntimeApprovalConfig(
            enabled=False, timeout_seconds=300.0, preauthorized_tools=()
        )
        # The managed-execution binding does not carry the prompt registry to
        # the executing module copy, so the canonical desktop-pet persona is
        # rendered here into the static base prompt instead.
        self.config.system_prompt = self.config.system_prompt or self._persona()
        self.user_root = Path(self.config.user_pygent_root)
        # dsh owns provider configuration; when its credential store has the
        # DeepSeek key, the pet runs on that same key and endpoint instead of
        # a second model setup in config.yaml.
        self._dsh_settings = load_dsh_settings(self.user_root)
        self._dsh = self._start_dsh(self._dsh_settings)
        self._lara_model_key = self._borrow_dsh_key()
        self._apply_lara_model(self.config)
        self.manager = SessionManager(self.config)
        self.session_id = resolve_session(self.manager)
        self.turn_count = 0
        self._turn_tasks: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._host: LaraExecutionHost | None = None
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
        config = load_run_config(**kwargs)
        self._apply_lara_model(config)
        return config

    def _borrow_dsh_key(self) -> str | None:
        if self._dsh is None:
            return None
        try:
            key = self._dsh.gateway.resolve_credential(DEEPSEEK_API_KEY_REF)
        except Exception as exc:
            log(f"could not read {DEEPSEEK_API_KEY_REF} from dsh: {exc!r}")
            return None
        if key is None:
            log(f"dsh has no {DEEPSEEK_API_KEY_REF}; the pet keeps its config.yaml model")
        return key

    def _apply_lara_model(self, config) -> None:
        """Point the pet's model at dsh's DeepSeek route when its key is available."""
        if self._lara_model_key is None:
            return
        mapping = self._dsh_settings.lara_model_mapping(LARA_DSH_KEY_ENV)
        config.model_config_mapping = mapping
        config.model_config = ModelConfig.from_mapping(mapping)
        config.model_configuration_status = "configured"
        config.model_configuration_error = None
        config.resolved_agent = ResolvedAgentConfig(
            alias=config.resolved_agent.alias if config.resolved_agent is not None else "default",
            default_model_group="dsh",
        )

    def _credential_environ(self):
        transient = {} if self._lara_model_key is None else {LARA_DSH_KEY_ENV: self._lara_model_key}
        return build_credential_environment(self.config.user_pygent_root, transient=transient)

    def _start_dsh(self, settings: DshSettings) -> DshDaemon | None:
        """Lara's seat as dsh system agent: one resident runtime for the pet's life.

        The daemon publishes its port for ``lara dsh`` (which Lara runs via
        bash) and reports finished dsh turns back into Lara's own session as
        ``<dsh-event>`` messages. A dsh that cannot boot (no credential, no
        runtime download) degrades to a pet without a worker, not a dead pet.
        """
        try:
            daemon = DshDaemon(
                settings.gateway_config(),
                state_dir=self.user_root,
                on_event=self._on_dsh_event,
            )
            daemon.start()
        except Exception as exc:
            log(f"dsh daemon unavailable: {exc!r}")
            return None
        log(f"dsh daemon on port {daemon.port}, home {settings.dsh_home}, mode {daemon.mode}")
        return daemon

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
        if host is None:
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
            credential_environ=self._credential_environ(),
            run_config_loader=self._config_loader,
            speak_channels=[PetSpeakChannel(loop, self.workspace)],
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
                out({"type": "error", "msg": f"bad request line: {line[:120]}"})
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
            out({"type": "error", "msg": f"unknown request type: {request.get('type')!r}"})

    async def _deliver(self, host: LaraExecutionHost, text: str) -> None:
        self.turn_count += 1
        input_id = f"turn-{time.time() * 1000:.0f}-{self.turn_count}"
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
            out({"type": "error", "msg": f"delivery failed: {exc!r}"})
            return
        if delivery.mode == "interrupt":
            self._emit_fork_reply(delivery.outcome)
            return
        assert delivery.turn is not None
        await self._watch_turn(delivery.turn, input_id)

    def _emit_fork_reply(self, outcome) -> None:
        """Render the fork ack; ``None`` means a newer interrupt superseded it."""

        if outcome is None:
            return
        speak_args = outcome.speak_args
        if speak_args is not None and speak_args.say_nothing:
            return
        reply = (speak_args.text if speak_args is not None else outcome.text) or ""
        if reply:
            out({
                "type": "speak",
                "text": reply,
                "attachments": list(speak_args.attachments) if speak_args is not None else [],
            })

    async def _watch_turn(self, turn: ManagedSessionTurn, turn_id: str) -> None:
        out({"type": "turn_started", "turn_id": turn_id})
        try:
            await self._stream_turn(turn)
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

    async def _stream_turn(self, turn: ManagedSessionTurn) -> None:
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
                    out({"type": "speak_delta", "text": suffix})


def main() -> None:
    # Windows pipes default to the locale codec; the Tauri host speaks UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    workspace = (
        Path(sys.argv[sys.argv.index("--workspace") + 1])
        if "--workspace" in sys.argv
        else user_lara_root() / "pet"
    )
    try:
        session = PetSession(workspace)
    except Exception as exc:
        out({"type": "error", "msg": f"bootstrap failed: {exc!r}"})
        return
    log(f"session {session.session_id} workspace {session.workspace}")
    try:
        asyncio.run(session.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
