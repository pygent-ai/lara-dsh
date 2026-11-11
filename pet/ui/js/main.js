import { loadSkin } from "./skin.js";
import { Pet } from "./pet.js";
import { Bubble } from "./bubble.js";
import { MockAgent } from "./agent.js";

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

  // Agent events drive pet state; speak events become bubbles (M1: mock).
  const agent = new MockAgent((event) => {
    switch (event.type) {
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
    }
  });

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
      agent.send(text);
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
