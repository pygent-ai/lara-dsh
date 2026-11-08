"""E2: the agent really writes and reads a workspace file.

Anchors: the production tool pipeline executes model-emitted ``write`` and
``read`` calls against the scenario workspace, and the final answer reflects
the tool result.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pygent import AIMessage, Context, Message, ToolCall

from _e2e_support import (
    ScriptedModelInvoker,
    install_cli_config,
    install_scripted_model,
    make_config,
    run_cli,
)

FILE_NAME = "e2e_note.txt"
FILE_CONTENT = "E2E_FILE_CONTENT"


def _tool_then_read_responder() -> Callable[[Message, Context], AIMessage]:
    script = [
        AIMessage(
            content="",
            tool_calls=(
                ToolCall(
                    call_id="write-1",
                    name="write",
                    arguments={"file_path": FILE_NAME, "content": FILE_CONTENT},
                ),
            ),
        ),
        AIMessage(
            content="",
            tool_calls=(
                ToolCall(
                    call_id="read-1",
                    name="read",
                    arguments={"file_path": FILE_NAME},
                ),
            ),
        ),
        AIMessage(content=f"已写入并读取 {FILE_NAME}：{FILE_CONTENT}"),
    ]
    index = 0

    def respond(_message: Message, _context: Context) -> AIMessage:
        nonlocal index
        answer = script[min(index, len(script) - 1)]
        index += 1
        return answer

    return respond


@pytest.mark.e2e
def test_agent_writes_and_reads_a_workspace_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(tmp_path)
    invoker = ScriptedModelInvoker(_tool_then_read_responder())
    install_scripted_model(monkeypatch, invoker)
    install_cli_config(monkeypatch, config)

    code, payload, stderr = run_cli(
        capsys,
        [
            "--workspace-root",
            str(config.workspace_root),
            "session",
            "run",
            "--new",
            "-m",
            f"请创建 {FILE_NAME} 并读取它，告诉我内容。",
        ],
    )
    assert code == 0, stderr
    assert payload["status"] == "passed"

    written = Path(config.workspace_root) / FILE_NAME
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == FILE_CONTENT
    assert FILE_CONTENT in payload["final_answer"]
    # write + read + final answer require three model calls in one turn.
    assert len(invoker.requests) >= 3
