/**
 * Touch D-Pad — Mobile touch controls with direction detection.
 */
class TouchDPad {
  constructor(element, controlSocket) {
    this.el = element;
    this.ctrl = controlSocket;
    this.activeKey = null;

    this.el.style.touchAction = "none"; // Prevent scrolling/zooming

    this.el.addEventListener("pointerdown", (e) => this._onPointer(e));
    this.el.addEventListener("pointermove", (e) => this._onPointer(e));
    this.el.addEventListener("pointerup", () => this._release());
    this.el.addEventListener("pointercancel", () => this._release());
    this.el.addEventListener("pointerleave", () => this._release());
  }

  _onPointer(e) {
    e.preventDefault();
    const rect = this.el.getBoundingClientRect();
    const cx = rect.width / 2;
    const cy = rect.height / 2;
    const x = e.clientX - rect.left - cx;
    const y = e.clientY - rect.top - cy;

    // Dead zone (center 20%)
    const deadZone = rect.width * 0.1;
    if (Math.abs(x) < deadZone && Math.abs(y) < deadZone) {
      this._release();
      return;
    }

    // Determine direction by dominant axis
    let newKey;
    if (Math.abs(x) > Math.abs(y)) {
      newKey = x > 0 ? "east" : "west";
    } else {
      newKey = y > 0 ? "south" : "north";
    }

    if (newKey !== this.activeKey) {
      if (this.activeKey) this.ctrl.keyup(this.activeKey);
      this.ctrl.keydown(newKey);
      this.activeKey = newKey;

      // Visual feedback
      this._updateVisual(newKey);
    }
  }

  _release() {
    if (this.activeKey) {
      this.ctrl.keyup(this.activeKey);
      this.activeKey = null;
      this._clearVisual();
    }
  }

  _updateVisual(dir) {
    this._clearVisual();
    const btn = this.el.querySelector(`[data-dir="${dir}"]`);
    if (btn) btn.classList.add("active");
  }

  _clearVisual() {
    this.el.querySelectorAll(".dpad-btn").forEach(b => b.classList.remove("active"));
  }
}
