"""Drive DeepSeek Harness (dsh) sessions: attach to a running dsh, or spawn one.

Both gateways talk to Lara's ``lara-session-control`` Cordis plugin
(``dsh_plugin/lara-session-control``) over a loopback JSON-RPC channel and
share one method surface (:class:`DshRuntimeGateway`).

:class:`AttachedDshGateway` connects to the dsh the user already runs
(desktop or ``dsh web``) with the plugin installed in its profile. The plugin
publishes its port to ``<dsh_home>/lara/dsh-plugin.json``; prompts, session
events, approvals and user questions all travel over that one socket, in the
same shapes as the dsh SDK wire. Sessions Lara creates there are ordinary
sessions of that UI.

:class:`DshGateway` owns a private dsh runtime subprocess (``dsh --profile
sdk``) through the official Python SDK, with the plugin layered on via
``--patch``; the SDK wire carries prompts, events, approvals and questions and
the plugin adds the session inventory. :func:`open_gateway` picks attach when
a live plugin is published and spawn otherwise.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig, RunResult
from deepseek_harness.client import NotificationSubscription, UserQuestionHandler
from deepseek_harness.models import IncomingRequest, Notification, UserQuestionRequest

from lara_shell import user_lara_root

DEFAULT_DSH_PROVIDER = "deepseek-official"
DEFAULT_DSH_MODEL = "deepseek-v4-flash"

SESSION_CONTROL_PLUGIN_DIR = Path(__file__).resolve().parent / "dsh_plugin" / "lara-session-control"
SESSION_CONTROL_PATCH = SESSION_CONTROL_PLUGIN_DIR / "cordis.patch.yml"
CONTROL_PORT_ENV = "LARA_DSH_CONTROL_PORT"
# Written by the plugin when it runs inside a dsh Lara did not spawn.
PLUGIN_ENDPOINT_FILENAME = "dsh-plugin.json"


@dataclass(frozen=True)
class DshGatewayConfig:
    dsh_home: Path
    cwd: Path
    provider: str = DEFAULT_DSH_PROVIDER
    model: str = DEFAULT_DSH_MODEL
    api_key: str | None = None
    base_url: str | None = None
    runtime_cwd: Path | None = None
    profile: str = "sdk"

    @classmethod
    def from_env(cls, dsh_home: str | Path, cwd: str | Path) -> "DshGatewayConfig":
        """Read provider credentials from the process environment.

        ``DEEPSEEK_BASE_URL`` / ``DEEPSEEK_API_KEY`` select any
        OpenAI-compatible endpoint (the dsh runtime speaks the DeepSeek
        wire protocol against it); ``DSH_MODEL`` overrides the model id.
        """
        runtime_cwd = os.environ.get("DSH_RUNTIME_CWD")
        return cls(
            dsh_home=Path(dsh_home),
            cwd=Path(cwd),
            model=os.environ.get("DSH_MODEL") or DEFAULT_DSH_MODEL,
            api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
            base_url=os.environ.get("DEEPSEEK_BASE_URL") or None,
            runtime_cwd=Path(runtime_cwd) if runtime_cwd else None,
        )


@dataclass(frozen=True)
class DshSessionEntry:
    """One session known to the dsh runtime: persisted, live, or both."""

    session_id: str
    cwd: str | None
    created_at: int
    origin: str
    parent_session_id: str | None
    event_count: int | None
    title: str | None
    live: bool
    status: str | None

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> "DshSessionEntry":
        return cls(
            session_id=str(payload["sessionId"]),
            cwd=payload.get("cwd"),
            created_at=int(payload["createdAt"]),
            origin=str(payload.get("origin") or "root"),
            parent_session_id=payload.get("parentSessionId"),
            event_count=payload.get("eventCount"),
            title=payload.get("title"),
            live=bool(payload.get("live")),
            status=payload.get("status"),
        )


def ensure_primary_runtime() -> Path:
    """Download the bundled dsh runtime resources once; reuse the cache."""
    from deepseek_harness_runtime import download_primary_runtime

    return download_primary_runtime()


class SessionControlError(RuntimeError):
    """The lara-session-control plugin rejected a request or is unreachable."""


ControlMessageHandler = Callable[[dict[str, Any]], None]


class SessionControlChannel:
    """Newline-delimited JSON-RPC client for the lara-session-control plugin.

    One socket carries this side's request/response pairs (matched by id)
    and, once this connection has subscribed, plugin→client traffic: every
    incoming message that carries a ``method`` (notification or request) is
    handed to ``on_message`` on the reader thread; requests are answered
    with :meth:`respond` / :meth:`respond_error`.
    """

    def __init__(
        self,
        port: int,
        *,
        timeout: float | None = 30.0,
        on_message: ControlMessageHandler | None = None,
    ) -> None:
        self._port = port
        self._timeout = timeout
        self._on_message = on_message
        self._sock: socket.socket | None = None
        self._reader: Any = None
        self._reader_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _PendingCall] = {}
        self._next_id = 0
        self._closed = threading.Event()

    @property
    def port(self) -> int:
        return self._port

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def connect(self, *, deadline_seconds: float = 10.0) -> None:
        """Connect to the plugin, retrying until the runtime has opened the port."""
        deadline = time.monotonic() + deadline_seconds
        while True:
            try:
                sock = socket.create_connection(("127.0.0.1", self._port), timeout=self._timeout)
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise SessionControlError(
                        f"lara-session-control is not listening on 127.0.0.1:{self._port}: {error}"
                    ) from error
                time.sleep(0.1)
                continue
            sock.settimeout(None)
            self._sock = sock
            self._reader = sock.makefile("r", encoding="utf-8", newline="\n")
            self._reader_thread = threading.Thread(
                target=self._read_loop, name="dsh-control-reader", daemon=True
            )
            self._reader_thread.start()
            return

    def close(self) -> None:
        sock = self._sock
        self._closed.set()
        if sock is None:
            return
        self._sock = None
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        self._fail_pending("session control channel closed")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        pending = _PendingCall()
        with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
            message["id"] = request_id
            self._pending[request_id] = pending
        try:
            self._send(message)
        except SessionControlError:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        if not pending.done.wait(self._timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise SessionControlError(f"{method}: no response from lara-session-control within {self._timeout}s")
        if pending.error is not None:
            raise SessionControlError(f"{method}: {pending.error}")
        result = pending.result
        if not isinstance(result, dict):
            raise SessionControlError(f"{method}: result must be a JSON object, got {result!r}")
        return result

    def respond(self, request_id: str | int, result: dict[str, Any]) -> None:
        """Answer a plugin→client request seen through ``on_message``."""
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def respond_error(self, request_id: str | int, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _send(self, message: dict[str, Any]) -> None:
        sock = self._sock
        if sock is None:
            raise SessionControlError("session control channel is not connected")
        with self._write_lock:
            try:
                sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
            except OSError as error:
                raise SessionControlError(f"session control channel write failed: {error}") from error

    def _read_loop(self) -> None:
        reader = self._reader
        try:
            for line in reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if isinstance(message.get("method"), str):
                    if self._on_message is not None:
                        self._on_message(message)
                elif message.get("id") is not None:
                    self._settle(message)
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            self._fail_pending("session control channel closed by the dsh runtime")

    def _settle(self, message: dict[str, Any]) -> None:
        with self._pending_lock:
            pending = self._pending.pop(message["id"], None)
        if pending is None:
            return
        if "error" in message:
            error = message["error"] or {}
            pending.error = f"{error.get('message')} (code {error.get('code')})"
        else:
            pending.result = message.get("result")
        pending.done.set()

    def _fail_pending(self, reason: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = reason
            item.done.set()


class _PendingCall:
    __slots__ = ("done", "result", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Any = None
        self.error: str | None = None


@contextmanager
def _without_inherited_deepseek_env(config: DshGatewayConfig) -> Iterator[None]:
    """Keep Lara's exported DEEPSEEK_* out of the dsh subprocess unless configured.

    The shell exports every stored credential into this process, and the SDK
    launches dsh with a copy of ``os.environ`` that can only be extended. A
    gateway without an explicit key/endpoint wants dsh to use its own
    credential store (``$DSH_HOME/.credentials.yaml``), which an inherited
    stale ``DEEPSEEK_API_KEY`` would shadow and an inherited
    ``DEEPSEEK_BASE_URL`` would redirect. Only the launch window is affected.
    """
    removed: dict[str, str] = {}
    for name, configured in (("DEEPSEEK_API_KEY", config.api_key), ("DEEPSEEK_BASE_URL", config.base_url)):
        if configured is None and name in os.environ:
            removed[name] = os.environ.pop(name)
    try:
        yield
    finally:
        os.environ.update(removed)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def assistant_text(data: dict[str, Any]) -> str:
    """Text blocks of one ``assistant/message`` session event's payload."""
    message = data.get("message") or {}
    parts = [
        str(block.get("text") or "")
        for block in message.get("content") or ()
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part).strip()


