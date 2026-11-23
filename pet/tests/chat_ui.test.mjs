import assert from "node:assert/strict";
import test from "node:test";

import { installNewMessageIndicator, installUnreadMessageEntry } from "../ui/js/chat_ui.mjs";

class FakeElement {
  constructor(classes = []) {
    this.classes = new Set(classes);
    this.listeners = new Map();
    this.classList = {
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
    };
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }

  dispatch(name) {
    this.listeners.get(name)?.();
  }
}

test("new messages keep the reader's scroll position and show an entry", () => {
  const history = new FakeElement();
  const indicator = new FakeElement(["hidden"]);
  let scrolls = 0;
  let atBottom = false;
  const notice = installNewMessageIndicator({
    history,
    indicator,
    isPanelOpen: () => true,
    atBottom: () => atBottom,
    scrollToBottom: () => {
      scrolls += 1;
    },
  });

  notice.afterAppend(false);
  assert.equal(scrolls, 0);
  assert.equal(indicator.classList.contains("hidden"), false);

  indicator.dispatch("click");
  assert.equal(scrolls, 1);
  assert.equal(indicator.classList.contains("hidden"), true);

  indicator.classList.remove("hidden");
  atBottom = true;
  history.dispatch("scroll");
  assert.equal(indicator.classList.contains("hidden"), true);
});

test("messages append normally when the reader is already at the bottom", () => {
  const history = new FakeElement();
  const indicator = new FakeElement(["hidden"]);
  let scrolls = 0;
  const notice = installNewMessageIndicator({
    history,
    indicator,
    isPanelOpen: () => true,
    atBottom: () => true,
    scrollToBottom: () => {
      scrolls += 1;
    },
  });

  notice.afterAppend(true);
  assert.equal(scrolls, 1);
  assert.equal(indicator.classList.contains("hidden"), true);
});

test("the unread dot always opens the durable history panel", () => {
  const dot = new FakeElement();
  const calls = [];
  installUnreadMessageEntry(dot, (open) => calls.push(open));

  dot.dispatch("click");
  assert.deepEqual(calls, [true]);
});
