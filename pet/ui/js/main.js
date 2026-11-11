import { loadSkin } from "./skin.js";
import { Pet } from "./pet.js";
import { Bubble } from "./bubble.js";
import { MockAgent, SidecarTransport } from "./agent.js";

const { listen } = window.__TAURI__.event;
const { getCurrentWindow, PhysicalPosition } = window.__TAURI__.window;
const { invoke } = window.__TAURI__.core;

async function positionBottomLeft() {
  const win = getCurrentWindow();
  const monitor = await win.currentMonitor();
  if (!monitor) return;
  const size = await win.outerSize();
  const x = monitor.position.x + monitor.size.width - size.width - 32;
  const y = monitor.position.y + monitor.size.height - size.height - 72;
  await win.setPosition(new PhysicalPosition(x, y));
}

async function main() {
  const petEl = document.getElementById("pet");
  const skin = await loadSkin("./skins/default");
  const pet = new Pet(petEl, skin);

  const bar = document.getElementById("input-bar");
  const input = document.getElementById("chat-input");
  const inputOpen = () => !bar.classList.contains("hidden");
  const petIdleState = () => (inputOpen() ? "listening" : "idle");

  const bubble = new Bubble(
    document.getElementById("bubble"),
    document.getElementById("bubble-text"),
    document.getElementById("bubble-close"),
    document.getElementById("bubble-panel"),
    document.getElementById("unread-dot"),
    {
      onTyping: (on) => pet.setState(on ? "speaking" : petIdleState()),
    },
  );

  // 聊天面板：完整历史 + 输入，气泡上的 💬 切换。
  const panel = document.getElementById("chat-panel");
  const history = document.getElementById("chat-history");
  const panelInput = document.getElementById("panel-input");
  const transcript = [];
  const renderTranscript = () => {
    history.innerHTML = "";
    for (const item of transcript) {
      const div = document.createElement("div");
      div.className = `chat-msg ${item.role}`;
      div.textContent = item.text;
      history.appendChild(div);
    }
    history.scrollTop = history.scrollHeight;
  };
  const panelOpen = () => !panel.classList.contains("hidden");
  const togglePanel = (open = panelOpen()) => {
    panel.classList.toggle("hidden", !open);
    if (open) {
      renderTranscript();
      panelInput.focus();
      bubble.engage();
    } else if (!inputOpen()) {
      pet.setState("idle");
    }
  };
  bubble.onPanel = () => togglePanel(true);
  document.getElementById("panel-collapse").addEventListener("click", () => togglePanel(false));
  const sendText = (text) => {
    if (!text) return;
    transcript.push({ role: "user", text });
    renderTranscript();
    bubble.engage();
    transport.send(text);
  };
  panelInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && panelInput.value.trim()) {
      const text = panelInput.value.trim();
      panelInput.value = "";
      sendText(text);
    } else if (e.key === "Escape") {
      togglePanel(false);
    }
  });

  // 未读红点：点击重新展开最后一条。
  document.getElementById("unread-dot").addEventListener("click", () => {
    bubble.setUnread(0);
    if (bubble.queue.length > 0) bubble.next();
    else bubble.show();
    pet.setState(petIdleState());
  });

  // One event protocol, two transports: the sidecar owns the real lara
  // realtime session; if it fails to come up, fall back to the mock so the
  // pet stays demonstrable.
  let fellBack = false;
  const handleAgentEvent = (event) => {
    switch (event.type) {
      case "sidecar_ready":
        break; // real session attached; bubbles now carry real speak events
      case "turn_started":
        pet.setState("thinking");
        break;
      case "speak":
        bubble.finalize(event.text);
        transcript.push({ role: "assistant", text: event.text });
        if (panelOpen()) renderTranscript();
        break;
      case "speak_delta":
        bubble.stream(event.text);
        pet.setState("speaking");
        break;
      case "turn_finished":
        pet.setState(petIdleState());
        if (event.error) {
          bubble.finalize(`这轮没跑完：${event.error}`);
        }
        break;
      case "sidecar_error":
        if (fellBack) return;
        fellBack = true;
        transport = mock;
        bubble.say(`sidecar 不可用（${event.msg}），暂时进入演示模式。`);
        break;
    }
  };
  const sidecar = new SidecarTransport(handleAgentEvent);
  const mock = new MockAgent(handleAgentEvent);
  let transport = sidecar;
  invoke("sidecar_status")
    .then((status) => {
      if (!status.running && status.error) {
        handleAgentEvent({ type: "sidecar_error", msg: status.error });
      }
    })
    .catch(() => {});
  await listen("pet://agent-event", (event) => transport.handle(event.payload));

  const toggleInput = (open = !inputOpen()) => {
    if (panelOpen()) togglePanel(false);
    bar.classList.toggle("hidden", !open);
    document.body.classList.toggle("input-open", open);
    if (open) {
      input.focus();
      pet.setState("listening");
      bubble.engage();
    } else {
      input.blur();
      pet.setState("idle");
    }
  };

  await listen("pet://triple-shift", () => toggleInput());

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && input.value.trim()) {
      const text = input.value.trim();
      input.value = "";
      sendText(text);
    } else if (e.key === "Escape") {
      toggleInput(false);
    }
  });
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
    const skins = await invoke("list_skins");
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

  await positionBottomLeft();
}

main().catch((e) => console.error("lara-pet: main failed:", e));
