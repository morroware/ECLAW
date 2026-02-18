/**
 * Touch D-Pad — Mobile touch controls with direction detection.
 * Includes haptic feedback and sound effects.
 */
class TouchDPad {
  constructor(element, controlSocket, sfx) {
    this.el = element;
    this.ctrl = controlSocket;
    this.sfx = sfx || null;
    this.activeKey = null;
    this._pointerDown = false; // Track whether pointer is actively pressed

    this.el.style.touchAction = "none"; // Prevent scrolling/zooming

    // Bind handlers so they can be removed in destroy()
    this._onPointerDownBound = (e) => this._onPointerDown(e);
    this._onPointerMoveBound = (e) => this._onPointerMove(e);
    this._releaseBound = (e) => this._releasePointer(e);

    this.el.addEventListener("pointerdown", this._onPointerDownBound);
    this.el.addEventListener("pointermove", this._onPointerMoveBound);
    this.el.addEventListener("pointerup", this._releaseBound);
    this.el.addEventListener("pointercancel", this._releaseBound);
    this.el.addEventListener("pointerleave", this._releaseBound);
  }

  destroy() {
    this._release();
    this.el.removeEventListener("pointerdown", this._onPointerDownBound);
    this.el.removeEventListener("pointermove", this._onPointerMoveBound);
    this.el.removeEventListener("pointerup", this._releaseBound);
    this.el.removeEventListener("pointercancel", this._releaseBound);
    this.el.removeEventListener("pointerleave", this._releaseBound);
  }

  _vibrate(ms) {
    if (navigator.vibrate) {
      try { navigator.vibrate(ms); } catch (e) { /* ignore */ }
    }
  }

  _onPointerDown(e) {
    e.preventDefault();
    this._pointerDown = true;
    // Capture pointer so move/up events keep firing even if finger/mouse leaves
    try { this.el.setPointerCapture(e.pointerId); } catch (_) {}
    this._handlePointer(e);
  }

  _onPointerMove(e) {
    // Only process movement when pointer is actively pressed (click-and-hold)
    if (!this._pointerDown) return;
    e.preventDefault();
    this._handlePointer(e);
  }

  _releasePointer(e) {
    this._pointerDown = false;
    if (e) {
      try { this.el.releasePointerCapture(e.pointerId); } catch (_) {}
    }
    this._release();
  }

  _handlePointer(e) {
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

      // Haptic and sound feedback on direction change
      this._vibrate(15);
      if (this.sfx) this.sfx.playMove();

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
