// Sprite animator: renders skin frames onto the pet canvas.
// The canvas is 32x32 logical pixels; CSS scales it up with pixelated rendering.
export class Pet {
  constructor(canvas, skin) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
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
