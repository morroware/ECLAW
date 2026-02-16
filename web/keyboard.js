/**
 * Desktop Keyboard Controls — WASD + Arrow Keys for movement, Space to drop.
 * Returns a cleanup function that removes all event listeners.
 */
function setupKeyboard(controlSocket) {
  const KEY_MAP = {
    ArrowUp: "north", KeyW: "north",
    ArrowDown: "south", KeyS: "south",
    ArrowLeft: "west", KeyA: "west",
    ArrowRight: "east", KeyD: "east",
  };
  const pressed = new Set();

  function onKeydown(e) {
    // Don't capture when typing in inputs
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;

    const dir = KEY_MAP[e.code];
    if (dir && !pressed.has(dir)) {
      pressed.add(dir);
      controlSocket.keydown(dir);
      e.preventDefault();
    }
    // Space = single-press drop (no hold needed)
    if (e.code === "Space" && !e.repeat) {
      controlSocket.dropStart();
      e.preventDefault();
    }
  }

  function onKeyup(e) {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;

    const dir = KEY_MAP[e.code];
    if (dir && pressed.has(dir)) {
      pressed.delete(dir);
      controlSocket.keyup(dir);
      e.preventDefault();
    }
  }

  // Safety: release all directions on window blur
  function onBlur() {
    for (const key of pressed) {
      controlSocket.keyup(key);
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
    }
    pressed.clear();
  };
}
