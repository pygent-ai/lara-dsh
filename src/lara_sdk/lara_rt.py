"""lara_sdk.lara_rt — lara 真实运行时的标准 Agent 适配层。

只 import lara 的代码、不复制实现：主 agent harness（``LaraAgent``）、会话
上下文（``SessionManager`` / ``LaraContext`` / 持久历史）、永续会话
（``EternalConversationHarness``）与实时对话（``lara-says`` 会话模式 +
speak/fork/nudge）全部由 lara 自己的组装根完成——本模块通过
``LocalExecutionHost``（lara orchestration 层，CLI 同款驱动）提交会话轮次，
并把结果映射到 :mod:`lara_sdk.interfaces` 的标准协议。

lara 侧的功能开关（均为原生机制）：

- 永续会话：部署总开关 ``RunConfig.eternal_conversation.enabled``（适配层经
  ``eternal_memory`` 参数覆写）+ 会话级 opt-in
  ``SessionManager.set_eternal_memory``；记忆投影经 ``memory_projection()``
  暴露。
- 实时对话：会话 mode = ``lara-says``（``realtime_enabled_for`` 仅对该模式
  开启实时交互，speak 是唯一用户可见输出通道）。

真实模型调用发生在 turn 内部；本模块的单元测试只覆盖模型边界之前的装配。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from lara_shell.config import load_run_config
from lara_shell.credentials import build_credential_environment
from lara.core.io import plain_object
from lara.orchestration import LocalExecutionHost
from lara.runtime.eternal_conversation import eternal_enabled_for, load_projection
from lara.schema import AgentSession, RunConfig
from lara.sessions import SessionManager, realtime_enabled_for
from lara.sessions.manager import LARA_SAYS_SESSION_MODE

from .interfaces import AgentInput, AgentResult

REALTIME_SESSION_MODE = LARA_SAYS_SESSION_MODE


class LaraSessionAgent:
    """会话型标准 agent：lara 主 harness + 上下文 + 永续会话 + 实时对话。

    用法::

        async with LaraSessionAgent(workspace_root=...) as agent:
            result = await agent.run("你好")

        # 实时对话会话（agent 静默工作，speak 为唯一可见输出）：
        async with LaraSessionAgent(mode=REALTIME_SESSION_MODE) as rt: ...

        # 永续会话（同时打开部署总开关与会话级开关）：
        async with LaraSessionAgent(eternal_memory=True) as agent:
            agent.enable_eternal_memory()

    工具、系统提示词与运行预算来自 lara 原生配置（agent profile、外部工具、
    max_steps），``AgentInput`` 的对应字段在 lara 路径下不生效；``label``
    用于结果的 ``label`` 字段。``config`` 供上层门面复用同一份已解析配置
    （模型组、scheduler、永续会话开关等）；省略时按 ``workspace_root`` 加载。
    """

    def __init__(
        self,
        *,
        workspace_root: str | None = None,
        session_id: str | None = None,
        mode: str = "chat",
        interactive_approvals: bool = False,
        eternal_memory: bool | None = None,
        config: RunConfig | None = None,
    ) -> None:
        self._config = config or load_run_config(workspace_root=workspace_root)
        if eternal_memory is not None:
            # 部署总开关覆写：标准适配层作为部署方决定是否启用永续会话；
            # 会话级 opt-in 仍由 enable_eternal_memory() 显式控制。
            self._config.eternal_conversation = replace(
                self._config.eternal_conversation, enabled=bool(eternal_memory)
            )
        self._manager = SessionManager(self._config)
        self._mode = mode
        self._interactive = interactive_approvals
        self._host: LocalExecutionHost | None = None
        self._session_id = session_id
        self._turn_seq = 0

    # —— 生命周期 ——

    async def __aenter__(self) -> "LaraSessionAgent":
        if self._host is not None:
            raise RuntimeError("LaraSessionAgent is already started")
        if self._session_id is None:
            self._session_id = self._manager.create(
                "lara", mode=self._mode
            ).session_id
        else:
            self._manager.load(self._session_id)
        self._host = LocalExecutionHost(
            credential_environ=build_credential_environment(
                self._config.user_lara_root
            ),
            run_config_loader=load_run_config,
        )
        await self._host.__aenter__()
        await self._host.turns.prewarm_session(
            config=self._config, manager=self._manager, session_id=self._session_id
        )
        return self

    async def __aexit__(self, *_args: object) -> None:
        host, self._host = self._host, None
        if host is not None:
            await host.aclose()

    # —— 标准 Agent 协议 ——

    async def run(self, request: AgentInput | str) -> AgentResult:
        """提交一轮 lara 会话轮次并等待最终结果。

        ``request.message`` 是本轮用户消息；系统提示词、工具与预算由 lara
        的会话配置决定。turn 失败（``result.error``）时抛出 ``RuntimeError``。
        """

        if isinstance(request, str):
            request = AgentInput(message=request)
        if self._host is None or self._session_id is None:
            raise RuntimeError(
                "LaraSessionAgent must be started (use 'async with') before run()"
            )
        self._turn_seq += 1
        turn = await self._host.turns.submit(
            config=self._config,
            manager=self._manager,
            session_id=self._session_id,
            message=request.message,
            turn_id=f"turn-{self._turn_seq:04d}",
            interactive_approvals=self._interactive,
            attachments=tuple(request.attachments),
        )
        output, _ = await turn.result()
        payload = plain_object(plain_object(output.data).get("result"))
        error = payload.get("error")
        if error:
            raise RuntimeError(f"lara turn failed: {error}")
        return AgentResult(
            label=request.label or f"lara-session.{self._session_id}",
            text=str(payload.get("final_answer") or ""),
            answer=payload,
            context={
                "session_id": self._session_id,
                "execution_id": turn.execution_id,
                "status": turn.status,
            },
        )

    # —— lara 会话上下文 ——

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def session(self) -> AgentSession:
        """当前持久会话对象（lara AgentSession）。"""

        self._require_session()
        return self._manager.load(self._session_id)  # type: ignore[arg-type]

    def history(self) -> tuple[Mapping[str, Any], ...]:
        """主 agent 的持久会话历史（跨进程永续）。"""

        return tuple(self.session.history)

    # —— 永续会话 ——

    def enable_eternal_memory(self, *, enabled: bool = True) -> AgentSession:
        """会话级永续会话开关（lara SessionManager.set_eternal_memory）。"""

        self._require_session()
        return self._manager.set_eternal_memory(
            self._session_id,  # type: ignore[arg-type]
            enabled=enabled,
        )

    @property
    def eternal_memory_enabled(self) -> bool:
        self._require_session()
        return eternal_enabled_for(self.session, self._config)

    def memory_projection(self) -> dict[str, Any]:
        """永续会话的工作记忆投影（lara load_projection）。"""

        return load_projection(self.session.session_dir)

    # —— 实时对话 ——

    @property
    def realtime_enabled(self) -> bool:
        """实时对话是否开启（仅 ``lara-says`` 模式会话）。"""

        return realtime_enabled_for(self.session)

    # —— 内部 ——

    def _require_session(self) -> None:
        if self._session_id is None:
            raise RuntimeError("LaraSessionAgent has no session; start it first")


__all__ = [
    "REALTIME_SESSION_MODE",
    "LaraSessionAgent",
]
