"""Attach mode: Lara drives a dsh it did not spawn through the published plugin.

A fake plugin server speaks the lara-session-control wire in both directions
(answers requests, pushes session events, asks approvals and questions), so
no dsh runtime boots here; the real plugin is exercised in
tests/test_dsh_attach_real.py.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lara_shell.dsh_daemon import DshDaemon, DshDaemonClient, DshEvent, DshEventBatch
from lara_shell.dsh_gateway import (
    PLUGIN_ENDPOINT_FILENAME,
    AttachedDshGateway,
    DshGateway,
    DshGatewayConfig,
    SessionControlError,
    open_gateway,
    probe_plugin_endpoint,
)


class FakePlugin:
    """index.js stand-in: one accepted peer at a time, full-duplex JSON-RPC."""

    def __init__(self, dsh_home: Path, *, attach: bool = True) -> None:
        self.dsh_home = dsh_home
        self.attach = attach
        self.received: list[dict[str, Any]] = []
        self.prompts: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []  # responses to plugin→client requests
        self.subscribed = threading.Event()
        self.sessions: dict[str, dict[str, Any]] = {}
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = int(self._listener.getsockname()[1])
        self._conn: socket.socket | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[str, threading.Event] = {}
        self._results: dict[str, Any] = {}
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def publish(self) -> Path:
        path = self.dsh_home / "lara" / PLUGIN_ENDPOINT_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"port": self.port, "pid": 4242, "version": "0.4.0", "startedAt": 1}), encoding="utf-8")
        return path

    def close(self) -> None:
        self._listener.close()
        conn = self._conn
        if conn is not None:
            conn.close()

    # -- plugin → client ------------------------------------------------------

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def event(self, session_id: str, kind: str, data: dict[str, Any]) -> None:
        self.notify("session.event", {"sessionId": session_id, "event": {"type": kind, "data": data}})

    def finish_turn(self, session_id: str, reply: str) -> None:
        self.notify("session.status", {"sessionId": session_id, "status": "running"})
        self.event(session_id, "turn/start", {"turn": 1})
        self.event(session_id, "assistant/message", {"message": {"role": "assistant", "content": [{"type": "text", "text": reply}]}})
        self.event(session_id, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        self.notify("session.status", {"sessionId": session_id, "status": "idle"})

    def request(self, method: str, params: dict[str, Any]) -> str:
        with self._lock:
            self._next_id += 1
            request_id = f"plugin-{self._next_id}"
            self._pending[request_id] = threading.Event()
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return request_id

    def result(self, request_id: str, timeout: float = 5) -> Any:
        assert self._pending[request_id].wait(timeout), f"no answer to {request_id}"
        return self._results[request_id]

    def _write(self, message: dict[str, Any]) -> None:
        conn = self._conn
        assert conn is not None, "no client connected"
        conn.sendall((json.dumps(message) + "\n").encode("utf-8"))

    # -- serving --------------------------------------------------------------

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn, conn.makefile("r", encoding="utf-8") as reader:
            for line in reader:
                message = json.loads(line)
                if "method" not in message:
                    request_id = str(message["id"])
                    self._results[request_id] = message.get("result", message.get("error"))
                    self._pending[request_id].set()
                    continue
                self.received.append(message)
                try:
                    result = self._dispatch(conn, message["method"], message.get("params") or {})
                    reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
                except KeyError:
                    reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"unknown lara-session-control method: {message['method']}"}}
                except Exception as exc:
                    reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": str(exc)}}
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))

    def _dispatch(self, conn: socket.socket, method: str, params: dict[str, Any]) -> dict[str, Any]:
        match method:
            case "lara/ping":
                return {"ok": True, "name": "lara-session-control", "version": "0.4.0", "pid": 4242, "attach": self.attach}
            case "lara/events/subscribe":
                self._conn = conn
                self.subscribed.set()
                return {"ok": True}
            case "lara/session/prompt":
                if not self.attach:
                    raise RuntimeError("the sessionController service is not mounted in this profile")
                self.prompts.append(params)
                session_id = params.get("sessionId") or "created-by-plugin"
                created = session_id not in self.sessions
                if created:
                    self.sessions[session_id] = {"sessionId": session_id, "cwd": params.get("cwd"), "createdAt": 1, "origin": "root", "live": True, "status": "running"}
                return {"sessionId": session_id, "messageId": f"req-{len(self.prompts)}", "created": created}
            case "lara/session/resume":
                return {"sessionId": params["sessionId"], "status": "idle"}
            case "lara/model/route":
                return {
                    "default": {"provider": "deepseek-official", "model": "deepseek-flash"},
                    "providers": [{"id": "deepseek-official", "name": "DeepSeek", "models": [{"id": "deepseek-flash", "name": "V4.1-Flash"}]}],
                    "apiKey": {"configured": True, "value": "sk-test"},
                    "account": {"signedIn": False},
                    "baseURL": "https://api.deepseek.com/v1",
                }
            case "lara/session/list":
                return {"sessions": list(self.sessions.values())}
            case "lara/session/stat":
                return {"session": self.sessions.get(params["sessionId"])}
            case "lara/session/cancel":
                return {"cancelled": params["sessionId"] in self.sessions}
            case _:
                raise KeyError(method)


@pytest.fixture
def plugin(tmp_path: Path) -> Iterator[FakePlugin]:
    fake = FakePlugin(tmp_path / "home")
    fake.publish()
    yield fake
    fake.close()


def _config(tmp_path: Path) -> DshGatewayConfig:
    return DshGatewayConfig(dsh_home=tmp_path / "home", cwd=tmp_path / "ws")


def test_probe_finds_the_published_plugin_and_ignores_a_stale_file(tmp_path: Path, plugin: FakePlugin) -> None:
    endpoint = probe_plugin_endpoint(tmp_path / "home")
    assert endpoint is not None and endpoint.port == plugin.port and endpoint.attach is True
    plugin.close()
    time.sleep(0.05)
    assert probe_plugin_endpoint(tmp_path / "home") is None  # file remains, nobody answers
    assert probe_plugin_endpoint(tmp_path / "elsewhere") is None  # no file at all


def test_open_gateway_attaches_only_to_a_live_plugin_with_prompt_support(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert isinstance(open_gateway(config), DshGateway)  # nothing published: spawn
    sdk_only = FakePlugin(tmp_path / "home", attach=False)
    sdk_only.publish()
    try:
        assert isinstance(open_gateway(config), DshGateway)  # sdk profile: cannot prompt
    finally:
        sdk_only.close()
    web = FakePlugin(tmp_path / "home")
    web.publish()
    try:
        assert isinstance(open_gateway(config), AttachedDshGateway)
    finally:
        web.close()


def test_model_route_exposes_dsh_default_and_credential_material(tmp_path: Path, plugin: FakePlugin) -> None:
    with AttachedDshGateway(_config(tmp_path)) as gateway:
        route = gateway.model_route()
    assert route["default"] == {"provider": "deepseek-official", "model": "deepseek-flash"}
    assert route["apiKey"] == {"configured": True, "value": "sk-test"}
    assert route["account"] == {"signedIn": False}
    assert route["baseURL"] == "https://api.deepseek.com/v1"
    assert route["providers"][0]["models"][0]["id"] == "deepseek-flash"


def test_attached_gateway_requires_a_live_plugin(tmp_path: Path) -> None:
    with pytest.raises(SessionControlError, match="no running dsh"):
        AttachedDshGateway(_config(tmp_path)).start()


def test_inventory_calls_do_not_subscribe(tmp_path: Path, plugin: FakePlugin) -> None:
    with AttachedDshGateway(_config(tmp_path)) as gateway:
        assert gateway.list_sessions() == []
        assert gateway.stat_session("nope") is None
    assert [m["method"] for m in plugin.received] == ["lara/ping", "lara/session/list", "lara/session/stat"]
    assert not plugin.subscribed.is_set()


def test_submit_roots_a_new_session_at_cwd_and_run_waits_for_the_turn(tmp_path: Path, plugin: FakePlugin) -> None:
    with AttachedDshGateway(_config(tmp_path)) as gateway:
        message_id = gateway.submit("s1", "hi", cwd=tmp_path / "proj")
        assert message_id == "req-1"
        # The model is dsh's own default (agentDefaultModel) — no override is sent.
        assert plugin.prompts[0] == {"text": "hi", "sessionId": "s1", "cwd": str((tmp_path / "proj").resolve())}
        # An existing session keeps its directory; a matching path is a no-op, another is refused.
        assert gateway.set_working_directory("s1", tmp_path / "proj") == str((tmp_path / "proj").resolve())
        with pytest.raises(SessionControlError, match="fixed"):
            gateway.set_working_directory("s1", tmp_path / "other")

        done: dict[str, Any] = {}

        def run() -> None:
            done["result"] = gateway.run("again", session_id="s1")

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert plugin.subscribed.wait(5)
        deadline = time.monotonic() + 5
        while len(plugin.prompts) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert plugin.prompts[1]["text"] == "again" and "cwd" not in plugin.prompts[1]
        plugin.finish_turn("other-session", "not yours")
        plugin.finish_turn("s1", "done")
        thread.join(5)
        assert done["result"] == ("done", "s1")


def test_daemon_attaches_and_relays_events_approvals_and_questions(tmp_path: Path, plugin: FakePlugin) -> None:
    events: list[DshEvent | DshEventBatch] = []
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root", on_event=events.append, batch_window_seconds=0) as daemon:
        assert daemon.mode == "attach"
        assert json.loads(daemon.state_path.read_text(encoding="utf-8"))["mode"] == "attach"
        assert plugin.subscribed.wait(5)  # approvals/questions wanted: subscribed at start
        with DshDaemonClient.connect(tmp_path / "root") as client:
            sent = client.send("build it", cwd=str(tmp_path / "proj"))
            session_id = sent["session_id"]
            assert plugin.prompts[-1]["sessionId"] == session_id

            # An approval for the Lara-driven session: surfaced, then answered over the same socket.
            wire_id = plugin.request("approval/request", {"sessionId": session_id, "toolName": "bash", "reason": "rm -rf"})
            deadline = time.monotonic() + 5
            while not client.requests() and time.monotonic() < deadline:
                time.sleep(0.01)
            pending = client.requests()[0]
            assert pending["kind"] == "approval" and pending["tool_name"] == "bash"
            client.approve(pending["request_id"], "allowed-once")
            assert plugin.result(wire_id) == {"outcome": "allowed-once"}

            # A question goes through the blocking handler on its own thread.
            qid = plugin.request("user-question/ask", {"requestId": "q1", "sessionId": session_id, "questions": [{"id": "which", "question": "Which one?", "options": [{"label": "A"}, {"label": "B"}]}]})
            deadline = time.monotonic() + 5
            while not client.requests() and time.monotonic() < deadline:
                time.sleep(0.01)
            question = client.requests()[0]
            assert question["kind"] == "question"
            client.answer(question["request_id"], "A")
            assert plugin.result(qid) == {"answers": [{"id": "which", "selected": ["A"]}]}

            plugin.finish_turn(session_id, "built")
            view, timed_out = client.wait(session_id, timeout=5)
            assert timed_out is False and view is not None and view.reply == "built"

            # The user's own session in that dsh: its status is visible, its turns are not reported.
            plugin.sessions["users-own"] = {"sessionId": "users-own", "cwd": None, "createdAt": 2, "origin": "root", "live": True, "status": "running"}
            plugin.finish_turn("users-own", "mine")
            view, _ = client.wait("users-own", timeout=5)
            assert view is not None and view.status == "idle" and view.reply == "mine"
    finished = [e for e in events if isinstance(e, DshEvent) and e.kind == "turn_finished"]
    assert [e.session_id for e in finished] == [session_id]
    assert finished[0].reply == "built"
    kinds = [e.kind for e in events if isinstance(e, DshEvent)]
    assert kinds[:2] == ["approval_request", "question"]
