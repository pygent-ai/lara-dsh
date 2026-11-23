# lara-dsh 本机验证报告（2026-11-21）

环境：Windows 10 26200 · dsh 0.2.2-rc.5（desktop profile）· lara-dsh 0.5.0（commit 0648a4f + 本机两处修复）· Node 25.6 · Python 3.13.
验证方式：真鼠标真键盘驱动桌宠 UI（`LARA_PET_ALLOW_INJECTED=1` 诊断开关）+ `lara dsh` CLI + sidecar stdio 协议。
结论速览：**主链路可用**（热键→输入条→agent→dsh 会话→气泡），发现 **9 个问题**，其中 2 个已修（见末尾附录）。

---

## ISSUE-1（严重）无可用模型时完全静默，用户什么也看不到

**现象**：用一个空的 `DSH_HOME`（等价于新机器/未登录/无 config.yaml）启动 sidecar，发一条消息，事件序列是：
```
{"type":"ready"} → {"type":"turn_started"} → {"type":"turn_finished"}
speak 事件数 = 0，error 事件数 = 0
```
输入条清空、没有任何气泡、没有任何提示。

**复现**
```bash
DSH_HOME=$(mktemp -d) lara-pet-sidecar --workspace $(mktemp -d)
# stdin: {"type":"send","text":"你好"}
```
**根因线索**：`src/lara_pet/sidecar.py::_borrow_dsh_model` 的 4 条 `return None` 分支只写 `sidecar.log`；`_apply_lara_model` 在路由为 None 时直接返回，`config.model_configuration_status` 保持 `unconfigured`；pygent `graph/core.py` 在 `llm is None` 时生成的是**普通 assistant 文本**，而桌宠只渲染 `speak` 事件。

**建议**：`_borrow_dsh_model` 失败时让 agent 立刻 `speak` 一条可操作提示（"请在 dsh 登录 / `lara dsh credentials DEEPSEEK_API_KEY --set`"）。

---

## ISSUE-2（严重）输入条展开期间到达的回复不冒气泡，只加未读角标

**现象**：输入条打开状态下发出任务 → agent 执行完成 → **没有气泡**，只有兔胸口出现红色未读 "1"。

**复现**：三击 Shift → 输入条展开（先别收）→ 发一条需要十几秒的任务 → 任务完成后观察。

**根因线索**：`pet/ui/js/bubble.js` 的渲染条件（疑似在输入条可见时把新消息压进未读队列）；未读只在胸口画数字。

**建议**：输入条展开时也渲染气泡（可叠在输入条上方），或把未读角标做成可点开的历史面板。

---

## ISSUE-3（严重）未读角标点不动、永不清零，错过的消息无入口

**现象**：点胸口红色 "1" 无任何反应（没有聊天面板、没有已读）；多轮交互后角标仍是 "1"。

**复现**：ISSUE-2 之后，鼠标点击角标位置（兔子胸口），观察 1 秒。

**建议**：点击角标打开消息列表 / 或按 Enter 收起输入条后自动补冒气泡，并提供"标记已读"。

---

## ISSUE-4（中）发出消息后输入条失焦，Esc 关不掉

**现象**：第一次 Esc 能关闭输入条；**发过一条消息之后** Esc 无效，必须再三击 Shift 才能收起。

**复现**：三击 Shift → 发一句话 → 按 Esc（无效）→ 三击 Shift（关闭）。

**根因线索**：发送后焦点转移到 webview 其它元素，Esc 处理依赖输入框 focus。

**建议**：Esc 在窗口级监听，或发送后仍把焦点留在输入框。

---

## ISSUE-5（中）持久化的模型选择在模型集合变化后不回退，直接报错

**现象**：
```
这轮没跑完：ModelProfileSelectionError(
  "model group 'pygent.v1:dsh' profile 'preferred:deepseek-v4-flash' is unavailable"
)
```
原因：`<project>/sessions/pygent.v1-says/session.json` 里仍存 `selected_model_key="deepseek-v4-flash"`，而借用 dsh 路由后可用模型 id 是 `deepseek-flash`（dsh account 路由模型）。运行时**没有回退到组内默认**，直接失败。

**复现**：把 `session.json.selected_model_key` 改成不存在的模型 id，再发消息。

**建议**：解析会话选择时校验 key 是否存在于当前 model_group，缺失则回落 default 并在日志/气泡里提示一次。

---

## ISSUE-6（中）`approve/reject/answer` 在默认策略下无法触发，等于死代码

