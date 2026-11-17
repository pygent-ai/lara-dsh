"""离线示例：用 lara 实现层构造 agent，按 lara_sdk 契约调用（无需 API key）。

``lara_sdk`` 是对外使用层，不负责构造 agent；本示例演示调用方如何把
``pygent.agent.pygent_v1.agents.runner.AgentRunner``（实现层）适配成 ``lara_sdk.Agent`` 协议，
再以标准 ``AgentInput`` / ``AgentResult`` 使用它。

模型用脚本假模型顶替，演示两条路径：
1. 纯文本问答（无工具）；
2. 经 ToolRegistry / ToolkitToolSurface 注册的 echo 工具（ReAct 工具轮）。

接入真实模型时，把 ``model_factory`` 换成
``pygent.agent.pygent_v1.agents.runner.build_model_layer_factory(invoker, ...)`` 的产物即可；
真实 lara 会话使用 ``lara_sdk.LaraSessionAgent``（见 run_lara_session_agent.py）。
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

from pygent.agent.pygent_v1.agents.runner import AgentRunner, ToolRegistry, ToolkitToolSurface
from lara_sdk import Agent, AgentInput, AgentLimits, AgentResult, ToolProfile


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
    tool_id="lara.example.echo",
    version="1.0.0",
    side_effect=ToolSideEffect.PURE,
    idempotency=IdempotencyPolicy.INHERENT,
    name="echo",
    description="Echo the given text back.",
    timeout=30,
    resource_key="lara-example",
    required_permissions=(),
)
async def echo(text: str) -> str:
    return f"echo:{text}"


class _RunnerAgent:
    """把 lara 的 ``AgentRunner``（实现层）适配为 ``lara_sdk.Agent`` 协议。"""

    def __init__(
        self, *, runner: AgentRunner, system_prompt: str, label: str
    ) -> None:
        self._runner = runner
        self._system_prompt = system_prompt
        self._label = label

    async def run(self, request: AgentInput | str) -> AgentResult:
        if isinstance(request, str):
            request = AgentInput(message=request)
        result = await self._runner.run(
            message=request.message,
            system_prompt=(
                request.system_prompt
                if request.system_prompt is not None
                else self._system_prompt
            ),
            tools=request.tools,
            limits=request.limits,
            label=request.label or self._label,
        )
        return AgentResult(
            label=result.label,
            text=result.text,
            answer=result.answer,
            context=result.context,
        )


async def main() -> None:
    # 1) 无工具：一条消息进，一段回答出。
    plain_agent: Agent = _RunnerAgent(
        runner=AgentRunner(
            model_factory=lambda _definitions: _ScriptedModel(
                AIMessage(content="你好！我是按标准接口跑起来的 agent。")
            )
        ),
        system_prompt="You are a helpful agent.",
        label="demo",
    )
    result = await plain_agent.run("打个招呼")
    print("[无工具]", result.text)

    # 2) 带工具：组合根注册工具面，调用方只提交 ToolProfile 引用（G4）。
    registry = ToolRegistry()
    registry.register("echo", ToolkitToolSurface(ToolKit(echo)))
    tool_agent: Agent = _RunnerAgent(
        runner=AgentRunner(
            model_factory=lambda _definitions: _ScriptedModel(
                AIMessage(
                    content="",
                    tool_calls=(
                        ToolCall(
                            call_id="t1", name="echo", arguments={"text": "hi"}
                        ),
                    ),
                ),
                AIMessage(content="工具返回：echo:hi"),
            ),
            registry=registry,
        ),
        system_prompt="You may use tools.",
        label="tool-demo",
    )
    result = await tool_agent.run(
        AgentInput(
            message="用 echo 工具回显 hi",
            tools=ToolProfile("echo"),
            limits=AgentLimits(max_steps=4, max_model_calls=4, max_tool_calls=4),
        )
    )
    print("[带工具]", result.text)


if __name__ == "__main__":
    asyncio.run(main())
