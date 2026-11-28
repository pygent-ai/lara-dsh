import assert from "node:assert/strict";
import test from "node:test";

import { createPetActivity } from "../ui/js/pet_activity.mjs";

test("a reply takes priority and returns to dsh work after it ends", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let state;
  const activity = createPetActivity((next) => { state = next; });

  activity.startTurn("foreground");
  activity.setDshWorking(true);
  assert.equal(state, "working");
  activity.streamReply("foreground");
  assert.equal(state, "speaking");
  activity.finishTurn("foreground");
  assert.equal(state, "speaking");
  t.mock.timers.tick(500);
  assert.equal(state, "working");
  activity.setDshWorking(false);
  assert.equal(state, "idle");
});

test("finishing another turn does not stop the reply being shown", () => {
  let state;
  const activity = createPetActivity((next) => { state = next; });

  activity.startTurn("first");
  activity.startTurn("second");
  activity.streamReply("second");
  activity.finishTurn("first");
  assert.equal(state, "speaking");
  assert.equal(activity.streaming, true);
  activity.endStream();
  assert.equal(state, "working");
  activity.finishTurn("second");
  assert.equal(state, "idle");
});

test("an immediate reply animates briefly and reset cancels its timer", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let state;
  const activity = createPetActivity((next) => { state = next; });

  activity.showReply();
  assert.equal(state, "speaking");
  activity.reset();
  activity.startTurn("next");
  t.mock.timers.tick(500);
  assert.equal(state, "working");
});
