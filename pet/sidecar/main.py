"""Lara Pet sidecar: owns THE single realtime lara session for the pet.

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
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from lara.core.io import plain_object
from lara.core.paths import user_lara_root
from lara.orchestration.execution_host import LocalExecutionHost
from lara.runtime.agent.prompt_models import PromptRenderContext
from lara.runtime.agent.speak_stream import SpeakStreamBridge
from lara.schema import RuntimeApprovalConfig
from lara.schema.models import RealtimeInteractionConfig
from lara.sessions import SessionManager, LARA_SAYS_SESSION_MODE
from lara_shell.config.loader import load_run_config
from lara_shell.credentials import build_credential_environment
from lara_sdk.lara_profile import lara_prompt_modules

CASE_ID = "pet"

# The pet's persona comes from the stock prompt profile; the speak channel is
# the only user-visible surface, so the sidecar appends its channel rules to
# the rendered profile (the runtime's per-turn reply-instruction alone does
# not keep the model off plain text).
SPEAK_CHANNEL_RULES = (
    "输出通道：\n"
    "- 用户只能看到 speak 工具发出的消息，其他一切文本输出都不可见；"
    "不要用普通文本回答用户。\n"
    "- 收到任务后先用 speak 说一句简短确认，再开始干活；过程中用 speak 汇报进展"
    "和提出问题，结束时用 speak 总结结果。\n"
    "- 每条 speak 尽量控制在一两句话，用用户的语言（默认中文）交流。"
)


def out(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def log(msg: str) -> None:
    print(f"pet-sidecar: {msg}", file=sys.stderr, flush=True)


def resolve_session(manager: SessionManager) -> str:
    """The kernel keeps exactly one lara-says session; resume or create it."""
    try:
        manager.load(LARA_SAYS_SESSION_MODE)
    except FileNotFoundError:
        manager.create(CASE_ID, mode=LARA_SAYS_SESSION_MODE)
    return LARA_SAYS_SESSION_MODE


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
        self.user_root = Path(self.config.user_lara_root)
        self.manager = SessionManager(self.config)
        self.session_id = resolve_session(self.manager)
        self.turn_lock = asyncio.Lock()
        self.turn_count = 0
        self.interrupt_count = 0
        self._active_turn = None

    def _persona(self) -> str:
        ctx = PromptRenderContext(
            session_id=LARA_SAYS_SESSION_MODE,
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
                    module.render(ctx)
                    for module in lara_prompt_modules()
                    if module.phase == "static"
                ),
                SPEAK_CHANNEL_RULES,
            ]
        )

    def _config_loader(self, **kwargs):
        kwargs.setdefault("workspace_root", str(self.workspace))
        return load_run_config(**kwargs)

    async def run(self) -> None:
        async with LocalExecutionHost(
            credential_environ=build_credential_environment(self.config.user_lara_root),
            run_config_loader=self._config_loader,
        ) as host:
            out({
                "type": "ready",
                "session_id": self.session_id,
                "workspace": str(self.workspace),
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
                    active = self._active_turn
                    if (
                        active is not None
                        and not active.done
                        and active.execution_id is not None
                        and active.run_ref is not None
                    ):
                        # Mid-turn: the fork-reply fast path answers now while
                        # the main turn keeps running (D-RT2/3). No lock — the
                        # runtime's per-session fork state handles supersede.
                        asyncio.create_task(self._handle_interrupt(active, text))
                    else:
                        async with self.turn_lock:
                            await self._run_turn(host, text)
                    continue
                out({"type": "error", "msg": f"unknown request type: {request.get('type')!r}"})

    async def _run_turn(self, host: LocalExecutionHost, text: str) -> None:
        self.turn_count += 1
        turn_id = f"turn-{time.time() * 1000:.0f}-{self.turn_count}"
        turn = await host.turns.submit(
            config=self.config,
            manager=self.manager,
            session_id=self.session_id,
            message=text,
            turn_id=turn_id,
            interactive_approvals=False,
            attachments=(),
        )
        self._active_turn = turn
        try:
            out({"type": "turn_started", "turn_id": turn_id})
            await turn.wait_ready()
            # Consumer-side streaming bridge: model.tool_call.delta events flow
            # through the turn stream; decode the speak arguments incrementally
            # (pygent whitelists the model-layer event kinds, so the bridge lives
            # here, on the renderer side of the bus).
            speak_bridge = SpeakStreamBridge()
            async with turn.subscribe() as stream:
                async for event in stream:
                    if event.kind == "lara.realtime.speak":
                        data = plain_object(event.data)
                        out({
                            "type": "speak",
                            "text": str(data.get("text") or ""),
                            "attachments": list(data.get("attachments") or ()),
                        })
                    else:
                        for suffix in speak_bridge.feed(event.kind, plain_object(event.data)):
                            out({"type": "speak_delta", "text": suffix})
            output, _ = await turn.result()
            result = plain_object(plain_object(output.data).get("result"))
            out({
                "type": "turn_finished",
                "turn_id": turn_id,
                "status": str(result.get("status") or turn.status),
                "error": str(result.get("error") or "") or None,
            })
        finally:
            self._active_turn = None

    async def _handle_interrupt(self, turn, text: str) -> None:
        """Mid-turn user message: the D-RT2/3 fork-reply fast path.

        The runtime supersedes an in-flight fork (cancel + numbered batch) and
        queues the exchange envelope so the main agent learns what was asked
        and answered at its next tool boundary.
        """

        self.interrupt_count += 1
        try:
            outcome = await turn.runtime_service.send_interrupt(
                turn.execution_id,
                manager=self.manager,
                run_ref=turn.run_ref,
                session_id=self.session_id,
                input_id=f"interrupt-{time.time() * 1000:.0f}-{self.interrupt_count}",
                message=text,
            )
        except Exception as exc:
            out({"type": "error", "msg": f"interrupt failed: {exc!r}"})
            return
        if outcome is None:
            return  # superseded by a newer interrupt; the newer one answers
        speak_args = outcome.speak_args
        if speak_args is not None and speak_args.say_nothing:
            return
        reply = (
            speak_args.text
            if speak_args is not None
            else outcome.text
        ) or ""
        if reply:
            out({
                "type": "speak",
                "text": reply,
                "attachments": list(speak_args.attachments) if speak_args is not None else [],
            })


def main() -> None:
    # Windows pipes default to the locale codec; the Tauri host speaks UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
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