**现象**：让 dsh 往工作区外写文件，期望弹审批 → `lara dsh requests` 连续 36 秒返回 `{"requests": []}`；实际是被硬拒绝：
```
[sandbox: file access denied under workspace-write mode]
```
默认策略是 `workspace-write` 直接拒绝，不会请求人工审批。

**影响**：桌宠"碰到需要许可的操作就问我"这条叙事在默认配置下无场景；`lara dsh approve/reject/decline/answer` 与桌宠的提问气泡链路都无法验证。

**建议**：明确默认策略下的产品预期（要么给出需要审批的场景，要么从文档/UI 中移除该能力描述）。

---

## ISSUE-7（轻）`lara dsh status` 与 `sessions` 返回结构不一致

- `sessions`：`{"sessions":[{...会话对象...}], "hidden_blank": n}`
- `status`：`{"session":{...会话对象...}, "via":"daemon"}`

写脚本按 `sessions` 的形状解析 `status` 会拿到全 None（我第一次就踩了）。建议统一（都用 `session`/`sessions` 键，或 `status` 直接返回会话对象）。

---

## ISSUE-8（轻）桌宠窗口位置在重启之间不一致

观测到同一套配置下兔子位置依次为 `(304,304)` → `(203,203)` → `(38,38)`（其间我只拖拽过一次到 `(675,0)`）。疑似位置持久化/恢复不一致，或与 DPI 缩放（150%）相关。

**复现**：连续重启 dsh 3 次，用 `GetWindowRect`（DPI 感知）记录 `Lara Pet` 窗口矩形。

---

## ISSUE-9（轻，文档/工程）`dsh plugin add file:` 在版本号不变时不会更新二进制

**现象**：改代码后重建 `dist/npm/lara-dsh`（版本仍是 0.5.0），执行
`dsh plugin --profile desktop add file:...` → pnpm 输出 `Already up to date`，profile 里仍是**旧 exe**（md5 不同）。

**复现**：比较 `dist/npm/lara-dsh/bin/*.exe` 与 `~/.dsh/profiles/desktop/node_modules/lara-dsh/bin/*.exe` 的 md5。

**建议**：INSTALL.md 补一句"本地 `file:` 重装需先 `dsh plugin remove` 再 `add`"，或构建脚本自动 bump 版本号。

---

## 附录 A：本机已应用的两处代码修复（未提交，待确认）

1. `src/lara_shell/dsh_settings.py::build_model_mapping` —— header 认证路由的 credential 由 `{"env": "LARA_DSH_NO_CREDENTIAL"}` 改为 `{"none": True}`。
   原因：pygent `CredentialRef.resolve()` 对未设置的 env 抛 `LookupError`，被 `graph/core.py` 吞成 `llm=None`，表现为"API key is not configured for agent alias 'default'"。
2. 同文件 `DshSettings.lara_model` 默认值改为 `None`（原来默认等于 `model`，会用不存在的 `deepseek-v4-flash` 覆盖 dsh 路由的真实模型 id `deepseek-flash`）；`tests/test_dsh_daemon.py` 里对应断言同步更新。

## 附录 B：验证通过项（供回归对照）

- 三击 Shift 打开输入条；**单击/双击 Shift 不触发**（IDE 双击 Shift 安全）
- 中文经剪贴板粘贴、Enter 发送、输入条清空
- 气泡完整渲染中文；点 × 关闭；Esc 首次可关闭
- 拖拽移动窗口并受屏幕边界约束
- 完整链路：UI 消息 → agent → 新建 dsh 会话（`dsh-968c106b57f2`，cwd `C:\Users\Ran\dsh-ui-test`）→ 文件 `dsh-ui.txt`=`hi-dsh`
- agent 会对无效目标先澄清（"没有 D 盘，要不要改成 C:\..."）
- CLI：`send/wait/resume/status/cancel/models/config/credentials/open` 均可用；`cancel` 能把 running 会话停止为 `cancelled`
- 两个并发会话互不干扰（不同 cwd、各自文件与回复）
- 关闭 dsh 后 pet/sidecar 全部退出；重启只拉起 1 个 pet，插件端口与 daemon 端口均刷新

---

## 补充（同日第二轮：流式 / 附件 / 多消息）

