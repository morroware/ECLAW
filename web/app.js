/**
 * ECLAW App — Main UI orchestration.
 * Manages application state, connects components, handles UI transitions.
 */
(function () {
  "use strict";

  // -- State ----------------------------------------------------------------
  let token = null;
  let playerState = null; // null, 'waiting', 'ready', 'active', 'done'
  let controlSocket = null;
  let statusWs = null;
  let moveTimerInterval = null;
  let readyTimerInterval = null;
  let streamPlayer = null;

  // -- DOM Elements ---------------------------------------------------------
  const $ = (sel) => document.querySelector(sel);
  const joinPanel = $("#join-panel");
  const waitingPanel = $("#waiting-panel");
  const readyPanel = $("#ready-panel");
  const controlsPanel = $("#controls-panel");
  const resultPanel = $("#result-panel");
  const joinForm = $("#join-form");
  const joinError = $("#join-error");
  const connectionDot = $("#connection-status");
  const viewerCount = $("#viewer-count");
  const latencyDisplay = $("#latency-display");
  const queueLength = $("#queue-length");
  const currentPlayerDisplay = $("#current-player-display");
  const gameStateDisplay = $("#game-state-display");
  const timerDisplay = $("#timer-display");

  // -- Initialization -------------------------------------------------------

  // Try to restore session from localStorage
  const savedToken = localStorage.getItem("eclaw_token");
  if (savedToken) {
    token = savedToken;
    checkSession(token);
  }

  // Connect status WebSocket for all viewers
  connectStatusWs();

  // Connect video stream
  initStream();

  // -- Status WebSocket (all viewers) ---------------------------------------

  function connectStatusWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    statusWs = new WebSocket(`${proto}//${location.host}/ws/status`);

    statusWs.onopen = () => {
      connectionDot.className = "status-dot connected";
    };

    statusWs.onmessage = (event) => {
      const msg = JSON.parse(event.data);

      if (msg.type === "queue_update") {
        queueLength.textContent = `Queue: ${msg.queue_length}`;
        currentPlayerDisplay.textContent = msg.current_player
          ? `Playing: ${msg.current_player}`
          : "";
        if (msg.viewer_count != null) {
          viewerCount.textContent = `${msg.viewer_count} viewer${msg.viewer_count !== 1 ? "s" : ""}`;
        }
      }

      if (msg.type === "state_update") {
        gameStateDisplay.textContent = formatState(msg.state);
      }

      if (msg.type === "turn_end") {
        gameStateDisplay.textContent = `${msg.result.toUpperCase()}!`;
        setTimeout(() => {
          gameStateDisplay.textContent = "";
        }, 3000);
      }
    };

    statusWs.onclose = () => {
      connectionDot.className = "status-dot disconnected";
      setTimeout(connectStatusWs, 3000);
    };
  }

  // -- Video Stream ---------------------------------------------------------

  function initStream() {
    const video = $("#stream-video");
    streamPlayer = new StreamPlayer(video, "/stream/cam");
    streamPlayer.connect().catch((err) => {
      console.warn("Stream not available:", err.message);
      // Stream may not be available in dev mode — that's OK
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
        token = savedToken;
        switchToState(data.state, data);
      } else {
        // Token expired or invalid
        localStorage.removeItem("eclaw_token");
        token = null;
      }
    } catch (e) {
      console.error("Session check failed:", e);
    }
  }

  // -- Join Queue -----------------------------------------------------------

  joinForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    joinError.textContent = "";

    const name = $("#join-name").value.trim();
    const email = $("#join-email").value.trim();

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
      localStorage.setItem("eclaw_token", token);

      switchToState("waiting", {
        position: data.position,
        estimated_wait_seconds: data.estimated_wait_seconds,
      });

      // Connect control WebSocket
      connectControlWs();
    } catch (e) {
      joinError.textContent = "Network error. Please try again.";
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

  // -- Drop Buttons ---------------------------------------------------------

  $("#drop-btn-desktop").addEventListener("click", () => {
    if (controlSocket) controlSocket.drop();
  });

  $("#drop-btn-mobile").addEventListener("click", () => {
    if (controlSocket) controlSocket.drop();
  });

  // -- Play Again -----------------------------------------------------------

  $("#play-again-btn").addEventListener("click", () => {
    cleanup();
    switchToState(null);
  });

  // -- Control WebSocket ----------------------------------------------------

  function connectControlWs() {
    if (!token) return;
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
    };

    controlSocket.onConnect = () => {
      latencyDisplay.textContent = "";
    };

    controlSocket.connect();

    // Set up keyboard controls
    setupKeyboard(controlSocket);

    // Set up touch D-pad
    const dpad = $("#dpad");
    if (dpad) {
      new TouchDPad(dpad, controlSocket);
    }
  }

  // -- State Updates from Server --------------------------------------------

  function handleStateUpdate(msg) {
    const state = msg.state;

    if (state === "moving") {
      switchToState("active", msg);
      startMoveTimer(msg);
    } else if (state === "dropping" || state === "post_drop") {
      timerDisplay.textContent = "";
      clearInterval(moveTimerInterval);
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

  // -- Timer Management -----------------------------------------------------

  function startMoveTimer(msg) {
    clearInterval(moveTimerInterval);
    let secondsLeft = 30; // default
    if (msg.try_move_seconds) secondsLeft = msg.try_move_seconds;

    updateTimerDisplay(secondsLeft);
    moveTimerInterval = setInterval(() => {
      secondsLeft--;
      updateTimerDisplay(secondsLeft);
      if (secondsLeft <= 0) {
        clearInterval(moveTimerInterval);
      }
    }, 1000);
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
    let left = seconds || 15;
    const el = $("#ready-timer");
    el.textContent = `${left}s`;

    readyTimerInterval = setInterval(() => {
      left--;
      el.textContent = `${left}s`;
      if (left <= 0) {
        clearInterval(readyTimerInterval);
        el.textContent = "Time's up!";
      }
    }, 1000);
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
          $("#wait-position").textContent = data.position || "—";
          if (data.estimated_wait_seconds) {
            const mins = Math.ceil(data.estimated_wait_seconds / 60);
            $("#wait-time").textContent = `~${mins} min`;
          }
        }
        if (!controlSocket) connectControlWs();
        break;

      case "ready":
        readyPanel.classList.remove("hidden");
        if (data && data.timeout_seconds) {
          startReadyTimer(data.timeout_seconds);
        } else {
          startReadyTimer(15);
        }
        break;

      case "active":
        controlsPanel.classList.remove("hidden");
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

  function cleanup() {
    if (controlSocket) {
      controlSocket.disconnect();
      controlSocket = null;
    }
    localStorage.removeItem("eclaw_token");
    token = null;
    playerState = null;
    clearInterval(moveTimerInterval);
    clearInterval(readyTimerInterval);
  }

  // -- Latency Display Update -----------------------------------------------
  setInterval(() => {
    if (controlSocket && controlSocket.latencyMs) {
      latencyDisplay.textContent = `${Math.abs(controlSocket.latencyMs)}ms`;
    }
  }, 2000);

  // -- Periodic Queue Status Refresh ----------------------------------------
  setInterval(async () => {
    try {
      const res = await fetch("/api/queue/status");
      if (res.ok) {
        const data = await res.json();
        queueLength.textContent = `Queue: ${data.queue_length}`;
        currentPlayerDisplay.textContent = data.current_player
          ? `Playing: ${data.current_player}`
          : "";
      }
    } catch (e) {
      // Ignore
    }
  }, 5000);
})();
