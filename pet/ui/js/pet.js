// Sprite animator: renders skin frames onto the pet canvas.
// The canvas is 32x32 logical pixels; CSS scales it up with pixelated rendering.
export class Pet {
  constructor(canvas, skin) {
    this.canvas = canvas;
    // willReadFrequently：点击穿透的命中判定（hit_test.js）会逐像素读这块
    // 画布；32x32 的软件画布读起来几乎无成本。
    this.ctx = canvas.getContext("2d", { willReadFrequently: true });
    this.ctx.imageSmoothingEnabled = false;
    this.skin = skin;
    this.state = "idle";
    this.frameIx = 0;
    this.lastTick = 0;
    requestAnimationFrame((t) => this.tick(t));
  }

  setState(name) {
    if (!this.skin.manifest.states[name] || this.state === name) return;
    this.state = name;
    this.frameIx = 0;
  }

  setSkin(skin) {
    this.skin = skin;
    // 皮肤文件是外部输入：缺当前状态就回 idle，别让动画循环崩掉
    if (!this.skin.manifest.states[this.state]) this.state = "idle";
    this.frameIx = 0;
    this.draw(this.skin.manifest.states[this.state].frames[0]);
  }

  tick(t) {
    const state = this.skin.manifest.states[this.state];
    if (t - this.lastTick >= 1000 / state.fps) {
      this.lastTick = t;
      this.frameIx = (this.frameIx + 1) % state.frames.length;
      this.draw(state.frames[this.frameIx]);
    }
    requestAnimationFrame((tt) => this.tick(tt));
  }

  draw(frame) {
    const { w, h } = this.skin.manifest.frame;
    this.ctx.clearRect(0, 0, w, h);
    this.ctx.drawImage(this.skin.image, frame * w, 0, w, h, 0, 0, w, h);
  }
}
