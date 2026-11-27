"""Long-lived dsh runtime host: Lara's system-agent seat over DeepSeek Harness.

One :class:`DshDaemon` holds one dsh gateway for as long as the hosting
process lives (the pet sidecar, or ``lara dsh daemon``): attached to the dsh
the user already runs when its Lara plugin is published, otherwise a private
runtime it spawns (:func:`open_gateway`). It follows the runtime-wide
notification stream, keeps the sessions it drives tracked, and serves
the ``lara dsh`` CLI over a loopback newline JSON-RPC channel whose port is
published in ``<user root>/dsh-daemon.json``. Work dispatched through the
daemon is asynchronous: ``send`` returns as soon as the prompt is queued, and
turn completions come back as :class:`DshEvent` callbacks that the host turns
into messages for Lara.

Daemon methods (all params/results are JSON objects):

    dsh/ping                                   -> {ok, pid, home, mode}   (mode: attach | spawn)
    dsh/send     {prompt, session_id?, cwd?}   -> {session_id, message_id, created}
                 (cwd on a new session roots it — and its write sandbox — there;
                  on an existing session it only changes the effective directory)
    dsh/sessions                               -> {sessions: [SessionView]}
    dsh/status   {session_id}                  -> {session: SessionView | null}
    dsh/wait     {session_id, timeout?}        -> {session: SessionView, timed_out}
    dsh/resume   {session_id}                  -> {session: SessionView}
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pygent.agent.pygent_v1.core.prompt import Prompt

from lara_shell.dsh_gateway import (
    DshGatewayConfig,
    DshRuntimeGateway,
    DshSessionEntry,
    SessionControlChannel,
    SessionControlError,
    assistant_text,
    open_gateway,
)

DAEMON_STATE_FILENAME = "dsh-daemon.json"
REPLY_EXCERPT_CHARS = 2000
# A runtime question nobody answered: approvals settle fail-closed
# ("unavailable"), questions are cancelled so the dsh tool call errors out.
REQUEST_TIMEOUT_SECONDS = 15 * 60
# turn_finished reports arriving within this window of each other are
# delivered together as one DshEventBatch; questions and approvals are never
# batched because each needs its own answer.
FINISHED_BATCH_WINDOW_SECONDS = 3.0


@dataclass(frozen=True)
class DshQuestion:
    """One ask_user_question item as the dsh tool posed it."""

    id: str
    question: str
    options: tuple[str, ...] = ()
    header: str | None = None
    detail: str | None = None
    multi_select: bool = False

    @classmethod
    def from_wire(cls, item: dict[str, Any]) -> "DshQuestion":
        return cls(
            id=str(item["id"]),
            question=str(item.get("question") or ""),
            options=tuple(str(o.get("label") or "") for o in item.get("options") or () if isinstance(o, dict)),
            header=item.get("header"),
            detail=item.get("detail"),
            multi_select=bool(item.get("multiSelect")),
        )


@dataclass(frozen=True)
class DshEvent:
    """A session-level fact worth telling Lara about.

    ``turn_finished`` carries ``finish_reason``/``reply``/``error``;
    ``approval_request`` carries ``request_id``/``tool_name``/``reason``;
    ``question`` carries ``request_id``/``questions``; ``request_cancelled``
    carries ``request_id`` (the runtime withdrew it before Lara answered).
    """

    kind: str
    session_id: str
    title: str | None = None
    finish_reason: str = ""
    reply: str = ""
    error: str | None = None
    request_id: str | None = None
    tool_name: str | None = None
    reason: str | None = None
    questions: tuple[DshQuestion, ...] = ()
    cwd: str | None = None

    def envelope(self) -> str:
        """Render the ``<dsh-event>`` message that opens a turn of its own."""
        session = {"id": self.session_id, "title": self.title or ""}
        if self.cwd is not None:
            session["cwd"] = self.cwd
        if self.kind == "turn_finished":
            session["finish_reason"] = self.finish_reason
        lines = [f"  {Prompt.self_closing('session', **session)}"]
        if self.kind == "turn_finished":
            lines.append(f"  <reply>{Prompt(self.reply).escape()}</reply>")
            if self.error:
                lines.append(f"  <error>{Prompt(self.error).escape()}</error>")
        elif self.kind == "approval_request":
            lines.append(f"  {Prompt.self_closing('request', id=self.request_id or '', tool=self.tool_name or '')}")
            if self.reason:
                lines.append(f"  <reason>{Prompt(self.reason).escape()}</reason>")
        elif self.kind == "question":
            lines.append(f"  {Prompt.self_closing('request', id=self.request_id or '')}")
            for question in self.questions:
                attrs: dict[str, Any] = {"id": question.id}
                if question.header:
                    attrs["header"] = question.header
                if question.multi_select:
                    attrs["multi_select"] = "true"
                body = [f"<text>{Prompt(question.question).escape()}</text>"]
                if question.detail:
                    body.append(f"<detail>{Prompt(question.detail).escape()}</detail>")
                body.extend(f"<option>{Prompt(option).escape()}</option>" for option in question.options)
                lines.append(_indent(str(Prompt("\n".join(body)).wrap("question", indent="  ", **attrs))))
        else:
            lines.append(f"  {Prompt.self_closing('request', id=self.request_id or '')}")
        return str(Prompt("\n".join(lines)).wrap("dsh-event", kind=self.kind))

    def context_text(self) -> str:
        """Plain-text rendering for a tool-result append.

        Pygent wraps appended context in ``<runtime-context>`` and escapes it,
        so XML here would reach the model as inert entities.
        """
        title = f" ({self.title})" if self.title else ""
        head = f"dsh event: {self.kind} — session {self.session_id}{title}"
        if self.kind == "turn_finished":
            lines = [f"{head}, finish_reason={self.finish_reason}"]
            if self.error:
                lines.append(f"error: {self.error}")
            lines.append(f"reply: {self.reply or '(none)'}")
            return "\n".join(lines)
        if self.kind == "approval_request":
            lines = [f"{head}, request_id={self.request_id}, tool={self.tool_name}"]
            if self.reason:
                lines.append(f"reason: {self.reason}")
            return "\n".join(lines)
        if self.kind == "question":
            lines = [f"{head}, request_id={self.request_id}"]
            for question in self.questions:
                options = f" options: {' | '.join(question.options)}" if question.options else ""
                lines.append(f"question [{question.id}]: {question.question}{options}")
            return "\n".join(lines)
        return f"{head}, request_id={self.request_id}"


@dataclass(frozen=True)
class DshEventBatch:
    """Several ``turn_finished`` reports that landed within one window.

    Delivered as a single message so Lara summarizes them in one breath
    instead of opening one turn (and one speak) per session.
    """

    events: tuple[DshEvent, ...]

    def envelope(self) -> str:
        body = "\n".join(_indent(event.envelope()) for event in self.events)
        return str(Prompt(body).wrap("dsh-events", count=len(self.events)))

    def context_text(self) -> str:
        return "\n".join(event.context_text() for event in self.events)


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())


@dataclass
class PendingRequest:
    """A runtime question awaiting Lara's answer.

    Approvals arrive as SDK host requests and are settled by responding on
    the wire with ``wire_id``; questions arrive through the SDK's blocking
    handler, which waits on ``settled`` and then reads ``answers`` (or sees
    ``declined``).
    """

    request_id: str
    kind: str  # "approval" | "question"
    session_id: str
    created_at: float
    tool_name: str | None = None
    reason: str | None = None
    questions: tuple[DshQuestion, ...] = ()
    wire_id: str | int | None = field(default=None, repr=False)
    settled: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    answers: list[dict[str, Any]] | None = field(default=None, repr=False)
    declined: bool = field(default=False, repr=False)

    def to_wire(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "kind": self.kind,
            "session_id": self.session_id,
            "age_seconds": round(time.monotonic() - self.created_at, 1),
            "tool_name": self.tool_name,
            "reason": self.reason,
            "questions": [asdict(q) for q in self.questions],
        }


@dataclass
class _TrackedSession:
    """Runtime-observed facts the inventory lacks: last reply and finish reason."""

    status: str | None = None
    reply: str = ""
    turn_reply: list[str] = field(default_factory=list)
    finish_reason: str | None = None
    error: str | None = None
    title: str | None = None
    cwd: str | None = None
    turns: int = 0
    step: int = 0
    last_tool: str | None = None
    turn_started_at: float | None = None  # time.time()
    # Dispatched or resumed through this daemon. An attached dsh also carries
    # the user's own sessions; their turns are tracked but never reported.
    owned: bool = False


@dataclass(frozen=True)
class SessionView:
    """Inventory entry merged with the daemon's tracked state."""

    session_id: str
    title: str | None
    cwd: str | None
    created_at: int | None
    origin: str
    parent_session_id: str | None
    live: bool
    status: str | None
    turns: int
    finish_reason: str | None
    reply: str
    error: str | None
    step: int = 0
    last_tool: str | None = None
    running_seconds: float | None = None
    event_count: int | None = None

    @classmethod
    def build(cls, entry: DshSessionEntry | None, tracked: _TrackedSession | None, session_id: str) -> "SessionView":
        tracked = tracked or _TrackedSession()
        status = tracked.status or (entry.status if entry else None)
        running_seconds = (
            round(time.time() - tracked.turn_started_at, 1)
            if status == "running" and tracked.turn_started_at is not None
            else None
        )
        return cls(
            session_id=session_id,
            title=(entry.title if entry else None) or tracked.title,
            cwd=entry.cwd if entry else tracked.cwd if tracked else None,
            created_at=entry.created_at if entry else None,
            origin=entry.origin if entry else "root",
            parent_session_id=entry.parent_session_id if entry else None,
            live=entry.live if entry else False,
            # The tracker follows the ordered notification stream (and marks a
            # just-queued prompt running); the inventory is a point-in-time poll.
            status=status,
            turns=tracked.turns,
            finish_reason=tracked.finish_reason,
            reply=tracked.reply,
            error=tracked.error,
            step=tracked.step,
            last_tool=tracked.last_tool,
            running_seconds=running_seconds,
            event_count=entry.event_count if entry else None,
        )

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> "SessionView":
        return cls(**payload)


