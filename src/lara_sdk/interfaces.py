"""lara_sdk.interfaces — 标准 Agent 接口层。

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

from lara.agents.runner import (
    DISABLED_COMPRESSION_WINDOW_TOKENS,
    AgentLimits,
    ToolProfile,
)

# 数据契约复用实现层的类型：调用方拿到的 AgentLimits / ToolProfile 与
# lara.agents.runner 是同一类型，可直接传给实现层，避免双份定义漂移。


@dataclass(frozen=True)
class AgentInput:
    """一次 agent 调用的标准输入。

    ``system_prompt`` 用于全新会话；延续既有上下文走实现层的 ``prefix``
    （``lara.agents.context.ContextWindow``），两者互斥。``tools`` / ``limits``
    省略时分别回退到实现层的默认工具面与默认预算。``attachments`` 是本轮
    随消息交付的工作区相对文件路径（最多 10 个，图片/视频走原生多模态，
    文本内嵌，其余按引用交付）。
    """

    message: str
    system_prompt: str | None = None
    tools: ToolProfile | None = None
    limits: AgentLimits | None = None
    label: str | None = None
    attachments: tuple[str, ...] = ()


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
