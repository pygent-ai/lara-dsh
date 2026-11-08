"""lara_sdk.simple_agent — 试点：符合标准接口的独立 agent。

对应 lara 的 ``ChildAgent`` 语义（G2 独立背景 agent）：自己的系统提示词、
全新上下文、工具一律通过 :class:`~lara_sdk.interfaces.ToolProfile`
引用注册的工具面；执行由 :class:`~lara_sdk.engine.PygentEngine` 驱动。

选它作为第一个标准化对象，因为它不依赖会话历史与 fork 机制，是最小的
完整 agent。fork / main 的标准化复用同一引擎与接口即可。
"""

from __future__ import annotations

from dataclasses import dataclass

from .engine import PygentEngine
from .interfaces import AgentInput, AgentLimits, AgentResult, ToolProfile


@dataclass(frozen=True)
class SimpleAgentSpec:
    """声明式描述一个独立 agent。"""

    name: str
    system_prompt: str
    tools: ToolProfile | None = None
    limits: AgentLimits | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("SimpleAgentSpec.name must be non-empty")
        if not self.system_prompt.strip():
            raise ValueError("SimpleAgentSpec.system_prompt must be non-empty")


class SimpleAgent:
    """标准 Agent 协议的试点实现：独立、无状态、每次运行全新上下文。

    ``run`` 同时接受纯字符串（便捷路径）与完整 :class:`AgentInput`（覆盖
    工具 / 预算 / 标签）；输入省略的字段回退到 spec，spec 省略的回退到
    引擎默认。
    """

    def __init__(
        self,
        spec: SimpleAgentSpec,
        *,
        engine: PygentEngine,
        label: str | None = None,
    ) -> None:
        self._spec = spec
        self._engine = engine
        self._label = label or f"simple-agent.{spec.name}"

    async def run(self, request: AgentInput | str) -> AgentResult:
        if isinstance(request, str):
            request = AgentInput(message=request)
        return await self._engine.run(
            AgentInput(
                message=request.message,
                system_prompt=(
                    request.system_prompt
                    if request.system_prompt is not None
                    else self._spec.system_prompt
                ),
                tools=(
                    request.tools if request.tools is not None else self._spec.tools
                ),
                limits=(
                    request.limits if request.limits is not None else self._spec.limits
                ),
                label=request.label or self._label,
            )
        )
