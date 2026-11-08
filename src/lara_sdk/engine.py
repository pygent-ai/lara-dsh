"""lara_sdk.engine — pygent 引擎适配层。

把 src/lara/agents/runner.py（``AgentRunner`` 调用缝）梳理为独立引擎：

- :class:`ToolSurface` / :class:`ToolRegistry` / :class:`ToolkitToolSurface`:
  统一工具面（G4），工具定义与执行始终来自同一来源。
- :class:`ContextWindow`: 可延续的上下文窗口（lara fork block-1 的最小形态）。
- :class:`PygentEngine`: 组装并驱动 pygent ReAct 循环的单一调用缝；每次运行
  新建 agent，并发运行之间不共享可变状态。
- :func:`build_model_layer_factory`: 生产用模型层工厂，重试策略对齐 lara。

框架无关的接口在 :mod:`lara_sdk.interfaces`；本模块是 pygent 绑定。
"""

from __future__ import annotations

from dataclasses import replace

from pygent import PygentAgent, UserMessage
from pygent.core import JsonValue

# 工具面/上下文/模型层与 lara agent 脚手架共用同一实现；lara_sdk
# 只按本层授权 reason_code 派生 ToolkitToolSurface（见下）。
from lara.agents.runner import (
    MODEL_RETRYABLE_ERROR_KINDS,
    ContextWindow,
    ModelFactory,
    ToolProfile,
    ToolRegistry,
    ToolSurface,
    _NoToolLayer,
    build_model_layer_factory,
)
from lara.agents.runner import ToolkitToolSurface as _ToolkitToolSurface

from .interfaces import AgentInput, AgentLimits, AgentResult


class ToolkitToolSurface(_ToolkitToolSurface):
    """进程内工具面；仅授权 reason_code 使用标准层的取值。"""

    _AUTHORIZATION_REASON_CODE = "lara-uniform-tool-surface"


class PygentEngine:
    """pygent ReAct 引擎：所有标准 agent 的单一调用缝。

    对应 lara 的 ``AgentRunner``：解析工具面 -> 以解析出的定义构建模型层 ->
    组装全新 ``PygentAgent`` 并驱动一次 ReAct 运行。``prefix`` 提供延续
    上下文（fork block-1），``request.system_prompt`` 提供全新会话提示词，
    两者必须恰好给出其一。
    """

    def __init__(
        self,
        *,
        model_factory: ModelFactory,
        registry: ToolRegistry | None = None,
        default_tools: ToolProfile | None = None,
        limits: AgentLimits | None = None,
        compression_prompt: str = (
            "Compress this agent run's transcript without losing tool errors "
            "or tool results."
        ),
    ) -> None:
        self._model_factory = model_factory
        self._registry = registry
        self._default_tools = default_tools
        self._limits = limits or AgentLimits()
        self._compression_prompt = compression_prompt

    async def run(
        self,
        request: AgentInput,
        *,
        prefix: ContextWindow | None = None,
    ) -> AgentResult:
        """运行一次 ReAct 调用。

        与 ``lara.agents.runner.AgentRunner.run`` 是镜像实现（入参/返回类型
        不同）；修改任一侧时必须同步另一侧。
        """

        if (prefix is None) == (request.system_prompt is None):
            raise ValueError(
                "provide exactly one of a prefix ContextWindow or "
                "AgentInput.system_prompt"
            )
        surface: ToolSurface | None = None
        profile = request.tools if request.tools is not None else self._default_tools
        if profile is not None:
            if self._registry is None:
                raise ValueError(
                    "engine has no ToolRegistry; cannot resolve the ToolProfile"
                )
            surface = self._registry.resolve(profile)
        limits = request.limits if request.limits is not None else self._limits
        label = request.label or "lara"
        definitions = surface.definitions if surface is not None else ()
        system_prompt = (
            prefix.system_prompt if prefix is not None else request.system_prompt
        )
        assert system_prompt is not None
        model = self._model_factory(definitions)
        agent = PygentAgent(
            system_prompt=system_prompt,
            compression_prompt=self._compression_prompt,
            model=model,
            compressor=model,
            tools=surface.tool_module() if surface is not None else _NoToolLayer(),
            context_window_tokens=limits.context_window_tokens,
            compression_trigger_ratio=limits.compression_trigger_ratio,
            max_compressions=limits.max_compressions,
            max_steps=limits.max_steps,
            max_model_calls=limits.max_model_calls,
            max_tool_calls=limits.max_tool_calls,
        )
        # 与 lara 运行时的 metadata 键惯例对齐（runner/fork 同用此键）。
        metadata: dict[str, JsonValue] = {"lara.agent.label": label}
        if prefix is not None:
            metadata = {**dict(prefix.metadata), **metadata}
        context = agent.new_context(tools=definitions, metadata=metadata)
        if prefix is not None:
            context = replace(
                context,
                system_prompt=prefix.system_prompt,
                messages=prefix.messages,
            )
        answer, final_context = await agent.invoke(
            UserMessage(content=request.message), context
        )
        return AgentResult(
            label=label, text=answer.content, answer=answer, context=final_context
        )
