"""standard_agent.interfaces — 标准 Agent 接口层。

从 src/lara/agents（runner.py / main.py / fork.py / child.py / context.py）
梳理出的框架无关接口：任何 agent 实现（pygent 引擎或其他引擎）只要满足这里的
协议，就能以同一种方式被调用、注册工具、限制预算。

设计不变量（沿用 lara-architecture）：

- G4 工具面唯一入口：agent 只持有 :class:`ToolProfile` 引用，工具面由组合根
  注册进 ToolRegistry；agent 永远不能提交可执行模块。
- G5 预算在执行侧：:class:`AgentLimits` 属于引擎/runner，不跟随 agent 定义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from lara.agents.runner import ToolProfile

# 压缩窗口禁用哨兵：远超任何真实模型的上下文窗口。
DISABLED_COMPRESSION_WINDOW_TOKENS = 1 << 60


@dataclass(frozen=True)
class AgentLimits:
    """单次运行的预算上限（默认值沿用 lara ``AgentRunner`` 的后台任务实践）。

    压缩三参数（``context_window_tokens`` / ``compression_trigger_ratio`` /
    ``max_compressions``）是上下文压缩预算，由引擎解释；预算始终留在
    引擎侧，不跟随 agent 定义（G5）。
    """

    max_steps: int = 16
    max_model_calls: int = 16
    max_tool_calls: int = 64
    context_window_tokens: int = DISABLED_COMPRESSION_WINDOW_TOKENS
    compression_trigger_ratio: float = 0.9
    max_compressions: int = 1


@dataclass(frozen=True)
class AgentInput:
    """一次 agent 调用的标准输入。

    ``system_prompt`` 用于全新会话；延续既有上下文走引擎层的 ``prefix``
    （见 engine.ContextWindow），两者互斥。``tools`` / ``limits`` 省略时
    分别回退到引擎默认工具面与默认预算。
    """

    message: str
    system_prompt: str | None = None
    tools: ToolProfile | None = None
    limits: AgentLimits | None = None
    label: str | None = None


@dataclass(frozen=True)
class AgentResult:
    """一次 agent 调用的标准输出。

    ``text`` 是最终回答文本；``answer`` / ``context`` 保留引擎原始对象，
    供需要消息历史、metadata 等细粒度信息的调用方使用。
    """

    label: str
    text: str
    answer: Any = None
    context: Any = None


@runtime_checkable
class Agent(Protocol):
    """标准 agent 协议：``run(AgentInput) -> AgentResult``。

    实现允许接受更宽的输入（例如同时接受纯字符串），但输出必须始终是
    :class:`AgentResult`。
    """

    async def run(self, request: AgentInput) -> AgentResult: ...
