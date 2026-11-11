// Speech bubble: FIFO queue, streaming + typewriter reveal, unread dot.
//
// Interaction contract (per design discussion):
// - speak_delta events stream the words into the current bubble live;
//   the final speak event confirms the text (authoritative).
// - A visible bubble with no user interaction for HOLD_UNSEEN_MS collapses
//   into the unread dot on the bunny; clicking the dot re-opens it.
// - While the input bar / chat panel is open (the user is engaged) the
//   bubble never auto-collapses.
// - × dismisses: counts as read; queued messages still rise one by one.
const TYPE_MS = 26;
const HOLD_MS = 6000;      // advance to the next queued bubble after this hold
const UNSEEN_MS = 25000;   // no interaction -> collapse to the unread dot

export class Bubble {
  constructor(rootEl, textEl, closeBtn, panelBtn, dotEl, { onTyping, onUnread }) {
    this.root = rootEl;
    this.textEl = textEl;
    this.dot = dotEl;
    this.queue = [];
    this.typing = false;
    this.streaming = false;
    this.visible = false;
    this.suppressed = false;
    this.unread = 0;
    this.holdTimer = null;
    this.unseenTimer = null;
    this.onTyping = onTyping;
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
  say(text) {
    this.queue.push(text);
    if (this.suppressed) {
      // 面板打开时消息只进面板历史，气泡不出场（计为已读）
      this.bumpUnread();
      this.setUnread(0);
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
    }
    this.streaming = true;
    this.textEl.textContent += text;
  }

  setSuppressed(on) {
    this.suppressed = on;
    if (on && this.visible) this.hide();
  }

  finalize(text) {
    if (this.streaming) {
      // Confirm what streamed (the full event is authoritative).
      this.streaming = false;
      this.textEl.textContent = text;
      this.afterDone();
      return;
    }
    this.say(text);
  }

  skip() {
    if (this.typing || this.streaming) return; // don't cut a message mid-flight
    clearTimeout(this.holdTimer);
    this.hide();
    this.setUnread(0);
    this.next();
  }

  next() {
    const text = this.queue.shift();
    if (text === undefined) return;
    this.show();
    this.typewriter(text);
  }

  show() {
    this.root.classList.remove("hidden");
    this.dot.classList.add("hidden");
    this.visible = true;
    this.setUnread(0);
    this.armUnseen();
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
    this.unseenTimer = setTimeout(() => {
      if (this.visible && !this.typing && !this.streaming) this.collapseToDot();
    }, UNSEEN_MS);
  }

  typewriter(text) {
    this.typing = true;
    this.textEl.textContent = "";
    this.onTyping(true);
    let i = 0;
    const step = () => {
      if (i < text.length) {
        i += 1;
        this.textEl.textContent = text.slice(0, i);
        setTimeout(step, TYPE_MS);
        return;
      }
      this.typing = false;
      this.onTyping(false);
      this.afterDone();
    };
    step();
  }

  afterDone() {
    if (this.queue.length > 0) {
      this.holdTimer = setTimeout(() => {
        this.hide();
        this.next();
      }, HOLD_MS);
    }
    // last message: stays visible (or collapses to the dot when ignored)
  }
}