### ✅ 流式输出正常
用 `speak_delta` 真机验证：发"用大约150字介绍"，0.45 秒帧序列里气泡文本逐步增长——
`好，我用一百五十字左右说一下…` → `我是住在你屏幕上的桌宠 Lara，背后是 dsh 在干活。你把任务丢给我，比 如` → 更长…
`src/lara_pet/sidecar.py:498` 发出增量，`pet/ui/js/bubble.js::stream()` 逐段追加，最终 `finalize()` 用权威全文覆盖。

### ✅ 附件显示正常（有小瑕疵）
发"把桌面上的 pet-ui-test.txt 作为附件发给我" → 气泡下方出现 chip：`pet-ui-test.txt`，正文写明内容。
`renderAttachments()` 只显示 basename，全路径放 `title`（tooltip）。已知限制：chip 不可点击（没有"打开/另存"动作）。

### ⚠️ 多条消息：气泡只保留**最新一条**，被打断的旧气泡直接丢弃（修正 ISSUE-2 描述）
- **间隔 10 秒**：气泡 `红色` → 之后被 `蓝色` 取代，顺序正确。
- **间隔 ~1.2 秒连续发 3 条**（会话里 3 条回复 `1`/`2`/`3` 都在，`tool_calls.jsonl` 可查）：界面每 2 秒采样的 14 帧**全部只显示 `3`**，`1`、`2` 从未出现。
  说明后到的 `speak` 会立刻覆盖当前气泡，而不是按 `bubble.js` 注释描述的"FIFO 队列 + 每 6 秒切换"。用户会漏掉中间消息。

### ⚠️ 未读角标的澄清（修正 ISSUE-3 的一半）
- 点角标本身**无反应**（这点成立）。
- 但**三击 Shift 打开输入条会把未读数清零**（`engage()` → `setUnread(0)`），所以角标不是永不清零，只是没有"点击查看"的入口。

### 对话框（聊天面板）的流式粒度
- 面板和气泡共用同一批 `speak_delta` 事件：`main.js::appendDelta()` 把每个增量追加到 `chat-msg.live` 行（`liveText += delta; row.textContent = liveText`）。
- 因此面板是**按模型增量块流式**（一次几字到十几个字），不是逐字。逐字（26ms/字）只出现在 `bubble.js::typewriter()`，用于**非流式**到达的消息（排队/旧消息）。
- `speak` 最终事件到达后，`removeLive()` + `appendTranscript()` 把它固化成普通历史行（不重建其它 DOM）。

### ISSUE-10（新，轻~中）面板滚上去之后，新回复落在视口外且无提示
`appendTranscript()`/`appendDelta()` 只在 `atBottom()` 为真时自动滚到底（注释："用户滚到上面在读时保持原位置"）。实测：手动往上滚过之后连发两条，回复都渲染在视口下方，面板上**没有任何"有新消息"提示**，看起来像没回复（我一度误判为"面板不显示回复"，滚动后才看到）。
建议：滚上去时给一个"↓ 新消息"浮标，或新回复时轻微提示。

---

## 补充（2026-11-22 第三轮：当前源码前端回归）

本轮只验证前端显示相关问题。先从当前源码重建 `lara-pet.exe`、
`lara-pet-sidecar.exe` 和 `lara.exe`，组装 `dist/npm/lara-dsh`，再按 dsh
官方插件方式安装到 `web` profile：

```powershell
npx --yes --package=@deepseek-ai/dsh@0.2.2-rc.5 --package=pnpm@11.28.5 -- `
  dsh plugin --profile web remove lara-dsh
npx --yes --package=@deepseek-ai/dsh@0.2.2-rc.5 --package=pnpm@11.28.5 -- `
  dsh plugin --profile web add file:E:/Projects/lara-dsh/dist/npm/lara-dsh
npx --yes --package=@deepseek-ai/dsh@0.2.2-rc.5 --package=pnpm@11.28.5 -- `
  dsh --profile web --no-open --port 0
