export function installNewMessageIndicator({
  history,
  indicator,
  isPanelOpen,
  atBottom,
  scrollToBottom,
}) {
  const hide = () => indicator.classList.add("hidden");
  const afterAppend = (stick) => {
    if (stick) {
      scrollToBottom();
      hide();
    } else if (isPanelOpen()) {
      indicator.classList.remove("hidden");
    }
  };

  history.addEventListener("scroll", () => {
    if (atBottom()) hide();
  });
  indicator.addEventListener("click", () => {
    scrollToBottom();
    hide();
  });

  return { afterAppend, hide };
}

export function installUnreadMessageEntry(dot, openPanel) {
  dot.addEventListener("click", () => openPanel(true));
}
