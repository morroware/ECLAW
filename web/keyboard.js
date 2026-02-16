/**
 * Desktop Keyboard Controls — WASD + Arrow Keys for movement, Space to drop (hold).
 */
function setupKeyboard(controlSocket) {
  const KEY_MAP = {
    ArrowUp: "north", KeyW: "north",
    ArrowDown: "south", KeyS: "south",
    ArrowLeft: "west", KeyA: "west",
    ArrowRight: "east", KeyD: "east",
  };
  const pressed = new Set();

  document.addEventListener("keydown", (e) => {
    // Don't capture when typing in inputs
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;

    const dir = KEY_MAP[e.code];
    if (dir && !pressed.has(dir)) {
      pressed.add(dir);
      controlSocket.keydown(dir);
      e.preventDefault();
    }
    if (e.code === "Space" && !pressed.has("drop")) {
      pressed.add("drop");
      controlSocket.dropStart();
      e.preventDefault();
    }
  });

  document.addEventListener("keyup", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;

    const dir = KEY_MAP[e.code];
    if (dir && pressed.has(dir)) {
      pressed.delete(dir);
      controlSocket.keyup(dir);
      e.preventDefault();
    }
    if (e.code === "Space" && pressed.has("drop")) {
      pressed.delete("drop");
      controlSocket.dropStop();
      e.preventDefault();
    }
  });

  // Safety: release all on window blur
  window.addEventListener("blur", () => {
    for (const key of pressed) {
      if (key === "drop") {
        controlSocket.dropStop();
      } else {
        controlSocket.keyup(key);
      }
    }
    pressed.clear();
  });
}