```

安装后 profile 内三枚 exe 与本地构建产物的 SHA-256 均一致。直接重复
`plugin add file:...` 仍只会输出 `Already up to date`，不会覆盖同版本二进制；
ISSUE-9 的建议（本地更新先 remove 再 add）仍然必要。

### 前端结果

| 项目 | 结果 | 证据 |
|---|---|---|
| 失败反馈是否静默 | **部分通过** | `ModelProfileSelectionError` 会以完整气泡显示，中文、英文、换行和按钮布局正常；未单独重测空 `DSH_HOME`。 |
| ISSUE-2：输入条展开时回复不冒泡 | **未复现，当前错误回复路径已修复** | 输入条保持展开，`turn_finished.error` 气泡仍显示在输入条上方，没有只变成未读角标。 |
| ISSUE-3：未读角标没有消息入口 | **仍复现** | 气泡自动折叠为红色 `1` 后，点击角标只会清掉角标，没有重新展开气泡，也没有打开历史。 |
| ISSUE-4：发送后 Esc 关不掉 | **未复现，当前已修复** | 输入条发送后内容清空，随后按 Esc 能立即关闭输入条。 |
| ISSUE-8：重启后窗口位置漂移 | **仍复现** | 未拖动窗口，连续三次由 dsh 插件拉起时坐标为 `(294,294)` → `(245,245)` → `(49,49)`；尺寸始终为 `300×400`。 |
| ISSUE-10：滚动后没有新消息提示 | **仍复现** | 面板停在较早消息时，历史从 6 条增加到 7 条，视口保持原处，但没有“↓ 新消息”、角标或其它提示。 |

### 本轮阻断与未覆盖项

- 项目当时锁定的 Python runtime 是 dsh `0.2.2-rc.2`，本机运行会话时报
  `@koromix/koffi-win32-x64/.../koffi.node` 缺失；切换到 npm 官方当前版本
  `@deepseek-ai/dsh@0.2.2-rc.5` 后该原生模块错误消失。
- 当前 dsh endpoint 返回 `Invalid token`；同时持久化的 Lara 会话仍选择
  `preferred:deepseek-v4-flash`，而当前 `pygent.v1:dsh` 模型组没有该 profile，
  因而又触发 `ModelProfileSelectionError`。
- 受上述高层失败影响，本轮只能验证错误气泡及面板行为，不能重新验证正常
  `speak_delta` 流式输出、连续多条 speak 的 FIFO、附件 chip。

本轮结束后恢复用户原有的 `Ctrl × 3` 快捷键设置；未修改业务代码。

---

## 补充（2026-11-22 第四轮：空 DSH_HOME 无模型路径实测）

补第三轮的缺口（"未单独重测空 `DSH_HOME`"）。从当前源码构建产物
`dist/npm/lara-dsh/bin/lara-pet.exe` 直接启动，`DSH_HOME` 指向空临时目录，
`LARA_PET_ALLOW_INJECTED=1`，用 SendInput 注入三击 Shift、剪贴板粘贴发送
"hi"，按窗口矩形截图确认 UI；协议层用 `lara-pet-sidecar --workspace` 手动
驱动 stdio 对照。

| 项目 | 结果 | 证据 |
|---|---|---|
| ISSUE-1：无可用模型时静默 | **通过** | 协议层：`send` 后事件流为 `speak`（提示原文含 "`lara dsh credentials DEEPSEEK_API_KEY --set`"）→ `turn_started` → `turn_finished status=error "no usable model route"`（修复前零 speak）；UI 层：气泡完整渲染提示原文，中文/反引号/排版正常；`no dsh daemon, so no route to borrow` 日志行同步出现。 |
| ISSUE-2：输入条展开时不冒泡 | **通过（错误路径）** | 气泡显示在展开的输入条正上方（placeholder 可见），随后 FIFO 轮换出错误行"这轮没跑完： no usable model route"；未读-only 压制未再出现。 |
| ISSUE-4：发送后 Esc 关不掉 | **通过** | 发送 "hi"、收到回复气泡后按 Esc，输入条立即关闭，兔子回 idle。 |
| ISSUE-3：未读角标没有消息入口 | **仍复现** | 光标在窗口内停留 500ms（click-through 已解除）后点击红点 "1"：角标未清、气泡不重展开、面板不开——比第三轮记录更差（那轮角标至少被清掉）。`main.js` 的 unread-dot 点击处理存在但事件没有到达，需查点击链路（嫌疑：petEl 的 mousedown→startDragging 或元素命中）。 |
| ISSUE-8：窗口位置漂移 | **仍复现** | 无人拖动，启动后窗口位于 `(147,147)`（600×800），稍后变为 `(392,392)`；`positionBottomLeft()` 的右下锚定未生效，也无位置持久化。 |

本轮仍未覆盖：正常 `speak_delta` 流式（无模型路径设计上不产生流式）、连续多条
speak 的 FIFO（需可用模型）、附件 chip。错误气泡路径在第三轮已测，本轮重复确认。

### 本轮自动化驱动的坑（供以后回归复用）

- SendInput 的 `INPUT` 结构必须含 `MOUSEINPUT`（x64 上 `cbSize=40`），否则
  返回 0（ERROR_INVALID_PARAMETER）；`SendKeys` 对裸 "+" 的合成流会让三击
  计不进 800ms 窗口（hook 日志全是 "tap #1"）。
- `KEYEVENTF_UNICODE` 注入的字符进不了 webview 的 textarea（事件到达但未落
  文本，原因未查）；剪贴板 + Ctrl-V 可用，与第一轮验证的结论一致。
- 点击前要等 ≥2 个 click-through 轮询周期（120ms/次），否则合成点击可能
  穿透到下层应用；注入键鼠必须带前台守卫（焦点不在桌宠时中止）。
- `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` 无效：wry 显式传自己的浏览器参
  数，CDP 直驱路线走不通。

本轮结束后已终止测试启动的 pet/sidecar 进程；未修改业务代码。

---

## 补充（2026-11-22 第五轮：窗口内空白区域逐像素点击穿透）

用户报告：宠物拉起后窗口内约一整块空白（兔兔上方与四周的透明区域）完全挡住
后面的应用。根因是 `spawn_click_through` 的矩形判定：光标在窗口矩形内一律
放行鼠标事件，透明像素照常拦截（旧版注释里标注"逐像素命中测试待做"）。

**修复**（`pet/src-tauri/src/lib.rs` + `pet/ui/js/hit_test.js` 新增）：

- Rust 轮询器改为 30ms 并继续独占 `set_ignore_cursor_events` 的写入；窗口外
  直接穿透；窗口内向 webview 发 `hitTestAt(x,y)` 探测（CSS 坐标 =
  物理差值 ÷ scale_factor）。
- 页面侧判定：`elementFromPoint` 命中 `#stage`/`body` 即透明；命中兔兔画布时
  读当前帧像素 alpha（32×32 画布 `getImageData` 1px，`willReadFrequently`）；
  其余元素（气泡/面板/把手/红点）一律实心。mousemove 主动上报 + 探测上报
  汇入 `report_hit_test` 命令，轮询器按"位置匹配 ±2px 且 500ms 内新鲜"取用，
  过期重探（气泡在静止光标下开合也能收敛）。
