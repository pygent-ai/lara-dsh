"""standard_agent — 从 lara agent 脚手架梳理出的标准接口与试点实现。

分层：

- ``interfaces``: 框架无关的标准接口（Agent 协议、AgentInput/AgentResult、
  ToolProfile、AgentLimits）。
- ``engine``: pygent 引擎适配（ToolSurface/ToolRegistry、ContextWindow、
  PygentEngine、生产模型层工厂）。
- ``simple_agent``: 试点实现（SimpleAgent，独立 agent 语义，对应 lara ChildAgent）。
- ``lara_rt``: 真实 lara 运行时适配（LaraSessionAgent、永续会话与实时对话开关）。
"""

from .engine import (
    ContextWindow,
    ModelFactory,
    PygentEngine,
    ToolRegistry,
    ToolSurface,
    ToolkitToolSurface,
    build_model_layer_factory,
    result_window,
)
from .interfaces import (
    Agent,
    AgentInput,
    AgentLimits,
    AgentResult,
    DISABLED_COMPRESSION_WINDOW_TOKENS,
    ToolProfile,
)
from .simple_agent import SimpleAgent, SimpleAgentSpec

__all__ = [
    "DISABLED_COMPRESSION_WINDOW_TOKENS",
    "Agent",
    "AgentInput",
    "AgentLimits",
    "AgentResult",
    "ContextWindow",
    "ModelFactory",
    "PygentEngine",
    "SimpleAgent",
    "SimpleAgentSpec",
    "ToolProfile",
    "ToolRegistry",
    "ToolSurface",
    "ToolkitToolSurface",
    "build_model_layer_factory",
    "result_window",
]
