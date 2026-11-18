"""Attach mode against a real, already-running dsh (the user's `dsh web`).

Needs: the Lara plugin installed in the web profile (`lara dsh plugin install`)
and a running `dsh web` on the real dsh home publishing
``<dsh home>/lara/dsh-plugin.json``. Skipped otherwise. Sessions created here
land in the user's own dsh, so they are prompted to write only under
``.tmp/`` of this repo.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from lara_shell import dsh_home
from lara_shell.dsh_daemon import DshDaemon, DshDaemonClient, DshEvent, DshEventBatch
from lara_shell.dsh_gateway import AttachedDshGateway, DshGatewayConfig, probe_plugin_endpoint
from lara_shell.dsh_settings import load_dsh_settings
from lara_shell import user_lara_root

REPO_TMP = Path(__file__).resolve().parent.parent / ".tmp"


def _live_endpoint():
    endpoint = probe_plugin_endpoint(dsh_home())
    if endpoint is None or not endpoint.attach:
        pytest.skip("no running dsh with the Lara plugin (web profile) to attach to")
    return endpoint


def _config(cwd: Path) -> DshGatewayConfig:
    settings = load_dsh_settings(user_lara_root())
    return DshGatewayConfig(dsh_home=dsh_home(), cwd=cwd, provider=settings.provider, model=settings.model)


@pytest.mark.e2e
def test_attached_gateway_runs_a_turn_in_the_users_dsh() -> None:
    _live_endpoint()
    workspace = REPO_TMP / f"attach-{uuid.uuid4().hex[:6]}"
    workspace.mkdir(parents=True)
    with AttachedDshGateway(_config(workspace)) as gateway:
        reply, session_id = gateway.run("Reply with exactly the word PONG and nothing else.")
        assert reply.strip() == "PONG"
        entry = gateway.stat_session(session_id)
        assert entry is not None and entry.live and entry.status == "idle"
        assert entry.cwd is not None and Path(entry.cwd).resolve() == workspace.resolve()
        # A Lara-created session is an ordinary session of that dsh: listed with the rest.
        assert session_id in {e.session_id for e in gateway.list_sessions()}


@pytest.mark.e2e
def test_daemon_attaches_writes_in_the_session_cwd_and_relays_a_question(tmp_path: Path) -> None:
    _live_endpoint()
    project = REPO_TMP / f"attach-proj-{uuid.uuid4().hex[:6]}"
    project.mkdir(parents=True)
    events: list[DshEvent | DshEventBatch] = []
    with DshDaemon(_config(project), state_dir=tmp_path / "root", on_event=events.append, batch_window_seconds=0) as daemon:
        assert daemon.mode == "attach"
        with DshDaemonClient.connect(tmp_path / "root") as client:
            sent = client.send("在当前目录创建 a.txt 内容 ATTACHED，完成后只回答：done", cwd=str(project))
            view, timed_out = client.wait(sent["session_id"], timeout=180)
            assert timed_out is False
            assert view is not None and view.status == "idle" and view.finish_reason == "completed"
            assert (project / "a.txt").read_text(encoding="utf-8").strip() == "ATTACHED"

            asked = client.send("用 ask_user_question 问我喜欢猫还是狗（两个选项 猫/狗），拿到答案后只回答：答案是<答案>", session_id=sent["session_id"])
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and not client.requests():
                time.sleep(1)
            (request,) = client.requests()
            assert request["kind"] == "question" and request["questions"][0]["options"] == ["猫", "狗"]
            client.answer(request["request_id"], "狗")
            view, _ = client.wait(asked["session_id"], timeout=120)
            assert view is not None and "狗" in view.reply
    kinds = [e.kind for e in events if isinstance(e, DshEvent)]
    assert kinds == ["turn_finished", "question", "turn_finished"]