class DshDaemon:
    """Own one dsh runtime, track its sessions, and serve the loopback control API."""

    def __init__(
        self,
        config: DshGatewayConfig,
        *,
        state_dir: Path,
        on_event: Callable[[DshEvent | DshEventBatch], None] | None = None,
        batch_window_seconds: float = FINISHED_BATCH_WINDOW_SECONDS,
    ) -> None:
        self._config = config
        self._state_dir = Path(state_dir)
        self._on_event = on_event
        self._gateway_slot: DshRuntimeGateway | None = None
        self._tracked: dict[str, _TrackedSession] = {}
        self._requests: dict[str, PendingRequest] = {}
        self._batch_window = batch_window_seconds
        self._finished_batch: list[DshEvent] = []
        self._batch_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._server: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._closing = threading.Event()
        self.port: int | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self._state_dir / DAEMON_STATE_FILENAME

    @property
    def gateway(self) -> DshRuntimeGateway:
        """The resident runtime, for in-process hosts (credentials, models)."""
        if self._gateway_slot is None:
            raise RuntimeError("dsh daemon is not started")
        return self._gateway_slot

    @property
    def mode(self) -> str:
        """``attach`` (the user's running dsh) or ``spawn`` (a private runtime)."""
        return self.gateway.mode

    def start(self) -> None:
        if self._server is not None:
            return
        self._refuse_if_another_daemon_is_live()
        self._gateway_slot = open_gateway(
            self._config, approval_requests=True, on_user_question=self._answer_user_question
        )
        self.gateway.start()
        self._threads.append(_spawn(self._follow_notifications, "dsh-daemon-notifications"))
        self._threads.append(_spawn(self._follow_requests, "dsh-daemon-requests"))
        self._threads.append(_spawn(self._expire_requests, "dsh-daemon-request-expiry"))
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        self._server = server
        self.port = int(server.getsockname()[1])
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps({"port": self.port, "pid": os.getpid(), "home": str(self._config.dsh_home), "mode": self.mode}),
            encoding="utf-8",
        )
        self._threads.append(_spawn(self._accept_loop, "dsh-daemon-accept"))

    def _refuse_if_another_daemon_is_live(self) -> None:
        """Two daemons would fight over the state file; a stale one is replaced."""
        try:
            client = DshDaemonClient.connect(self._state_dir, home=self._config.dsh_home)
        except DaemonUnavailable:
            return
        client.close()
        raise RuntimeError(
            f"another dsh daemon is already serving {self._config.dsh_home}"
        )

    def close(self) -> None:
        self._closing.set()
        timer = self._batch_timer
        if timer is not None:
            timer.cancel()
            self._flush_finished()
        if self._server is not None:
            try:
                self._server.close()
            finally:
                self._server = None
        try:
            if self.state_path.is_file() and self._owns_state_file():
                self.state_path.unlink()
        except OSError:
            pass
        if self._gateway_slot is not None:
            self._gateway_slot.close()

    def __enter__(self) -> "DshDaemon":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _owns_state_file(self) -> bool:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            return state.get("pid") == os.getpid() and state.get("port") == self.port
        except (OSError, ValueError):
            return False

    # -- operations (also callable in-process) -------------------------------

    def send(self, prompt: str, *, session_id: str | None = None, cwd: str | None = None) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        if cwd and session_id:
            raise ValueError("cwd applies only to new sessions")
        if cwd and not Path(cwd).is_absolute():
            raise ValueError("cwd must be an absolute path")
        entry = self.gateway.stat_session(session_id) if session_id else None
        created = entry is None
        target = session_id or f"dsh-{uuid.uuid4().hex[:12]}"
        # A session's directory is fixed at creation and becomes its write
        # sandbox, and dsh desktop only lists sessions of workspaces it has
        # registered. Root new sessions at the configured workspace (Lara
        # resolves it from dsh's own default workspace) instead of letting the
        # runtime stamp its process cwd.
        session_cwd = (cwd or str(self._config.cwd)) if entry is None else entry.cwd
        with self._lock:
            tracked = self._tracked.setdefault(target, _TrackedSession())
            tracked.owned = True
            tracked.cwd = session_cwd
            tracked.finish_reason = None
            tracked.error = None
            tracked.status = "running"
            tracked.turn_started_at = time.time()
        message_id = self.gateway.submit(target, prompt, cwd=session_cwd if created else None)
        return {"session_id": target, "message_id": message_id, "created": created, "cwd": session_cwd}

    def sessions(self) -> list[SessionView]:
        entries = self.gateway.list_sessions()
        with self._lock:
            views = [SessionView.build(entry, self._tracked.get(entry.session_id), entry.session_id) for entry in entries]
            known = {entry.session_id for entry in entries}
            for session_id, tracked in self._tracked.items():
                if session_id not in known:
                    views.append(SessionView.build(None, tracked, session_id))
        return views

    def status(self, session_id: str) -> SessionView | None:
        entry = self.gateway.stat_session(session_id)
        with self._lock:
            tracked = self._tracked.get(session_id)
        if entry is None and tracked is None:
            return None
        return SessionView.build(entry, tracked, session_id)

    def wait(self, session_id: str, *, timeout: float | None = None) -> tuple[SessionView | None, bool]:
        """Block until the session's agent is idle; return (view, timed_out)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        timed_out = False
        with self._idle:
            while True:
                tracked = self._tracked.get(session_id)
                if tracked is None or tracked.status != "running":
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    timed_out = True
                    break
                self._idle.wait(remaining)
        # status() takes the same non-reentrant lock; call it after releasing.
        return self.status(session_id), timed_out

    def cancel(self, session_id: str) -> bool:
        """Stop the session's current work; returns False when it is not live."""
        return self.gateway.cancel_session(session_id)

    def requests(self) -> list[PendingRequest]:
        with self._lock:
            return sorted(self._requests.values(), key=lambda item: item.created_at)

    def approve(self, request_id: str, outcome: str) -> PendingRequest:
        if outcome not in {"allowed-once", "rejected"}:
            raise ValueError("outcome must be allowed-once or rejected")
        pending = self._take_request(request_id, "approval")
        self._settle_approval(pending, outcome)
        return pending

    def answer(self, request_id: str, answers: dict[str, str] | str) -> PendingRequest:
        """Answer a question request.

        ``answers`` maps question id → text, or is one text applied to every
        question. Text equal to an option label selects it; anything else is
        a free-form answer.
        """
        pending = self._take_request(request_id, "question")
        wire: list[dict[str, Any]] = []
        for question in pending.questions:
            text = answers if isinstance(answers, str) else answers.get(question.id)
            if text is None:
                raise ValueError(f"no answer for question {question.id!r}")
            text = text.strip()
            if text in question.options:
                wire.append({"id": question.id, "selected": [text]})
            else:
                wire.append({"id": question.id, "selected": [], "custom": text})
        pending.answers = wire
        pending.settled.set()
        return pending

    def decline(self, request_id: str) -> PendingRequest:
        """Withdraw a question (the dsh tool call fails) or reject an approval."""
        with self._lock:
            pending = self._requests.get(request_id)
        if pending is None:
            raise ValueError(f"unknown or settled request: {request_id}")
        if pending.kind == "approval":
            return self.approve(request_id, "rejected")
        self._take_request(request_id, "question")
        pending.declined = True
        pending.settled.set()
        return pending

    def _settle_approval(self, pending: PendingRequest, outcome: str) -> None:
        assert pending.wire_id is not None
        try:
            self.gateway.respond(pending.wire_id, {"outcome": outcome})
        except Exception as exc:  # the runtime withdrew it or went away
            print(f"dsh daemon: approval {pending.request_id} could not be answered: {exc!r}", file=sys.stderr)

    def _take_request(self, request_id: str, kind: str) -> PendingRequest:
        with self._lock:
            pending = self._requests.get(request_id)
            if pending is None:
                raise ValueError(f"unknown or settled request: {request_id}")
            if pending.kind != kind:
                raise ValueError(f"request {request_id} is a {pending.kind}, not a {kind}")
            del self._requests[request_id]
        return pending

    def resume(self, session_id: str) -> SessionView:
        self.gateway.resume(session_id)
        with self._lock:
            tracked = self._tracked.setdefault(session_id, _TrackedSession())
            tracked.owned = True
            tracked.status = "idle"
        view = self.status(session_id)
        assert view is not None
        return view

    # -- runtime questions (SDK wire) ------------------------------------------

    def _register(self, pending: PendingRequest) -> None:
        with self._lock:
            self._requests[pending.request_id] = pending
            title = self._tracked.get(pending.session_id, _TrackedSession()).title
        self._on_event_safe(DshEvent(
            kind="approval_request" if pending.kind == "approval" else "question",
            session_id=pending.session_id,
            title=title,
            request_id=pending.request_id,
            tool_name=pending.tool_name,
            reason=pending.reason,
            questions=pending.questions,
        ))

    def _withdraw(self, request_id: str) -> None:
        """The runtime stopped waiting (tool cancelled, turn ended) before Lara answered."""
        with self._lock:
            pending = self._requests.pop(request_id, None)
            if pending is None:
                return
            title = self._tracked.get(pending.session_id, _TrackedSession()).title
        self._on_event_safe(DshEvent(kind="request_cancelled", session_id=pending.session_id, title=title, request_id=request_id))

    def _follow_requests(self) -> None:
        """``approval/request`` host requests, one PendingRequest each."""
        while not self._closing.is_set():
            try:
                request = self.gateway.next_request()
            except Exception:
                return  # runtime closed
            if request.method != "approval/request":
                self.gateway.respond(request.id, {})  # unknown host request: acknowledge, do nothing
                continue
            payload = request.payload
            self._register(PendingRequest(
                request_id=f"approval-{request.id}",
                kind="approval",
                session_id=str(payload.get("sessionId") or ""),
                created_at=time.monotonic(),
                tool_name=str(payload.get("toolName") or "") or None,
                reason=str(payload.get("reason") or "") or None,
                wire_id=request.id,
            ))

    def _answer_user_question(self, request: Any, cancelled: threading.Event) -> dict[str, Any]:
        """SDK ``on_user_question`` handler: block on this thread until Lara answers.

        Raising fails the dsh tool call (the SDK responds with an error); a
        runtime cancellation sets ``cancelled`` and the SDK discards whatever
        we return afterwards.
        """
        pending = PendingRequest(
            request_id=str(request.request_id),
            kind="question",
            session_id=str(request.session_id),
            created_at=time.monotonic(),
            questions=tuple(DshQuestion.from_wire(q) for q in request.questions if isinstance(q, dict)),
        )
        self._register(pending)
        while not pending.settled.wait(0.5):
            if cancelled.is_set():
                self._withdraw(pending.request_id)
                raise RuntimeError("user question cancelled by the runtime")
            if self._closing.is_set():
                raise RuntimeError("dsh daemon is closing")
        if pending.declined or pending.answers is None:
            raise RuntimeError("user question declined by Lara")
        return {"answers": pending.answers}

    def _expire_requests(self) -> None:
        while not self._closing.wait(30):
            self._sweep_expired_requests()

    def _sweep_expired_requests(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [p for p in self._requests.values() if now - p.created_at > REQUEST_TIMEOUT_SECONDS]
            for pending in expired:
                del self._requests[pending.request_id]
        for pending in expired:
            if pending.kind == "approval":
                self._settle_approval(pending, "unavailable")
            else:
                pending.declined = True
                pending.settled.set()

    # -- notification following ---------------------------------------------

    def _follow_notifications(self) -> None:
        subscription = self.gateway.notifications()
        while not self._closing.is_set():
            try:
                notification = subscription.next()
            except Exception:
                return  # runtime closed
            if notification.method == "session.status":
                self._on_status(str(notification.payload["sessionId"]), str(notification.payload["status"]))
            elif notification.method == "session.event":
                event = notification.payload["event"]
                if isinstance(event, dict):
                    try:
                        self._on_session_event(str(notification.payload["sessionId"]), event)
                    except Exception as exc:  # one odd event must not stop session tracking
                        print(f"dsh daemon: ignoring malformed session event: {exc!r}", file=sys.stderr)

    def _on_status(self, session_id: str, status: str) -> None:
        with self._idle:
            tracked = self._tracked.setdefault(session_id, _TrackedSession())
            tracked.status = status
            if status == "idle":
                self._idle.notify_all()

    def _on_session_event(self, session_id: str, event: dict[str, Any]) -> None:
        kind = event.get("type")
        data = event.get("data") or {}
        finished: DshEvent | None = None
        with self._lock:
            tracked = self._tracked.setdefault(session_id, _TrackedSession())
            if kind == "turn/start":
                tracked.turn_reply = []
                tracked.turns = int(data.get("turn") or tracked.turns + 1)
                tracked.step = 0
                tracked.last_tool = None
                tracked.turn_started_at = time.time()
            elif kind == "step/start":
                tracked.step = int(data.get("step") or tracked.step + 1)
            elif kind == "tool/call":
                tracked.last_tool = str(data.get("name") or "") or tracked.last_tool
            elif kind == "session/title":
                tracked.title = str(data.get("title") or "") or tracked.title
            elif kind == "assistant/message":
                text = assistant_text(data)
                if text:
                    tracked.turn_reply.append(text)
            elif kind == "turn/end":
                reason = data.get("reason")
                # A cancelled turn ends without a reason object.
                if not isinstance(reason, dict):
                    reason = {"kind": "cancelled"}
                tracked.finish_reason = str(reason.get("kind") or "unknown")
                error = reason.get("error")
                tracked.error = str(error.get("message") if isinstance(error, dict) else error) if error else None
                tracked.reply = "\n".join(tracked.turn_reply).strip()
                if not tracked.owned:
                    return
                finished = DshEvent(
                    kind="turn_finished",
                    session_id=session_id,
                    title=tracked.title,
                    finish_reason=tracked.finish_reason,
                    reply=tracked.reply[-REPLY_EXCERPT_CHARS:],
                    error=tracked.error,
                    cwd=tracked.cwd,
                )
        if finished is not None and self._on_event is not None:
            self._queue_finished(finished)

    def _queue_finished(self, event: DshEvent) -> None:
        """Hold a turn_finished report briefly so simultaneous ones ship together."""
        if self._batch_window <= 0:
            self._on_event_safe(event)
            return
        with self._lock:
            self._finished_batch.append(event)
            if self._batch_timer is None:
                self._batch_timer = threading.Timer(self._batch_window, self._flush_finished)
                self._batch_timer.daemon = True
                self._batch_timer.start()

    def _flush_finished(self) -> None:
        with self._lock:
            events = tuple(self._finished_batch)
            self._finished_batch.clear()
            self._batch_timer = None
        if not events:
            return
        self._on_event_safe(events[0] if len(events) == 1 else DshEventBatch(events))

    def _on_event_safe(self, event: "DshEvent | DshEventBatch") -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception as exc:  # a host callback failure must not stop tracking
            print(f"dsh daemon: on_event failed: {exc!r}", file=sys.stderr)

    # -- loopback RPC --------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._closing.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            _spawn(lambda: self._serve_connection(conn), "dsh-daemon-conn")

    def _serve_connection(self, conn: socket.socket) -> None:
        with conn, conn.makefile("r", encoding="utf-8", newline="\n") as reader:
            for line in reader:
                line = line.strip()
                if not line:
                    continue
                reply = self._handle_line(line)
                try:
                    conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                except OSError:
                    return

    def _handle_line(self, line: str) -> dict[str, Any]:
        try:
            message = json.loads(line)
        except ValueError:
            return _error(None, -32600, "invalid JSON")
        request_id = message.get("id") if isinstance(message, dict) else None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(request_id, -32600, "invalid JSON-RPC request")
        params = message.get("params") or {}
        try:
            result = self._dispatch(message["method"], params)
        except _MethodNotFound as exc:
            return _error(request_id, -32601, str(exc))
        except (ValueError, SessionControlError) as exc:
            return _error(request_id, -32602, str(exc))
        except Exception as exc:  # the daemon must survive a bad request
            return _error(request_id, -32603, f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        match method:
            case "dsh/ping":
                return {"ok": True, "pid": os.getpid(), "home": str(self._config.dsh_home), "mode": self.mode}
            case "dsh/send":
                return self.send(
                    str(params.get("prompt") or ""),
                    session_id=_optional_str(params.get("session_id")),
                    cwd=_optional_str(params.get("cwd")),
                )
            case "dsh/sessions":
                return {"sessions": [view.to_wire() for view in self.sessions()]}
            case "dsh/status":
                view = self.status(_required_str(params, "session_id"))
                return {"session": None if view is None else view.to_wire()}
            case "dsh/wait":
                timeout = params.get("timeout")
                view, timed_out = self.wait(
                    _required_str(params, "session_id"),
                    timeout=None if timeout is None else float(timeout),
                )
                return {"session": None if view is None else view.to_wire(), "timed_out": timed_out}
            case "dsh/resume":
                return {"session": self.resume(_required_str(params, "session_id")).to_wire()}
            case "dsh/cancel":
                return {"cancelled": self.cancel(_required_str(params, "session_id"))}
            case "dsh/requests":
                return {"requests": [item.to_wire() for item in self.requests()]}
            case "dsh/approve":
                pending = self.approve(_required_str(params, "request_id"), _required_str(params, "outcome"))
                return {"request": pending.to_wire()}
            case "dsh/answer":
                answers = params.get("answers")
                if not isinstance(answers, (dict, str)):
                    raise ValueError("answers must be a string or an object of question id → text")
                pending = self.answer(_required_str(params, "request_id"), answers)
                return {"request": pending.to_wire()}
            case "dsh/decline":
                return {"request": self.decline(_required_str(params, "request_id")).to_wire()}
            case "dsh/models":
                return {"providers": self.gateway.list_models()}
            case "dsh/credentials/describe":
                return self.gateway.describe_credential(_required_str(params, "ref"))
            case "dsh/credentials/set":
                self.gateway.set_credential(_required_str(params, "ref"), _optional_str(params.get("value")))
                return {"ok": True}
            case _:
                raise _MethodNotFound(f"unknown dsh daemon method: {method}")


class _MethodNotFound(Exception):
    pass


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _required_str(params: dict[str, Any], key: str) -> str:
    value = _optional_str(params.get(key))
    if value is None:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _spawn(target: Callable[[], None], name: str) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


# -- client ------------------------------------------------------------------


class DaemonUnavailable(RuntimeError):
    """No running daemon is published for this user root."""


class DshDaemonClient:
    """``lara dsh`` side of the loopback channel."""

    def __init__(self, channel: SessionControlChannel) -> None:
        self._channel = channel

    @classmethod
    def connect(cls, state_dir: Path, *, home: Path | None = None) -> "DshDaemonClient":
        path = Path(state_dir) / DAEMON_STATE_FILENAME
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            port = int(state["port"])
            published_home = Path(state["home"])
            if not 0 < port <= 65535:
                raise ValueError("invalid daemon port")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DaemonUnavailable(f"no dsh daemon is published at {path}") from exc
        channel = SessionControlChannel(port, timeout=None)
        try:
            channel.connect(deadline_seconds=1.0)
            identity = channel.call("dsh/ping", timeout=1.0)
            reported_home = identity.get("home")
            if (
                identity.get("pid") != state.get("pid")
                or not isinstance(reported_home, str)
                or Path(reported_home) != published_home
                or (home is not None and Path(reported_home) != home)
            ):
                raise SessionControlError("daemon identity does not match the requested home")
        except SessionControlError as exc:
            channel.close()
            raise DaemonUnavailable(f"dsh daemon at port {port} is not answering: {exc}") from exc
        return cls(channel)

    def close(self) -> None:
        self._channel.close()

    def __enter__(self) -> "DshDaemonClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def send(self, prompt: str, *, session_id: str | None = None, cwd: str | Path | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"prompt": prompt}
        if session_id:
            params["session_id"] = session_id
        if cwd:
            params["cwd"] = str(Path(cwd).expanduser().resolve())
        return self._channel.call("dsh/send", params)

    def sessions(self) -> list[SessionView]:
        return [SessionView.from_wire(item) for item in self._channel.call("dsh/sessions")["sessions"]]

    def status(self, session_id: str) -> SessionView | None:
        payload = self._channel.call("dsh/status", {"session_id": session_id})["session"]
        return None if payload is None else SessionView.from_wire(payload)

    def wait(self, session_id: str, *, timeout: float | None = None) -> tuple[SessionView | None, bool]:
        params: dict[str, Any] = {"session_id": session_id}
        if timeout is not None:
            params["timeout"] = timeout
        result = self._channel.call("dsh/wait", params)
        payload = result["session"]
        return (None if payload is None else SessionView.from_wire(payload)), bool(result["timed_out"])

    def resume(self, session_id: str) -> SessionView:
        return SessionView.from_wire(self._channel.call("dsh/resume", {"session_id": session_id})["session"])

    def cancel(self, session_id: str) -> bool:
        return bool(self._channel.call("dsh/cancel", {"session_id": session_id})["cancelled"])

    def requests(self) -> list[dict[str, Any]]:
        return list(self._channel.call("dsh/requests")["requests"])

    def approve(self, request_id: str, outcome: str = "allowed-once") -> dict[str, Any]:
        return self._channel.call("dsh/approve", {"request_id": request_id, "outcome": outcome})["request"]

    def answer(self, request_id: str, answers: dict[str, str] | str) -> dict[str, Any]:
        return self._channel.call("dsh/answer", {"request_id": request_id, "answers": answers})["request"]

    def decline(self, request_id: str) -> dict[str, Any]:
        return self._channel.call("dsh/decline", {"request_id": request_id})["request"]

    def models(self) -> list[dict[str, Any]]:
        return list(self._channel.call("dsh/models")["providers"])

    def describe_credential(self, ref: str) -> dict[str, Any]:
        return self._channel.call("dsh/credentials/describe", {"ref": ref})

    def set_credential(self, ref: str, value: str | None) -> None:
        self._channel.call("dsh/credentials/set", {"ref": ref, "value": value or ""})


__all__ = [
    "DAEMON_STATE_FILENAME",
    "DaemonUnavailable",
    "DshDaemon",
    "DshDaemonClient",
    "DshEvent",
    "DshEventBatch",
    "DshQuestion",
    "PendingRequest",
    "SessionView",
]
