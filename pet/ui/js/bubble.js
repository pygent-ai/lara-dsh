// Speech bubble: FIFO queue, streaming as-is (no replay animation), unread dot.
//
// Interaction contract (per design discussion):
// - speak_delta events stream the words into the current bubble live;
//   the final speak event confirms the text (authoritative).
// - A visible bubble with no user interaction for HOLD_UNSEEN_MS collapses
//   into the unread dot on the bunny; clicking the dot re-opens it.
// - While the input bar / chat panel is open (the user is engaged) the
//   bubble never auto-collapses.
// - 面板开着时到达的消息只在面板历史里出场；收起面板后气泡接力，
//   把队列里的消息接着放完（见 setSuppressed(false)）。
// - × dismisses: counts as read; queued messages still rise one by one.
const HOLD_MS = 6000;      // advance to the next queued bubble after this hold
// No interaction -> collapse to the unread dot. The clock starts once the
// text has fully appeared (stream done), and the allowance
// scales with length so a long reply gets reading time; bounded both ways
// so every message collapses eventually and none collapses instantly.
const UNSEEN_MIN_MS = 25000;
const UNSEEN_MAX_MS = 90000;
const UNSEEN_MS_PER_CHAR = 60;

// 附件是 workspace 相对路径；气泡 chip 只显示文件名。
const basename = (path) => String(path).split(/[\\/]/).pop() || String(path);

export class Bubble {
  constructor(rootEl, textEl, attachEl, closeBtn, panelBtn, dotEl, { onReveal, onUnread }) {
    this.root = rootEl;
    this.textEl = textEl;
    this.attachEl = attachEl;
    this.dot = dotEl;
    this.queue = [];
    this.typing = false;
    this.streaming = false;
    this.visible = false;
    this.suppressed = false;
    this.unread = 0;
    this.holdTimer = null;
    this.unseenTimer = null;
    this.onReveal = onReveal;
    this.onUnread = onUnread;
    closeBtn.addEventListener("click", () => this.skip());
    panelBtn.addEventListener("click", () => this.onPanel?.());
  }

  // —— user-engagement hooks ————————————————————————————————————————
  noteUserActivity() {
    clearTimeout(this.unseenTimer);
    this.unseenTimer = null;
    if (this.visible) this.armUnseen();
  }

  engage() {
    // The user is typing: the bubble stays put and counts as read.
    clearTimeout(this.unseenTimer);
    this.unseenTimer = null;
    this.setUnread(0);
  }

  onPanel = null;

  // —— queue & visibility ————————————————————————————————————————————
  // Queue entries are { text, attachments } — speak 可只发文件不发文字。
  say(text, attachments = []) {
    this.queue.push({ text, attachments });
    if (this.suppressed) {
      // 面板开着：消息只进面板历史，气泡不出场；收起面板时
      // setSuppressed(false) 会把队列里的消息接着放成气泡。
      return;
    }
    if (this.typing || this.streaming) return;
    if (!this.visible) {
      this.next();
      return;
    }
    // 上一条已展示完且正停留：安排换场，别让新消息干等
    if (!this.holdTimer) {
      this.holdTimer = setTimeout(() => {
        this.hide();
        this.next();
      }, HOLD_MS);
    }
  }

  stream(text) {
    if (this.suppressed) return;
    if (this.typing || this.queue.length > 0) return; // never interleave
    if (!this.visible) this.show();
    if (!this.streaming) {
      // 新一条消息的流式开始：清掉停留期旧内容与待换场计时
      clearTimeout(this.holdTimer);
      this.holdTimer = null;
      this.textEl.textContent = "";
      this.renderAttachments([]);
    }
    this.streaming = true;
    this.textEl.textContent += text;
  }

  setSuppressed(on) {
    this.suppressed = on;
    if (on) {
      if (this.visible) this.hide();
      return;
    }
    // 面板收起：气泡接力。队列里有面板期间到达的消息就接着放；
    // 当前气泡还在停留的话安排换场，别让排队消息干等。
    if (!this.visible) {
      if (this.queue.length > 0) this.next();
    } else if (this.queue.length > 0 && !this.holdTimer) {
      this.holdTimer = setTimeout(() => {
        this.hide();
        this.next();
      }, HOLD_MS);
    }
  }

  finalize(text, attachments = []) {
    if (this.streaming) {
      // Confirm what streamed (the full event is authoritative).
      this.streaming = false;
      this.textEl.textContent = text;
      this.renderAttachments(attachments);
      this.onReveal?.();
      this.afterDone();
      return;
    }
    this.say(text, attachments);
  }

  abortStream() {
    if (this.streaming) this.hide();
  }

  skip() {
    if (this.typing || this.streaming) return; // don't cut a message mid-flight
    clearTimeout(this.holdTimer);
    this.hide();
    this.setUnread(0);
    this.next();
  }

  next() {
    const item = this.queue.shift();
    if (item === undefined) return;
    this.show();
    this.renderAttachments(item.attachments);
    this.reveal(item.text);
  }

  // speak 附件：workspace 相对路径，气泡里只放文件名，全路径进 tooltip。
  renderAttachments(paths) {
    this.attachEl.replaceChildren();
    this.attachEl.classList.toggle("hidden", !(paths && paths.length > 0));
    for (const path of paths || []) {
      const chip = document.createElement("div");
      chip.className = "bubble-file";
      chip.textContent = `📄 ${basename(path)}`;
      chip.title = path;
      this.attachEl.appendChild(chip);
    }
  }

  show() {
    this.root.classList.remove("hidden");
    this.dot.classList.add("hidden");
    this.visible = true;
    this.setUnread(0);
    // The unseen clock is armed by afterDone(), once the text is complete.
    clearTimeout(this.unseenTimer);
    this.unseenTimer = null;
  }

  hide() {
    this.root.classList.add("hidden");
    this.visible = false;
    this.typing = false;
    this.streaming = false;
    clearTimeout(this.holdTimer);
    clearTimeout(this.unseenTimer);
    this.unseenTimer = null;
  }

  collapseToDot() {
    // Bubble seen-but-ignored: shrink to the unread dot and keep counting.
    this.hide();
    this.dot.textContent = String(Math.max(1, this.unread));
    this.dot.classList.remove("hidden");
    this.onUnread?.(this.unread);
  }

  bumpUnread() {
    this.setUnread(this.unread + 1);
  }

  setUnread(count) {
    this.unread = count;
    if (count === 0 && !this.visible) this.dot.classList.add("hidden");
  }

  armUnseen() {
    clearTimeout(this.unseenTimer);
    const length = this.textEl.textContent.length;
    const delay = Math.min(UNSEEN_MAX_MS, Math.max(UNSEEN_MIN_MS, length * UNSEEN_MS_PER_CHAR));
    this.unseenTimer = setTimeout(() => {
      if (this.visible && !this.typing && !this.streaming) this.collapseToDot();
    }, delay);
  }

  // speak 流出多少就显示多少：非流式消息也整段直接呈现，不再做逐字回放。
  // 流式消息由 stream() 逐块追加，两条路径看到的就是 speak 实际产生的文本。
  reveal(text) {
    this.typing = false;
    this.textEl.textContent = text;
    this.onReveal?.();
    this.afterDone();
  }

  afterDone() {
    if (this.queue.length > 0) {
      this.holdTimer = setTimeout(() => {
        this.hide();
        this.next();
      }, HOLD_MS);
      return;
    }
    // last message: stays visible, then collapses to the dot when ignored
    this.armUnseen();
  }
}