- 缺答案时维持原状（pass-through-first：刚进入的像素最多晚一个轮询周期变成
  可点，空白像素永不拦点击）；启动时先把窗口置为穿透，避免本地状态与 wry
  初始状态失配导致首次空白悬停不生效。

**证据**（窗口矩形 `(441,441,1041,1241)`、scale 2；EXSTYLE 直读
`WS_EX_TRANSPARENT` 位）：

| 探测点（帧像素） | 预期 | 实测 |
|---|---|---|
| 窗口上方空白 CSS(150,60) | 穿透 | TRANSPARENT=True ✓ |
| 画布透明边距 CSS(66,224)→帧(2,3) | 穿透 | True ✓（旧版这里拦点击） |
| 两耳之间 CSS(150,230)→帧(16,4) | 穿透 | True ✓（逐像素生效） |
| 脸颊中心 CSS(150,274)→帧(16,12) | 实心 | False ✓ |
| 腿部 CSS(114,370)→帧(10,28) | 实心 | False ✓ |
| 腹部 CSS(150,340)→帧(16,23) | 实心 | False ✓ |

穿透点击落点验证：原子注入（absolute move+down 同批 SendInput）点击空白区，
ClickTarget 测试窗获得前台（Edge→ClickTarget），说明点击落到后面窗口。
兔兔实心像素的 EXSTYLE 翻转依赖 `hitTestAt`→`report_hit_test`→轮询器应用
全链路，链路成立即"实心可点"语义与旧版一致（同一 wry 开关）。

**本轮合成点击对兔兔本体仍无可见反应**（右键设置面板不开、startDragging 不
动），与第四轮 ISSUE-3 的现象同源，且同样的注入对本轮自建 WinForms 窗也时
灵时不灵（客户区 Click 事件不触发、LL mouse hook 需消息泵才收得到事件），
判定为自动化注入链路在 WebView2 上的限制而非本次回归；EXSTYLE 与
WindowFromPoint 均证明窗口处于可收事件状态。待用户实际拖动/右键确认。

已交付：`dist/npm/lara-dsh/bin/lara-pet.exe` 与 web profile 安装副本均已替换
为本轮构建（9,632,768 字节），并以真实用户环境重启宠物（sidecar 重新 attach
dsh daemon，`lara-says` 会话续用）。`tests/test_pet_sidecar_routing.py` 12
项通过（存量 sidecar 改动不受影响）。
