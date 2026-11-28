// One animation decision for overlapping foreground turns, worker jobs, and replies.
export function createPetActivity(render) {
  const turns = new Set();
  let dshWorking = false;
  let streamingTurn = null;
  let replyTimer = null;

  const update = () => {
    if (streamingTurn !== null || replyTimer !== null) render("speaking");
    else render(turns.size || dshWorking ? "working" : "idle");
  };

  const showReply = () => {
    clearTimeout(replyTimer);
    replyTimer = setTimeout(() => {
      replyTimer = null;
      update();
    }, 500);
    update();
  };

  return {
    get streaming() { return streamingTurn !== null; },
    get hasTurns() { return turns.size > 0; },
    startTurn(id) {
      turns.add(id);
      update();
    },
    finishTurn(id) {
      turns.delete(id);
      if (id === streamingTurn) {
        streamingTurn = null;
        showReply();
      } else {
        update();
      }
    },
    setDshWorking(working) {
      dshWorking = working;
      update();
    },
    streamReply(id) {
      streamingTurn = id;
      update();
    },
    endStream() {
      streamingTurn = null;
      update();
    },
    showReply,
    reset() {
      turns.clear();
      dshWorking = false;
      streamingTurn = null;
      clearTimeout(replyTimer);
      replyTimer = null;
      update();
    },
  };
}
