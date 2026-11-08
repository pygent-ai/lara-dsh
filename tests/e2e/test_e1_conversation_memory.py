"""E1: a CLI chat session remembers the previous turn.

Anchors: ``lara session run --new`` creates a persistent chat session and the
second turn is assembled from the first turn's committed history.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pygent import AIMessage, Context, Message

from _e2e_support import (
    ScriptedModelInvoker,
    content_text,
    context_text,
    install_cli_config,
    install_scripted_model,
    make_config,
    run_cli,
)
from lara.sessions import SessionManager

CODE_WORD = "BLUE-OTTER-42"


def _memory_responder(message: Message, context: Context) -> AIMessage:
    seen = context_text(context) + "\n" + content_text(message)
    if CODE_WORD in seen:
        return AIMessage(content=f"你让我记住的代号是 {CODE_WORD}。")
    return AIMessage(content="我没有找到上一轮留下的代号。")


@pytest.mark.e2e
def test_session_run_new_keeps_multi_turn_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(tmp_path)
    invoker = ScriptedModelInvoker(_memory_responder)
    install_scripted_model(monkeypatch, invoker)
    install_cli_config(monkeypatch, config)
    workspace = str(config.workspace_root)

    code, first, stderr = run_cli(
        capsys,
        [
            "--workspace-root",
            workspace,
            "session",
            "run",
            "--new",
            "-m",
            f"请记住这个代号：{CODE_WORD}",
        ],
    )
    assert code == 0, stderr
    assert first["status"] == "passed"
    session_id = first["session_id"]
    assert Path(first["run_dir"]).is_dir()

    code, second, stderr = run_cli(
        capsys,
        [
            "--workspace-root",
            workspace,
            "session",
            "run",
            session_id,
            "-m",
            "刚才我让你记住的代号是什么？",
        ],
    )
    assert code == 0, stderr
    assert second["session_id"] == session_id
    assert second["status"] == "passed"
    # The scripted responder only emits the word when the first turn is
    # present in the model context; a stateless runtime would answer "lost".
    assert CODE_WORD in second["final_answer"]
    assert Path(second["run_dir"]).is_dir()

    assert len(invoker.requests) >= 2
    _, second_context = invoker.requests[-1]
    assert CODE_WORD in context_text(second_context)

    session = SessionManager(config).load(session_id)
    persisted = "\n".join(str(item) for item in session.history)
    assert CODE_WORD in persisted
