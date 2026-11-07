"""standard_agent 试点实现的单元测试。

沿用 tests/test_lara_agents.py 的假模型手法：脚本化 ``Module``
驱动 pygent ReAct 循环，不访问网络。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pygent import (
    AIMessage,
    Context,
    IdempotencyPolicy,
    Message,
    Module,
    ToolCall,
    ToolKit,
    ToolMessage,
    ToolResult,
    ToolSideEffect,
    UserMessage,
    tool,
)

from standard_agent import (
    Agent,
    AgentInput,
    AgentResult,
    ContextWindow,
    PygentEngine,
    SimpleAgent,
    SimpleAgentSpec,
    ToolProfile,
    ToolRegistry,
    ToolkitToolSurface,
)


class _ScriptState:
    """脚本模型的可变状态（trusted live resource）。"""

    __slots__ = ("answers", "seen")

    def __init__(self, answers: Sequence[AIMessage]) -> None:
        self.answers = list(answers)
        self.seen: list[tuple[Message, Context]] = []


class _ScriptedModel(Module[Message, AIMessage]):
    """按脚本依次返回答案的假模型；最后一个答案循环复用。"""

    trusted_live_resource_attributes = ("state",)

    def __init__(self, script: Sequence[AIMessage]) -> None:
        super().__init__()
        if not script:
            raise ValueError("script must contain at least one answer")
        self.state = _ScriptState(script)

    async def forward(
        self, message: Message, context: Context
    ) -> tuple[AIMessage, Context]:
        self.state.seen.append((message, context))
        if len(self.state.answers) > 1:
            return self.state.answers.pop(0), context
        return self.state.answers[0], context


@tool(
    tool_id="lara.test.echo",
    version="1.0.0",
    side_effect=ToolSideEffect.PURE,
    idempotency=IdempotencyPolicy.INHERENT,
    name="echo",
    description="Echo the given text back.",
    timeout=30,
    resource_key="lara-agents-test",
    required_permissions=(),
)
async def echo(text: str) -> str:
    return f"echo:{text}"


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register("echo", ToolkitToolSurface(ToolKit(echo)))
    return registry


def _agent(
    script: Sequence[AIMessage], *, with_tools: bool = True
) -> tuple[SimpleAgent, _ScriptedModel]:
    model = _ScriptedModel(script)
    engine = PygentEngine(
        model_factory=lambda _definitions: model,
        registry=_echo_registry() if with_tools else None,
        default_tools=ToolProfile("echo") if with_tools else None,
    )
    agent = SimpleAgent(
        SimpleAgentSpec(name="greeter", system_prompt="You are a standard agent."),
        engine=engine,
    )
    return agent, model


def _tool_call_answer(call_id: str = "t1", text: str = "hi") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=(
            ToolCall(call_id=call_id, name="echo", arguments={"text": text}),
        ),
    )


async def test_simple_agent_without_tools_returns_text() -> None:
    agent, model = _agent([AIMessage(content="done")], with_tools=False)

    result = await agent.run("hello")

    assert result.text == "done"
    assert result.label == "simple-agent.greeter"
    assert isinstance(result, AgentResult)
    # 输入未覆盖时回退到 spec 的系统提示词。
    assert model.state.seen[0][1].system_prompt == "You are a standard agent."


async def test_simple_agent_input_overrides_spec_prompt_and_label() -> None:
    model = _ScriptedModel([AIMessage(content="overridden")])
    engine = PygentEngine(model_factory=lambda _definitions: model)
    agent = SimpleAgent(
        SimpleAgentSpec(name="greeter", system_prompt="spec prompt"),
        engine=engine,
        label="default-label",
    )

    result = await agent.run(
        AgentInput(
            message="hi",
            system_prompt="request prompt",
            label="turn-label",
        )
    )

    assert result.text == "overridden"
    assert result.label == "turn-label"
    assert model.state.seen[0][1].system_prompt == "request prompt"


async def test_simple_agent_satisfies_standard_protocol() -> None:
    agent, _model = _agent([AIMessage(content="ok")])

    assert isinstance(agent, Agent)


async def test_simple_agent_runs_tool_call_and_returns_answer() -> None:
    agent, model = _agent([_tool_call_answer(), AIMessage(content="tool used")])

    result = await agent.run(AgentInput(message="please echo hi"))

    assert result.text == "tool used"
    tool_messages = [
        message for message in result.context.messages if isinstance(message, ToolMessage)
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].results[0].output == "echo:hi"
    # 一次工具轮 + 一次最终轮。
    assert len(model.state.seen) == 2


async def test_unknown_tool_profile_fails_before_any_model_call() -> None:
    agent, model = _agent([AIMessage(content="never")])

    with pytest.raises(ValueError, match="not registered"):
        await agent.run(AgentInput(message="hello", tools=ToolProfile("missing")))

    assert model.state.seen == []


async def test_engine_rejects_missing_and_conflicting_context_origin() -> None:
    engine = PygentEngine(
        model_factory=lambda _definitions: _ScriptedModel([AIMessage(content="x")])
    )

    # 两者都缺。
    with pytest.raises(ValueError, match="exactly one"):
        await engine.run(AgentInput(message="hello"))
    # 两者都给。
    with pytest.raises(ValueError, match="exactly one"):
        await engine.run(
            AgentInput(message="hello", system_prompt="fresh"),
            prefix=ContextWindow(system_prompt="forked"),
        )


async def test_engine_prefix_continues_existing_context() -> None:
    model = _ScriptedModel([AIMessage(content="continued")])
    engine = PygentEngine(model_factory=lambda _definitions: model)
    window = ContextWindow(
        system_prompt="forked prompt",
        messages=(UserMessage(content="earlier"),),
    )

    result = await engine.run(AgentInput(message="continue"), prefix=window)

    assert result.text == "continued"
    assert result.context.system_prompt == "forked prompt"


async def test_spec_rejects_empty_name_and_system_prompt() -> None:
    with pytest.raises(ValueError, match="name"):
        SimpleAgentSpec(name="  ", system_prompt="prompt")
    with pytest.raises(ValueError, match="system_prompt"):
        SimpleAgentSpec(name="agent", system_prompt="   ")
