"""Real-runtime tests for the dsh daemon and the pet sidecar's dsh seat.

Both drive the real dsh runtime and the real model behind Lara's configured
connection, so they are skipped without ``DEEPSEEK_API_KEY``. The sidecar
test additionally needs Lara's own model (``<dsh home>/lara/config.yaml``) and
is marked ``e2e``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lara_shell import dsh_home
from lara_shell.dsh_daemon import DshDaemon, DshDaemonClient, DshEvent, DshEventBatch
from lara_shell.dsh_gateway import ensure_primary_runtime

from _dsh_support import real_gateway_config

REQUIRES_API_KEY = pytest.mark.skipif(
    not os.environ.get("DEEPSEEK_API_KEY") and not (dsh_home() / ".credentials.yaml").is_file(),
    reason="real dsh runtime needs DEEPSEEK_API_KEY or a key in dsh's credential store",
)


@REQUIRES_API_KEY
def test_daemon_dispatches_and_reports_against_the_real_runtime(tmp_path: Path) -> None:
    ensure_primary_runtime()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    events: list[DshEvent | DshEventBatch] = []
    config = real_gateway_config(tmp_path / "dsh-home", workspace)
    with DshDaemon(config, state_dir=tmp_path / "root", on_event=events.append):
        with DshDaemonClient.connect(tmp_path / "root") as client:
            sent = client.send("在当前目录创建 d.txt 内容 DAEMON，完成后只回答：done", cwd=str(project))
            view, timed_out = client.wait(sent["session_id"], timeout=180)
            assert timed_out is False
            assert view is not None and view.status == "idle" and view.finish_reason == "completed"
            assert view.cwd == str(project)
            assert (project / "d.txt").read_text(encoding="utf-8").strip() == "DAEMON"
            assert view.turns == 1 and view.last_tool is not None
            assert [v.session_id for v in client.sessions()] == [sent["session_id"]]

            # A question reaches the owner and the answer flows back.
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


@pytest.mark.e2e
@REQUIRES_API_KEY
def test_pet_sidecar_relays_a_dsh_task_and_its_report(tmp_path: Path) -> None:
    """User → Lara → `lara dsh send` → dsh writes the file → Lara speaks the report."""
    sidecar = Path(__file__).resolve().parents[1] / "pet" / "sidecar" / "main.py"
    workspace = tmp_path / "pet-ws"
    workspace.mkdir()
    process = subprocess.Popen(
        [sys.executable, str(sidecar), "--workspace", str(workspace)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None
    messages: list[dict] = []
    ready = threading.Event()

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            messages.append(message)
            if message.get("type") == "ready":
                ready.set()

    threading.Thread(target=read_stdout, daemon=True).start()
    stderr_lines: list[str] = []

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line.rstrip())

    threading.Thread(target=read_stderr, daemon=True).start()
    try:
        assert ready.wait(60), f"sidecar never became ready; stderr={stderr_lines[-10:]}"
        assert messages[0].get("dsh_port") is not None, "sidecar started without its dsh daemon"
        process.stdin.write(json.dumps({"type": "send", "text": f"让 dsh 在 {workspace} 里创建 relay.md 写一句问候。派完就结束这轮，不要等它、不要查状态。"}, ensure_ascii=False) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            spoken = [m["text"] for m in messages if m.get("type") == "speak"]
            if (workspace / "relay.md").is_file() and any("relay.md" in text for text in spoken) and len(spoken) >= 2:
                break
            time.sleep(2)
        else:
            pytest.fail(f"no dsh report reached the pet; messages={messages[-6:]}")
        # The report turn is harness-framed (automation.trigger), not a user turn.
        assert any(m.get("type") == "turn_started" and str(m.get("turn_id", "")).startswith("dsh-event-") for m in messages)
    finally:
        process.stdin.close()
        try:
            process.wait(30)
        except subprocess.TimeoutExpired:
            process.kill()
