"""lara_sdk.lara_rt 的单元测试。

隔离策略：monkeypatch ``lara.config.loader.user_lara_root`` 指向临时目录，
会话存储、运行时 SQLite、凭据读取全部落在临时目录；凭据缺失时
``LaraAgent.llm`` 为 ``None``，因此不会发起任何真实模型调用。
"""

from __future__ import annotations

import pytest

from lara_sdk import Agent, REALTIME_SESSION_MODE, LaraSessionAgent


@pytest.fixture()
def isolated_root(tmp_path, monkeypatch):
    """把 lara 的用户根指到临时目录，实现会话/凭据/SQLite 全隔离。"""

    user_root = tmp_path / "lara-user-root"
    user_root.mkdir()
    import lara_shell.config.loader as loader

    monkeypatch.setattr(loader, "user_lara_root", lambda: user_root)
    return tmp_path


async def test_lifecycle_feature_switches_and_persistence(isolated_root) -> None:
    async with LaraSessionAgent(
        workspace_root=isolated_root, eternal_memory=True
    ) as agent:
        assert agent.session_id is not None
        # chat 模式不开实时对话。
        assert agent.realtime_enabled is False
        # 永续会话还需要会话级 opt-in。
        assert agent.eternal_memory_enabled is False
        agent.enable_eternal_memory()
        assert agent.eternal_memory_enabled is True
        assert agent.history() == ()
        assert agent.memory_projection() == {}
        session_id = agent.session_id

    # 换一个实例重开同一会话：会话与开关跨实例持久（lara 持久存储）。
    async with LaraSessionAgent(
        workspace_root=isolated_root, session_id=session_id, eternal_memory=True
    ) as agent2:
        assert agent2.eternal_memory_enabled is True
        assert agent2.realtime_enabled is False
        assert agent2.session_id == session_id


async def test_realtime_mode_session(isolated_root) -> None:
    async with LaraSessionAgent(
        workspace_root=isolated_root, mode=REALTIME_SESSION_MODE
    ) as agent:
        assert agent.realtime_enabled is True


async def test_lara_session_agent_satisfies_standard_protocol(isolated_root) -> None:
    agent = LaraSessionAgent(workspace_root=isolated_root)

    assert isinstance(agent, Agent)


async def test_run_requires_started_agent(isolated_root) -> None:
    agent = LaraSessionAgent(workspace_root=isolated_root)

    with pytest.raises(RuntimeError, match="started"):
        await agent.run("hello")


async def test_master_switch_override(isolated_root) -> None:
    # eternal_memory=None 时尊重 lara 配置；显式 False 强制关闭总开关。
    agent = LaraSessionAgent(workspace_root=isolated_root, eternal_memory=False)
    assert agent._config.eternal_conversation.enabled is False
