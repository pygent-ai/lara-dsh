"""Real-case tests for DshGateway: drive actual dsh sessions end to end.

These tests launch a real dsh runtime and call the real DeepSeek model.
They are skipped when DEEPSEEK_API_KEY is not configured in the process
environment. The bundled runtime resources are downloaded once into the
resource cache shared by ``DSH_RESOURCE_CACHE``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lara_shell import dsh_home
from lara_shell.dsh_gateway import DshGateway, ensure_primary_runtime

from _dsh_support import real_gateway_config

REQUIRES_API_KEY = pytest.mark.skipif(
    not os.environ.get("DEEPSEEK_API_KEY") and not (dsh_home() / ".credentials.yaml").is_file(),
    reason="real dsh runtime needs DEEPSEEK_API_KEY or a key in dsh's credential store",
)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return workspace


@REQUIRES_API_KEY
def test_dsh_session_survives_a_new_gateway_process(tmp_path: Path) -> None:
    """A durable dsh session continues across gateway processes."""
    ensure_primary_runtime()
    home = tmp_path / "dsh-home"
    with DshGateway(real_gateway_config(home, _workspace(tmp_path))) as first:
        _, session_id = first.run(
            "记住数字 42。只回答：收到", session_id="lara-dsh-smoke-001"
        )
    assert session_id

    with DshGateway(real_gateway_config(home, _workspace(tmp_path))) as second:
        reply, _ = second.resume_run(
            session_id, "我让你记住的数字是多少？只回答数字本身"
        )
    assert "42" in reply


@REQUIRES_API_KEY
def test_dsh_agent_performs_real_work_in_the_workspace(tmp_path: Path) -> None:
    """The dsh agent executes real tool work inside the gateway cwd."""
    workspace = _workspace(tmp_path)
    ensure_primary_runtime()
    with DshGateway(real_gateway_config(tmp_path / "dsh-home", workspace)) as gateway:
        reply, _ = gateway.run(
            "在当前工作目录创建文件 dsh-ok.txt，内容写 DSH-OK。完成后只回答：done"
        )
    marker = workspace / "dsh-ok.txt"
    assert marker.is_file(), f"agent did not create the file; reply: {reply[:200]}"
    assert "DSH-OK" in marker.read_text(encoding="utf-8")


@REQUIRES_API_KEY
def test_session_control_plugin_tracks_live_and_persisted_sessions(tmp_path: Path) -> None:
    """The lara-session-control plugin inventories sessions across runtimes.

    Needs only a booting runtime (no model reply is asserted), so it also
    passes with an invalid key: the turn ends with an auth error but the
    session still exists, runs, and goes idle.
    """
    ensure_primary_runtime()
    home = tmp_path / "dsh-home"
    workspace = _workspace(tmp_path)

    with DshGateway(real_gateway_config(home, workspace)) as first:
        assert first.control.call("lara/ping")["ok"] is True
        assert first.list_sessions() == []
        assert first.stat_session("lara-ctl-001") is None

        subscription = first.notifications()
        first.submit("lara-ctl-001", "只回答：收到")
        live = first.stat_session("lara-ctl-001")
        assert live is not None
        assert live.live is True
        assert live.status == "running"
        assert live.cwd == str(workspace)

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            notification = subscription.next()
            if notification.method == "session.status" and notification.payload["status"] == "idle":
                break
        else:
            pytest.fail("session never went idle")
        idle = first.stat_session("lara-ctl-001")
        assert idle is not None and idle.status == "idle"
        assert [entry.session_id for entry in first.list_sessions()] == ["lara-ctl-001"]

    with DshGateway(real_gateway_config(home, workspace)) as second:
        (persisted,) = second.list_sessions()
        assert persisted.session_id == "lara-ctl-001"
        assert persisted.live is False
        assert persisted.status is None
        assert persisted.created_at == live.created_at
        assert persisted.cwd == str(workspace)
        # Title is folded from the persisted log, not from a live Session.
        assert persisted.title == "只回答：收到"


@REQUIRES_API_KEY
def test_created_session_is_rooted_at_its_own_cwd(tmp_path: Path) -> None:
    """A plugin-created session works and writes inside its own directory.

    The SDK stamps sessions with the runtime's single ``initialize`` cwd, which
    is also the sandbox write boundary; ``create_session`` roots a session
    elsewhere. Needs a working model to exercise the write.
    """
    ensure_primary_runtime()
    runtime_ws = _workspace(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    with DshGateway(real_gateway_config(tmp_path / "dsh-home", runtime_ws)) as gateway:
        entry = gateway.create_session("lara-proj-001", project)
        assert entry.cwd == str(project)
        assert entry.live is True and entry.status == "idle"

        subscription = gateway.notifications("lara-proj-001")
        gateway.submit("lara-proj-001", "在当前工作目录创建文件 rooted.txt，内容写 ROOTED。完成后只回答：done")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            notification = subscription.next()
            if notification.method == "session.status" and notification.payload["status"] == "idle":
                break
        else:
            pytest.fail("session never went idle")
        assert (project / "rooted.txt").read_text(encoding="utf-8").strip() == "ROOTED"
        assert not (runtime_ws / "rooted.txt").exists()

        with pytest.raises(Exception, match="already"):
            gateway.create_session("lara-proj-001", project)


@REQUIRES_API_KEY
def test_plugin_routes_approvals_and_questions_to_the_owner(tmp_path: Path) -> None:
    """A registered answerer receives sandbox escalations and ask_user_question."""
    import queue
    import threading

    import shutil
    import uuid

    ensure_primary_runtime()
    workspace = _workspace(tmp_path)
    # The sandbox lets workspace-write touch the platform temp area, where
    # tmp_path lives; a real escalation needs a directory outside both.
    outside = Path.cwd() / ".tmp" / f"pytest-outside-{uuid.uuid4().hex[:8]}"
    outside.mkdir(parents=True)
    notifications: queue.Queue[tuple[str, dict]] = queue.Queue()
    try:
        _exercise_owner_requests(tmp_path, workspace, outside, notifications)
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def _exercise_owner_requests(tmp_path: Path, workspace: Path, outside: Path, notifications) -> None:
    import threading

    with DshGateway(
        real_gateway_config(tmp_path / "dsh-home", workspace),
        on_control_notification=lambda method, params: notifications.put((method, params)),
    ) as gateway:
        answered: list[str] = []

        def answer_loop() -> None:
            while True:
                method, params = notifications.get()
                answered.append(method)
                if method == "lara/approval/request":
                    gateway.respond_approval(params["requestId"], "allowed-once")
                elif method == "lara/question/request":
                    gateway.respond_question(
                        params["requestId"],
                        [{"id": q["id"], "selected": [q["options"][-1]["label"]]} for q in params["questions"]],
                    )

        threading.Thread(target=answer_loop, daemon=True).start()
        subscription = gateway.notifications()

        def run(session_id: str, prompt: str) -> str:
            gateway.submit(session_id, prompt)
            reply = ""
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                notification = subscription.next()
                payload: dict = dict(notification.payload)
                if payload.get("sessionId") != session_id:
                    continue
                event: dict = dict(payload.get("event") or {})
                if notification.method == "session.event" and event.get("type") == "assistant/message":
                    blocks: list = list(event["data"]["message"]["content"])
                    reply = "".join(b.get("text", "") for b in blocks if b.get("type") == "text") or reply
                if notification.method == "session.status" and payload["status"] == "idle":
                    return reply
            pytest.fail(f"session {session_id} never went idle")

        run(
            "lara-appr-001",
            f"用 write 工具在 {outside / 'out.txt'} 写入 OUT。该路径在工作区外会被沙箱拒绝；"
            "被拒后按提示用 sandbox_permissions=danger-full-access + justification 重试一次申请审批。完成后只回答 done",
        )
        assert (outside / "out.txt").read_text(encoding="utf-8").strip() == "OUT"
        assert "lara/approval/request" in answered

        reply = run(
            "lara-ask-001",
            "用 ask_user_question 工具问我喜欢红色还是蓝色（给两个选项 红色/蓝色），拿到答案后只回答：答案是<颜色>",
        )
        assert "蓝色" in reply
        assert "lara/question/request" in answered

        gateway.submit("lara-cancel-001", "用 pwsh 执行 Start-Sleep -Seconds 120，然后回答 done")
        time.sleep(8)
        assert gateway.cancel_session("lara-cancel-001") is True
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            notification = subscription.next()
            payload = dict(notification.payload)
            if payload.get("sessionId") == "lara-cancel-001" and notification.method == "session.status" and payload["status"] == "idle":
                break
        else:
            pytest.fail("cancelled session never went idle")
        assert gateway.cancel_session("never-created") is False
