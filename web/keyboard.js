/**
 * Desktop Keyboard Controls — WASD + Arrow Keys for movement, Space to drop.
 * Returns a cleanup function that removes all event listeners.
 *
 * Key design decisions:
 *  - preventDefault is called for ALL game keys (arrows, WASD, Space) on EVERY
 *    event, including repeats, to stop the browser from scrolling the page.
 *  - dropStart() is only sent on the first non-repeat Space press.
 *  - Direction keydowns are de-duplicated via the `pressed` set so holding a
 *    key doesn't flood the server.
 *  - Visual feedback: highlights the matching on-screen desktop D-Pad button
 *    when a keyboard key is held, providing immediate visual confirmation.
 */
function setupKeyboard(controlSocket, sfx) {
  const KEY_MAP = {
    ArrowUp: "north", KeyW: "north",
    ArrowDown: "south", KeyS: "south",
    ArrowLeft: "west", KeyA: "west",
    ArrowRight: "east", KeyD: "east",
  };

  const DIR_TO_SELECTOR = {
    north: ".vdpad-up",
    south: ".vdpad-down",
    west:  ".vdpad-left",
    east:  ".vdpad-right",
  };

  const pressed = new Set();

  function highlightBtn(dir, active) {
    const btn = document.querySelector(DIR_TO_SELECTOR[dir]);
    if (btn) {
      if (active) btn.classList.add("active");
      else btn.classList.remove("active");
    }
  }

  function onKeydown(e) {
    // Don't capture when typing in inputs
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;

    const dir = KEY_MAP[e.code];
    if (dir) {
      e.preventDefault(); // Always prevent arrow/WASD from scrolling
      if (!pressed.has(dir)) {
        pressed.add(dir);
        controlSocket.keydown(dir);
        highlightBtn(dir, true);
        if (sfx) sfx.playMove();
      }
    }

    // Space = single-press drop. Always preventDefault to block page scroll,
    // but only fire dropStart on the initial keydown (not repeats).
    if (e.code === "Space") {
      e.preventDefault();
      if (!e.repeat) {
        controlSocket.dropStart();
        if (sfx) sfx.playDrop();
      }
    }
  }

  function onKeyup(e) {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;

    const dir = KEY_MAP[e.code];
    if (dir) {
      e.preventDefault();
      if (pressed.has(dir)) {
        pressed.delete(dir);
        controlSocket.keyup(dir);
        highlightBtn(dir, false);
      }
    }
  }

  // Safety: release all directions on window blur
  function onBlur() {
    for (const key of pressed) {
      controlSocket.keyup(key);
      highlightBtn(key, false);
    }
    pressed.clear();
  }

  document.addEventListener("keydown", onKeydown);
  document.addEventListener("keyup", onKeyup);
  window.addEventListener("blur", onBlur);

  // Return cleanup function to prevent listener accumulation on reconnect
  return function teardown() {
    document.removeEventListener("keydown", onKeydown);
    document.removeEventListener("keyup", onKeyup);
    window.removeEventListener("blur", onBlur);
    // Release any held directions
    for (const key of pressed) {
      controlSocket.keyup(key);
      highlightBtn(key, false);
    }
    pressed.clear();
  };
}
