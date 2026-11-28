"""Tests for the dsh daemon and settings without a dsh runtime.

The gateway is replaced by an in-memory fake that replays recorded SDK
notifications, so the tracker, loopback RPC, client, and event envelopes are
exercised exactly as the real notification thread drives them.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lara_shell import dsh_daemon as daemon_module
from lara_shell.dsh_daemon import DAEMON_STATE_FILENAME, DaemonUnavailable, DshDaemon, DshDaemonClient, DshEvent, DshEventBatch, DshQuestion
from lara_shell.dsh_gateway import DshGatewayConfig, DshSessionEntry, SessionControlError as SessionControlErrorAlias
from lara_shell.dsh_settings import dsh_target


class _Notification:
    def __init__(self, method: str, payload: dict[str, Any]) -> None:
        self.method = method
        self.payload = payload


class _Subscription:
    def __init__(self, source: "queue.Queue[_Notification | None]") -> None:
        self._source = source

    def next(self) -> _Notification:
        item = self._source.get()
        if item is None:
            raise RuntimeError("closed")
        return item


class FakeGateway:
    """Records calls; `emit` replays notifications into the daemon's follower."""

    mode = "fake"

    def __init__(self, config: DshGatewayConfig, *, approval_requests: bool = False, on_user_question=None) -> None:
        self.config = config
        self.approval_requests = approval_requests
        self.on_user_question = on_user_question
        self._requests: queue.Queue[Any] = queue.Queue()
        self.responses: list[tuple[Any, dict[str, Any]]] = []  # (wire id, result)
        self.question_results: dict[str, Any] = {}  # requestId -> answer dict or Exception
        self.cancels: list[str] = []
        self.live: set[str] = set()
        self.started = False
        self.closed = False
        self.submits: list[tuple[str, str]] = []
        self.creates: list[tuple[str, str]] = []
        self.resumes: list[str] = []
        self.inventory: list[DshSessionEntry] = []
        self._queue: queue.Queue[_Notification | None] = queue.Queue()

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True
        self._queue.put(None)
        self._requests.put(RuntimeError("closed"))

    def notifications(self, session_id: str | None = None) -> _Subscription:
        return _Subscription(self._queue)

    def submit(self, session_id: str, prompt: str, *, cwd: str | None = None) -> str:
        if cwd is not None:
            self.create_session(session_id, cwd)
        self.submits.append((session_id, prompt))
        return f"msg-{len(self.submits)}"

    def resume(self, session_id: str) -> None:
        self.resumes.append(session_id)

    def cancel_session(self, session_id: str) -> bool:
        self.cancels.append(session_id)
        return session_id in self.live

    def next_request(self) -> Any:
        item = self._requests.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))

    # -- wire replay helpers -------------------------------------------------

    def approval(self, wire_id: int, session_id: str, tool: str, reason: str | None = None) -> None:
        """An `approval/request` host request arriving on the SDK wire."""
        payload: dict[str, Any] = {"sessionId": session_id, "toolName": tool}
        if reason is not None:
            payload["reason"] = reason
        self._requests.put(SimpleNamespace(id=wire_id, method="approval/request", payload=payload))

    def ask(self, request_id: str, session_id: str, questions: list[dict[str, Any]]) -> threading.Event:
        """A `user-question/ask` host request: the SDK calls the handler on its own thread."""
        handler = self.on_user_question
        assert handler is not None
        cancelled = threading.Event()
        request = SimpleNamespace(request_id=request_id, session_id=session_id, questions=questions)

        def run() -> None:
            try:
                self.question_results[request_id] = handler(request, cancelled)
            except Exception as exc:
                self.question_results[request_id] = exc

        threading.Thread(target=run, daemon=True).start()
        return cancelled

    def create_session(self, session_id: str, cwd: str) -> DshSessionEntry:
        self.creates.append((session_id, cwd))
        entry = _entry(session_id, cwd=cwd, live=True, status="idle")
        self.inventory.append(entry)
        return entry

    def list_sessions(self) -> list[DshSessionEntry]:
        return list(self.inventory)

    def stat_session(self, session_id: str) -> DshSessionEntry | None:
        return next((e for e in self.inventory if e.session_id == session_id), None)

    # -- replay helpers ------------------------------------------------------

    def emit(self, method: str, payload: dict[str, Any]) -> None:
        self._queue.put(_Notification(method, payload))

    def event(self, session_id: str, kind: str, data: dict[str, Any]) -> None:
        self.emit("session.event", {"sessionId": session_id, "event": {"type": kind, "data": data}})

    def finish_turn(self, session_id: str, reply: str, *, reason: dict[str, Any] | None = None, title: str | None = None) -> None:
        self.emit("session.status", {"sessionId": session_id, "status": "running"})
        self.event(session_id, "turn/start", {"turn": 1})
        if title:
            self.event(session_id, "session/title", {"title": title})
        self.event(session_id, "step/start", {"turn": 1, "step": 1})
        self.event(session_id, "tool/call", {"turn": 1, "step": 1, "callId": "c1", "name": "write", "arguments": "{}"})
        self.event(session_id, "step/start", {"turn": 1, "step": 2})
        self.event(session_id, "assistant/message", {"message": {"role": "assistant", "content": [{"type": "reasoning", "text": "thinking"}, {"type": "text", "text": reply}]}})
        self.event(session_id, "turn/end", {"turn": 1, "reason": reason or {"kind": "completed"}})
        self.emit("session.status", {"sessionId": session_id, "status": "idle"})


