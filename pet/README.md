# Lara Pet — 像素兔桌宠前端

lara agent 的桌宠形态前端：Tauri 2 壳 + 像素兔皮肤 + 全局三击 Shift 唤出输入条 +
`speak` 消息以队列气泡的形式从兔兔头顶冒出。

## 架构

```
┌─ Tauri 壳（本目录，Rust + 静态 JS）──────────────┐
│  pet/            Tauri 工程                      │
│   ├─ src-tauri/   Rust：透明置顶窗口、全局键盘钩子 │
│   │               sidecar 进程管理、事件中继      │
│   ├─ sidecar/     Python：唯一 lara-says 会话     │
│   ├─ ui/          前端：宠物画布、气泡、输入条     │
│   └─ tools/       资产生成（sprite / 图标）        │
└──────────────────────────────────────────────────┘
```

- **全局三击 Shift**：`src-tauri/src/hook.rs` 低级键盘钩子，从第一击起 800ms
  窗口内三击触发；序列中的第二、三次 keydown 会被吞掉，IDE 的双击 Shift
  快捷键不会误触。触发后经 `pet://triple-shift` 事件送到前端切换输入条。
- **气泡**：`ui/js/bubble.js`。FIFO 队列逐条冒泡，打字机逐字显示；打完若队列里
  还有下一条，停留 6s 后自动顶上；最后一条常驻，直到点 × 或被 × 跳过。
  未读红点/聊天面板切换在 M2 后半。
- **sidecar**：`sidecar/main.py`（`.venv` Python 运行）。持有内核唯一的
  `lara-says` realtime 会话（session id 即 `"lara-says"`，由内核保证单例），
  开启 realtime interaction、关闭无人值守没人点的审批弹窗；每条用户消息经
  `host.turns.submit` 提交 turn，`lara.realtime.speak` 事件经 Rust 中继到
  前端。协议：JSONL over stdio（`send` 进，`ready/turn_started/speak/
  speak_delta/turn_finished/error` 出）。模型与凭据沿用用户
  `~/.lara-dsh/config.yaml` 与 keyring；workspace 默认 `~/lara-pet`
  （`LARA_PET_WORKSPACE` 可覆盖）。sidecar 起不来时前端自动回退 mock。
- **真流式**：内核 `lara.runtime.agent.speak_stream.SpeakStreamBridge` 把
  `model.tool_call.delta` 里的 speak 参数增量解码成文本后缀；桥放在 sidecar
  消费端（pygent 对模型层 emit 的 kind 有白名单），以 `speak_delta` 事件送
  前端实时上屏，最终 `speak` 事件为准收尾。
- **人设**：桌面兔兔的系统提示词来自 `lara_sdk.lara_profile.lara_prompt_modules`
  （`system.pet` 模块），sidecar 在加载配置时渲染进 `config.system_prompt`。
  （pygent 托管执行的绑定不会把 prompt registry 带到执行副本，所以注册表
  方式在此路径不生效——sidecar 改走静态渲染。）
- **皮肤**：`ui/skins/<name>/skin.json + sheet.png`。manifest 声明每状态的帧
  序列与 fps（idle/listening/thinking/speaking），换肤 = 加文件夹 + 设置面板
  下拉切换。默认兔由 `python pet/tools/gen_assets.py` 生成（可重跑再生成，
  改 FRAMES/参数可自定义）。

## 运行（M1）

```bash
# 方式一：直接 cargo（不需要 npm）
cd pet/src-tauri
cargo run

# 方式二：tauri CLI
cd pet
npm install
npm run tauri dev
```

- **托盘**：左键显示/隐藏兔兔，右键菜单同款 + 退出。
- **点击穿透**：Rust 每 120ms 轮询光标与窗口矩形，窗口外自动
  `ignore_cursor_events`，兔兔不再挡住后面的应用。
- **设置**：右键兔兔打开面板——触发键（Shift/Ctrl/Alt/`/CapsLock）、连击次数、
  时间窗、皮肤切换；持久化在 `~/.lara-dsh/pet-config.json`，热键即时生效。
- **聊天面板**：气泡上的 💬 切换完整历史 + 输入框。

## 运行

```bash
# 方式一：直接 cargo（不需要 npm）
cd pet/src-tauri
cargo run

# 方式二：tauri CLI
cd pet
npm install
npm run tauri dev
```

双击/三击配置的快捷键唤出输入条 → 输入回车 → 兔兔进入思考状态，气泡实时
流式显示真实模型经 speak 说出的话。左键拖动兔兔，右键设置/退出，托盘同款。

## 路线

- M1：壳 + 皮肤 + 快捷键 + 输入条 + mock 气泡 ✅
- M2：sidecar 接唯一 lara-says 会话、未读红点、聊天面板、托盘、设置面板 ✅
- M3：`speak_delta` 真流式（内核 SpeakStreamBridge）✅、点击穿透 ✅；
  语音化、皮肤商店、更多动画状态是后续方向

## 已知限制

- 光标在窗口矩形内时透明边角仍会拦截鼠标（逐像素命中测试待做）
- 流式粒度取决于 provider 的 SSE 分块（glm 常整段一次性到，短句近似直出）
- turn 内消息串行排队（steering 插队语义待接内核 RuntimeSteeringConfig）