class DshRuntimeGateway:
    """What Lara needs from a dsh runtime, however it is reached.

    Subclasses supply :attr:`control` (the plugin channel) plus the prompt,
    event and request plumbing; the inventory, cancellation, model catalog
    and credential methods below are the plugin's and shared.
    """

    mode: str = ""

    def __init__(self, config: DshGatewayConfig) -> None:
        self._config = config
        self._control: SessionControlChannel | None = None

    @property
    def config(self) -> DshGatewayConfig:
        return self._config

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def start(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    @property
    def control(self) -> SessionControlChannel:
        if self._control is None:
            raise SessionControlError("gateway is not started")
        return self._control

    # -- prompting ------------------------------------------------------------

    def run(self, prompt: str, *, session_id: str | None = None) -> tuple[str, str]:
        """Run one prompt to completion; return ``(final_response, session_id)``."""
        raise NotImplementedError

    def resume_run(self, session_id: str, prompt: str) -> tuple[str, str]:
        """Restore a persisted session and continue it with one prompt."""
        raise NotImplementedError

    def submit(self, session_id: str, prompt: str, *, cwd: str | Path | None = None) -> str:
        """Queue one prompt without waiting for the turn; return the message id.

        An unknown ``session_id`` creates the session, rooted at ``cwd`` when
        given. Progress arrives through :meth:`notifications`; use
        :meth:`stat_session` for the live status.
        """
        raise NotImplementedError

    def resume(self, session_id: str) -> None:
        """Restore a persisted session into the runtime without prompting it."""
        raise NotImplementedError

    def set_working_directory(self, session_id: str, path: str | Path) -> str:
        """Point a session at ``path``; return the effective cwd."""
        raise NotImplementedError

    # -- events and runtime→host requests ----------------------------------------

    def notifications(self) -> "EventSubscription":
        """Runtime-wide ``session.event`` / ``session.status`` notifications."""
        raise NotImplementedError

    def next_request(self) -> IncomingRequest:
        """Block for the next runtime→host request (``approval/request``).

        Raises once the runtime closes. User questions never appear here when
        ``on_user_question`` is set; they are answered on their own thread.
        """
        raise NotImplementedError

    def respond(self, request_id: str | int, result: dict[str, Any]) -> None:
        """Answer a request from :meth:`next_request` (approval: ``{"outcome": ...}``)."""
        raise NotImplementedError

    # -- plugin-served, shared -----------------------------------------------

    def list_sessions(self) -> list[DshSessionEntry]:
        """Every session the runtime knows: persisted in ``dsh_home`` or live."""
        self.start()
        result = self.control.call("lara/session/list")
        return [DshSessionEntry.from_wire(item) for item in result["sessions"]]

    def stat_session(self, session_id: str) -> DshSessionEntry | None:
        self.start()
        result = self.control.call("lara/session/stat", {"sessionId": session_id})
        payload = result.get("session")
        return None if payload is None else DshSessionEntry.from_wire(payload)

    def cancel_session(self, session_id: str) -> bool:
        """Cancel the live agent's current work; False when the session is not live."""
        self.start()
        return bool(self.control.call("lara/session/cancel", {"sessionId": session_id})["cancelled"])

    def list_models(self) -> list[dict[str, Any]]:
        """Providers registered in the runtime's llm service with their model catalogs."""
        self.start()
        return list(self.control.call("lara/models/list")["providers"])

    def describe_credential(self, ref: str) -> dict[str, Any]:
        self.start()
        return self.control.call("lara/credentials/describe", {"ref": ref})

    def resolve_credential(self, ref: str) -> str | None:
        """The secret dsh would use for ``ref`` (file store, then its launch env)."""
        self.start()
        value = self.control.call("lara/credentials/resolve", {"ref": ref}).get("value")
        return value if isinstance(value, str) and value else None

    def set_credential(self, ref: str, value: str | None) -> None:
        """Store (or with ``None``/empty, remove) a secret in dsh's credential file."""
        self.start()
        self.control.call("lara/credentials/set", {"ref": ref, "value": value or ""})


class EventSubscription:
    """Queue-backed reader with the SDK subscription's ``next``/``close`` shape."""

    def __init__(self, on_close: Callable[["EventSubscription"], None] | None = None) -> None:
        self._items: queue.Queue[Notification | BaseException] = queue.Queue()
        self._on_close = on_close
        self._closed = False

    def put(self, item: Notification | BaseException) -> None:
        self._items.put(item)

    def next(self, timeout_seconds: float | None = None) -> Notification:
        try:
            item = self._items.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise TimeoutError("timed out waiting for a dsh notification") from exc
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._on_close is not None:
            self._on_close(self)


class _SdkEventSubscription(EventSubscription):
    """Adapts the SDK's :class:`NotificationSubscription` to :class:`EventSubscription`."""

    def __init__(self, inner: NotificationSubscription) -> None:
        super().__init__()
        self._inner = inner

    def next(self, timeout_seconds: float | None = None) -> Notification:
        return self._inner.next(timeout_seconds)

    def close(self) -> None:
        self._inner.close()


class DshGateway(DshRuntimeGateway):
    """Own one private dsh runtime subprocess (spawn mode)."""

    mode = "spawn"

    def __init__(
        self,
        config: DshGatewayConfig,
        *,
        approval_requests: bool = False,
        on_user_question: UserQuestionHandler | None = None,
    ) -> None:
        """``approval_requests`` routes sandbox escalations to :meth:`next_request`;
        ``on_user_question`` answers ``ask_user_question`` (blocking, on an SDK
        thread, with a cancellation event). Without them the runtime's own
        fail-closed answerers apply."""
        super().__init__(config)
        self._approval_requests = approval_requests
        self._on_user_question = on_user_question
        self._harness: DeepSeekHarness | None = None

    def start(self) -> None:
        if self._harness is not None:
            return
        # The compiled dsh runtime extracts its embedded node_modules to
        # "<runtime subprocess cwd drive>:\snapshot\node\..." and loads native
        # modules from there. Keep that extraction on a stable, always-
        # writable drive instead of the agent workspace's drive: with an
        # E:-drive workspace the extraction lands on E:\snapshot and every
        # turn fails with a koffi.node load error.
        runtime_cwd = self._config.runtime_cwd or (user_lara_root() / "dsh-runtime-cwd")
        runtime_cwd.mkdir(parents=True, exist_ok=True)
        self._config.dsh_home.mkdir(parents=True, exist_ok=True)
        control_port = _free_loopback_port()
        config = DeepSeekHarnessConfig(
            provider=self._config.provider,
            model=self._config.model,
            cwd=str(self._config.cwd),
            runtime_cwd=str(runtime_cwd),
            dsh_home=str(self._config.dsh_home),
            profile=self._config.profile,
            patches=(str(SESSION_CONTROL_PATCH),),
            env={CONTROL_PORT_ENV: str(control_port)},
            base_url=self._config.base_url,
            api_key=self._config.api_key,
            approval_requests=self._approval_requests,
            on_user_question=self._on_user_question,
        )
        harness = DeepSeekHarness(config)
        with _without_inherited_deepseek_env(self._config):
            harness.start()
        self._harness = harness
        control = SessionControlChannel(control_port)
        try:
            control.connect()
        except SessionControlError:
            control.close()
            self.close()
            raise
        self._control = control

    def _client(self) -> Any:
        self.start()
        assert self._harness is not None
        return self._harness.client

    def run(self, prompt: str, *, session_id: str | None = None) -> tuple[str, str]:
        self.start()
        assert self._harness is not None
        result = self._harness.run(prompt, session_id=session_id)
        if self._is_bootstrap_race(result):
            # The first turn after a runtime boot can race the node_modules
            # snapshot extraction; rerunning on the same durable session
            # succeeds once the snapshot is in place.
            result = self._harness.run(prompt, session_id=result.session_id)
        return result.final_response, result.session_id

    def resume_run(self, session_id: str, prompt: str) -> tuple[str, str]:
        self.start()
        assert self._harness is not None
        session = self._harness.resume_session(session_id)
        result = session.run(prompt)
        if self._is_bootstrap_race(result):
            result = session.run(prompt)
        return result.final_response, result.session_id

    def submit(self, session_id: str, prompt: str, *, cwd: str | Path | None = None) -> str:
        if cwd is not None:
            self.create_session(session_id, cwd)
        return self._client().session_prompt(session_id, [{"type": "text", "text": prompt}])

    def resume(self, session_id: str) -> None:
        self._client().resume_session(session_id)

    def create_session(self, session_id: str, cwd: str | Path) -> DshSessionEntry:
        """Create an idle session rooted at ``cwd`` and bring it live.

        The SDK stamps every session it creates with the runtime's one
        ``initialize`` cwd, and that header cwd is the sandbox's write
        boundary for the session's life. The control plugin persists the
        header with the requested directory instead; resuming it here makes
        it an ordinary SDK-owned session that works inside ``cwd``.
        """
        self.start()
        self.control.call(
            "lara/session/create",
            {"sessionId": session_id, "cwd": str(Path(cwd).resolve())},
        )
        self.resume(session_id)
        entry = self.stat_session(session_id)
        assert entry is not None
        return entry

    def next_request(self) -> IncomingRequest:
        return self._client().next_request()

    def respond(self, request_id: str | int, result: dict[str, Any]) -> None:
        assert self._harness is not None
        self._harness.client.respond(request_id, result)

    def set_working_directory(self, session_id: str, path: str | Path) -> str:
        """Point a session (creating it when unknown) at ``path``; return the effective cwd."""
        return self._client().set_working_directory(session_id, str(path))

    def notifications(self) -> EventSubscription:
        return _SdkEventSubscription(self._client().subscribe_notifications())

    @staticmethod
    def _is_bootstrap_race(result: RunResult) -> bool:
        return result.finish_reason == "error" and not result.final_response

    def close(self) -> None:
        if self._control is not None:
            self._control.close()
            self._control = None
        if self._harness is not None:
            self._harness.close()
            self._harness = None


# -- attach mode ----------------------------------------------------------------


@dataclass(frozen=True)
class PluginEndpoint:
    """Where the plugin listens (``<dsh_home>/lara/dsh-plugin.json``) and what ``lara/ping`` said.

    ``attach`` is True only after a ping confirmed ``sessionController`` is
    mounted there, i.e. prompts can be delivered.
    """

    port: int
    pid: int | None
    version: str | None
    attach: bool = False

    @classmethod
    def from_file(cls, dsh_home: Path) -> "PluginEndpoint | None":
        path = Path(dsh_home) / "lara" / PLUGIN_ENDPOINT_FILENAME
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            port = int(record["port"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        pid = record.get("pid")
        return cls(
            port=port,
            pid=int(pid) if isinstance(pid, int) else None,
            version=str(record["version"]) if isinstance(record.get("version"), str) else None,
        )


def probe_plugin_endpoint(dsh_home: Path, *, deadline_seconds: float = 0.5) -> PluginEndpoint | None:
    """The published plugin endpoint if a dsh is answering on it, else None."""
    endpoint = PluginEndpoint.from_file(dsh_home)
    if endpoint is None:
        return None
    probe = SessionControlChannel(endpoint.port, timeout=2.0)
    try:
        probe.connect(deadline_seconds=deadline_seconds)
        pong = probe.call("lara/ping")
    except SessionControlError:
        return None
    finally:
        probe.close()
    if pong.get("name") != "lara-session-control":
        return None
    return PluginEndpoint(
        port=endpoint.port,
        pid=int(pong["pid"]) if isinstance(pong.get("pid"), int) else endpoint.pid,
        version=str(pong.get("version") or endpoint.version or ""),
        attach=bool(pong.get("attach")),
    )


class AttachedDshGateway(DshRuntimeGateway):
    """Drive the dsh the user already runs, through the plugin installed in it.

    Everything goes over the plugin socket. Once subscribed, the plugin
    pushes ``session.event`` / ``session.status`` and asks ``approval/request``
    and ``user-question/ask`` for the sessions this gateway prompted, in the
    SDK wire's shapes; nothing is subscribed until events or requests are
    asked for, so a one-off inventory call never steals the daemon's wire.
    """

    mode = "attach"

    def __init__(
        self,
        config: DshGatewayConfig,
        *,
        approval_requests: bool = False,
        on_user_question: UserQuestionHandler | None = None,
    ) -> None:
        super().__init__(config)
        self._approval_requests = approval_requests
        self._on_user_question = on_user_question
        self.endpoint: PluginEndpoint | None = None
        self._subscribed = False
        self._lock = threading.Lock()
        self._subscriptions: list[EventSubscription] = []
        self._requests: queue.Queue[IncomingRequest | BaseException] = queue.Queue()
        self._question_cancels: dict[str, threading.Event] = {}

    def start(self) -> None:
        if self._control is not None:
            return
        endpoint = probe_plugin_endpoint(self._config.dsh_home)
        if endpoint is None:
            raise SessionControlError(
                f"no running dsh publishes the Lara plugin under {self._config.dsh_home / 'lara' / PLUGIN_ENDPOINT_FILENAME}"
            )
        if not endpoint.attach:
            raise SessionControlError(
                "the running dsh has the Lara plugin but no sessionController (sdk profile); "
                "install the plugin into the web profile to attach"
            )
        control = SessionControlChannel(endpoint.port, on_message=self._on_message)
        control.connect(deadline_seconds=2.0)
        self._control = control
        self.endpoint = endpoint
        if self._approval_requests or self._on_user_question is not None:
            self._subscribe()

    def close(self) -> None:
        control = self._control
        self._control = None
        if control is not None:
            control.close()
        self._fail_followers(SessionControlError("attached dsh gateway closed"))

    def _subscribe(self) -> None:
        if not self._subscribed:
            self.control.call("lara/events/subscribe")
            self._subscribed = True

    # -- prompting ------------------------------------------------------------

    def submit(self, session_id: str, prompt: str, *, cwd: str | Path | None = None) -> str:
        self.start()
        # provider/model take effect only on a session the plugin creates for
        # this prompt: Lara's configured model for Lara's sessions, the user's
        # own choice untouched on theirs.
        params: dict[str, Any] = {
            "text": prompt,
            "sessionId": session_id,
            "provider": self._config.provider,
            "model": self._config.model,
        }
        if cwd is not None:
            params["cwd"] = str(Path(cwd).resolve())
        return str(self.control.call("lara/session/prompt", params)["messageId"])

    def select_model(self, session_id: str, provider: str, model: str) -> None:
        """Switch one session's model (session-local; dsh's saved default is untouched)."""
        self.start()
        self.control.call(
            "lara/session/select-model",
            {"sessionId": session_id, "provider": provider, "model": model},
        )

    def resume(self, session_id: str) -> None:
        self.start()
        self.control.call("lara/session/resume", {"sessionId": session_id})

    def set_working_directory(self, session_id: str, path: str | Path) -> str:
        """A session's directory is fixed at creation; only confirm it matches."""
        entry = self.stat_session(session_id)
        if entry is None:
            raise SessionControlError(f"session {session_id} does not exist in the attached dsh")
        requested = str(Path(path).resolve())
        if entry.cwd is not None and Path(entry.cwd).resolve() != Path(requested):
            raise SessionControlError(
                f"session {session_id} is rooted at {entry.cwd}; the attached dsh keeps a session's directory fixed"
            )
        return entry.cwd or requested

    def run(self, prompt: str, *, session_id: str | None = None) -> tuple[str, str]:
        self.start()
        self._subscribe()
        target = session_id or f"dsh-{uuid.uuid4().hex[:12]}"
        events = self.notifications()
        try:
            self.submit(target, prompt, cwd=None if session_id else self._config.cwd)
            return self._await_turn(events, target), target
        finally:
            events.close()

    def resume_run(self, session_id: str, prompt: str) -> tuple[str, str]:
        self.resume(session_id)
        return self.run(prompt, session_id=session_id)

    def _await_turn(self, events: EventSubscription, session_id: str) -> str:
        reply: list[str] = []
        while True:
            notification = events.next()
            if notification.method != "session.event" or str(notification.payload.get("sessionId")) != session_id:
                continue
            event = notification.payload.get("event")
            if not isinstance(event, dict):
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                data = {}
            if event.get("type") == "assistant/message":
                text = assistant_text(data)
                if text:
                    reply.append(text)
            elif event.get("type") == "turn/end":
                return "\n".join(reply).strip()

    # -- events and runtime→host requests ----------------------------------------

    def notifications(self) -> EventSubscription:
        self.start()
        self._subscribe()
        subscription = EventSubscription(on_close=self._drop_subscription)
        with self._lock:
            self._subscriptions.append(subscription)
        return subscription

    def _drop_subscription(self, subscription: EventSubscription) -> None:
        with self._lock:
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)

    def next_request(self) -> IncomingRequest:
        self.start()
        self._subscribe()
        item = self._requests.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def respond(self, request_id: str | int, result: dict[str, Any]) -> None:
        self.control.respond(request_id, result)

    def _on_message(self, message: dict[str, Any]) -> None:
        method = str(message["method"])
        params = message.get("params")
        payload = params if isinstance(params, dict) else {}
        request_id = message.get("id")
        if request_id is None:
            if method == "user-question.cancelled":
                cancelled = self._question_cancels.get(str(payload.get("requestId")))
                if cancelled is not None:
                    cancelled.set()
                return
            notification = Notification(method=method, payload=payload)
            with self._lock:
                subscribers = list(self._subscriptions)
            for subscriber in subscribers:
                subscriber.put(notification)
            return
        incoming = IncomingRequest(id=request_id, method=method, payload=payload)
        if method == "user-question/ask" and self._on_user_question is not None:
            self._start_user_question(incoming)
        else:
            self._requests.put(incoming)

    def _start_user_question(self, incoming: IncomingRequest) -> None:
        """Answer on a worker so the reader stays free for cancellation and events."""
        handler = self._on_user_question
        assert handler is not None
        try:
            request = UserQuestionRequest.model_validate(incoming.payload)
        except ValueError:
            self.control.respond_error(incoming.id, -32602, "invalid user-question/ask parameters")
            return
        cancelled = threading.Event()
        self._question_cancels[request.request_id] = cancelled

        def answer() -> None:
            try:
                result = handler(request, cancelled)
                wire = result if isinstance(result, dict) else result.model_dump(exclude_none=True)
                if not cancelled.is_set():
                    self.control.respond(incoming.id, wire)
            except Exception as exc:
                if not cancelled.is_set():
                    try:
                        self.control.respond_error(incoming.id, -32603, str(exc))
                    except SessionControlError:
                        pass
            finally:
                self._question_cancels.pop(request.request_id, None)

        threading.Thread(target=answer, name="dsh-attached-question", daemon=True).start()

    def _fail_followers(self, error: BaseException) -> None:
        with self._lock:
            subscribers = list(self._subscriptions)
            self._subscriptions.clear()
            cancels = list(self._question_cancels.values())
            self._question_cancels.clear()
        for subscriber in subscribers:
            subscriber.put(error)
        for cancelled in cancels:
            cancelled.set()
        self._requests.put(error)


def open_gateway(
    config: DshGatewayConfig,
    *,
    approval_requests: bool = False,
    on_user_question: UserQuestionHandler | None = None,
) -> DshRuntimeGateway:
    """Attach to the running dsh when its Lara plugin answers; else spawn one.

    The returned gateway is not started. Attach is chosen only when the
    published plugin runs in a profile with ``sessionController`` (web or
    desktop), where prompts can be delivered.
    """
    endpoint = probe_plugin_endpoint(config.dsh_home)
    if endpoint is not None and endpoint.attach:
        return AttachedDshGateway(config, approval_requests=approval_requests, on_user_question=on_user_question)
    return DshGateway(config, approval_requests=approval_requests, on_user_question=on_user_question)
