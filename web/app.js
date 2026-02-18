/**
 * ECLAW App — Main UI orchestration for The Castle Fun Center.
 * Manages application state, connects components, handles UI transitions,
 * sound effects, visual feedback, confetti celebrations, screen effects,
 * current player HUD, and auto-refresh on timeout.
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
  let _moveEndTime = 0; // For timer bar percentage
  let _moveStartTime = 0;
  let _lastWarningBeep = 0; // Prevent double-beeps

  // -- Sound Engine ---------------------------------------------------------
  const sfx = new SoundEngine();

  // Restore mute preference
  if (localStorage.getItem("eclaw_muted") === "1") {
    sfx.muted = true;
  }

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
  const soundToggle = $("#sound-toggle");
  const timerBar = $("#timer-bar");
  const streamContainer = $("#stream-container");
  const dropBtn = $("#drop-btn");
  const screenFlash = $("#screen-flash");
  const currentPlayerHud = $("#current-player-hud");
  const playerHudName = $("#player-hud-name");

  // -- Sound Toggle ---------------------------------------------------------
  function updateSoundIcon() {
    if (!soundToggle) return;
    soundToggle.textContent = sfx.muted ? "\u{1F507}" : "\u{1F50A}";
    soundToggle.classList.toggle("muted", sfx.muted);
  }
  updateSoundIcon();

  if (soundToggle) {
    soundToggle.addEventListener("click", () => {
      sfx.unlock();
      sfx.toggleMute();
      localStorage.setItem("eclaw_muted", sfx.muted ? "1" : "0");
      updateSoundIcon();
    });
  }

  // Unlock audio on any user interaction (required by browsers)
  function unlockAudio() {
    sfx.unlock();
    document.removeEventListener("click", unlockAudio);
    document.removeEventListener("touchstart", unlockAudio);
    document.removeEventListener("keydown", unlockAudio);
  }
  document.addEventListener("click", unlockAudio);
  document.addEventListener("touchstart", unlockAudio);
  document.addEventListener("keydown", unlockAudio);

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

        // Update the current player HUD on video
        updateCurrentPlayerHud(msg.current_player);

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

  // -- Current Player HUD (visible to all spectators) -----------------------

  function updateCurrentPlayerHud(name) {
    if (!currentPlayerHud || !playerHudName) return;

    if (name) {
      playerHudName.textContent = name;
      currentPlayerHud.classList.remove("hidden");
    } else {
      currentPlayerHud.classList.add("hidden");
    }
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
          updateCurrentPlayerHud(data.current_player);
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
          <span class="history-meta">${tries}${timeAgo ? " \u00B7 " + timeAgo : ""}</span>
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
    joinBtn.querySelector(".btn-text").textContent = "JOINING...";

    try {
      const res = await fetch("/api/queue/join", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email }),
      });

      if (!res.ok) {
        const err = await res.json();
        joinError.textContent = err.detail || "Failed to join queue";
        joinPanel.classList.add("shake");
        setTimeout(() => joinPanel.classList.remove("shake"), 400);
        return;
      }

      const data = await res.json();
      token = data.token;
      playerName = name;
      localStorage.setItem("eclaw_token", token);
      localStorage.setItem("eclaw_name", name);

      sfx.playJoinQueue();

      switchToState("waiting", {
        position: data.position,
        estimated_wait_seconds: data.estimated_wait_seconds,
      });
    } catch (e) {
      joinError.textContent = "Network error. Please try again.";
      joinPanel.classList.add("shake");
      setTimeout(() => joinPanel.classList.remove("shake"), 400);
    } finally {
      joinBtn.disabled = false;
      joinBtn.querySelector(".btn-text").textContent = "JOIN QUEUE";
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
      sfx.playReadyConfirm();
    }
  });

  // -- Drop Button (unified — single button for desktop + mobile) ----------

  function setupDropButton(btn) {
    if (!btn) return;
    let _dropActive = false;

    function startDrop(e) {
      e.preventDefault();
      if (_dropActive) return;
      _dropActive = true;
      if (controlSocket) {
        controlSocket.dropStart();
        sfx.playDrop();
        vibrate(50);
      }
      btn.classList.add("active");
    }

    function endDrop(e) {
      if (e) e.preventDefault();
      if (!_dropActive) return;
      _dropActive = false;
      if (controlSocket) {
        controlSocket.dropEnd();
      }
      btn.classList.remove("active");
    }

    // Mouse events
    btn.addEventListener("mousedown", startDrop);
    btn.addEventListener("mouseup", endDrop);
    btn.addEventListener("mouseleave", () => { if (_dropActive) endDrop(); });

    // Touch events
    btn.addEventListener("touchstart", startDrop);
    btn.addEventListener("touchend", endDrop);
    btn.addEventListener("touchcancel", endDrop);

    btn.style.touchAction = "none";
  }

  setupDropButton(dropBtn);

  // D-Pad input is handled entirely by TouchDPad (pointer events).
  // TouchDPad requires pointer-down before processing movement,
  // so it works correctly as click-and-hold on both desktop and mobile.

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
    _keyboardTeardown = setupKeyboard(controlSocket, sfx);

    // Set up touch D-pad on the unified dpad element
    const dpad = $("#dpad");
    if (dpad) {
      _dpadInstance = new TouchDPad(dpad, controlSocket, sfx);
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
      // Sound for new try (after first)
      if (msg.current_try && msg.current_try > 1) {
        sfx.playNextTry();
      }
    } else if (state === "dropping") {
      timerDisplay.textContent = "DROPPING!";
      timerDisplay.style.color = "#f59e0b";
      $("#ctrl-timer").textContent = "DROPPING!";
      $("#ctrl-timer").style.color = "#f59e0b";
      clearInterval(moveTimerInterval);
      setControlsEnabled(false);
      updateTimerBar(0);
      sfx.playDropping();
    } else if (state === "post_drop") {
      timerDisplay.textContent = "Checking...";
      timerDisplay.style.color = "#60a5fa";
      $("#ctrl-timer").textContent = "Checking...";
      $("#ctrl-timer").style.color = "#60a5fa";
      clearInterval(moveTimerInterval);
      setControlsEnabled(false);
      updateTimerBar(0);
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

    if (enabled) {
      if (dpad) dpad.classList.remove("disabled");
      if (dropBtn) { dropBtn.disabled = false; dropBtn.innerHTML = '<span class="drop-btn-icon">&#9660;</span>DROP<span class="key-hint">SPACE</span>'; }
    } else {
      if (dpad) dpad.classList.add("disabled");
      if (dropBtn) { dropBtn.disabled = true; dropBtn.innerHTML = "DROPPING\u2026"; }
    }
  }

  // -- Timer Management -----------------------------------------------------

  function startMoveTimer(msg) {
    clearInterval(moveTimerInterval);
    _lastWarningBeep = 0;

    // Prefer server-provided remaining time (SSOT) over full duration.
    // state_seconds_left is the actual time remaining on the server's timer,
    // critical for correct display after WebSocket reconnection.
    let secondsLeft = (msg.state_seconds_left != null && msg.state_seconds_left > 0)
      ? msg.state_seconds_left
      : (msg.try_move_seconds || 30);

    // Use deadline-based approach to prevent drift from setInterval inaccuracy
    const endTime = Date.now() + secondsLeft * 1000;
    _moveEndTime = endTime;
    _moveStartTime = Date.now();

    function tick() {
      const now = Date.now();
      const msLeft = Math.max(0, endTime - now);
      const left = Math.ceil(msLeft / 1000);
      updateTimerDisplay(left);

      // Timer progress bar
      const totalMs = _moveEndTime - _moveStartTime;
      const pct = totalMs > 0 ? (msLeft / totalMs) * 100 : 0;
      updateTimerBar(pct, left);

      // Timer warning beeps for the last 5 seconds
      if (left <= 5 && left > 0 && left !== _lastWarningBeep) {
        _lastWarningBeep = left;
        sfx.playTimerWarning(left);
        vibrate(30);
      }

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

  function updateTimerBar(pct, seconds) {
    if (!timerBar) return;
    timerBar.style.width = Math.max(0, Math.min(100, pct)) + "%";
    timerBar.classList.remove("danger", "warning");
    if (seconds != null) {
      if (seconds <= 5) timerBar.classList.add("danger");
      else if (seconds <= 10) timerBar.classList.add("warning");
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
        // Auto-refresh: player didn't press Ready in time
        autoRefreshOnTimeout();
      } else {
        el.textContent = `${left}s`;
      }
    }

    tick();
    readyTimerInterval = setInterval(tick, 250);
  }

  // -- Auto-Refresh on Timeout ----------------------------------------------

  function autoRefreshOnTimeout() {
    // Clean up the session since the player timed out
    cleanup();
    // Brief delay so the user sees "Time's up!" before reload
    setTimeout(() => {
      location.reload();
    }, 2000);
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
    updateTimerBar(0);
    if (streamContainer) streamContainer.classList.remove("playing");

    // Reset result panel classes
    resultPanel.classList.remove("result-win", "result-loss");

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
        sfx.playYourTurn();
        vibrate([100, 50, 100]);
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
        if (streamContainer) streamContainer.classList.add("playing");
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
          const icon = $("#result-icon");
          const glow = $("#result-glow");

          // Reset classes
          icon.className = "result-icon";
          glow.className = "card-glow";

          if (result === "win") {
            icon.textContent = "\u{1F3C6}";
            icon.classList.add("win-icon");
            title.textContent = "YOU WON!";
            title.className = "win";
            glow.classList.add("win-glow");
            resultPanel.classList.add("result-win");
            message.textContent = "Congratulations! You grabbed a prize!";
            sfx.playWin();
            vibrate([100, 50, 100, 50, 200]);
            triggerScreenFlash("win");
            spawnConfetti();
            // Second burst of confetti after a short delay
            setTimeout(() => spawnConfetti(), 800);
          } else if (result === "loss") {
            icon.textContent = "\u{1F61E}";
            icon.classList.add("loss-icon");
            title.textContent = "No Luck";
            title.className = "loss";
            glow.classList.add("loss-glow");
            resultPanel.classList.add("result-loss");
            message.textContent = "Better luck next time!";
            sfx.playLoss();
            triggerScreenFlash("loss");
          } else if (result === "expired") {
            icon.textContent = "\u{231B}";
            icon.classList.add("loss-icon");
            title.textContent = "Time's Up";
            title.className = "loss";
            glow.classList.add("loss-glow");
            resultPanel.classList.add("result-loss");
            message.textContent = "Your turn has ended.";
            sfx.playLoss();
          } else {
            icon.textContent = "\u{1F3AE}";
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

  // -- Screen Flash Effect --------------------------------------------------

  function triggerScreenFlash(type) {
    if (!screenFlash) return;
    // Remove any existing classes
    screenFlash.className = "";
    // Force reflow to restart animation
    void screenFlash.offsetWidth;
    screenFlash.classList.add(type === "win" ? "flash-win" : "flash-loss");
    // Clean up after animation
    setTimeout(() => {
      screenFlash.className = "";
    }, 1000);
  }

  // -- Enhanced Confetti Effect ---------------------------------------------

  function spawnConfetti() {
    const container = $("#confetti-container");
    if (!container) return;

    const colors = [
      "#f59e0b", "#7c3aed", "#10b981", "#ef4444",
      "#3b82f6", "#ec4899", "#06b6d4", "#f97316",
      "#fbbf24", "#a78bfa",
    ];
    const shapes = ["confetti-rect", "confetti-circle", "confetti-strip", "confetti-star"];
    const animations = ["confettiFall", "confettiSway"];
    const count = 60;

    for (let i = 0; i < count; i++) {
      const piece = document.createElement("div");
      const shape = shapes[Math.floor(Math.random() * shapes.length)];
      const anim = animations[Math.floor(Math.random() * animations.length)];

      piece.className = `confetti-piece ${shape}`;
      piece.style.left = Math.random() * 100 + "%";
      piece.style.background = colors[Math.floor(Math.random() * colors.length)];
      piece.style.animationName = anim;
      piece.style.animationDelay = (Math.random() * 1.5) + "s";
      piece.style.animationDuration = (2 + Math.random() * 2) + "s";
      piece.style.opacity = (0.7 + Math.random() * 0.3).toFixed(2);

      container.appendChild(piece);
    }

    // Clean up after animation
    setTimeout(() => {
      container.innerHTML = "";
    }, 5000);
  }

  // -- Haptic Feedback ------------------------------------------------------

  function vibrate(pattern) {
    if (navigator.vibrate) {
      try { navigator.vibrate(pattern); } catch (e) { /* ignore */ }
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
