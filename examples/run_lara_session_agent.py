"""lara 真实运行时示例：主 agent harness + 永续会话（需要真实凭据）。

本示例会发起**真实模型调用**（凭据来自 ~/.lara 的用户配置），请确认后再跑：

    cd C:\\Projects\\lara-agent
    uv run python -m examples.run_lara_session_agent

演示内容：
1. 多轮 chat 会话（LaraAgent harness + 持久上下文），开启永续会话；
2. 打印每轮的最终回答、会话历史长度与工作记忆投影；
3. 实时对话（lara-says 模式）的开启方式见底部注释。
"""

from __future__ import annotations

import asyncio

from standard_agent.lara_rt import REALTIME_SESSION_MODE, LaraSessionAgent


async def main() -> None:
    # eternal_memory=True 打开 lara 的部署总开关；会话级 opt-in 在下面显式开启。
    async with LaraSessionAgent(eternal_memory=True) as agent:
        agent.enable_eternal_memory()
        print(f"session: {agent.session_id}")
        print(f"eternal memory: {agent.eternal_memory_enabled}")
        print(f"realtime: {agent.realtime_enabled}")

        for user_input in ("用一句话介绍你自己", "再把你刚才的回答压缩成五个词"):
            result = await agent.run(user_input)
            print(f"\n> {user_input}\n{result.text}")
            print(f"history: {len(agent.history())} 条")

        projection = agent.memory_projection()
        print(f"\nmemory projection keys: {sorted(projection.keys())}")

    # 同一会话可再次打开继续对话（上下文与记忆都在）：
    # async with LaraSessionAgent(session_id=agent.session_id, eternal_memory=True) as a2:
    #     follow_up = await a2.run("继续")

    # 实时对话会话（speak 是唯一可见输出通道）：
    # async with LaraSessionAgent(mode=REALTIME_SESSION_MODE) as rt:
    #     await rt.run("开始静默工作")


if __name__ == "__main__":
    asyncio.run(main())
