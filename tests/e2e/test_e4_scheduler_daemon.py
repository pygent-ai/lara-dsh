"""E4: CLI task submission -> daemon ``--once`` -> dedicated session turn.

Anchors: signed user submission through ``lara scheduler add``, daemon
promotion and due-task execution through ``lara scheduler run --once``, the
dedicated session turn, and durable scheduler ``runs`` rows.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest
from pygent import AIMessage

import lara_shell.ops.cli.scheduler as scheduler_cli
from _e2e_support import (
    ScriptedModelInvoker,
    install_cli_config,
    install_scripted_model,
    make_config,
    run_cli,
)
from lara_shell.ops.cli.main import main as cli_main
from pygent.agent.pygent_v1.scheduler.daemon import SchedulerDaemon, TaskResult
from pygent.agent.pygent_v1.scheduler.message import build_task_message
from pygent.agent.pygent_v1.scheduler.store import SchedulerStore, scheduler_db_path
from pygent.agent.pygent_v1.sessions import SessionManager


class InProcessTaskRunner:
    """Run each scheduler task through the real CLI session-turn path."""

    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root
        self.tasks: list[object] = []
        self.outputs: list[str] = []

    def run(self, task: object) -> TaskResult:
        self.tasks.append(task)
        message = build_task_message(
            task_id=task.id,  # type: ignore[attr-defined]
            name=task.name,  # type: ignore[attr-defined]
            source=task.source,  # type: ignore[attr-defined]
            instructions=task.instructions,  # type: ignore[attr-defined]
            user_message=task.prompt,  # type: ignore[attr-defined]
            user_identity="default",
        )
        out = io.StringIO()
        err = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli_main(
                [
                    "--workspace-root",
                    self.workspace_root,
                    "session",
                    "run",
                    task.session,  # type: ignore[attr-defined]
                    "-m",
                    message,
                ]
            )
        output = out.getvalue() + err.getvalue()
        self.outputs.append(output)
        return TaskResult(
            exit_code=code,
            output=output,
            error=None if code == 0 else output,
        )


@pytest.mark.e2e
def test_scheduler_cli_add_daemon_once_runs_dedicated_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(tmp_path)
    invoker = ScriptedModelInvoker(
        lambda _message, _context: AIMessage(content="scheduler task executed")
    )
    install_scripted_model(monkeypatch, invoker)
    install_cli_config(monkeypatch, config)
    workspace = str(config.workspace_root)

    code, keys, stderr = run_cli(
        capsys, ["--workspace-root", workspace, "scheduler", "init-keys"]
    )
    assert code == 0, stderr
    assert keys["status"] == "ready"

    manager = SessionManager(config)
    dedicated = manager.create("scheduler", mode="chat")

    code, submitted, stderr = run_cli(
        capsys,
        [
            "--workspace-root",
            workspace,
            "scheduler",
            "add",
            "--name",
            "e2e-task",
            "--schedule",
            "at 2020-01-01T00:00:00+00:00",
            "--prompt",
            "执行 E4 定时任务",
            "--session",
            dedicated.session_id,
        ],
    )
    assert code == 0, stderr
    assert submitted["kind"] == "task"
    assert submitted["source"] == "user"

    runner = InProcessTaskRunner(workspace)
    real_daemon = SchedulerDaemon

    def daemon_factory(run_config: object, **kwargs: object) -> SchedulerDaemon:
        return real_daemon(run_config, runner=runner, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(scheduler_cli, "SchedulerDaemon", daemon_factory)
    code, cycle, stderr = run_cli(
        capsys, ["--workspace-root", workspace, "scheduler", "run", "--once"]
    )
    assert code == 0, stderr
    assert cycle["submissions"] == 1
    assert cycle["tasks"] == 1
    assert len(runner.tasks) == 1

    store = SchedulerStore(scheduler_db_path(config.pygent_root))
    task = store.list_tasks()[0]
    assert task.session == dedicated.session_id
    runs = store.list_runs(task_id=task.id)
    assert len(runs) == 1
    assert runs[0].status == "success"
    assert "scheduler task executed" in runs[0].output

    turn_payload = json.loads(runner.outputs[0])
    assert turn_payload["session_id"] == dedicated.session_id
    assert turn_payload["status"] == "passed"
    assert turn_payload["final_answer"] == "scheduler task executed"
    assert Path(turn_payload["run_dir"]).is_dir()

    session = manager.load(dedicated.session_id)
    assert "scheduler task executed" in "\n".join(
        str(item) for item in session.history
    )
