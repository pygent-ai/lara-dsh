"""Tests for the dsh daemon and settings without a dsh runtime.

The gateway is replaced by an in-memory fake that replays recorded SDK
notifications, so the tracker, loopback RPC, client, and event envelopes are
exercised exactly as the real notification thread drives them.
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

import pytest

from lara_shell import dsh_daemon as daemon_module
from lara_shell.dsh_daemon import DAEMON_STATE_FILENAME, DaemonUnavailable, DshDaemon, DshDaemonClient, DshEvent, DshQuestion
from lara_shell.dsh_gateway import DshGatewayConfig, DshSessionEntry, SessionControlError as SessionControlErrorAlias
from lara_shell.dsh_settings import load_dsh_settings


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

    def __init__(self, config: DshGatewayConfig, *, on_control_notification=None) -> None:
        self.config = config
        self.on_control_notification = on_control_notification
        self.approvals: list[tuple[str, str]] = []
        self.answers: list[tuple[str, Any, bool]] = []
        self.cancels: list[str] = []
        self.live: set[str] = set()
        self.started = False
        self.closed = False
        self.submits: list[tuple[str, str]] = []
        self.cwd_sets: list[tuple[str, str]] = []
        self.creates: list[tuple[str, str]] = []
        self.resumes: list[str] = []
        self.inventory: list[DshSessionEntry] = []
        self._queue: queue.Queue[_Notification | None] = queue.Queue()

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True
        self._queue.put(None)

    def notifications(self, session_id: str | None = None) -> _Subscription:
        return _Subscription(self._queue)

    def submit(self, session_id: str, prompt: str) -> str:
        self.submits.append((session_id, prompt))
        return f"msg-{len(self.submits)}"

    def set_working_directory(self, session_id: str, path: str) -> str:
        self.cwd_sets.append((session_id, path))
        return path

    def resume(self, session_id: str) -> None:
        self.resumes.append(session_id)

    def cancel_session(self, session_id: str) -> bool:
        self.cancels.append(session_id)
        return session_id in self.live

    def respond_approval(self, request_id: str, outcome: str) -> None:
        self.approvals.append((request_id, outcome))

    def respond_question(self, request_id: str, answers, *, cancelled: bool = False) -> None:
        self.answers.append((request_id, answers, cancelled))

    def control(self, method: str, params: dict[str, Any]) -> None:
        """Replay a plugin→answerer notification on the control channel."""
        assert self.on_control_notification is not None
        self.on_control_notification(method, params)

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

    monkeypatch.setattr(daemon_module, "DshGateway", factory)
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
    events: list[DshEvent] = []
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root", on_event=events.append) as daemon:
        gateway = fake_gateway[0]
        with DshDaemonClient.connect(tmp_path / "root") as client:
            sent = client.send("做点事", cwd="E:/proj")
            assert sent["created"] is True
            session_id = sent["session_id"]
            assert session_id.startswith("dsh-")
            assert gateway.submits == [(session_id, "做点事")]
            # A new session is rooted at cwd (sandbox boundary), not merely cd'd.
            assert gateway.creates == [(session_id, "E:/proj")]
            assert gateway.cwd_sets == []

            # Queued but no notification yet: the daemon already reports it
            # running, even though the inventory still says the fresh session is idle.
            view = client.status(session_id)
            assert view is not None and view.status == "running" and view.cwd == "E:/proj"
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
            assert gateway.cwd_sets == []
            # An existing session given a cwd only changes its effective directory.
            client.send("去那边", session_id=session_id, cwd="E:/proj/sub")
            assert gateway.cwd_sets == [(session_id, "E:/proj/sub")]
            assert gateway.creates == [(session_id, "E:/proj")]
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
    assert events[0].kind == "turn_finished"
    assert events[0].reply == "搞定了"
    envelope = events[0].envelope()
    assert envelope.startswith('<dsh-event kind="turn_finished">')
    assert f'<session id="{session_id}" title="做点事" finish_reason="completed" />' in envelope
    assert "<reply>搞定了</reply>" in envelope
    assert "<error>" not in envelope
    assert "<error>boom</error>" in events[2].envelope()
    # The tool-result append form is plain text: pygent escapes appended
    # context inside <runtime-context>, so XML there would reach the model inert.
    text = events[0].context_text()
    assert text.splitlines()[0] == f"dsh event: turn_finished — session {session_id} (做点事), finish_reason=completed"
    assert text.endswith("reply: 搞定了")
    assert "<" not in text
    assert "error: boom" in events[2].context_text()
    assert events[2].context_text().endswith("reply: (none)")


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


def test_settings_default_without_files(tmp_path: Path) -> None:
    settings = load_dsh_settings(tmp_path, environ={}, credentials={})
    assert settings.dsh_home == tmp_path / "dsh-home"
    assert settings.workspace == tmp_path / "pet"
    assert settings.model == "deepseek-v4-flash"
    assert settings.base_url is None and settings.api_key is None and settings.connection is None
    # Without a connection the plain DEEPSEEK_* environment is the whole story.
    env_only = load_dsh_settings(tmp_path, environ={"DEEPSEEK_API_KEY": "sk-env"}, credentials={})
    assert env_only.api_key == "sk-env" and env_only.base_url is None


def test_settings_resolve_connection_from_lara_config(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "connections:\n  az:\n    provider: 'az'\n    credential:\n      env: 'AZ_KEY'\n"
        "    protocols:\n      openai_chat_completions:\n        base_url: 'https://az.example/v1'\n",
        encoding="utf-8",
    )
    (tmp_path / "dsh.yaml").write_text(
        "connection: az\nmodel: deepseek-v4-flash\nhome: dsh-az\nworkspace: E:/work\n", encoding="utf-8"
    )
    settings = load_dsh_settings(tmp_path, environ={}, credentials={"AZ_KEY": "sk-test"})
    assert settings.connection == "az"
    assert settings.base_url == "https://az.example/v1"
    assert settings.api_key == "sk-test"
    assert settings.dsh_home == tmp_path / "dsh-az"
    assert settings.workspace == Path("E:/work")
    config = settings.gateway_config()
    assert config.base_url == "https://az.example/v1" and config.cwd == Path("E:/work")

    # Environment overrides the file layer.
    overridden = load_dsh_settings(
        tmp_path,
        environ={"DEEPSEEK_BASE_URL": "http://local/v1", "DSH_MODEL": "deepseek-v4-pro", "DSH_HOME": "elsewhere"},
        credentials={"AZ_KEY": "sk-test"},
    )
    assert overridden.base_url == "http://local/v1"
    assert overridden.api_key == "sk-test"  # redirect without a key keeps the connection's
    assert overridden.model == "deepseek-v4-pro"
    assert overridden.dsh_home == tmp_path / "elsewhere"

    # A stray DEEPSEEK_API_KEY in the environment (the shell exports every
    # stored credential) must not be paired with the connection's endpoint.
    stray = load_dsh_settings(tmp_path, environ={"DEEPSEEK_API_KEY": "sk-other-provider"}, credentials={"AZ_KEY": "sk-test"})
    assert stray.base_url == "https://az.example/v1"
    assert stray.api_key == "sk-test"

    # Explicit redirect with its own key replaces the pair.
    redirected = load_dsh_settings(
        tmp_path, environ={"DEEPSEEK_BASE_URL": "http://local/v1", "DEEPSEEK_API_KEY": "sk-local"}, credentials={"AZ_KEY": "sk-test"}
    )
    assert (redirected.base_url, redirected.api_key) == ("http://local/v1", "sk-local")


def test_settings_reject_unknown_connection_or_missing_credential(tmp_path: Path) -> None:
    (tmp_path / "dsh.yaml").write_text("connection: nope\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not defined"):
        load_dsh_settings(tmp_path, environ={}, credentials={})
    (tmp_path / "config.yaml").write_text(
        "connections:\n  az:\n    credential:\n      env: 'AZ_KEY'\n"
        "    protocols:\n      openai_chat_completions:\n        base_url: 'https://az.example/v1'\n",
        encoding="utf-8",
    )
    (tmp_path / "dsh.yaml").write_text("connection: az\n", encoding="utf-8")
    with pytest.raises(ValueError, match="AZ_KEY.*not configured"):
        load_dsh_settings(tmp_path, environ={}, credentials={})


def test_runtime_questions_reach_lara_and_answers_go_back(tmp_path: Path, fake_gateway: list[FakeGateway]) -> None:
    events: list[DshEvent] = []
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root", on_event=events.append):
        gateway = fake_gateway[0]
        gateway.control("lara/approval/request", {"requestId": "r-1", "sessionId": "s1", "toolName": "write", "callId": "c1", "reason": "escalate sandbox"})
        gateway.control("lara/question/request", {"requestId": "r-2", "sessionId": "s1", "questions": [
            {"id": "color", "question": "红还是蓝？", "header": "选颜色", "options": [{"label": "红色"}, {"label": "蓝色"}]},
            {"id": "why", "question": "为什么？"},
        ]})
        with DshDaemonClient.connect(tmp_path / "root") as client:
            listed = client.requests()
            assert [(r["request_id"], r["kind"], r["tool_name"]) for r in listed] == [("r-1", "approval", "write"), ("r-2", "question", None)]
            assert listed[1]["questions"][0]["options"] == ["红色", "蓝色"]

            assert client.approve("r-1")["request_id"] == "r-1"
            assert gateway.approvals == [("r-1", "allowed-once")]
            # Settled requests are gone; answering twice is an error, not a resend.
            with pytest.raises(SessionControlErrorAlias, match="unknown or settled"):
                client.approve("r-1")

            client.answer("r-2", {"color": "蓝色", "why": "好看"})
            assert gateway.answers == [("r-2", [{"id": "color", "selected": ["蓝色"]}, {"id": "why", "selected": [], "custom": "好看"}], False)]
            assert client.requests() == []

            # One text for every question; a declined question is cancelled.
            gateway.control("lara/question/request", {"requestId": "r-3", "sessionId": "s1", "questions": [{"id": "a", "question": "A?"}, {"id": "b", "question": "B?", "options": [{"label": "随便"}]}]})
            client.answer("r-3", "随便")
            assert gateway.answers[-1] == ("r-3", [{"id": "a", "selected": [], "custom": "随便"}, {"id": "b", "selected": ["随便"]}], False)
            gateway.control("lara/question/request", {"requestId": "r-4", "sessionId": "s1", "questions": [{"id": "a", "question": "A?"}]})
            client.decline("r-4")
            assert gateway.answers[-1] == ("r-4", None, True)
            # Declining an approval rejects it.
            gateway.control("lara/approval/request", {"requestId": "r-5", "sessionId": "s1", "toolName": "pwsh"})
            client.decline("r-5")
            assert gateway.approvals[-1] == ("r-5", "rejected")
            # The runtime withdrawing a request removes it and tells Lara.
            gateway.control("lara/approval/request", {"requestId": "r-6", "sessionId": "s1", "toolName": "pwsh"})
            gateway.control("lara/approval/cancelled", {"requestId": "r-6"})
            assert client.requests() == []
            with pytest.raises(SessionControlErrorAlias, match="unknown or settled"):
                client.approve("r-6")
            # A wrong-kind answer is rejected before anything is sent.
            gateway.control("lara/approval/request", {"requestId": "r-7", "sessionId": "s1", "toolName": "pwsh"})
            with pytest.raises(SessionControlErrorAlias, match="is a approval, not a question"):
                client.answer("r-7", "x")
            assert client.requests()[0]["request_id"] == "r-7"

    kinds = [(e.kind, e.request_id) for e in events]
    assert kinds == [("approval_request", "r-1"), ("question", "r-2"), ("question", "r-3"), ("question", "r-4"), ("approval_request", "r-5"), ("approval_request", "r-6"), ("request_cancelled", "r-6"), ("approval_request", "r-7")]

    approval = events[0].envelope()
    assert approval.startswith('<dsh-event kind="approval_request">')
    assert '<request id="r-1" tool="write" />' in approval
    assert "<reason>escalate sandbox</reason>" in approval
    question = events[1].envelope()
    assert '<request id="r-2" />' in question
    assert '<question id="color" header="选颜色">' in question
    assert "<text>红还是蓝？</text>" in question and "<option>蓝色</option>" in question
    assert '<question id="why">' in question
    assert "dsh event: approval_request — session s1, request_id=r-1, tool=write" in events[0].context_text()
    assert "question [color]: 红还是蓝？ options: 红色 | 蓝色" in events[1].context_text()
    assert events[6].context_text() == "dsh event: request_cancelled — session s1, request_id=r-6"


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
        gateway.control("lara/approval/request", {"requestId": "r-1", "sessionId": "s1", "toolName": "write"})
        gateway.control("lara/question/request", {"requestId": "r-2", "sessionId": "s1", "questions": [{"id": "a", "question": "A?"}]})
        # Drive the sweep directly instead of waiting for its 30 s tick.
        daemon._sweep_expired_requests()
        assert gateway.approvals == [("r-1", "unavailable")]
        assert gateway.answers == [("r-2", None, True)]
        assert daemon.requests() == []


def test_second_daemon_refuses_to_start_beside_a_live_one(tmp_path: Path, fake_gateway: list[FakeGateway]) -> None:
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root"):
        second = DshDaemon(_config(tmp_path), state_dir=tmp_path / "root")
        with pytest.raises(RuntimeError, match="already serving"):
            second.start()
        assert fake_gateway[1].started is False  # refused before booting a runtime
    # A stale state file (daemon gone) does not block a new daemon.
    (tmp_path / "root" / DAEMON_STATE_FILENAME).write_text(json.dumps({"port": 1, "pid": 0, "home": "x"}), encoding="utf-8")
    with DshDaemon(_config(tmp_path), state_dir=tmp_path / "root") as daemon:
        assert daemon.port is not None and daemon.port != 1


def test_settings_file_roundtrip_and_validation(tmp_path: Path) -> None:
    from lara_shell.dsh_settings import read_dsh_settings_file, write_dsh_settings_file

    (tmp_path / "config.yaml").write_text(
        "connections:\n  az:\n    credential:\n      env: 'AZ_KEY'\n"
        "    protocols:\n      openai_chat_completions:\n        base_url: 'https://az.example/v1'\n",
        encoding="utf-8",
    )
    assert read_dsh_settings_file(tmp_path) == {}
    path = write_dsh_settings_file(tmp_path, {"model": "deepseek-v4-pro", "home": "dsh-x"})
    assert path == tmp_path / "dsh.yaml"
    assert read_dsh_settings_file(tmp_path) == {"model": "deepseek-v4-pro", "home": "dsh-x"}
    # Unset with None; unknown keys are refused before writing.
    write_dsh_settings_file(tmp_path, {"home": None})
    assert read_dsh_settings_file(tmp_path) == {"model": "deepseek-v4-pro"}
    with pytest.raises(ValueError, match="unknown dsh setting"):
        write_dsh_settings_file(tmp_path, {"nope": "x"})
    # A connection that does not resolve is rolled back, leaving the file as it was.
    with pytest.raises(ValueError, match="not defined"):
        write_dsh_settings_file(tmp_path, {"connection": "missing"})
    assert read_dsh_settings_file(tmp_path) == {"model": "deepseek-v4-pro"}
