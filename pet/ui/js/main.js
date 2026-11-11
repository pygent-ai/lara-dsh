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
  const skin = await loadSkin();
  const pet = new Pet(document.getElementById("pet"), skin);

  const bar = document.getElementById("input-bar");
  const input = document.getElementById("chat-input");
  const inputOpen = () => !bar.classList.contains("hidden");
  const petIdleState = () => (inputOpen() ? "listening" : "idle");

  const bubble = new Bubble(
    document.getElementById("bubble"),
    document.getElementById("bubble-text"),
    document.getElementById("bubble-close"),
    {
      onTyping: (on) => pet.setState(on ? "speaking" : petIdleState()),
    },
  );

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
        bubble.say(event.text);
        break;
      case "speak_delta":
        break; // streaming preview lands with the M3 kernel bridge
      case "turn_finished":
        pet.setState(petIdleState());
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
    bar.classList.toggle("hidden", !open);
    document.body.classList.toggle("input-open", open);
    if (open) {
      input.focus();
      pet.setState("listening");
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
      transport.send(text);
    } else if (e.key === "Escape") {
      toggleInput(false);
    }
  });

  // Drag the bunny anywhere on screen; right-click offers quit (tray in M2).
  document.getElementById("pet").addEventListener("mousedown", (e) => {
    if (e.button === 0) getCurrentWindow().startDragging();
  });
  document.getElementById("pet").addEventListener("contextmenu", (e) => {
    e.preventDefault();
    if (confirm("退出 Lara Pet？")) invoke("quit_app");
  });

  await positionBottomLeft();
}

main().catch((e) => console.error("lara-pet: main failed:", e));
