// Agent transport. M1 ships a MockAgent so the pet is demonstrable without the
// Python sidecar; the future SidecarTransport speaks the same event protocol:
//   { type: "turn_started" }
//   { type: "speak", text, attachments }   (final, complete speak delivery)
//   { type: "speak_delta", text }          (streaming preview, M3 bridge)
//   { type: "turn_finished" }
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