@pytest.fixture
def fake_gateway(monkeypatch: pytest.MonkeyPatch) -> list[FakeGateway]:
    created: list[FakeGateway] = []

    def factory(config: DshGatewayConfig, **kwargs: Any) -> FakeGateway:
        gateway = FakeGateway(config, **kwargs)
        created.append(gateway)
        return gateway

    monkeypatch.setattr(daemon_module, "open_gateway", factory)
    return created


def _config(tmp_path: Path) -> DshGatewayConfig:
    return DshGatewayConfig(dsh_home=tmp_path / "home", cwd=tmp_path / "ws")


def _entry(session_id: str, **overrides: Any) -> DshSessionEntry:
    values: dict[str, Any] = dict(session_id=session_id, cwd="C:/w", created_at=100, origin="root", parent_session_id=None, event_count=3, title=None, live=False, status=None)
    values.update(overrides)
    return DshSessionEntry(**values)


def _await(condition, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


def test_daemon_publishes_port_and_cleans_up(tmp_path: Path, fake_gateway: list[FakeGateway]) -> None:
    daemon = DshDaemon(_config(tmp_path), state_dir=tmp_path / "root")
    daemon.start()
    state = json.loads((tmp_path / "root" / DAEMON_STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["port"] == daemon.port
    assert state["home"] == str(tmp_path / "home")
    assert fake_gateway[0].started
    daemon.close()
    assert not (tmp_path / "root" / DAEMON_STATE_FILENAME).exists()
    assert fake_gateway[0].closed


def test_send_wait_and_events_round_trip_over_loopback(tmp_path: Path, fake_gateway: list[FakeGateway]) -> None:
    events: list[DshEvent | DshEventBatch] = []
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root", on_event=events.append, batch_window_seconds=0) as daemon:
        gateway = fake_gateway[0]
        with DshDaemonClient.connect(tmp_path / "root") as client:
            project = str(tmp_path / "project")
            sent = client.send("做点事", cwd=project)
            assert sent["cwd"] == project
            assert sent["created"] is True
            session_id = sent["session_id"]
            assert session_id.startswith("dsh-")
            assert gateway.submits == [(session_id, "做点事")]
            # A new session is rooted at cwd (sandbox boundary), not merely cd'd.
            assert gateway.creates == [(session_id, project)]

            # Queued but no notification yet: the daemon already reports it
            # running, even though the inventory still says the fresh session is idle.
            view = client.status(session_id)
            assert view is not None and view.status == "running" and view.cwd == project
            assert view.running_seconds is not None and view.running_seconds >= 0

            # wait() with a timeout returns timed_out while the turn is open.
            _, timed_out = client.wait(session_id, timeout=0.2)
            assert timed_out is True

            gateway.finish_turn(session_id, "搞定了", title="做点事")
            view, timed_out = client.wait(session_id, timeout=5)
            assert timed_out is False
            assert view is not None
            assert view.status == "idle"
            assert view.finish_reason == "completed"
            assert view.reply == "搞定了"  # reasoning blocks are not part of the reply
            assert view.title == "做点事"
            assert view.turns == 1
            assert view.live is True
            assert view.step == 2 and view.last_tool == "write"
            assert view.running_seconds is None  # idle

            # Continue the same session: explicit id, no cwd change.
            again = client.send("再来", session_id=session_id)
            assert again["created"] is False
            with pytest.raises(RuntimeError, match="only to new sessions"):
                client.send("去那边", session_id=session_id, cwd=tmp_path / "other")
            assert gateway.creates == [(session_id, project)]
            gateway.finish_turn(session_id, "ok")
            view, _ = client.wait(session_id, timeout=5)
            assert view is not None and view.finish_reason == "completed" and view.reply == "ok"

            client.send("再来一次", session_id=session_id)
            gateway.finish_turn(session_id, "", reason={"kind": "error", "error": {"message": "boom", "code": "AUTH"}})
            view, _ = client.wait(session_id, timeout=5)
            assert view is not None and view.finish_reason == "error" and view.error == "boom"

            listed = client.sessions()
            assert [v.session_id for v in listed] == [session_id]

            # Inventory-only sessions still appear, without tracked state.
            gateway.inventory.append(_entry("old", title="旧"))
            old = client.status("old")
            assert old is not None and old.turns == 0 and old.title == "旧" and old.status is None
            assert client.status("missing") is None

        _await(lambda: len(events) == 3)
    single = [e for e in events if isinstance(e, DshEvent)]
    assert len(single) == len(events)
    assert single[0].kind == "turn_finished"
    assert single[0].reply == "搞定了"
    envelope = single[0].envelope()
    assert envelope.startswith('<dsh-event kind="turn_finished">')
    assert f'<session id="{session_id}" title="做点事" cwd="{project}" finish_reason="completed" />' in envelope
    assert "<reply>搞定了</reply>" in envelope
    assert "<error>" not in envelope
    assert "<error>boom</error>" in single[2].envelope()
    # The tool-result append form is plain text: pygent escapes appended
    # context inside <runtime-context>, so XML there would reach the model inert.
    text = single[0].context_text()
    assert text.splitlines()[0] == f"dsh event: turn_finished — session {session_id} (做点事), finish_reason=completed"
    assert text.endswith("reply: 搞定了")
    assert "<" not in text
    assert "error: boom" in single[2].context_text()
    assert single[2].context_text().endswith("reply: (none)")


def test_resume_marks_session_idle_and_restores_it(tmp_path: Path, fake_gateway: list[FakeGateway]) -> None:
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root"):
        gateway = fake_gateway[0]
        gateway.inventory = [_entry("s-old", title="旧会话")]
        with DshDaemonClient.connect(tmp_path / "root") as client:
            view = client.resume("s-old")
            assert gateway.resumes == ["s-old"]
            assert view.status == "idle"
            assert view.title == "旧会话"


def test_bad_requests_do_not_kill_the_daemon(tmp_path: Path, fake_gateway: list[FakeGateway]) -> None:
    from lara_shell.dsh_gateway import SessionControlChannel, SessionControlError

    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root") as daemon:
        assert daemon.port is not None
        channel = SessionControlChannel(daemon.port)
        channel.connect(deadline_seconds=2)
        try:
            with pytest.raises(SessionControlError, match="prompt must not be empty"):
                channel.call("dsh/send", {"prompt": "   "})
            with pytest.raises(SessionControlError, match="session_id must be a non-empty string"):
                channel.call("dsh/status", {})
            with pytest.raises(SessionControlError, match="unknown dsh daemon method"):
                channel.call("dsh/nope")
            assert channel.call("dsh/ping")["ok"] is True
        finally:
            channel.close()


def test_client_reports_missing_or_stale_daemon(tmp_path: Path) -> None:
    with pytest.raises(DaemonUnavailable, match="no dsh daemon is published"):
        DshDaemonClient.connect(tmp_path)
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    (tmp_path / DAEMON_STATE_FILENAME).write_text(json.dumps({"port": port, "pid": 1, "home": "x"}), encoding="utf-8")
    with pytest.raises(DaemonUnavailable, match="not answering"):
        DshDaemonClient.connect(tmp_path)


def test_concurrent_clients_are_served(tmp_path: Path, fake_gateway: list[FakeGateway]) -> None:
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root"):
        results: list[bool] = []

        def ping() -> None:
            with DshDaemonClient.connect(tmp_path / "root") as client:
                results.append(client.status("nothing") is None)

        threads = [threading.Thread(target=ping) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert results == [True] * 4


# -- settings -----------------------------------------------------------------


def test_dsh_target_reads_workspace_from_dsh_home(tmp_path: Path) -> None:
    store = tmp_path / "storages" / "workspace.json"
    store.parent.mkdir()
    store.write_text(json.dumps({
        "global": {"defaultWorkspaceId": "active"},
        "tables": {"workspaces": {"active": {"path": "workspace"}}},
    }), encoding="utf-8")
    assert dsh_target(tmp_path).cwd == tmp_path / "workspace"
    assert dsh_target(tmp_path, tmp_path / "explicit").cwd == tmp_path / "explicit"



def test_runtime_questions_reach_lara_and_answers_go_back(tmp_path: Path, fake_gateway: list[FakeGateway]) -> None:
    events: list[DshEvent | DshEventBatch] = []
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root", on_event=events.append):
        gateway = fake_gateway[0]
        assert gateway.approval_requests is True and gateway.on_user_question is not None
        gateway.approval(7, "s1", "write", "escalate sandbox")
        gateway.ask("q-2", "s1", [
            {"id": "color", "question": "红还是蓝？", "header": "选颜色", "options": [{"label": "红色"}, {"label": "蓝色"}]},
            {"id": "why", "question": "为什么？"},
        ])
        with DshDaemonClient.connect(tmp_path / "root") as client:
            _await(lambda: len(client.requests()) == 2)
            listed = client.requests()
            assert [(r["request_id"], r["kind"], r["tool_name"]) for r in listed] == [("approval-7", "approval", "write"), ("q-2", "question", None)]
            assert listed[1]["questions"][0]["options"] == ["红色", "蓝色"]

            assert client.approve("approval-7")["request_id"] == "approval-7"
            assert gateway.responses == [(7, {"outcome": "allowed-once"})]  # answered on the wire by its JSON-RPC id
            with pytest.raises(SessionControlErrorAlias, match="unknown or settled"):
                client.approve("approval-7")

            client.answer("q-2", {"color": "蓝色", "why": "好看"})
            _await(lambda: "q-2" in gateway.question_results)
            assert gateway.question_results["q-2"] == {"answers": [{"id": "color", "selected": ["蓝色"]}, {"id": "why", "selected": [], "custom": "好看"}]}
            assert client.requests() == []

            # One text for every question; a declined question fails the tool call.
            gateway.ask("q-3", "s1", [{"id": "a", "question": "A?"}, {"id": "b", "question": "B?", "options": [{"label": "随便"}]}])
            _await(lambda: client.requests() != [])
            client.answer("q-3", "随便")
            _await(lambda: "q-3" in gateway.question_results)
            assert gateway.question_results["q-3"] == {"answers": [{"id": "a", "selected": [], "custom": "随便"}, {"id": "b", "selected": ["随便"]}]}
            gateway.ask("q-4", "s1", [{"id": "a", "question": "A?"}])
            _await(lambda: client.requests() != [])
            client.decline("q-4")
            _await(lambda: "q-4" in gateway.question_results)
            assert isinstance(gateway.question_results["q-4"], RuntimeError)
            # Declining an approval rejects it.
            gateway.approval(8, "s1", "pwsh")
            _await(lambda: client.requests() != [])
            client.decline("approval-8")
            assert gateway.responses[-1] == (8, {"outcome": "rejected"})
            # The runtime withdrawing a question removes it and tells Lara.
            cancelled = gateway.ask("q-6", "s1", [{"id": "a", "question": "A?"}])
            _await(lambda: client.requests() != [])
            cancelled.set()
            _await(lambda: client.requests() == [])
            with pytest.raises(SessionControlErrorAlias, match="unknown or settled"):
                client.answer("q-6", "x")
            # A wrong-kind answer is rejected before anything is sent.
            gateway.approval(9, "s1", "pwsh")
            _await(lambda: client.requests() != [])
            with pytest.raises(SessionControlErrorAlias, match="is a approval, not a question"):
                client.answer("approval-9", "x")
            assert client.requests()[0]["request_id"] == "approval-9"

    single = [e for e in events if isinstance(e, DshEvent)]
    assert len(single) == len(events)
    kinds = [(e.kind, e.request_id) for e in single]
    assert kinds == [("approval_request", "approval-7"), ("question", "q-2"), ("question", "q-3"), ("question", "q-4"), ("approval_request", "approval-8"), ("question", "q-6"), ("request_cancelled", "q-6"), ("approval_request", "approval-9")]

    approval = single[0].envelope()
    assert approval.startswith('<dsh-event kind="approval_request">')
    assert '<request id="approval-7" tool="write" />' in approval
    assert "<reason>escalate sandbox</reason>" in approval
    question = single[1].envelope()
    assert '<request id="q-2" />' in question
    assert '<question id="color" header="选颜色">' in question
    assert "<text>红还是蓝？</text>" in question and "<option>蓝色</option>" in question
    assert '<question id="why">' in question
    assert "dsh event: approval_request — session s1, request_id=approval-7, tool=write" in single[0].context_text()
    assert "question [color]: 红还是蓝？ options: 红色 | 蓝色" in single[1].context_text()
    assert single[6].context_text() == "dsh event: request_cancelled — session s1, request_id=q-6"


def test_cancel_marks_running_session(tmp_path: Path, fake_gateway: list[FakeGateway]) -> None:
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root"):
        gateway = fake_gateway[0]
        gateway.live.add("s1")
        with DshDaemonClient.connect(tmp_path / "root") as client:
            client.send("跑", session_id="s1")
            assert client.cancel("s1") is True
            assert gateway.cancels == ["s1"]
            # The runtime ends a cancelled turn without a reason object.
            gateway.event("s1", "turn/end", {"turn": 1, "reason": None})
            gateway.emit("session.status", {"sessionId": "s1", "status": "idle"})
            view, _ = client.wait("s1", timeout=5)
            assert view is not None and view.finish_reason == "cancelled" and view.status == "idle"
            assert client.cancel("not-live") is False


def test_unanswered_requests_expire_fail_closed(tmp_path: Path, fake_gateway: list[FakeGateway], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_module, "REQUEST_TIMEOUT_SECONDS", -1.0)
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root") as daemon:
        gateway = fake_gateway[0]
        gateway.approval(1, "s1", "write")
        gateway.ask("q-2", "s1", [{"id": "a", "question": "A?"}])
        _await(lambda: len(daemon.requests()) == 2)
        # Drive the sweep directly instead of waiting for its 30 s tick.
        daemon._sweep_expired_requests()
        assert gateway.responses == [(1, {"outcome": "unavailable"})]
        _await(lambda: "q-2" in gateway.question_results)
        assert isinstance(gateway.question_results["q-2"], RuntimeError)
        assert daemon.requests() == []


def test_second_daemon_refuses_to_start_beside_a_live_one(tmp_path: Path, fake_gateway: list[FakeGateway]) -> None:
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root"):
        second = DshDaemon(_config(tmp_path), state_dir=tmp_path / "root")
        with pytest.raises(RuntimeError, match="already serving"):
            second.start()
        assert len(fake_gateway) == 1  # refused before opening a second runtime
    # A stale state file (daemon gone) does not block a new daemon.
    (tmp_path / "root" / DAEMON_STATE_FILENAME).write_text(json.dumps({"port": 1, "pid": 0, "home": "x"}), encoding="utf-8")
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root") as daemon:
        assert daemon.port is not None and daemon.port != 1


def test_simultaneous_finished_reports_are_batched(tmp_path: Path, fake_gateway: list[FakeGateway]) -> None:
    events: list[Any] = []
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root", on_event=events.append, batch_window_seconds=0.3) as daemon:
        gateway = fake_gateway[0]
        daemon.send("A", session_id="s1")
        daemon.send("B", session_id="s2")
        gateway.finish_turn("s1", "A 做完了", title="任务A")
        gateway.finish_turn("s2", "B 做完了", title="任务B")
        # A turn of a session nobody dispatched through the daemon is not reported.
        gateway.finish_turn("someone-elses", "quiet")
        # An approval in the same window is delivered on its own, immediately.
        gateway.approval(1, "s3", "write")
        _await(lambda: len(events) >= 1)
        first = events[0]
        assert isinstance(first, DshEvent) and first.kind == "approval_request"
        _await(lambda: len(events) == 2, timeout=3)
        batch = events[1]
        assert isinstance(batch, DshEventBatch)
        assert [e.session_id for e in batch.events] == ["s1", "s2"]
        envelope = batch.envelope()
        assert envelope.startswith('<dsh-events count="2">')
        assert f'<session id="s1" title="任务A" cwd="{tmp_path / "ws"}" finish_reason="completed" />' in envelope
        assert "<reply>B 做完了</reply>" in envelope
        assert batch.context_text().count("dsh event: turn_finished") == 2

        # A lone report after the window closes is delivered unbatched.
        time.sleep(0.4)
        daemon.send("C", session_id="s4")
        gateway.finish_turn("s4", "C 做完了")
        _await(lambda: len(events) == 3, timeout=3)
        assert isinstance(events[2], DshEvent) and events[2].session_id == "s4"

        # A report still held when the daemon closes is flushed, not lost.
        daemon.send("D", session_id="s5")
        gateway.finish_turn("s5", "D 做完了")
        _await(lambda: bool(daemon._finished_batch))
    assert len(events) == 4 and isinstance(events[3], DshEvent) and events[3].session_id == "s5"
