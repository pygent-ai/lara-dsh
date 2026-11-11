// Speech-bubble queue: FIFO, typewriter reveal, × skips, last message stays.
//
// Per the interaction design: bubbles do not auto-hide; while the queue has
// more messages, each holds HOLD_MS then the next rises; the final message
// stays visible until dismissed. Unread red dot + chat panel switch come in M2.
const TYPE_MS = 26;
const HOLD_MS = 6000;

export class Bubble {
  constructor(rootEl, textEl, closeBtn, { onTyping }) {
    this.root = rootEl;
    this.textEl = textEl;
    this.queue = [];
    this.typing = false;
    this.holdTimer = null;
    this.timers = new Set();
    this.onTyping = onTyping;
    closeBtn.addEventListener("click", () => this.skip());
  }

  say(text) {
    this.queue.push(text);
    if (!this.typing && this.root.classList.contains("hidden")) this.next();
  }

  skip() {
    if (this.typing) return; // don't cut the typewriter mid-sentence
    clearTimeout(this.holdTimer);
    this.hide();
    this.next();
  }

  hide() {
    this.root.classList.add("hidden");
  }

  next() {
    const text = this.queue.shift();
    if (text === undefined) return;
    this.show();
    this.typewriter(text);
  }

  show() {
    this.root.classList.remove("hidden");
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
        const t = setTimeout(step, TYPE_MS);
        this.timers.add(t);
        return;
      }
      this.typing = false;
      this.onTyping(false);
      if (this.queue.length > 0) {
        this.holdTimer = setTimeout(() => {
          this.hide();
          this.next();
        }, HOLD_MS);
      }
      // last message: stay visible until ×
    };
    step();
  }
}
