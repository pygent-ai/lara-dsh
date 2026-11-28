// Agent transport. Two implementations speak ONE event protocol:
//   { type: "turn_started", turnId }
//   { type: "dsh_activity", working }
//   { type: "speak", text, attachments }   (final, complete speak delivery)
//   { type: "speak_delta", text, turnId }
//   { type: "turn_finished", turnId }
// plus transport-lifecycle events: sidecar_ready / sidecar_error.
export class SidecarTransport {
  constructor(onEvent) {
    this.onEvent = onEvent;
  }

  handle(event) {
    switch (event?.type) {
      case "ready":
        this.onEvent({ type: "sidecar_ready", session: event.session_id, working: event.dsh_working === true });
        break;
      case "dsh_activity":
        this.onEvent({ type: "dsh_activity", working: event.working === true });
        break;
      case "turn_started":
        this.onEvent({ type: "turn_started", turnId: event.turn_id });
        break;
      case "speak":
        this.onEvent({
          type: "speak",
          text: String(event.text || ""),
          attachments: Array.isArray(event.attachments) ? event.attachments : [],
        });
        break;
      case "speak_delta":
        // Streaming preview of the speak text being generated; the final
        // speak event confirms (and replaces) what streamed.
        this.onEvent({ type: "speak_delta", text: String(event.text || ""), turnId: event.turn_id });
        break;
      case "turn_finished":
        this.onEvent({ type: "turn_finished", turnId: event.turn_id, error: event.error || null });
        break;
      case "error":
        // fatal marks a dead session (bootstrap failure); everything else is
        // a glitch on one message and the sidecar keeps serving.
        this.onEvent({
          type: "sidecar_error",
          msg: String(event.msg || "sidecar error"),
          fatal: event.fatal === true,
        });
        break;
      case "sidecar_exited":
        this.onEvent({ type: "sidecar_error", msg: "sidecar exited", fatal: true });
        break;
      case "log":
        console.log("[sidecar]", event.msg);
        break;
    }
  }

  send(text) {
    window.__TAURI__.core
      .invoke("agent_send", { text })
      .catch((e) => this.onEvent({ type: "sidecar_error", msg: String(e), fatal: false }));
  }
}

export class MockAgent {
  constructor(emit) {
    this.emit = emit;
    this.nextTurn = 0;
  }

  // Events keep flowing through the shared listener after a fallback; the
  // mock owns nothing on that wire, so it just absorbs them.
  handle() {}

  send(text) {
    const turnId = ++this.nextTurn;
    this.emit({ type: "turn_started", turnId });
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
    setTimeout(() => this.emit({ type: "turn_finished", turnId }), 2700);
  }
}
