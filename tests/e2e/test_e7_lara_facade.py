"""E7: the complete ``lara_sdk.Lara`` facade from a clean workspace.

Anchor: construct ``Lara`` from :class:`LaraConfig` only, start the session and
the embedded scheduler, run a conversation turn, submit an agent task, let the
embedded loop execute it, and stop. The real session store, orchestration,
tool pipeline and scheduler run; only the remote model is scripted (unless the
explicit real-model smoke test is enabled).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from pygent import AIMessage, Context, Message

from _e2e_support import (
    ScriptedModelInvoker,
    content_text,
    context_text,
    install_scripted_model,
    make_config,
)
from lara_sdk import Lara, LaraConfig, SchedulerSettings


def _text(message: Message, context: Context) -> str:
    return f"{content_text(message)}\n{context_text(context)}"


def _responder(message: Message, context: Context) -> AIMessage:
    if "task-context" in _text(message, context):
        return AIMessage(content="scheduled facade e2e")
    return AIMessage(content="facade e2e answer")


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.e2e
async def test_lara_facade_from_config_to_scheduled_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, scheduler_agent_create=True)
    invoker = ScriptedModelInvoker(_responder)
    install_scripted_model(monkeypatch, invoker)
    monkeypatch.setattr("lara_sdk.agent.load_run_config", lambda **_kwargs: config)

    facade = Lara(
        LaraConfig(
            workspace_root=config.workspace_root,
            model_group="e2e",
            realtime=False,
            scheduler=SchedulerSettings(
                enabled=True,
                poll_seconds=0.02,
                agent_create_enabled=True,
            ),
        )
    )
    await facade.start()
    try:
        result = await facade.run("你好，门面")
        assert result.text == "facade e2e answer"
        assert facade.history()

        task = await facade.schedule_task(
            "facade-e2e",
            "at 2020-01-01T00:00:00+00:00",
            "execute the scheduled facade task",
        )
        assert task.source == "agent"
        # pygent records the run when it starts, so a non-empty `runs()` is the
        # in-flight row; wait for it to reach a terminal status.
        await _wait_until(
            lambda: any(r.status != "running" for r in facade.tasks().runs(task.id))
        )

        runs = facade.tasks().runs(task.id)
        assert len(runs) == 1
        assert runs[0].status == "success"
        assert "scheduled facade e2e" in runs[0].output
        # One scripted model call for the chat turn, one for the task turn.
        assert len(invoker.requests) >= 2
    finally:
        await facade.stop()


@pytest.mark.e2e
@pytest.mark.skipif(
    os.environ.get("LARA_E2E_REAL_MODEL") != "1",
    reason="real-model smoke test; set LARA_E2E_REAL_MODEL=1 to enable",
)
async def test_lara_facade_real_model_smoke(tmp_path: Path) -> None:
    """One real provider call through the facade (credentials from ~/.lara)."""

    facade = Lara(
        LaraConfig(
            workspace_root=tmp_path / "workspace",
            realtime=False,
            scheduler=SchedulerSettings(enabled=False),
        )
    )
    await facade.start()
    try:
        result = await facade.run("Reply with exactly: facade-ok")
        assert "facade-ok" in result.text.lower()
        assert facade.history()
    finally:
        await facade.stop()
