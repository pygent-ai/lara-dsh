"""离线示例：SimpleAgent 的标准用法（无需任何 API key）。

模型用脚本假模型顶替，演示两条路径：
1. 纯文本问答（无工具）；
2. 经 ToolRegistry / ToolkitToolSurface 注册的 echo 工具（ReAct 工具轮）。

接入真实模型时，把 ``model_factory`` 换成
``build_model_layer_factory(invoker, model_group_name=..., models=...)``
的产物即可，其余接线完全不变（见
``standard_agent.engine.build_model_layer_factory``）。
"""

from __future__ import annotations

import asyncio

from pygent import (
    AIMessage,
    Context,
    IdempotencyPolicy,
    Message,
    Module,
    ToolCall,
    ToolKit,
    ToolSideEffect,
    tool,
)

from standard_agent import (
    PygentEngine,
    SimpleAgent,
    SimpleAgentSpec,
    ToolProfile,
    ToolRegistry,
    ToolkitToolSurface,
)


class _ScriptState:
    """不透明适配器：pygent 要求 live resource 是自定义对象，不能是裸容器。"""

    __slots__ = ("answers",)

    def __init__(self, answers: list[AIMessage]) -> None:
        self.answers = answers


class _ScriptedModel(Module[Message, AIMessage]):
    """最简脚本模型：依次返回预置答案，最后一个循环复用。"""

    trusted_live_resource_attributes = ("state",)

    def __init__(self, *answers: AIMessage) -> None:
        super().__init__()
        self.state = _ScriptState(list(answers))

    async def forward(
        self, message: Message, context: Context
    ) -> tuple[AIMessage, Context]:
        if len(self.state.answers) > 1:
            return self.state.answers.pop(0), context
        return self.state.answers[0], context


@tool(
    tool_id="lara-agent.example.echo",
    version="1.0.0",
    side_effect=ToolSideEffect.PURE,
    idempotency=IdempotencyPolicy.INHERENT,
    name="echo",
    description="Echo the given text back.",
    timeout=30,
    resource_key="lara-agent-example",
    required_permissions=(),
)
async def echo(text: str) -> str:
    return f"echo:{text}"


async def main() -> None:
    # 1) 无工具：一条消息进，一段回答出。
    plain_engine = PygentEngine(
        model_factory=lambda _definitions: _ScriptedModel(
            AIMessage(content="你好！我是按标准接口跑起来的 agent。")
        )
    )
    agent = SimpleAgent(
        SimpleAgentSpec(name="demo", system_prompt="You are a helpful agent."),
        engine=plain_engine,
    )
    result = await agent.run("打个招呼")
    print("[无工具]", result.text)

    # 2) 带工具：组合根注册工具面，agent 只持有 ToolProfile 引用（G4）。
    registry = ToolRegistry()
    registry.register("echo", ToolkitToolSurface(ToolKit(echo)))
    tool_engine = PygentEngine(
        model_factory=lambda _definitions: _ScriptedModel(
            AIMessage(
                content="",
                tool_calls=(
                    ToolCall(call_id="t1", name="echo", arguments={"text": "hi"}),
                ),
            ),
            AIMessage(content="工具返回：echo:hi"),
        ),
        registry=registry,
        default_tools=ToolProfile("echo"),
    )
    tool_agent = SimpleAgent(
        SimpleAgentSpec(name="tool-demo", system_prompt="You may use tools."),
        engine=tool_engine,
    )
    result = await tool_agent.run("用 echo 工具回显 hi")
    print("[带工具]", result.text)


if __name__ == "__main__":
    asyncio.run(main())
