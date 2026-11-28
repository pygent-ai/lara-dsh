import { loadSkin } from "./skin.js";
import { Pet } from "./pet.js";
import { createPetActivity } from "./pet_activity.mjs";
import { Bubble } from "./bubble.js";
import { MockAgent, SidecarTransport } from "./agent.js";
import { installHitTest } from "./hit_test.js";
import { installNewMessageIndicator, installUnreadMessageEntry } from "./chat_ui.mjs";

const { listen } = window.__TAURI__.event;
const { getCurrentWindow } = window.__TAURI__.window;
const { invoke } = window.__TAURI__.core;

async function main() {
  const petEl = document.getElementById("pet");
  const skin = await loadSkin("./skins/default");
  const pet = new Pet(petEl, skin);
  const activity = createPetActivity((state) => pet.setState(state));
  // 逐像素点击穿透的页面侧判定；必须在 Pet 之后安装，让画布上下文带上
  // willReadFrequently（hit_test.js 每次命中要读一个像素）。
  installHitTest(invoke);

  const bar = document.getElementById("input-bar");
  const input = document.getElementById("chat-input");
  const inputOpen = () => !bar.classList.contains("hidden");

  const bubble = new Bubble(
    document.getElementById("bubble"),
    document.getElementById("bubble-text"),
    document.getElementById("bubble-attachments"),
    document.getElementById("bubble-close"),
    document.getElementById("bubble-panel"),
    document.getElementById("unread-dot"),
    {
      onReveal: activity.showReply,
    },
  );

  // 聊天面板：完整历史 + 输入，气泡上的 💬 切换。
  const panel = document.getElementById("chat-panel");
  const history = document.getElementById("chat-history");
  const newMessageIndicator = document.getElementById("new-message-indicator");
  const panelInput = document.getElementById("panel-input");
  const transcript = [];
  // 用户正读着上面的历史时（离底部超过这个距离）新消息不抢滚动位置。
  const SCROLL_STICKY_PX = 24;
  const atBottom = () =>
    history.scrollHeight - history.scrollTop - history.clientHeight <= SCROLL_STICKY_PX;
  const scrollToBottom = () => {
    history.scrollTop = history.scrollHeight;
  };
  const panelOpen = () => !panel.classList.contains("hidden");
  const newMessageNotice = installNewMessageIndicator({
    history,
    indicator: newMessageIndicator,
    isPanelOpen: panelOpen,
    atBottom,
    scrollToBottom,
  });
  const buildMessage = (item) => {
    const div = document.createElement("div");
    div.className = `chat-msg ${item.role}`;
    // speak 允许只发文件不发文字；文字为空时面板只显示附件块。
    if (item.text) div.textContent = item.text;
    const attachments = item.role === "assistant" ? item.attachments || [] : [];
    if (attachments.length > 0) {
      const files = document.createElement("div");
      files.className = "chat-attachments";
      for (const path of attachments) {
        const file = document.createElement("div");
        file.className = "chat-file";
        file.textContent = `📄 ${path}`;
        file.title = path;
        files.appendChild(file);
      }
      div.appendChild(files);
    }
    return div;
  };
  // 流式中的助手消息和"正在输入"占位共用一个活动行，排在历史末尾；
  // 最终 speak 到达时它变成普通历史项，不重建其他 DOM。
  let liveRow = null;
  let liveText = "";
  const removeLive = () => {
    liveRow?.remove();
    liveRow = null;
    liveText = "";
  };
  const ensureLive = () => {
    if (liveRow) return liveRow;
    liveRow = document.createElement("div");
    liveRow.className = "chat-msg assistant live";
    history.appendChild(liveRow);
    return liveRow;
  };
  const showTyping = () => {
    const stick = atBottom();
    const row = ensureLive();
    if (liveText) return; // already streaming text
    row.classList.add("typing");
    row.textContent = "";
    for (let i = 0; i < 3; i += 1) row.appendChild(document.createElement("i"));
    newMessageNotice.afterAppend(stick);
  };
  const appendDelta = (delta) => {
    const stick = atBottom();
    const row = ensureLive();
    if (!liveText) {
      row.classList.remove("typing");
      row.textContent = "";
    }
    liveText += delta;
    row.textContent = liveText;
    newMessageNotice.afterAppend(stick);
  };
  // 新消息追加到面板：只插入这一条，用户滚到上面在读时保持原位置。
  const appendTranscript = (item) => {
    const stick = atBottom();
    transcript.push(item);
    removeLive();
    history.appendChild(buildMessage(item));
    newMessageNotice.afterAppend(stick);
  };
  const renderTranscript = () => {
    history.innerHTML = "";
    for (const item of transcript) history.appendChild(buildMessage(item));
    liveRow = null;
    const pending = liveText;
    liveText = "";
    if (pending) appendDelta(pending);
    scrollToBottom();
    newMessageNotice.hide();
  };
  const togglePanel = (open = panelOpen()) => {
    panel.classList.toggle("hidden", !open);
    document.body.classList.toggle("chat-open", open);
    if (open) {
      bar.classList.add("hidden");
      // 从输入条切过来：收掉输入条的 body 状态和焦点，面板接管输入。
      document.body.classList.remove("input-open");
      input.blur();
    }
    bubble.setSuppressed(open);
    if (open) {
      renderTranscript();
      panelInput.focus();
      bubble.engage();
    }
  };
  bubble.onPanel = () => togglePanel(true);
  document.getElementById("panel-collapse").addEventListener("click", () => togglePanel(false));
  // 输入条 ↔ 面板 两种对话形态来回切换。
  document.getElementById("input-to-panel").addEventListener("click", () => togglePanel(true));
  // 边缘把手 → 系统原生 resize 循环；North 方向底边固定，窗口往上变大。
  for (const handle of document.querySelectorAll("#resize-handles .rh")) {
    handle.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      getCurrentWindow().startResizeDragging(handle.dataset.dir).catch(console.error);
    });
  }
  const sendText = (text) => {
    if (!text) return;
    appendTranscript({ role: "user", text });
    bubble.engage();
    transport.send(text);
  };
  // 两个输入框都是 textarea：Enter 发送，Shift+Enter 换行；中文输入法
  // 组合期间（选候选词）的 Enter 属于输入法，不发送。
  const bindComposer = (box, onSend, onEscape) => {
    let composing = false;
    // 多行时随内容长高，封顶由 CSS max-height 管；发送后缩回一行。
    const autosize = () => {
      box.style.height = "auto";
      box.style.height = `${box.scrollHeight}px`;
    };
    box.addEventListener("input", autosize);
    box.addEventListener("compositionstart", () => {
      composing = true;
    });
    box.addEventListener("compositionend", () => {
      composing = false;
    });
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        if (e.shiftKey) return; // newline
        if (composing || e.isComposing || e.keyCode === 229) return;
        e.preventDefault();
        const text = box.value.trim();
        if (!text) return;
        box.value = "";
        autosize();
        onSend(text);
      } else if (e.key === "Escape") {
        onEscape();
      }
    });
  };
  bindComposer(panelInput, sendText, () => togglePanel(false));

  // 未读红点是稳定的消息入口：气泡可能已结束展示周期，但历史始终完整。
  installUnreadMessageEntry(document.getElementById("unread-dot"), togglePanel);

  // One event protocol, two transports: the sidecar owns the real lara
  // realtime session; if it fails to come up, fall back to the mock so the
  // pet stays demonstrable.
  let fellBack = false;
  const handleAgentEvent = (event) => {
    switch (event.type) {
      case "sidecar_ready":
      case "dsh_activity":
        activity.setDshWorking(event.working);
        break;
      case "turn_started":
        activity.startTurn(event.turnId);
        showTyping();
        break;
      case "speak":
        bubble.finalize(event.text, event.attachments);
        if (panelOpen()) activity.showReply();
        activity.endStream();
        appendTranscript({
          role: "assistant",
          text: event.text,
          attachments: event.attachments,
        });
        break;
      case "speak_delta":
        if (!event.text) break;
        bubble.stream(event.text);
        appendDelta(event.text);
        activity.streamReply(event.turnId);
        break;
      case "turn_finished": {
        const wasStreaming = activity.streaming;
        activity.finishTurn(event.turnId);
        if (!activity.streaming && (liveText || !activity.hasTurns)) removeLive();
        if (event.error) {
          if (wasStreaming && !activity.streaming) bubble.abortStream();
          const text = `这轮没跑完：${event.error}`;
          bubble.finalize(text);
          // 面板开着时气泡被抑制，错误要在面板里内联可见。
          appendTranscript({ role: "assistant error", text });
        }
        break;
      }
      case "sidecar_error":
        if (fellBack) return;
        if (event.fatal) {
          // 会话没了（bootstrap 失败 / 进程退出）：唯一进入演示模式的路径。
          fellBack = true;
          transport = mock;
          activity.reset();
          const notice = `sidecar 不可用（${event.msg}），暂时进入演示模式。`;
          bubble.say(notice);
          appendTranscript({ role: "assistant error", text: notice });
        } else {
          // 瞬时错误（单条投递失败等）：sidecar 还活着，提示后照常收发。
          const text = `这轮没送达：${event.msg}`;
          bubble.say(text);
          appendTranscript({ role: "assistant error", text });
        }
        break;
    }
  };
  const sidecar = new SidecarTransport(handleAgentEvent);
  const mock = new MockAgent(handleAgentEvent);
  let transport = sidecar;
  invoke("sidecar_status")
    .then((status) => {
      if (!status.running && status.error) {
        // the sidecar never came up: the only fatal kind at startup
        handleAgentEvent({ type: "sidecar_error", msg: status.error, fatal: true });
      }
    })
    .catch(() => {});
  await listen("pet://agent-event", (event) => transport.handle(event.payload));

  const toggleInput = (open = !inputOpen()) => {
    if (panelOpen()) togglePanel(false);
    bar.classList.toggle("hidden", !open);
    document.body.classList.toggle("input-open", open);
    if (open) {
      // 抢系统焦点：三击时用户键盘大概率在前台应用里，不抢焦点按键会
      // 打进那个应用而不是输入条。
      getCurrentWindow().setFocus().catch(() => {});
      input.focus();
      bubble.engage();
    } else {
      input.blur();
    }
  };
  // 面板 → 输入条：收起面板（气泡接力续放队列里的消息）再唤出输入条。
  document.getElementById("panel-to-input").addEventListener("click", () => {
    togglePanel(false);
    toggleInput(true);
  });

  await listen("pet://triple-shift", () => toggleInput());

  bindComposer(input, sendText, () => toggleInput(false));
  input.addEventListener("input", () => bubble.engage());

  // Drag the bunny; right-click menu: settings / quit (tray covers quit too).
  petEl.addEventListener("mousedown", (e) => {
    if (e.button === 0) getCurrentWindow().startDragging();
  });
  petEl.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    toggleSettings(true);
  });

  // —— 设置面板：热键 + 皮肤 ————————————————————————————————————
  const settingsPanel = document.getElementById("settings-panel");
  const setKey = document.getElementById("set-key");
  const setTaps = document.getElementById("set-taps");
  const setWindow = document.getElementById("set-window");
  const setSkin = document.getElementById("set-skin");
  const toggleSettings = (open = settingsPanel.classList.contains("hidden")) => {
    settingsPanel.classList.toggle("hidden", !open);
    if (open) loadSettingsIntoForm();
  };
  const loadSettingsIntoForm = async () => {
    const config = await invoke("get_pet_config");
    setKey.value = config.hotkey.key;
    setTaps.value = String(config.hotkey.taps);
    setWindow.value = String(config.hotkey.window_ms);
    // Bundled skin packs are listed in skins/index.json (ships with the UI).
    const skins = await fetch("./skins/index.json").then((r) => (r.ok ? r.json() : ["default"]));
    setSkin.innerHTML = "";
    for (const name of skins) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      setSkin.appendChild(option);
    }
    setSkin.value = config.skin;
  };
  const applySettings = async () => {
    await invoke("set_pet_config", {
      patch: {
        hotkey: {
          key: setKey.value,
          taps: Number(setTaps.value),
          window_ms: Number(setWindow.value),
        },
        skin: setSkin.value,
      },
    });
    const newSkin = await loadSkin(`./skins/${setSkin.value}`);
    pet.setSkin(newSkin);
  };
  for (const el of [setKey, setTaps, setWindow]) {
    el.addEventListener("change", () => applySettings().catch(console.error));
  }
  setSkin.addEventListener("change", () => applySettings().catch(console.error));
  document.getElementById("settings-close").addEventListener("click", () => toggleSettings(false));
  // saved skin (if any)
  invoke("get_pet_config")
    .then(async (config) => {
      if (config.skin && config.skin !== "default") {
        pet.setSkin(await loadSkin(`./skins/${config.skin}`));
      }
    })
    .catch(() => {});
}

main().catch((e) => console.error("lara-pet: main failed:", e));
