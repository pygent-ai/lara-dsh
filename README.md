# lara-agent

从 lara 项目**完整抽取的独立 agent 实现仓**。本仓自包含：不依赖原 lara 仓
（无路径依赖、无 import 引用），`src/lara` 是 lara agent 运行时的完整代码
梳理副本，`standard_agent` 是其上的标准接口层。

## 抽取范围

**保留（lara agent 实现的完整链路）：**

| 子系统 | 内容 |
| --- | --- |
| `lara/agents` | ReAct 脚手架：AgentRunner 单一调用缝、fork/child/main、ContextWindow、统一工具面（G2/G3/G4/G5） |
| `lara/runtime` | 前台主 agent harness（`LaraAgent`）、生产工具管线、**永续会话**（`eternal_conversation.py`）、**实时对话**（`realtime/`：speak/fork-reply/nudge）、上下文压缩、模型层、reminders、bash tasks、attachments、mcp |
| `lara/sessions` | `SessionManager`：会话持久化、历史、fork、case run、`set_eternal_memory`、`lara-says` 实时模式 |
| `lara/orchestration` | `LocalExecutionHost` / `SessionTurnService` / `ManagedSessionTurn`：会话轮次的编排与执行 |
| `lara/runtime/service.py` | 组合根 `LaraRuntimeService`：工具装配、模型资源解析、容量/审批/协作 |
| `lara/schema` | `RunConfig` / `AgentSession` / `CaseRunRef` 等数据契约 |
| `lara/config` | `load_run_config`：用户配置合并、模型配置解析 |
| `lara/credentials` | 凭据存取（keyring 可选组） |
| `lara/dispatch`、`lara/tracing`、`lara/core` | 分发服务、事件存储、IO/路径/脱敏等基础件 |

**裁剪（与 agent 实现无关）：** `evaluation`、`repair`、`automations`、
`workflows` 四个子系统、`lara_api` FastAPI 服务层、桌面端（apps/）、评测
脚手架（BenchRunner/swe-smoke）。`lara/__init__.py` 与 `cli/main.py` 已相应
瘦身（CLI 仅保留 session / credentials / projects 子命令）。

**import 包名保持 `lara` 不变**——被复制的模块一行未改，保证与原仓行为一致。
注意：本仓与原 lara 仓不能装进同一个 Python 环境（包名冲突）。

## 标准接口层（`standard_agent`）

| 模块 | 说明 |
| --- | --- |
| `interfaces.py` | 框架无关标准协议：`Agent` / `AgentInput` / `AgentResult` / `ToolProfile` / `AgentLimits` |
| `engine.py` | pygent 引擎适配（独立可用）：`PygentEngine`、`ToolRegistry`、`ContextWindow` |
| `simple_agent.py` | 试点实现：`SimpleAgent`（独立 agent 语义） |
| `lara_rt.py` | **lara 真实运行时适配**：主 harness + 上下文 + 永续会话 + 实时对话 → 标准接口 |

### 三大能力的 lara 原生机制

- **主 agent harness + 上下文**：`LaraAgent` + `SessionManager` 持久历史 + `LaraContext`
- **永续会话**：`RunConfig.eternal_conversation`（总开关）+ `set_eternal_memory`（会话 opt-in）+ 动态记忆投影（`dynamic-memory-cli`）
- **实时对话**：`lara-says` 会话模式 + `speak` 工具 + fork 回复 + nudge

```python
from standard_agent.lara_rt import LaraSessionAgent, REALTIME_SESSION_MODE

# 主 harness + 永续会话
async with LaraSessionAgent(eternal_memory=True) as agent:
    agent.enable_eternal_memory()
    result = await agent.run("你好")     # 真实模型调用（凭据来自 ~/.lara）

# 实时对话会话（speak 是唯一可见输出通道）
async with LaraSessionAgent(mode=REALTIME_SESSION_MODE) as rt: ...
```

也可以直接用抽取版 CLI（会话聊天驱动）：

```bash
python -m lara session chat --help
```

## 快速开始

```bash
cd C:\Projects\lara-agent

uv sync                          # 独立环境（含 dynamic-memory-cli git 依赖）
uv run pytest                    # 全部测试（含导入完整性冒烟）
uv run python -m examples.run_simple_agent          # 离线示例（无网络）
uv run python -m examples.run_lara_session_agent    # 真实运行时示例（会调模型）

# 本机开发期验证也可复用原 lara 的 venv（pyproject 已注明）：
C:\Projects\lara\.venv\Scripts\python.exe -m pytest -q
```

## 溯源

- 抽取自 `C:\Projects\lara`（lara 0.3.3，pygent-ai 0.3.33）。
- 修改仅限：删除被裁剪子系统、瘦身 `lara/__init__.py` 与 `cli/main.py`、
  新增 `standard_agent/`、tests 与示例；`src/lara` 其余代码为原样副本。
- 后续同步策略：以原仓为上游，按模块对照搬运变更（副本内避免就地魔改，
  保持"梳理副本 + 标准接口层"的分层）。
