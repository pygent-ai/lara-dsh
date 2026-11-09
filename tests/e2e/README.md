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
| E7 完整门面 | `test_e7_lara_facade.py` | 从零 `LaraConfig` → `Lara.start` → `run` 对话 → `schedule_task` → 内嵌调度循环执行 → `stop`；另含可选真实模型冒烟 |

### 实时交互矩阵（R1-R14）

`test_realtime_matrix.py` 复用 E3 的进程内骨架：真实 `LaraRuntimeService` +
真实 managed turn，只把模型替换为可门控的脚本化 invoker。R2/R3 直接由 E3
覆盖，不再重复实现。

| 场景 | 测试 | 锚定的功能行为 |
| --- | --- | --- |
| R1 空闲时新消息 | `test_r1_idle_realtime_turn_replies_directly_through_speak` | 空闲 `lara-says` 会话走 `start_turn` 正常前台轮次，`lara.realtime.speak` 事件直达，不创建 fork |
| R2 中途打断 | `test_e3_realtime_supersede.py` | `send_interrupt` 启动 fork-reply 执行并生成信封 |
| R3 连发两条 | `test_e3_realtime_supersede.py` | 第二条插话取消第一条 fork，合并为编号批次 |
| R4 结束没有 speak | `test_r4_pure_text_ending_injects_finalizer_reminder` | 纯文本结束触发 `turn_finalizer` 提醒；`speak(say_nothing=true)` 以 `SILENT_ACKNOWLEDGEMENT` 收尾 |
| R5 开头没有 speak | `test_r5_first_tool_result_injects_speak_reminder_once` | `first_result_seen` 在首个工具结果注入一次首轮提醒，后续边界不重复 |
| R6 中间多轮没有 speak | `test_r6_five_silent_tool_rounds_inject_periodic_nudge` | 默认 `nudge_interval=5` 在第 5 个工具边界注入循环 nudge，第 4 轮不注入 |
| R7 连发三条 | `test_r7_three_interrupts_merge_into_one_fork_envelope` | 第三次插话取消前两条 fork，三条消息合并进一个 `<user-asked>` 信封 |
| R8 插话回复被主 agent 消费 | `test_r8_main_agent_claims_envelope_at_next_tool_boundary` | 主 agent 在下一个工具边界 claim 信封，信封进入下一次模型请求的上下文 |
| R9 `speak(say_nothing=true)` | `test_r9_say_nothing_acknowledges_silently_without_envelope` | 工具结果为 `SILENT_ACKNOWLEDGEMENT`，无 speak 事件、无信封、无 finalizer |
| R10 结束时有 speak | `test_r10_speaking_turn_end_skips_finalizer_reminder` | 本轮已成功 speak 后，纯文本收尾不再排 `turn_finalizer` |
| R11 非实时会话 | `test_r11_non_realtime_session_has_no_speak_or_reminders` | 普通 `chat` 会话无 `speak` 工具，三类实时提醒均不注入，也不排 finalizer |
| R12 fork 超时 | `test_r12_fork_deadline_cancels_fork_without_envelope` | `fork_deadline_seconds` 到期取消 fork 模型调用，不产生信封，后续插话可重新开始 |
| R13 插话到达时主轮即将结束 | `test_r13_pending_envelope_survives_into_next_turn_context` | 无后续工具边界时信封保持待领，下一轮 `start_turn` 通过 `claim_agent_messages` 拾取并进入开场上下文 |
| R14 fork 低思考强度 | `test_r14_fork_reply_uses_low_reasoning_effort_profile` | 可控制 reasoning 的模型上，fork 走专属 profile 并携带 `provider_options.reasoning_effort="low"`；主轮 profile 不变 |

共享门控/记录工具在 `_realtime_support.py`：`ScriptedRealtimeInvoker` 记录每次
主轮与 fork 模型调用，测试用 `asyncio.Event` 精确控制“主轮运行中”“fork 已
完成”“主轮才结束”等时序；`assert_no_extra_main_call` 会等待被排队的后续轮
次真正浮现后再断言其不存在，避免固定 sleep 漏掉回归。

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
uv run pytest tests/e2e/test_realtime_matrix.py -q
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

## 矩阵跑出的两个实现缺陷（已修复）

这两个缺陷不是测试辅助代码的问题，而是真实运行时行为：

1. **R10：已 speak 的轮次仍被追加 finalizer。**
   `_maybe_start_finalizer` 原先只看最后一条 assistant 消息是否带工具调用，
   没有检查本轮是否已经成功 speak。于是“先 `speak(text=...)`、再以纯文本
   结束”的轮次会多排一个 bottom-line 轮次，重复要求模型决定是否说话。
   修复：`decide_turn_finalization` 增加 `spoke` 条件，
   `session_execution._turn_spoke` 从本轮工具结果中识别成功的 `speak`。
   发现测试：`test_r10_speaking_turn_end_skips_finalizer_reminder`。

2. **R13：主轮结束后到达的信封会一直待领。**
   agent message 只在 `RuntimeReminderModule` 的工具边界 claim。插话 fork
   在主轮没有后续工具边界时完成，信封虽已入队，但下一轮开场不会读取它，
   可能长期滞留。注意 `collect_pending` 只负责 git observation，并不领取
   agent message；真正缺的是开场 claim。修复：`start_turn` 在 realtime 用户
   轮次组装开场上下文时 claim 待领信封，并在执行启动后确认送达；内部
   `lara.realtime.finalizer` 轮次刻意不领取，留给下一轮用户消息。
   发现测试：`test_r13_pending_envelope_survives_into_next_turn_context`。

两条测试都做过反向验证：临时移除对应修复后，R10/R13 分别失败；恢复后全绿。

## 关于真实模型调用

默认实现全部使用脚本化模型：不访问网络、不需要凭据、结果确定。脚本化
invoker 只替换模型边界，CLI、会话、工具管线、pygent 执行、调度器、SQLite
持久化都是真实实现。

E7 另有一个显式开启的真实模型冒烟场景（不进入默认断言集）：

```bash
LARA_E2E_REAL_MODEL=1 uv run pytest tests/e2e/test_e7_lara_facade.py -q
```

它使用 `~/.lara` 的真实配置与凭据，通过门面发起一次真实对话调用；未设置该
环境变量时自动跳过。若后续需要增加更多“真实模型冒烟”场景，可复用
`_e2e_support.make_config` 的 workspace 隔离；不要为了真实模型调用破坏本目录
现有场景的确定性与零凭据依赖。
