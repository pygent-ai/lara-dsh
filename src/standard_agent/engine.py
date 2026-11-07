"""standard_agent.engine — pygent 引擎适配层。

把 src/lara/agents/runner.py（``AgentRunner`` 调用缝）梳理为独立引擎：

- :class:`ToolSurface` / :class:`ToolRegistry` / :class:`ToolkitToolSurface`:
  统一工具面（G4），工具定义与执行始终来自同一来源。
- :class:`ContextWindow`: 可延续的上下文窗口（lara fork block-1 的最小形态）。
- :class:`PygentEngine`: 组装并驱动 pygent ReAct 循环的单一调用缝；每次运行
  新建 agent，并发运行之间不共享可变状态。
- :func:`build_model_layer_factory`: 生产用模型层工厂，重试策略对齐 lara。

框架无关的接口在 :mod:`standard_agent.interfaces`；本模块是 pygent 绑定。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, cast, runtime_checkable

from pygent import (
    AIMessage,
    Context,
    ExponentialBackoff,
    GenerationConfig,
    Message,
    ModelCallLayer,
    ModelErrorKind,
    Module,
    ModelGroup,
    PygentAgent,
    RetryPolicy,
    ToolAuthorizationDecision,
    ToolAuthorizationRequest,
    ToolDefinition,
    ToolKit,
    ToolMessage,
    UserMessage,
)
from pygent.core import JsonValue
from pygent.llm import ModelCallPolicy, ModelEntry, ModelInvoker

from .interfaces import AgentInput, AgentLimits, AgentResult, ToolProfile

# 与 lara.runtime.agent.core.MODEL_RETRYABLE_ERROR_KINDS 对齐的模型层可重试错误。
MODEL_RETRYABLE_ERROR_KINDS = (
    ModelErrorKind.TIMEOUT,
    ModelErrorKind.RATE_LIMIT,
    ModelErrorKind.UNAVAILABLE,
    ModelErrorKind.INVALID_RESPONSE,
)

ModelFactory = Callable[[tuple[ToolDefinition, ...]], Module[Message, AIMessage]]


@dataclass(frozen=True)
class ContextWindow:
    """一等、可延续的上下文窗口。

    lara 中它是 fork 原语的流通货币（G3）；这里保留最小形态：系统提示词 +
    消息序列 + metadata，可从上一次运行的产物构造并继续对话。
    """

    system_prompt: str
    messages: tuple[Message, ...] = ()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.system_prompt, str) or not self.system_prompt.strip():
            raise ValueError("ContextWindow.system_prompt must be a non-empty string")
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "metadata", dict(self.metadata))


@runtime_checkable
class ToolSurface(Protocol):
    """一个工具面：模型可见的定义 + 可执行的模块（G4 单管线）。

    工具面只应由组合根构建。已知限制（lara-architecture §6.8）：生产前台
    管线（审计、EventStore、文件效果）需要上下文工厂与后置钩子，当前的
    两成员协议承载不了；进程内实现见 :class:`ToolkitToolSurface`。
    """

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]: ...

    def tool_module(self) -> Module[AIMessage, ToolMessage]: ...


class ToolkitToolSurface:
    """基于 pygent ``ToolKit`` 的进程内工具面（无审计/事件管线）。

    适用于组合根与测试；生产运行时应注册走持久工具管线的工具面。
    """

    def __init__(self, toolkit: ToolKit) -> None:
        self._toolkit = toolkit

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._toolkit.definitions

    def tool_module(self) -> Module[AIMessage, ToolMessage]:
        def authorize(
            request: ToolAuthorizationRequest, context: Context
        ) -> ToolAuthorizationDecision:
            del context
            return ToolAuthorizationDecision(
                call_id=request.call.call_id,
                allowed=True,
                reason_code="lara-uniform-tool-surface",
            )

        return self._toolkit.local_layer(authorization_adapter=authorize)


class ToolRegistry:
    """组合根的工具面注册表，按 id 寻址。"""

    def __init__(self) -> None:
        self._surfaces: dict[str, ToolSurface] = {}

    def register(self, surface_id: str, surface: ToolSurface) -> None:
        if not surface_id.strip():
            raise ValueError("surface_id must be non-empty")
        if surface_id in self._surfaces:
            raise ValueError(f"tool surface {surface_id!r} is already registered")
        self._surfaces[surface_id] = surface

    def resolve(self, profile: ToolProfile) -> ToolSurface:
        try:
            return self._surfaces[profile.surface_id]
        except KeyError:
            raise ValueError(
                f"tool surface {profile.surface_id!r} is not registered; tool "
                "profiles must reference surfaces registered by the composition root"
            ) from None


class _NoToolLayer(Module[AIMessage, ToolMessage]):
    """未配置工具面时的占位层；模型发出工具调用即为引擎缺陷。"""

    async def forward(
        self, message: AIMessage, context: Context
    ) -> tuple[ToolMessage, Context]:
        raise RuntimeError(
            "agent run has no tool surface configured but the model emitted "
            f"a tool call ({message.tool_calls!r})"
        )


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


def result_window(result: AgentResult) -> ContextWindow:
    """把一次运行的结果上下文转成可延续窗口（fork 链式派生的素材）。"""

    context = cast("Context", result.context)
    return ContextWindow(
        system_prompt=context.system_prompt,
        messages=tuple(context.messages),
        metadata=cast("Mapping[str, JsonValue]", context.metadata),
    )


def build_model_layer_factory(
    invoker: ModelInvoker,
    *,
    model_group_name: str,
    models: Sequence[ModelEntry],
    max_attempts_per_model: int = 3,
    attempt_idle_timeout_seconds: float = 120.0,
) -> ModelFactory:
    """构建生产用 :class:`ModelFactory`（重试策略对齐 lara 后台任务默认值）。

    工厂接收每次运行解析出的工具定义，保证模型可见工具与可执行工具面
    同源（G4）。``models`` 是运行配置中模型组的 ``ModelEntry`` 列表，
    ``invoker`` 在本地执行请求。
    """

    def factory(definitions: tuple[ToolDefinition, ...]) -> ModelCallLayer:
        return ModelCallLayer(
            model_group=ModelGroup(name=model_group_name, models=tuple(models)),
            policy=ModelCallPolicy(),
            retry_policy=RetryPolicy(
                max_attempts_per_model=max_attempts_per_model,
                retry_on=MODEL_RETRYABLE_ERROR_KINDS,
                attempt_idle_timeout_seconds=attempt_idle_timeout_seconds,
                backoff=ExponentialBackoff(initial=0.5, maximum=4.0, multiplier=2.0),
            ),
            generation=GenerationConfig(),
            tools=tuple(definitions),
            invoker=invoker,
        )

    return factory
