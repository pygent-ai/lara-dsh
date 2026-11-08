# E2E 场景测试（功能锚点）

本目录不是单元测试的补充，而是**功能入口的锚点**。每个场景都从真实入口出发
（CLI / 真实 `LaraRuntimeService` / 真实调度器 daemon），只把远程模型替换为
可脚本化的进程内 invoker。因此删除某个功能性入口时，对应场景会直接 import
失败或在第一个真实调用处失败，而不是像随实现一起删除的单元测试那样“全绿”。

## 场景清单

| 场景 | 文件 | 锚定的功能入口 |
| --- | --- | --- |
| E1 对话与记忆 | `test_e1_conversation_memory.py` | `lara session run --new` 创建持久会话；第二轮从第一轮 committed history 组装上下文 |
| E2 工具执行任务 | `test_e2_tool_execution.py` | 生产工具管线执行模型发出的 `write` / `read`，文件真实落盘且最终回答包含结果 |
| E3 实时插话与 supersede | `test_e3_realtime_supersede.py` | `LaraRuntimeService.send_interrupt` → `_start_fork_reply` → fork-reply 信封入队；第二条插话取消第一条 fork 并生成合并批次 |
| E4 定时任务全链 | `test_e4_scheduler_daemon.py` | `scheduler init-keys` → `scheduler add`（user 签名）→ `scheduler run --once` → 专属会话轮次 → `runs` 落盘 |
| E5 触发总线门禁 | `test_e5_trigger_bus_gate.py` | agent 来源提交进入 `needs_review`；用户 `approve` 后才可触发；agent 请求 `high` 归一为 `normal` 并留下审计说明 |
| E6 事件触发 | `test_e6_event_trigger.py` | agent 签名 `emit_event` → daemon 消费事件 → 仅匹配的 `on <event>` 任务触发 |

每个场景使用独立的 `tmp_path` 作为 workspace + user root；会话、SQLite、
调度器 keys/runs 全部落在该临时目录，场景之间没有共享状态。

## 运行方式

整个 E2E 套件：

```bash
uv run pytest tests/e2e -q
```

按 marker 运行（marker 已在 `pyproject.toml` 注册）：

```bash
uv run pytest -m e2e -q
```

单独运行一个场景：

```bash
uv run pytest tests/e2e/test_e3_realtime_supersede.py -q
uv run pytest tests/e2e/test_e4_scheduler_daemon.py -q
```

全量回归（现有单元测试 + E2E）：

```bash
uv run pytest -q
```

## 为什么 E3 是防误删锚点

第一批死代码清理曾把实时插话入口（`send_interrupt` / fork-reply 链）当成
“测试专用路径”删除，相关单元测试也随实现一起删除，所以测试仍然全绿。E3
的防误删设计是：

1. **import 即锚定**：测试直接
   `from lara.runtime.service import LaraRuntimeService` 并调用
   `service.send_interrupt(...)`。入口被删除时，测试不是“断言失败”，而是
   收集阶段直接 `ImportError` / `AttributeError`。
2. **真实 service + 真实主轮**：用真实 `LaraRuntimeService` 和
   `LocalExecutionHost` 启动一个受门控的慢速主模型轮次，而不是伪造
   `send_interrupt` 的状态机。主轮必须真的在运行，插话路径才有意义。
3. **锚定整条链**：脚本化 invoker 通过 `_model_invokers` 注入（`LaraAgent`
   支持的 `model_invoker` 注入点），因此 `_start_fork_reply` 必须真的创建
   fork 执行并调用模型。只保留 `send_interrupt` 而删掉 fork 链，测试会在
   第二次模型请求处失败。
4. **锚定 supersede 语义**：连发第二条插话时断言第一条 fork 执行收到取消、
   第二条请求内容为 `1. 第一个插话\n2. 第二个插话`，最终只入队一个合并
   `<user-asked>` 信封。

E3 还顺带覆盖了 Windows 路径安全性：supersede 轮次 id 形如
`fork:<input>:g<generation>`，其中冒号不能直接进入渲染 prompt 文件名。
`lara/tracing/events.py` 的 `_turn_file_name` 会做文件名清洗，而事件元数据
仍保留原始 `turn_id`。

## 关于真实模型调用

默认实现全部使用脚本化模型：不访问网络、不需要凭据、结果确定。脚本化
invoker 只替换模型边界，CLI、会话、工具管线、pygent 执行、调度器、SQLite
持久化都是真实实现。若后续需要增加“真实模型冒烟”场景，可复用
`_e2e_support.make_config` 的 workspace 隔离，并在有凭据时注入真实 invoker；
不要为了真实模型调用破坏本目录现有场景的确定性与零凭据依赖。
