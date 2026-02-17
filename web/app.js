/**
 * ECLAW App — Main UI orchestration.
 * Manages application state, connects components, handles UI transitions.
 */
(function () {
  "use strict";

  // -- State ----------------------------------------------------------------
  let token = null;
  let playerName = null; // Name used when joining — for matching in queue updates
  let playerState = null; // null, 'waiting', 'ready', 'active', 'done'
  let controlSocket = null;
  let statusWs = null;
  let moveTimerInterval = null;
  let readyTimerInterval = null;
  let streamPlayer = null;
  let _keyboardTeardown = null;
  let _dpadInstance = null;
  let _statusReconnectDelay = 3000;

  // -- DOM Elements ---------------------------------------------------------
  const $ = (sel) => document.querySelector(sel);
  const joinPanel = $("#join-panel");
  const waitingPanel = $("#waiting-panel");
  const readyPanel = $("#ready-panel");
  const controlsPanel = $("#controls-panel");
  const resultPanel = $("#result-panel");
  const joinForm = $("#join-form");
  const joinError = $("#join-error");
  const joinBtn = $("#join-btn");
  const connectionDot = $("#connection-status");
  const viewerCount = $("#viewer-count");
  const latencyDisplay = $("#latency-display");
  const queueLength = $("#queue-length");
  const currentPlayerDisplay = $("#current-player-display");
  const gameStateDisplay = $("#game-state-display");
  const timerDisplay = $("#timer-display");
  const queueList = $("#queue-list");
  const historyList = $("#history-list");

  // -- Initialization -------------------------------------------------------

  // Try to restore session from localStorage
  const savedToken = localStorage.getItem("eclaw_token");
  const savedName = localStorage.getItem("eclaw_name");
  if (savedToken) {
    token = savedToken;
    playerName = savedName;
    checkSession(token);
  }

  // Connect status WebSocket for all viewers
  connectStatusWs();

  // Connect video stream
  initStream();

  // Initial data fetches
  fetchQueueList();
  fetchHistory();

  // -- Status WebSocket (all viewers) ---------------------------------------

  function connectStatusWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    statusWs = new WebSocket(`${proto}//${location.host}/ws/status`);

    statusWs.onopen = () => {
      connectionDot.className = "status-dot connected";
      _statusReconnectDelay = 3000; // Reset backoff on successful connect
    };

    statusWs.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); }
      catch (e) { console.warn("Bad status WS message:", e); return; }

      if (msg.type === "queue_update") {
        queueLength.textContent = `Queue: ${msg.queue_length}`;
        currentPlayerDisplay.textContent = msg.current_player
          ? `Playing: ${msg.current_player}`
          : "";
        if (msg.viewer_count != null) {
          viewerCount.textContent = `${msg.viewer_count} viewer${msg.viewer_count !== 1 ? "s" : ""}`;
        }
        // Real-time queue list update from WebSocket
        if (msg.entries) {
          renderQueueList(msg.entries);

          // Update the waiting panel's position if the player is in the queue.
          // The entries are ordered: active/ready first, then waiting by position.
          if (playerState === "waiting" && playerName) {
            let waitingIndex = 0;
            for (const entry of msg.entries) {
              if (entry.state === "waiting") waitingIndex++;
              if (entry.name === playerName && entry.state === "waiting") {
                $("#wait-position").textContent = waitingIndex;
                break;
              }
            }
          }
        }
      }

      if (msg.type === "state_update") {
        gameStateDisplay.textContent = formatState(msg.state);
      }

      if (msg.type === "turn_end") {
        const result = (msg.result || "").toUpperCase();
        gameStateDisplay.textContent = `${result}!`;
        setTimeout(() => {
          gameStateDisplay.textContent = "";
        }, 3000);
        // Refresh history after a turn ends
        fetchHistory();
      }
    };

    statusWs.onclose = () => {
      connectionDot.className = "status-dot disconnected";
      setTimeout(connectStatusWs, _statusReconnectDelay);
      _statusReconnectDelay = Math.min(_statusReconnectDelay * 1.5, 30000);
    };

    statusWs.onerror = () => {
      // onclose will fire after this
    };
  }

  // -- Video Stream ---------------------------------------------------------

  function initStream() {
    const video = $("#stream-video");
    streamPlayer = new StreamPlayer(video, "/stream/cam");
    streamPlayer.connect().catch((err) => {
      console.warn("Stream not available:", err.message);
    });
  }

  // -- Session Check --------------------------------------------------------

  async function checkSession(savedToken) {
    try {
      const res = await fetch("/api/session/me", {
        headers: { Authorization: `Bearer ${savedToken}` },
      });
      if (res.ok) {
        const data = await res.json();
        // Terminal states — clear stale token and reset to join screen
        if (data.state === "done" || data.state === "cancelled") {
          localStorage.removeItem("eclaw_token");
          token = null;
          switchToState(null);
          return;
        }
        token = savedToken;
        switchToState(data.state, data);
      } else {
        localStorage.removeItem("eclaw_token");
        token = null;
      }
    } catch (e) {
      console.error("Session check failed:", e);
    }
  }

  // -- Queue List -----------------------------------------------------------

  async function fetchQueueList() {
    try {
      const res = await fetch("/api/queue");
      if (res.ok) {
        const data = await res.json();
        renderQueueList(data.entries);
        queueLength.textContent = `Queue: ${data.total}`;
        if (data.current_player) {
          currentPlayerDisplay.textContent = `Playing: ${data.current_player}`;
        }
      }
    } catch (e) {
      // Ignore — server may not be ready
    }
  }

  function renderQueueList(entries) {
    if (!entries || entries.length === 0) {
      queueList.innerHTML = '<li class="queue-empty">No one in the queue</li>';
      return;
    }

    queueList.innerHTML = entries
      .map((entry, i) => {
        let stateLabel = "";
        let stateClass = "";
        if (entry.state === "active") {
          stateLabel = "PLAYING";
          stateClass = "state-active";
        } else if (entry.state === "ready") {
          stateLabel = "READY";
          stateClass = "state-ready";
        } else {
          stateLabel = `#${i + 1}`;
          stateClass = "state-waiting";
        }

        return `<li class="queue-entry ${stateClass}">
          <span class="queue-name">${escapeHtml(entry.name)}</span>
          <span class="queue-state">${stateLabel}</span>
        </li>`;
      })
      .join("");
  }

  // -- Game History ---------------------------------------------------------

  async function fetchHistory() {
    try {
      const res = await fetch("/api/history");
      if (res.ok) {
        const data = await res.json();
        renderHistory(data.entries);
      }
    } catch (e) {
      // Ignore
    }
  }

  function renderHistory(entries) {
    if (!entries || entries.length === 0) {
      historyList.innerHTML = '<p class="text-dim">No games yet</p>';
      return;
    }

    historyList.innerHTML = entries
      .map((entry) => {
        const isWin = entry.result === "win";
        const resultLabel = isWin ? "WIN" : (entry.result || "").toUpperCase();
        const resultClass = isWin ? "result-win" : "result-loss";
        const tries = entry.tries_used != null ? `${entry.tries_used} tries` : "";
        const timeAgo = entry.completed_at ? formatTimeAgo(entry.completed_at) : "";

        return `<div class="history-entry">
          <span class="history-result ${resultClass}">${resultLabel}</span>
          <span class="history-name">${escapeHtml(entry.name)}</span>
          <span class="history-meta">${tries}${timeAgo ? " · " + timeAgo : ""}</span>
        </div>`;
      })
      .join("");
  }

  // -- Join Queue -----------------------------------------------------------

  joinForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    joinError.textContent = "";

    const name = $("#join-name").value.trim();
    const email = $("#join-email").value.trim();

    // Disable button to prevent double-submit
    joinBtn.disabled = true;
    joinBtn.textContent = "Joining...";

    try {
      const res = await fetch("/api/queue/join", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email }),
      });

      if (!res.ok) {
        const err = await res.json();
        joinError.textContent = err.detail || "Failed to join queue";
        return;
      }

      const data = await res.json();
      token = data.token;
      playerName = name;
      localStorage.setItem("eclaw_token", token);
      localStorage.setItem("eclaw_name", name);

      switchToState("waiting", {
        position: data.position,
        estimated_wait_seconds: data.estimated_wait_seconds,
      });
    } catch (e) {
      joinError.textContent = "Network error. Please try again.";
    } finally {
      joinBtn.disabled = false;
      joinBtn.textContent = "Join Queue";
    }
  });

  // -- Leave Queue ----------------------------------------------------------

  $("#leave-btn").addEventListener("click", async () => {
    if (!token) return;
    try {
      await fetch("/api/queue/leave", {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
    } catch (e) {
      // Ignore
    }
    cleanup();
    switchToState(null);
  });

  // -- Ready Confirm --------------------------------------------------------

  $("#ready-btn").addEventListener("click", () => {
    if (controlSocket) {
      controlSocket.readyConfirm();
    }
  });

  // -- Drop Buttons (single click to drop) ----------------------------------

  function setupDropButton(btn) {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      if (controlSocket) controlSocket.dropStart();
    });
    // Prevent mousedown from giving the button focus — if the button has
    // focus, a subsequent Space press fires both the keyboard handler AND the
    // button's default click action, causing a double-drop or missed drop.
    btn.addEventListener("mousedown", (e) => {
      e.preventDefault();
    });
    btn.style.touchAction = "none";
  }

  setupDropButton($("#drop-btn-desktop"));
  setupDropButton($("#drop-btn-mobile"));

  // -- Play Again -----------------------------------------------------------

  $("#play-again-btn").addEventListener("click", () => {
    cleanup();
    switchToState(null);
  });

  // -- Control WebSocket ----------------------------------------------------

  function connectControlWs() {
    if (!token) return;

    // Clean up previous keyboard/dpad listeners to prevent accumulation
    if (_keyboardTeardown) {
      _keyboardTeardown();
      _keyboardTeardown = null;
    }
    if (_dpadInstance) {
      _dpadInstance.destroy();
      _dpadInstance = null;
    }
    if (controlSocket) controlSocket.disconnect();

    controlSocket = new ControlSocket(token);

    controlSocket.onAuthOk = (msg) => {
      switchToState(msg.state, msg);
    };

    controlSocket.onStateChange = (msg) => {
      handleStateUpdate(msg);
    };

    controlSocket.onReadyPrompt = (msg) => {
      switchToState("ready", msg);
    };

    controlSocket.onTurnEnd = (msg) => {
      switchToState("done", msg);
    };

    controlSocket.onError = (msg) => {
      console.error("Control error:", msg.message);
      // Handle auth failures on reconnect
      if (msg.message === "Invalid token" || msg.message === "Auth required") {
        cleanup();
        switchToState(null);
        joinError.textContent = "Session expired. Please rejoin the queue.";
      }
    };

    controlSocket.onConnect = () => {
      latencyDisplay.textContent = "";
    };

    controlSocket.onDisconnect = () => {
      latencyDisplay.textContent = "Reconnecting...";
      latencyDisplay.style.color = "var(--danger)";
    };

    controlSocket.connect();

    // Set up keyboard controls (returns a teardown function)
    _keyboardTeardown = setupKeyboard(controlSocket);

    // Set up touch D-pad
    const dpad = $("#dpad");
    if (dpad) {
      _dpadInstance = new TouchDPad(dpad, controlSocket);
    }
  }

  // -- State Updates from Server --------------------------------------------

  function handleStateUpdate(msg) {
    const state = msg.state;

    if (state === "moving") {
      switchToState("active", msg);
      startMoveTimer(msg);
      // Re-enable controls after a drop cycle (next try)
      setControlsEnabled(true);
    } else if (state === "dropping") {
      timerDisplay.textContent = "DROPPING!";
      timerDisplay.style.color = "#f59e0b";
      $("#ctrl-timer").textContent = "DROPPING!";
      $("#ctrl-timer").style.color = "#f59e0b";
      clearInterval(moveTimerInterval);
      setControlsEnabled(false);
    } else if (state === "post_drop") {
      timerDisplay.textContent = "Checking...";
      timerDisplay.style.color = "#60a5fa";
      $("#ctrl-timer").textContent = "Checking...";
      $("#ctrl-timer").style.color = "#60a5fa";
      clearInterval(moveTimerInterval);
      setControlsEnabled(false);
    } else if (state === "ready_prompt") {
      switchToState("ready", msg);
    } else if (state === "idle") {
      // Turn ended, show result if we were playing
      if (playerState === "active" || playerState === "ready") {
        switchToState("done", { result: "expired" });
      }
    }

    // Update try counter
    if (msg.current_try && msg.max_tries) {
      $("#ctrl-try").textContent = `Try ${msg.current_try}/${msg.max_tries}`;
      $("#try-display").textContent = `Try ${msg.current_try}/${msg.max_tries}`;
    }
  }

  function setControlsEnabled(enabled) {
    const dpad = $("#dpad");
    const dropDesktop = $("#drop-btn-desktop");
    const dropMobile = $("#drop-btn-mobile");
    const hint = $("#keyboard-hint");

    if (enabled) {
      if (dpad) dpad.classList.remove("disabled");
      if (dropDesktop) { dropDesktop.disabled = false; dropDesktop.textContent = "DROP (Space)"; }
      if (dropMobile) { dropMobile.disabled = false; dropMobile.textContent = "DROP"; }
      if (hint) hint.classList.remove("disabled");
    } else {
      if (dpad) dpad.classList.add("disabled");
      if (dropDesktop) { dropDesktop.disabled = true; dropDesktop.textContent = "DROPPING..."; }
      if (dropMobile) { dropMobile.disabled = true; dropMobile.textContent = "DROPPING..."; }
      if (hint) hint.classList.add("disabled");
    }
  }

  // -- Timer Management -----------------------------------------------------

  function startMoveTimer(msg) {
    clearInterval(moveTimerInterval);
    // Prefer server-provided remaining time (SSOT) over full duration.
    // state_seconds_left is the actual time remaining on the server's timer,
    // critical for correct display after WebSocket reconnection.
    let secondsLeft = (msg.state_seconds_left != null && msg.state_seconds_left > 0)
      ? msg.state_seconds_left
      : (msg.try_move_seconds || 30);

    // Use deadline-based approach to prevent drift from setInterval inaccuracy
    const endTime = Date.now() + secondsLeft * 1000;

    function tick() {
      const left = Math.max(0, Math.ceil((endTime - Date.now()) / 1000));
      updateTimerDisplay(left);
      if (left <= 0) {
        clearInterval(moveTimerInterval);
      }
    }

    tick();
    moveTimerInterval = setInterval(tick, 250);
  }

  function updateTimerDisplay(seconds) {
    const display = seconds > 0 ? `${seconds}s` : "0s";
    timerDisplay.textContent = display;
    $("#ctrl-timer").textContent = display;

    // Color coding
    if (seconds <= 5) {
      timerDisplay.style.color = "#ef4444";
      $("#ctrl-timer").style.color = "#ef4444";
    } else if (seconds <= 10) {
      timerDisplay.style.color = "#f59e0b";
      $("#ctrl-timer").style.color = "#f59e0b";
    } else {
      timerDisplay.style.color = "";
      $("#ctrl-timer").style.color = "";
    }
  }

  function startReadyTimer(seconds) {
    clearInterval(readyTimerInterval);
    const endTime = Date.now() + (seconds || 15) * 1000;
    const el = $("#ready-timer");

    function tick() {
      const left = Math.max(0, Math.ceil((endTime - Date.now()) / 1000));
      if (left <= 0) {
        clearInterval(readyTimerInterval);
        el.textContent = "Time's up!";
      } else {
        el.textContent = `${left}s`;
      }
    }

    tick();
    readyTimerInterval = setInterval(tick, 250);
  }

  // -- UI State Switching ---------------------------------------------------

  function switchToState(newState, data) {
    playerState = newState;

    // Hide all panels
    joinPanel.classList.add("hidden");
    waitingPanel.classList.add("hidden");
    readyPanel.classList.add("hidden");
    controlsPanel.classList.add("hidden");
    resultPanel.classList.add("hidden");

    clearInterval(moveTimerInterval);
    clearInterval(readyTimerInterval);
    timerDisplay.textContent = "";

    switch (newState) {
      case null:
        joinPanel.classList.remove("hidden");
        break;

      case "waiting":
        waitingPanel.classList.remove("hidden");
        if (data) {
          $("#wait-position").textContent = data.position || "--";
          if (data.estimated_wait_seconds) {
            const mins = Math.ceil(data.estimated_wait_seconds / 60);
            $("#wait-time").textContent = `~${mins} min`;
          }
        }
        if (!controlSocket) connectControlWs();
        break;

      case "ready":
        readyPanel.classList.remove("hidden");
        if (data && data.state_seconds_left != null && data.state_seconds_left > 0) {
          // SSOT: use server-provided remaining time (accurate on reconnect)
          startReadyTimer(data.state_seconds_left);
        } else if (data && data.timeout_seconds) {
          startReadyTimer(data.timeout_seconds);
        } else {
          startReadyTimer(15);
        }
        if (!controlSocket) connectControlWs();
        break;

      case "active":
        controlsPanel.classList.remove("hidden");
        // Remove focus from any element (e.g. the Ready button) so that
        // Space keydown goes to the document-level keyboard handler
        // instead of re-triggering a focused button.
        if (document.activeElement && document.activeElement !== document.body) {
          document.activeElement.blur();
        }
        if (!controlSocket) connectControlWs();
        break;

      case "done":
        resultPanel.classList.remove("hidden");
        if (data) {
          const result = data.result || "unknown";
          const title = $("#result-title");
          const message = $("#result-message");

          if (result === "win") {
            title.textContent = "YOU WON!";
            title.className = "win";
            message.textContent = "Congratulations! You grabbed a prize!";
          } else if (result === "loss") {
            title.textContent = "No Luck";
            title.className = "loss";
            message.textContent = "Better luck next time!";
          } else {
            title.textContent = "Turn Over";
            title.className = "";
            message.textContent = `Result: ${result}`;
          }
        }
        break;

      default:
        joinPanel.classList.remove("hidden");
    }
  }

  // -- Helpers --------------------------------------------------------------

  function formatState(state) {
    const map = {
      idle: "Waiting for Player",
      ready_prompt: "Player Ready?",
      moving: "PLAYING",
      dropping: "DROPPING!",
      post_drop: "Checking...",
      turn_end: "Turn Over",
    };
    return map[state] || state;
  }

  function formatTimeAgo(isoStr) {
    if (!isoStr) return "";
    const now = Date.now();
    const then = new Date(isoStr + (isoStr.endsWith("Z") ? "" : "Z")).getTime();
    const diffS = Math.floor((now - then) / 1000);

    if (diffS < 0) return "just now";
    if (diffS < 60) return "just now";
    if (diffS < 3600) return `${Math.floor(diffS / 60)}m ago`;
    if (diffS < 86400) return `${Math.floor(diffS / 3600)}h ago`;
    return `${Math.floor(diffS / 86400)}d ago`;
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function cleanup() {
    if (_keyboardTeardown) {
      _keyboardTeardown();
      _keyboardTeardown = null;
    }
    if (_dpadInstance) {
      _dpadInstance.destroy();
      _dpadInstance = null;
    }
    if (controlSocket) {
      controlSocket.disconnect();
      controlSocket = null;
    }
    localStorage.removeItem("eclaw_token");
    localStorage.removeItem("eclaw_name");
    token = null;
    playerName = null;
    playerState = null;
    latencyDisplay.textContent = "";
    latencyDisplay.style.color = "";
    clearInterval(moveTimerInterval);
    clearInterval(readyTimerInterval);
  }

  // -- Latency Display Update -----------------------------------------------
  setInterval(() => {
    if (controlSocket && controlSocket.latencyMs) {
      latencyDisplay.textContent = `${Math.abs(controlSocket.latencyMs)}ms`;
      latencyDisplay.style.color = "";
    }
  }, 2000);

  // -- Periodic History Refresh ---------------------------------------------
  // Queue updates are handled in real-time by the status WebSocket.
  // Only history needs a periodic fallback since it's not pushed on every change.
  // Pause polling when tab is hidden to reduce unnecessary requests.
  let _historyInterval = setInterval(fetchHistory, 30000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearInterval(_historyInterval);
    } else {
      fetchHistory();
      _historyInterval = setInterval(fetchHistory, 30000);
    }
  });
})();
