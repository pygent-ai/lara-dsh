# Lara

这个仓里的 agent 叫 **Lara**。它的名字不是标签，而是原理：`src/lara` 就是
Lara 的完整实现——主 agent harness、会话上下文、永续会话、实时对话、ReAct
脚手架与统一工具面。prompt 层的自我认知与之对应（`src/lara/runtime/agent/prompt_sources.py`）：

> "You are Lara, an interactive coding agent for software engineering work."

从 lara 项目**完整抽取的独立 agent 实现仓**。本仓运行时自包含：不依赖原
lara 仓（无路径依赖、无 import 引用），`src/lara` 是 lara agent 运行时的完整代码
梳理副本，`src/lara_sdk` 是其上的对外使用层（契约与入口）。统一 src 布局。

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
瘦身（CLI 保留 session / credentials / projects / scheduler 子命令）。

**已知的上游残留（未就地修改，以保持副本可对照）：** `src/lara` 的 prompt
与部分 docstring 仍提到上游的 `lara automation` 子命令、`lara_api` 适配层和
`docs/design/...` 设计文档；这些能力/文档在本仓已裁剪或未包含。需要自动化
或 API 能力时请使用原 lara 仓。

**发行名与 import 包名统一为 `lara`**——除上述瘦身与文档修正外，被复制的代码
保持原样，以对齐原仓行为。
注意：本仓与原 lara 仓不能装进同一个 Python 环境（包名冲突）。

## 对外使用层（`lara_sdk`）

`lara_sdk` 是 lara 的**对外使用契约与入口**：`lara` 是完整实现（agent 构造、
runner、运行时），`lara_sdk` 只描述调用方如何**使用** agent——标准协议与数据
契约在 `interfaces.py`，真实运行时入口在 `lara_rt.py`。

| 模块 | 说明 |
| --- | --- |
| `interfaces.py` | 框架无关标准协议：`Agent` / `AgentInput` / `AgentResult` / `ToolProfile` / `AgentLimits` |
| `lara_rt.py` | **lara 真实运行时适配**：主 harness + 上下文 + 永续会话 + 实时对话 → 标准接口 |

agent 构造留在 `lara` 实现层（`lara.agents.runner.AgentRunner`、
`lara.agents.ChildAgent` 等）；`lara_sdk` 不再提供 `PygentEngine` /
`SimpleAgent` 之类的构造或试点实现。

### 三大能力的 lara 原生机制

- **主 agent harness + 上下文**：`LaraAgent` + `SessionManager` 持久历史 + `LaraContext`
- **永续会话**：`RunConfig.eternal_conversation`（总开关）+ `set_eternal_memory`（会话 opt-in）+ 动态记忆投影（`dynamic-memory-cli`）
- **实时对话**：`lara-says` 会话模式 + `speak` 工具 + fork 回复 + nudge

```python
from lara_sdk.lara_rt import LaraSessionAgent, REALTIME_SESSION_MODE

# 主 harness + 永续会话
async with LaraSessionAgent(eternal_memory=True) as agent:
    agent.enable_eternal_memory()
    result = await agent.run("你好")     # 真实模型调用（凭据来自 ~/.lara）

# 实时对话会话（speak 是唯一可见输出通道）
async with LaraSessionAgent(mode=REALTIME_SESSION_MODE) as rt: ...
```

也可以直接用抽取版 CLI（会话聊天驱动）：

```bash
uv run lara session chat --help
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
# 该方式未安装本仓，裸跑示例需自带 src 到 sys.path：
set PYTHONPATH=src && C:\Projects\lara\.venv\Scripts\python.exe -m examples.run_simple_agent
```

## 溯源

- 抽取自 `C:\Projects\lara`（lara 0.3.3，pygent-ai 0.3.33）。
- 抽取时修改仅限：删除被裁剪子系统、瘦身 `lara/__init__.py` 与 `cli/main.py`、
  新增 `src/lara_sdk/`、tests 与示例；`src/lara` 其余代码为抽取时的
  原样副本。
- 功能演进（有意偏离上游忠实副本）：新增
  `src/lara/runtime/fork_context.py`，以 `session_fork_window()` 统一实时
  fork-reply 的 live 上下文与 memory extractor 的 frozen 上下文；`service.py`
  实时路径与 extractor 改走该入口，`agents` 包仍不依赖 runtime。
- 功能演进（XML 提示词信封）：新增 `src/lara/core/prompt.py`
  （`Prompt` + `KNOWN_ENVELOPES`），13 个信封站点迁移到流式 Prompt API；
  golden 测试锁定迁移前后字节输出，元测试扫描未登记的标签字面量。
  `summary` 仅登记为 model-generated/parser-contract，不提供渲染函数。
- 功能演进（scheduler）：新增 `src/lara/scheduler/` 独立守护进程与
  `lara scheduler` CLI。任务/事件/控制先写入签名 submissions，daemon 验签后
  才晋升 tasks/runs；支持 every/at/daily/on、租约防重、补跑标 missed、
  HMAC 来源分离、Windows 登录自启与失败重启。任务执行通过
  `<task-context>` 信封（含 instructions）交给子进程会话。
- 功能演进（realtime supersede）：实时 fork-reply 采用每会话
  `SessionForkState`，in-flight 连发会取消旧 handle 并以编号合并消息重发；
  generation 守卫阻止迟到结果写入孤儿信封。
- 维护（死代码清理 + nudge 修复）：删除 11 项确认无用的 helper/re-export；
  realtime nudge interval 改为读取 `nudge_interval` 配置；turn 切换时重置
  silent-round 计数。验证：137 passed、pyright 0；CLI 两轮 E2E 均
  `status=passed` 且 `final_answer=first-ok`。
- 重构（去重 X2 + SDK 瘦身）：`src/lara_sdk` 的工具面、上下文窗口与模型层工厂
  改为从 `lara.agents` 直接导入（仅 import 依赖，不反向修改 lara 分层）；随后
  按"只负责使用、不负责构造"删除 `engine.py`（`PygentEngine` 与 `AgentRunner`
  完全镜像）与 `simple_agent.py`（试点实现），构造职责归 `lara`，SDK 仅保留
  `interfaces` 契约与 `lara_rt` 运行时适配器。
- 后续同步策略：以原仓为上游，按模块对照搬运变更；副本内避免无记录的就地
  魔改，有意的功能演进需在本节记录（见上条），以保持"梳理副本 + 对外使用层"
  的分层。
