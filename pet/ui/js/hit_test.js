// 逐像素点击穿透的命中判定：Rust 轮询器对"光标在窗口矩形内"不再一律放行
// 鼠标事件，而是把当前位置交给页面判定（window.__laraPet.hitTestAt 探测 +
// 页面收到事件时的 mousemove 主动上报），只有可见/可交互内容才接收点击。
// 一个点算"实心"：命中的元素不是透明底板（#stage/body/html）；命中兔兔画布
// 时只算当前帧真正有像素的区域，画布四角的透明边距照常穿透。
// 探测一律上报、mousemove 去重上报；页面侧不直接改穿透状态，单一写入方在
// Rust 轮询器，避免两边互相打架。
export function installHitTest(invoke) {
  const stage = document.getElementById("stage");
  const petCanvas = document.getElementById("pet");
  const petCtx = petCanvas.getContext("2d");
  let last = null; // 最近一次上报 { x, y, solid }

  const petPixelSolid = (x, y) => {
    const rect = petCanvas.getBoundingClientRect();
    const px = Math.min(
      petCanvas.width - 1,
      Math.max(0, Math.floor(((x - rect.left) / rect.width) * petCanvas.width)),
    );
    const py = Math.min(
      petCanvas.height - 1,
      Math.max(0, Math.floor(((y - rect.top) / rect.height) * petCanvas.height)),
    );
    return petCtx.getImageData(px, py, 1, 1).data[3] > 0;
  };

  const solidAt = (x, y) => {
    const el = document.elementFromPoint(x, y);
    if (!el || el === stage || el === document.body || el === document.documentElement) {
      return false;
    }
    return el !== petCanvas || petPixelSolid(x, y);
  };

  const report = (x, y, solid, dedup) => {
    if (
      dedup &&
      last &&
      last.solid === solid &&
      Math.abs(last.x - x) < 2 &&
      Math.abs(last.y - y) < 2
    ) {
      return; // 同一位置同一答案：Rust 侧的缓存仍然有效
    }
    last = { x, y, solid };
    invoke("report_hit_test", { x, y, solid }).catch(() => {});
  };

  window.__laraPet = {
    hitTestAt: (x, y) => report(x, y, solidAt(x, y), false),
  };
  // 事件放行期间页面同时主动上报，气泡开合这类"光标没动但内容变了"的
  // 情形由 Rust 侧的定期重探兜底。
  window.addEventListener("mousemove", (e) => {
    report(e.clientX, e.clientY, solidAt(e.clientX, e.clientY), true);
  });
}
