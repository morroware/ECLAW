/**
 * ECLAW Interactive Embed — Full play experience for iframe embedding.
 * Adapted from app.js with iframe-specific changes:
 * - Uses sessionStorage instead of localStorage (cross-origin iframe safety)
 * - Resets inline instead of location.reload()
 * - Sends postMessage events to parent frame
 * - Accepts postMessage commands from parent frame
 * - Query parameter customization (theme, sounds, accent color)
 */
(function () {
  "use strict";

  // -- Query Parameters ----------------------------------------------------
  var params = new URLSearchParams(location.search);

  if (params.get("theme") === "light") {
    document.documentElement.classList.add("theme-light");
  }
  if (params.get("accent")) {
    document.documentElement.style.setProperty("--primary", "#" + params.get("accent"));
    document.documentElement.style.setProperty("--primary-bright", "#" + params.get("accent"));
  }
  if (params.get("bg")) {
    document.documentElement.style.setProperty("--bg", "#" + params.get("bg"));
  }

  // -- State ---------------------------------------------------------------
  var token = null;
  var playerName = null;
  var playerState = null;
  var controlSocket = null;
  var statusWs = null;
  var moveTimerInterval = null;
  var readyTimerInterval = null;
  var streamPlayer = null;
  var _keyboardTeardown = null;
  var _dpadInstance = null;
  var _statusReconnectDelay = 3000;
  var _moveEndTime = 0;
  var _moveStartTime = 0;
  var _lastWarningBeep = 0;

  // -- Sound Engine --------------------------------------------------------
  var sfx = new SoundEngine();

  if (params.get("sounds") === "0") {
    sfx.muted = true;
  } else if (sessionStorage.getItem("eclaw_embed_muted") === "1") {
    sfx.muted = true;
  }

  // Unlock audio on user interaction
  function unlockAudio() {
    sfx.unlock();
    document.removeEventListener("click", unlockAudio);
    document.removeEventListener("touchstart", unlockAudio);
    document.removeEventListener("keydown", unlockAudio);
  }
  document.addEventListener("click", unlockAudio);
  document.addEventListener("touchstart", unlockAudio);
  document.addEventListener("keydown", unlockAudio);

  // -- DOM Elements --------------------------------------------------------
  var $ = function (sel) { return document.querySelector(sel); };
  var joinPanel = $("#join-panel");
  var waitingPanel = $("#waiting-panel");
  var readyPanel = $("#ready-panel");
  var controlsPanel = $("#controls-panel");
  var resultPanel = $("#result-panel");
  var joinForm = $("#join-form");
  var joinError = $("#join-error");
  var joinBtn = $("#join-btn");
  var queueLength = $("#queue-length");
  var currentPlayerDisplay = $("#current-player-display");
  var gameStateDisplay = $("#game-state-display");
  var timerDisplay = $("#timer-display");
  var timerBar = $("#timer-bar");
  var streamContainer = $("#stream-container");
  var dropBtn = $("#drop-btn");
  var screenFlash = $("#screen-flash");
  var currentPlayerHud = $("#current-player-hud");
  var playerHudName = $("#player-hud-name");
  var streamReconnectBtn = $("#stream-reconnect");

  // -- Initialization ------------------------------------------------------

  // Try to restore session from sessionStorage
  var savedToken = sessionStorage.getItem("eclaw_embed_token");
  var savedName = sessionStorage.getItem("eclaw_embed_name");
  if (savedToken) {
    token = savedToken;
    playerName = savedName;
    checkSession(savedToken);
  }

  connectStatusWs();
  initStream();

  // -- Page Lifecycle (bfcache) --------------------------------------------
  window.addEventListener("pagehide", function () {
    if (streamPlayer) streamPlayer.disconnect();
    if (statusWs) { statusWs.close(); statusWs = null; }
  });

  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      initStream();
      connectStatusWs();
    }
  });

  // -- Status WebSocket (all viewers) --------------------------------------

  function connectStatusWs() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    statusWs = new WebSocket(proto + "//" + location.host + "/ws/status");

    statusWs.onopen = function () {
      _statusReconnectDelay = 3000;
    };

    statusWs.onmessage = function (event) {
      var msg;
      try { msg = JSON.parse(event.data); }
      catch (e) { return; }

      if (msg.type === "queue_update") {
        queueLength.textContent = "Queue: " + msg.queue_length;
        currentPlayerDisplay.textContent = msg.current_player
          ? "Playing: " + msg.current_player : "";

        updateCurrentPlayerHud(msg.current_player);

        if (msg.viewer_count != null) {
          notifyParent("queue_update", {
            queue_length: msg.queue_length,
            viewer_count: msg.viewer_count
          });
        }

        // Update waiting panel position
        if (msg.entries && playerState === "waiting" && playerName) {
          var waitingIndex = 0;
          for (var i = 0; i < msg.entries.length; i++) {
            if (msg.entries[i].state === "waiting") waitingIndex++;
            if (msg.entries[i].name === playerName && msg.entries[i].state === "waiting") {
              $("#wait-position").textContent = waitingIndex;
              break;
            }
          }
        }
      }

      if (msg.type === "state_update") {
        gameStateDisplay.textContent = formatState(msg.state);
      }

      if (msg.type === "turn_end") {
        var result = (msg.result || "").toUpperCase();
        gameStateDisplay.textContent = result + "!";
        setTimeout(function () { gameStateDisplay.textContent = ""; }, 3000);
      }
    };

    statusWs.onclose = function () {
      setTimeout(connectStatusWs, _statusReconnectDelay);
      _statusReconnectDelay = Math.min(_statusReconnectDelay * 1.5, 30000);
    };

    statusWs.onerror = function () {};
  }

  // -- Current Player HUD --------------------------------------------------

  function updateCurrentPlayerHud(name) {
    if (!currentPlayerHud || !playerHudName) return;
    if (name) {
      playerHudName.textContent = name;
      currentPlayerHud.classList.remove("hidden");
    } else {
      currentPlayerHud.classList.add("hidden");
    }
  }

  // -- Video Stream --------------------------------------------------------

  function initStream() {
    var video = $("#stream-video");
    streamPlayer = new StreamPlayer(video, "/stream/cam");

    streamPlayer.onStatusChange = function (status) {
      if (!streamReconnectBtn) return;
      if (status === "reconnecting") {
        streamReconnectBtn.classList.remove("hidden");
      } else if (status === "playing") {
        streamReconnectBtn.classList.add("hidden");
      }
    };

    streamPlayer.connect().catch(function (err) {
      console.warn("Stream not available:", err.message);
    });
  }

  if (streamReconnectBtn) {
    streamReconnectBtn.addEventListener("click", function () {
      if (streamPlayer) {
        streamReconnectBtn.classList.add("hidden");
        streamPlayer.reconnect();
      }
    });
  }

  // -- Session Check -------------------------------------------------------

  function checkSession(savedToken) {
    fetch("/api/session/me", {
      headers: { Authorization: "Bearer " + savedToken }
    }).then(function (res) {
      if (res.ok) return res.json();
      sessionStorage.removeItem("eclaw_embed_token");
      token = null;
      return null;
    }).then(function (data) {
      if (!data) return;
      if (data.state === "done" || data.state === "cancelled") {
        sessionStorage.removeItem("eclaw_embed_token");
        token = null;
        switchToState(null);
        return;
      }
      token = savedToken;
      switchToState(data.state, data);
    }).catch(function () {});
  }

  // -- Join Queue ----------------------------------------------------------

  joinForm.addEventListener("submit", function (e) {
    e.preventDefault();
    joinError.textContent = "";

    var name = $("#join-name").value.trim();
    var email = $("#join-email").value.trim();

    joinBtn.disabled = true;
    joinBtn.querySelector(".btn-text").textContent = "JOINING...";

    fetch("/api/queue/join", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, email: email })
    }).then(function (res) {
      if (!res.ok) {
        return res.json().then(function (err) {
          joinError.textContent = err.detail || "Failed to join queue";
          joinPanel.classList.add("shake");
          setTimeout(function () { joinPanel.classList.remove("shake"); }, 400);
          throw new Error("join_failed");
        });
      }
      return res.json();
    }).then(function (data) {
      token = data.token;
      playerName = name;
      sessionStorage.setItem("eclaw_embed_token", token);
      sessionStorage.setItem("eclaw_embed_name", name);

      sfx.playJoinQueue();
      notifyParent("joined", { position: data.position });

      switchToState("waiting", {
        position: data.position,
        estimated_wait_seconds: data.estimated_wait_seconds
      });
    }).catch(function (e) {
      if (e.message !== "join_failed") {
        joinError.textContent = "Network error. Please try again.";
        joinPanel.classList.add("shake");
        setTimeout(function () { joinPanel.classList.remove("shake"); }, 400);
      }
    }).finally(function () {
      joinBtn.disabled = false;
      joinBtn.querySelector(".btn-text").textContent = "JOIN QUEUE";
    });
  });

  // -- Leave Queue ---------------------------------------------------------

  $("#leave-btn").addEventListener("click", function () {
    if (!token) return;
    fetch("/api/queue/leave", {
      method: "DELETE",
      headers: { Authorization: "Bearer " + token }
    }).catch(function () {});
    cleanup();
    switchToState(null);
  });

  // -- Ready Confirm -------------------------------------------------------

  $("#ready-btn").addEventListener("click", function () {
    if (controlSocket) {
      controlSocket.readyConfirm();
      sfx.playReadyConfirm();
    }
  });

  // -- Drop Button ---------------------------------------------------------

  function setupDropButton(btn) {
    if (!btn) return;
    var _dropActive = false;

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
      if (controlSocket) controlSocket.dropEnd();
      btn.classList.remove("active");
    }

    btn.addEventListener("mousedown", startDrop);
    btn.addEventListener("mouseup", endDrop);
    btn.addEventListener("mouseleave", function () { if (_dropActive) endDrop(); });
    btn.addEventListener("touchstart", startDrop);
    btn.addEventListener("touchend", endDrop);
    btn.addEventListener("touchcancel", endDrop);
    btn.style.touchAction = "none";
  }

  setupDropButton(dropBtn);

  // -- Play Again ----------------------------------------------------------

  $("#play-again-btn").addEventListener("click", function () {
    cleanup();
    switchToState(null);
  });

  // -- Control WebSocket ---------------------------------------------------

  function connectControlWs() {
    if (!token) return;

    if (_keyboardTeardown) { _keyboardTeardown(); _keyboardTeardown = null; }
    if (_dpadInstance) { _dpadInstance.destroy(); _dpadInstance = null; }
    if (controlSocket) controlSocket.disconnect();

    controlSocket = new ControlSocket(token);

    controlSocket.onAuthOk = function (msg) {
      switchToState(msg.state, msg);
    };

    controlSocket.onStateChange = function (msg) {
      handleStateUpdate(msg);
    };

    controlSocket.onReadyPrompt = function (msg) {
      switchToState("ready", msg);
    };

    controlSocket.onTurnEnd = function (msg) {
      switchToState("done", msg);
      notifyParent("turn_end", { result: msg.result });
    };

    controlSocket.onError = function (msg) {
      if (msg.message === "Invalid token" || msg.message === "Auth required") {
        cleanup();
        switchToState(null);
        joinError.textContent = "Session expired. Please rejoin the queue.";
      }
    };

    controlSocket.connect();

    _keyboardTeardown = setupKeyboard(controlSocket, sfx);

    var dpad = $("#dpad");
    if (dpad) {
      _dpadInstance = new TouchDPad(dpad, controlSocket, sfx);
    }
  }

  // -- State Updates from Server -------------------------------------------

  function handleStateUpdate(msg) {
    var state = msg.state;

    if (state === "moving") {
      switchToState("active", msg);
      startMoveTimer(msg);
      setControlsEnabled(true);
      notifyParent("playing", {});
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
      if (playerState === "active" || playerState === "ready") {
        switchToState("done", { result: "expired" });
      }
    }

    if (msg.current_try && msg.max_tries) {
      $("#ctrl-try").textContent = "Try " + msg.current_try + "/" + msg.max_tries;
      $("#try-display").textContent = "Try " + msg.current_try + "/" + msg.max_tries;
    }
  }

  function setControlsEnabled(enabled) {
    var dpad = $("#dpad");
    if (enabled) {
      if (dpad) dpad.classList.remove("disabled");
      if (dropBtn) {
        dropBtn.disabled = false;
        dropBtn.innerHTML = '<span class="drop-btn-icon">&#9660;</span>DROP<span class="key-hint">SPACE</span>';
      }
    } else {
      if (dpad) dpad.classList.add("disabled");
      if (dropBtn) { dropBtn.disabled = true; dropBtn.innerHTML = "DROPPING\u2026"; }
    }
  }

  // -- Timer Management ----------------------------------------------------

  function startMoveTimer(msg) {
    clearInterval(moveTimerInterval);
    _lastWarningBeep = 0;

    var secondsLeft = (msg.state_seconds_left != null && msg.state_seconds_left > 0)
      ? msg.state_seconds_left
      : (msg.try_move_seconds || 30);

    var endTime = Date.now() + secondsLeft * 1000;
    _moveEndTime = endTime;
    _moveStartTime = Date.now();

    function tick() {
      var now = Date.now();
      var msLeft = Math.max(0, endTime - now);
      var left = Math.ceil(msLeft / 1000);
      updateTimerDisplay(left);

      var totalMs = _moveEndTime - _moveStartTime;
      var pct = totalMs > 0 ? (msLeft / totalMs) * 100 : 0;
      updateTimerBar(pct, left);

      if (left <= 5 && left > 0 && left !== _lastWarningBeep) {
        _lastWarningBeep = left;
        sfx.playTimerWarning(left);
        vibrate(30);
      }

      if (left <= 0) clearInterval(moveTimerInterval);
    }

    tick();
    moveTimerInterval = setInterval(tick, 250);
  }

  function updateTimerDisplay(seconds) {
    var display = seconds > 0 ? seconds + "s" : "0s";
    timerDisplay.textContent = display;
    $("#ctrl-timer").textContent = display;

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
    var endTime = Date.now() + (seconds || 15) * 1000;
    var el = $("#ready-timer");

    function tick() {
      var left = Math.max(0, Math.ceil((endTime - Date.now()) / 1000));
      if (left <= 0) {
        clearInterval(readyTimerInterval);
        el.textContent = "Time's up!";
        // Reset to join state instead of reload (iframe-friendly)
        setTimeout(function () {
          cleanup();
          switchToState(null);
        }, 2000);
      } else {
        el.textContent = left + "s";
      }
    }

    tick();
    readyTimerInterval = setInterval(tick, 250);
  }

  // -- UI State Switching --------------------------------------------------

  function switchToState(newState, data) {
    playerState = newState;

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
            var mins = Math.ceil(data.estimated_wait_seconds / 60);
            $("#wait-time").textContent = "~" + mins + " min";
          }
        }
        if (!controlSocket) connectControlWs();
        break;

      case "ready":
        readyPanel.classList.remove("hidden");
        sfx.playYourTurn();
        vibrate([100, 50, 100]);
        if (data && data.state_seconds_left != null && data.state_seconds_left > 0) {
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
        if (document.activeElement && document.activeElement !== document.body) {
          document.activeElement.blur();
        }
        if (!controlSocket) connectControlWs();
        break;

      case "done":
        resultPanel.classList.remove("hidden");
        if (data) {
          var result = data.result || "unknown";
          var title = $("#result-title");
          var message = $("#result-message");
          var icon = $("#result-icon");
          var glow = $("#result-glow");

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
          } else if (result === "loss") {
            icon.textContent = "\u{1F61E}";
            icon.classList.add("loss-icon");
            title.textContent = "😞";
            title.className = "loss";
            glow.classList.add("loss-glow");
            resultPanel.classList.add("result-loss");
            message.textContent = "Better luck next time!";
            sfx.playLoss();
            triggerScreenFlash("loss");
          } else if (result === "done") {
            icon.textContent = "\u{1F3AE}";
            icon.classList.add("loss-icon");
            title.textContent = "Turn Over";
            title.className = "";
            resultPanel.classList.add("result-loss");
            message.textContent = "Thanks for playing!";
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
            message.textContent = "Result: " + result;
          }
        }
        break;

      default:
        joinPanel.classList.remove("hidden");
    }
  }

  // -- Screen Flash Effect -------------------------------------------------

  function triggerScreenFlash(type) {
    if (!screenFlash) return;
    screenFlash.className = "";
    void screenFlash.offsetWidth;
    screenFlash.classList.add(type === "win" ? "flash-win" : "flash-loss");
    setTimeout(function () { screenFlash.className = ""; }, 1000);
  }

  // -- Haptic Feedback -----------------------------------------------------

  function vibrate(pattern) {
    if (navigator.vibrate) {
      try { navigator.vibrate(pattern); } catch (e) { /* ignore */ }
    }
  }

  // -- Helpers -------------------------------------------------------------

  function formatState(state) {
    var map = {
      idle: "Waiting for Player",
      ready_prompt: "Player Ready?",
      moving: "PLAYING",
      dropping: "DROPPING!",
      post_drop: "Checking...",
      turn_end: "Turn Over"
    };
    return map[state] || state;
  }

  function cleanup() {
    if (_keyboardTeardown) { _keyboardTeardown(); _keyboardTeardown = null; }
    if (_dpadInstance) { _dpadInstance.destroy(); _dpadInstance = null; }
    if (controlSocket) { controlSocket.disconnect(); controlSocket = null; }
    sessionStorage.removeItem("eclaw_embed_token");
    sessionStorage.removeItem("eclaw_embed_name");
    token = null;
    playerName = null;
    playerState = null;
    clearInterval(moveTimerInterval);
    clearInterval(readyTimerInterval);
  }

  // -- postMessage API (parent frame integration) --------------------------

  function notifyParent(type, data) {
    if (window.parent !== window) {
      window.parent.postMessage({
        source: "eclaw-embed",
        type: type,
        position: data && data.position,
        queue_length: data && data.queue_length,
        viewer_count: data && data.viewer_count,
        result: data && data.result
      }, "*");
    }
  }

  // Accept commands from parent frame
  window.addEventListener("message", function (event) {
    if (!event.data || event.data.target !== "eclaw-embed") return;

    var action = event.data.action;
    if (action === "join" && event.data.name && event.data.email) {
      // Programmatic join
      var nameInput = $("#join-name");
      var emailInput = $("#join-email");
      if (nameInput && emailInput && playerState === null) {
        nameInput.value = event.data.name;
        emailInput.value = event.data.email;
        joinForm.dispatchEvent(new Event("submit", { cancelable: true }));
      }
    } else if (action === "leave") {
      if (token) {
        fetch("/api/queue/leave", {
          method: "DELETE",
          headers: { Authorization: "Bearer " + token }
        }).catch(function () {});
        cleanup();
        switchToState(null);
      }
    }
  });
})();
