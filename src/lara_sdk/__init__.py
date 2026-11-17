"""lara_sdk — lara 的对外使用契约与入口。

``lara`` 是完整实现（agent 构造、runner、运行时）；``lara_sdk`` 只描述调用方
如何**使用** agent：

- ``interfaces``: 框架无关的标准协议与数据契约（Agent 协议、
  AgentInput/AgentResult、ToolProfile、AgentLimits）。
- ``lara_rt``: 真实 lara 运行时适配器（LaraSessionAgent、永续会话与实时对话）。
- ``agent``: 完整 Agent 门面（Lara），组合运行时会话、永续会话、实时对话与
  内嵌调度器，提供 start/run/schedule_task/emit_event/tasks/history/stop。
- ``execution_host``: 运行时组合点（LaraExecutionHost），宿主在此注册 speak
  channel 等出口；桌宠、TTS、聊天桥接都是一个 SpeakChannel。

本层不提供 agent 构造或试点实现；需要构造 agent 时使用 ``lara`` 的实现层
（如 ``lara.agents.runner.AgentRunner`` / ``lara.agents.ChildAgent``）。
"""

from .agent import Lara, LaraConfig, SchedulerSettings
from .interfaces import (
    Agent,
    AgentInput,
    AgentLimits,
    AgentResult,
    DISABLED_COMPRESSION_WINDOW_TOKENS,
    ToolProfile,
)
from .lara_rt import REALTIME_SESSION_MODE, LaraSessionAgent

__all__ = [
    "DISABLED_COMPRESSION_WINDOW_TOKENS",
    "REALTIME_SESSION_MODE",
    "Agent",
    "AgentInput",
    "AgentLimits",
    "AgentResult",
    "Lara",
    "LaraConfig",
    "LaraSessionAgent",
    "SchedulerSettings",
    "ToolProfile",
]
