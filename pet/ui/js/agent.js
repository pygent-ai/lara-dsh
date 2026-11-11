// Agent transport. Two implementations speak ONE event protocol:
//   { type: "turn_started" }
//   { type: "speak", text, attachments }   (final, complete speak delivery)
//   { type: "speak_delta", text }          (streaming preview, M3 bridge)
//   { type: "turn_finished" }
// plus transport-lifecycle events: sidecar_ready / sidecar_error.
export class SidecarTransport {
  constructor(onEvent) {
    this.onEvent = onEvent;
  }

  handle(event) {
    switch (event?.type) {
      case "ready":
        this.onEvent({ type: "sidecar_ready", session: event.session_id });
        break;
      case "turn_started":
        this.onEvent({ type: "turn_started" });
        break;
      case "speak":
        this.onEvent({
          type: "speak",
          text: String(event.text || ""),
          attachments: Array.isArray(event.attachments) ? event.attachments : [],
        });
        break;
      case "turn_finished":
        this.onEvent({ type: "turn_finished", error: event.error || null });
        break;
      case "error":
        this.onEvent({ type: "sidecar_error", msg: String(event.msg || "sidecar error") });
        break;
      case "sidecar_exited":
        this.onEvent({ type: "sidecar_error", msg: "sidecar exited" });
        break;
      case "log":
        console.log("[sidecar]", event.msg);
        break;
    }
  }

  send(text) {
    window.__TAURI__.core
      .invoke("agent_send", { text })
      .catch((e) => this.onEvent({ type: "sidecar_error", msg: String(e) }));
  }
}

export class MockAgent {
  constructor(emit) {
    this.emit = emit;
  }

  send(text) {
    this.emit({ type: "turn_started" });
    setTimeout(
      () => this.emit({ type: "speak", text: `收到：「${text}」`, attachments: [] }),
      700,
    );
    setTimeout(
      () =>
        this.emit({
          type: "speak",
          text:
            "M1 阶段我还是复读兔，等 sidecar 接上 lara 的 realtime 会话，" +
            "我说的每一句都是 agent 通过 speak 发来的真话。",
          attachments: [],
        }),
      2600,
    );
    setTimeout(() => this.emit({ type: "turn_finished" }), 2700);
  }
}
