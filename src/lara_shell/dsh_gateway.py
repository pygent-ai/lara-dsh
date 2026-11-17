"""Drive DeepSeek Harness (dsh) sessions through the official Python SDK.

One :class:`DshGateway` owns one dsh runtime subprocess (``dsh --profile
sdk``). Sessions created through it are durable inside the configured
``dsh_home``: the same session id can be resumed later from this gateway or
from any other DeepSeek Harness SDK client.

The runtime boots with Lara's ``lara-session-control`` Cordis plugin
(``dsh_plugin/lara-session-control``) layered on top of the profile. The SDK
wire has no session inventory, so that plugin serves one over a loopback
JSON-RPC channel whose port this process picks; live ``session.event`` /
``session.status`` notifications for every session in the runtime already
arrive on the SDK transport and are exposed through :meth:`DshGateway.notifications`.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig, RunResult
from deepseek_harness.client import NotificationSubscription

DEFAULT_DSH_PROVIDER = "deepseek-official"
DEFAULT_DSH_MODEL = "deepseek-v4-flash"

SESSION_CONTROL_PLUGIN_DIR = Path(__file__).resolve().parent / "dsh_plugin" / "lara-session-control"
SESSION_CONTROL_PATCH = SESSION_CONTROL_PLUGIN_DIR / "cordis.patch.yml"
CONTROL_PORT_ENV = "LARA_DSH_CONTROL_PORT"


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


ControlNotificationHandler = Callable[[str, dict[str, Any]], None]


class SessionControlChannel:
    """Newline-delimited JSON-RPC client for the lara-session-control plugin.

    One socket carries request/response pairs (matched by id) and, once this
    connection has registered as answerer, plugin→client notifications
    (approval and question requests) delivered to ``on_notification`` from
    the reader thread.
    """

    def __init__(
        self,
        port: int,
        *,
        timeout: float | None = 30.0,
        on_notification: ControlNotificationHandler | None = None,
    ) -> None:
        self._port = port
        self._timeout = timeout
        self._on_notification = on_notification
        self._sock: socket.socket | None = None
        self._reader: Any = None
        self._reader_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _PendingCall] = {}
        self._next_id = 0

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
        sock = self._sock
        if sock is None:
            raise SessionControlError("session control channel is not connected")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        pending = _PendingCall()
        with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
            message["id"] = request_id
            self._pending[request_id] = pending
        with self._write_lock:
            try:
                sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
            except OSError as error:
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                raise SessionControlError(f"session control channel write failed: {error}") from error
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
                if message.get("id") is not None:
                    self._settle(message)
                elif isinstance(message.get("method"), str) and self._on_notification is not None:
                    params = message.get("params")
                    self._on_notification(message["method"], params if isinstance(params, dict) else {})
        except (OSError, ValueError):
            pass
        finally:
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


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class DshGateway:
    """Own one dsh runtime subprocess and expose its session capabilities."""

    def __init__(
        self,
        config: DshGatewayConfig,
        *,
        on_control_notification: ControlNotificationHandler | None = None,
    ) -> None:
        self._config = config
        self._on_control_notification = on_control_notification
        self._harness: DeepSeekHarness | None = None
        self._control: SessionControlChannel | None = None

    def __enter__(self) -> "DshGateway":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def start(self) -> None:
        if self._harness is not None:
            return
        # The compiled dsh runtime extracts its embedded node_modules to
        # "<runtime subprocess cwd drive>:\snapshot\node\..." and loads native
        # modules from there. Keep that extraction on a stable, always-
        # writable drive instead of the agent workspace's drive: with an
        # E:-drive workspace the extraction lands on E:\snapshot and every
        # turn fails with a koffi.node load error.
        runtime_cwd = self._config.runtime_cwd or (
            Path.home() / ".lara-dsh" / "dsh-runtime-cwd"
        )
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
        )
        harness = DeepSeekHarness(config)
        harness.start()
        self._harness = harness
        control = SessionControlChannel(
            control_port, on_notification=self._on_control_notification
        )
        try:
            control.connect()
            if self._on_control_notification is not None:
                # Only a client that handles notifications may answer for the
                # runtime; without one the approval chain fails closed as before.
                control.call("lara/answerer/register")
        except SessionControlError:
            control.close()
            self.close()
            raise
        self._control = control

    @property
    def control(self) -> SessionControlChannel:
        if self._control is None:
            raise SessionControlError("gateway is not started")
        return self._control

    def run(self, prompt: str, *, session_id: str | None = None) -> tuple[str, str]:
        """Run one prompt and return ``(final_response, session_id)``."""
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
        """Restore a persisted session and continue it with one prompt."""
        self.start()
        assert self._harness is not None
        session = self._harness.resume_session(session_id)
        result = session.run(prompt)
        if self._is_bootstrap_race(result):
            result = session.run(prompt)
        return result.final_response, result.session_id

    def submit(self, session_id: str, prompt: str) -> str:
        """Queue one prompt without waiting for the turn; return the message id.

        An unknown ``session_id`` creates the session. Progress arrives through
        :meth:`notifications`; use :meth:`stat_session` for the live status.
        """
        self.start()
        assert self._harness is not None
        return self._harness.client.session_prompt(
            session_id, [{"type": "text", "text": prompt}]
        )

    def resume(self, session_id: str) -> None:
        """Restore a persisted session into this runtime without prompting it."""
        self.start()
        assert self._harness is not None
        self._harness.client.resume_session(session_id)

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

    def cancel_session(self, session_id: str) -> bool:
        """Cancel the live agent's current work; False when the session is not live."""
        self.start()
        return bool(self.control.call("lara/session/cancel", {"sessionId": session_id})["cancelled"])

    def respond_approval(self, request_id: str, outcome: str) -> None:
        """Settle a ``lara/approval/request``: allowed-once, rejected, cancelled, or unavailable."""
        self.control.call("lara/approval/respond", {"requestId": request_id, "outcome": outcome})

    def respond_question(
        self, request_id: str, answers: list[dict[str, Any]] | None, *, cancelled: bool = False
    ) -> None:
        """Settle a ``lara/question/request`` with per-question answers, or cancel it."""
        params: dict[str, Any] = {"requestId": request_id}
        if cancelled:
            params["cancelled"] = True
        else:
            params["answers"] = answers or []
        self.control.call("lara/question/respond", params)

    def set_working_directory(self, session_id: str, path: str | Path) -> str:
        """Point a session (creating it when unknown) at ``path``; return the effective cwd."""
        self.start()
        assert self._harness is not None
        return self._harness.client.set_working_directory(session_id, str(path))

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

    def notifications(self, session_id: str | None = None) -> NotificationSubscription:
        """Subscribe to SDK notifications: one session tree, or runtime-wide."""
        self.start()
        assert self._harness is not None
        client = self._harness.client
        if session_id is None:
            return client.subscribe_notifications()
        return client.subscribe_session_notifications(session_id)

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
