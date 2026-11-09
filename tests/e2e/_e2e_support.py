"""Shared helpers for the end-to-end functional-anchor scenarios.

The suite deliberately runs the real runtime, session store, tool pipeline
and scheduler. Only the remote model is replaced by a scripted in-process
invoker, so every scenario is deterministic and needs no credentials.
"""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pygent import AIMessage, Context, Message
from pygent.llm import ModelExecution, ModelProviderResponse

from lara.schema import ResolvedAgentConfig, RunConfig

Responder = Callable[[Message, Context], AIMessage | Awaitable[AIMessage]]

# A complete native model configuration without a network credential. The
# scripted invoker replaces the provider, but the runtime still needs concrete
# model/group entries to assemble its managed model layers.
MODEL_CONFIG_MAPPING: dict[str, Any] = {
    "connections": {
        "e2e": {
            "provider": "e2e-scripted",
            "credential": {"none": True},
            "protocols": {
                "openai_chat_completions": {"base_url": "http://127.0.0.1:1"}
            },
        }
    },
    "models": {
        "scripted": {
            "connection": "e2e",
            "model_id": "scripted",
            "protocol": "openai_chat_completions",
            "capabilities": {
                "modalities": {"input": ["text"], "output": ["text"]},
                "streaming": {"output": []},
                "tools": {
                    "call": True,
                    "choice": ["auto"],
                    "parallel": False,
                },
                "structured_output": {"json_object": False, "json_schema": False},
                "reasoning": {"supported": False, "controllable": False},
                "limits": {"context_tokens": 100_000, "max_output_tokens": 4_096},
            },
        }
    },
    "model_groups": {"e2e": {"models": ["scripted"]}},
}


def reasoning_controllable_model_config_mapping() -> dict[str, Any]:
    """A scripted model whose reasoning effort can be steered externally."""

    mapping = copy.deepcopy(MODEL_CONFIG_MAPPING)
    mapping["models"]["scripted"]["capabilities"]["reasoning"] = {
        "supported": True,
        "controllable": True,
    }
    return mapping


def make_config(
    tmp_path: Path,
    *,
    realtime: bool = False,
    scheduler_agent_create: bool = False,
    model_config_mapping: dict[str, Any] | None = None,
) -> RunConfig:
    """Create one isolated workspace/user-root pair for a scenario."""

    workspace = tmp_path / "workspace"
    user_root = tmp_path / "user"
    workspace.mkdir(parents=True, exist_ok=True)
    user_root.mkdir(parents=True, exist_ok=True)
    return RunConfig(
        workspace_root=str(workspace),
        user_lara_root=str(user_root),
        model_config_mapping=(
            MODEL_CONFIG_MAPPING
            if model_config_mapping is None
            else model_config_mapping
        ),
        resolved_agent=ResolvedAgentConfig(
            alias="default", default_model_group="e2e"
        ),
        realtime_interaction={"enabled": realtime},  # type: ignore[arg-type]
        # CLI session turns are noninteractive; the scenario tool pipeline is
        # exercised without pausing for an approval that no test user can give.
        runtime_approvals={"enabled": False},  # type: ignore[arg-type]
        scheduler={"agent_create_enabled": scheduler_agent_create},  # type: ignore[arg-type]
    )


class ScriptedModelInvoker:
    """Minimal ``ModelInvoker`` driven by a Python responder."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.requests: list[tuple[Message, Context]] = []
        self.closed = False

    def execute(
        self,
        *,
        model_group: Any,
        retry_policy: Any,
        generation: Any,
        message: Message,
        context: Context,
        tools: tuple[Any, ...] = (),
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> ModelExecution:
        del model_group, retry_policy, generation, tools, deadline, cancel_event
        self.requests.append((message, context))

        async def operation(_emit: Any) -> ModelProviderResponse:
            answer = self.responder(message, context)
            if inspect.isawaitable(answer):
                answer = await answer
            return ModelProviderResponse(message=answer, finish_reason="stop")

        return ModelExecution(operation)

    async def aclose(self) -> None:
        self.closed = True

    async def close(self) -> None:
        self.closed = True


def install_scripted_model(monkeypatch: Any, invoker: ScriptedModelInvoker) -> None:
    """Make every new LaraAgent use the scripted model instead of a provider."""

    monkeypatch.setattr(
        "lara.runtime.agent.core.build_model_invoker",
        lambda *_args, **_kwargs: invoker,
    )


def install_cli_config(monkeypatch: Any, config: RunConfig) -> None:
    """Point the CLI session and scheduler adapters at the scenario config."""

    import lara.cli.scheduler as scheduler_cli
    import lara.cli.sessions as sessions_cli

    monkeypatch.setattr(sessions_cli, "load_run_config", lambda **_kwargs: config)
    monkeypatch.setattr(scheduler_cli, "load_run_config", lambda **_kwargs: config)


def run_cli(capsys: Any, argv: list[str]) -> tuple[int, Any, str]:
    """Invoke the real CLI entry point and parse its JSON stdout."""

    from lara.cli.main import main

    code = main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return code, payload, captured.err


def content_text(message: Message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


def context_text(context: Context) -> str:
    return "\n".join(content_text(item) for item in context.messages)


__all__ = [
    "MODEL_CONFIG_MAPPING",
    "RunConfig",
    "ScriptedModelInvoker",
    "content_text",
    "context_text",
    "install_cli_config",
    "install_scripted_model",
    "make_config",
    "run_cli",
]
