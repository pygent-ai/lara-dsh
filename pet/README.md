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
  开启 realtime interaction、关闭无人值守没人点的审批弹窗，注入桌兔人设提示词；
  每条用户消息经 `host.turns.submit` 提交 turn，`lara.realtime.speak` 事件经
  Rust 中继到前端。协议：JSONL over stdio（`send` 进，`ready/turn_started/
  speak/turn_finished/error` 出）。模型与凭据沿用用户 `~/.lara/config.yaml`
  与 keyring；workspace 默认 `~/lara-pet`（`LARA_PET_WORKSPACE` 可覆盖）。
  sidecar 起不来时前端自动回退 mock 演示模式。
- **皮肤**：`ui/skins/<name>/skin.json + sheet.png`。manifest 声明每状态的帧
  序列与 fps（idle/listening/thinking/speaking），换肤 = 加文件夹。默认兔由
  `python pet/tools/gen_assets.py` 生成（可重跑再生成，改 FRAMES/参数可自定义）。

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

双击 Shift 三下唤出输入条 → 输入回车 → 兔兔进入思考状态，随后逐条冒泡回复
（sidecar 正常时是真实模型经 speak 发来的消息）。左键拖动兔兔，右键退出。

## 路线

- M1：壳 + 皮肤 + 三击 + 输入条 + mock 气泡 ✅
- M2（本阶段）：Python sidecar 接 lara 单 lara-says realtime 会话 ✅；
  未读红点、聊天面板切换、托盘、快捷键/皮肤设置面板待做
- M3：内核 `lara.realtime.speak.delta` 流式桥（`model.tool_call.delta` 已通到
  lara 事件总线，差最后一层识别 speak 参数增量）、状态动画细化、点击穿透

## 已知限制

- 窗口透明边角仍会拦截鼠标点击（点击穿透在 M3，配合光标轮询做命中切换）
- 快捷键暂固定为三击 Shift，改绑定与皮肤切换的设置面板待做
- 气泡超长时依赖窗口宽度截断换行，展开大卡片在聊天面板里做
- sidecar 内 turn 串行排队（steering 插队语义待接内核 RuntimeSteeringConfig）
