/**
 * ECLAW Watch-Only Embed — Spectator-only view of the live stream.
 * Connects to the WebRTC stream and status WebSocket for real-time HUD updates.
 * Supports query parameter customization for theming and layout.
 */
(function () {
  "use strict";

  // -- Query Parameters ----------------------------------------------------
  var params = new URLSearchParams(location.search);

  // Theme
  if (params.get("theme") === "light") {
    document.documentElement.classList.add("theme-light");
  }

  // Custom colors
  if (params.get("accent")) {
    document.documentElement.style.setProperty("--primary", "#" + params.get("accent"));
    document.documentElement.style.setProperty("--primary-bright", "#" + params.get("accent"));
  }
  if (params.get("bg")) {
    document.documentElement.style.setProperty("--bg", "#" + params.get("bg"));
  }

  // Footer visibility
  var showFooter = params.get("footer") !== "0";
  var playUrl = params.get("playurl") || location.origin;

  // -- DOM Elements --------------------------------------------------------
  var $ = function (sel) { return document.querySelector(sel); };
  var connectionDot = $("#connection-status");
  var viewerCount = $("#viewer-count");
  var gameStateDisplay = $("#game-state-display");
  var timerDisplay = $("#timer-display");
  var tryDisplay = $("#try-display");
  var timerBar = $("#timer-bar");
  var currentPlayerHud = $("#current-player-hud");
  var playerHudName = $("#player-hud-name");
  var footer = $("#embed-footer");
  var playLink = $("#play-link");
  var streamReconnectBtn = $("#stream-reconnect");

  // -- Footer Setup --------------------------------------------------------
  if (!showFooter && footer) {
    footer.classList.add("hidden");
  }
  if (playLink) {
    playLink.href = playUrl;
  }

  // -- Video Stream --------------------------------------------------------
  var video = $("#stream-video");
  var streamPlayer = new StreamPlayer(video, "/stream/cam");
  var playOverlay = $("#play-overlay");
  var playOverlayBtn = $("#play-overlay-btn");
  var muteToggle = $("#mute-toggle");
  var pipToggle = $("#pip-toggle");
  var fullscreenToggle = $("#fullscreen-toggle");

  streamPlayer.onStatusChange = function (status) {
    if (!streamReconnectBtn) return;
    if (status === "reconnecting") {
      streamReconnectBtn.classList.remove("hidden");
    } else if (status === "playing") {
      streamReconnectBtn.classList.add("hidden");
      detectAutoplayBlock(video);
    }
  };

  streamPlayer.connect().catch(function (err) {
    console.warn("Stream not available:", err.message);
  });

  if (streamReconnectBtn) {
    streamReconnectBtn.addEventListener("click", function () {
      if (streamPlayer) {
        streamReconnectBtn.classList.add("hidden");
        streamPlayer.reconnect();
      }
    });
  }

  // -- Autoplay Detection --------------------------------------------------

  function detectAutoplayBlock(vid) {
    if (!vid) return;
    setTimeout(function () {
      if (vid.paused && vid.readyState >= 2) {
        showPlayOverlay();
      }
    }, 500);

    vid.addEventListener("canplay", function onCanPlay() {
      if (vid.paused) showPlayOverlay();
      vid.removeEventListener("canplay", onCanPlay);
    });
  }

  function showPlayOverlay() {
    if (playOverlay) playOverlay.classList.remove("hidden");
  }

  function hidePlayOverlay() {
    if (playOverlay) playOverlay.classList.add("hidden");
  }

  if (playOverlayBtn) {
    playOverlayBtn.addEventListener("click", function () {
      if (video) {
        video.play().then(function () {
          hidePlayOverlay();
        }).catch(function () {
          video.muted = true;
          video.play().then(function () {
            hidePlayOverlay();
            updateMuteUI(true);
          }).catch(function () {});
        });
      }
    });
  }

  if (playOverlay) {
    playOverlay.addEventListener("click", function (e) {
      if (e.target === playOverlay && playOverlayBtn) playOverlayBtn.click();
    });
  }

  // -- Mute / Unmute Toggle ------------------------------------------------

  function updateMuteUI(muted) {
    var iconMuted = $("#icon-muted");
    var iconUnmuted = $("#icon-unmuted");
    if (iconMuted && iconUnmuted) {
      if (muted) {
        iconMuted.classList.remove("hidden");
        iconUnmuted.classList.add("hidden");
        if (muteToggle) muteToggle.setAttribute("aria-label", "Unmute");
      } else {
        iconMuted.classList.add("hidden");
        iconUnmuted.classList.remove("hidden");
        if (muteToggle) muteToggle.setAttribute("aria-label", "Mute");
      }
    }
  }

  if (muteToggle) {
    muteToggle.addEventListener("click", function () {
      if (!video) return;
      video.muted = !video.muted;
      updateMuteUI(video.muted);
    });
  }

  // -- Picture-in-Picture Toggle -------------------------------------------

  if (pipToggle) {
    if (!document.pictureInPictureEnabled) {
      pipToggle.classList.add("hidden");
    } else {
      pipToggle.addEventListener("click", function () {
        if (!video) return;
        if (document.pictureInPictureElement) {
          document.exitPictureInPicture().catch(function () {});
        } else {
          video.requestPictureInPicture().catch(function () {});
        }
      });
    }
  }

  // -- Fullscreen Toggle ---------------------------------------------------

  if (fullscreenToggle) {
    fullscreenToggle.addEventListener("click", function () {
      var container = $("#embed-app");
      if (!container) return;
      if (document.fullscreenElement || document.webkitFullscreenElement) {
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      } else {
        (container.requestFullscreen || container.webkitRequestFullscreen).call(container);
      }
    });

    function onFullscreenChange() {
      var isFs = !!(document.fullscreenElement || document.webkitFullscreenElement);
      var iconExpand = $("#icon-expand");
      var iconCompress = $("#icon-compress");
      if (iconExpand && iconCompress) {
        iconExpand.classList.toggle("hidden", isFs);
        iconCompress.classList.toggle("hidden", !isFs);
      }
    }

    document.addEventListener("fullscreenchange", onFullscreenChange);
    document.addEventListener("webkitfullscreenchange", onFullscreenChange);
  }

  // -- Timer State ---------------------------------------------------------
  var _timerInterval = null;
  var _moveEndTime = 0;
  var _moveStartTime = 0;

  function startTimer(secondsLeft) {
    clearInterval(_timerInterval);
    var endTime = Date.now() + secondsLeft * 1000;
    _moveEndTime = endTime;
    _moveStartTime = Date.now();

    function tick() {
      var msLeft = Math.max(0, endTime - Date.now());
      var left = Math.ceil(msLeft / 1000);
      timerDisplay.textContent = left > 0 ? left + "s" : "";

      // Color coding
      if (left <= 5) {
        timerDisplay.style.color = "#ef4444";
      } else if (left <= 10) {
        timerDisplay.style.color = "#f59e0b";
      } else {
        timerDisplay.style.color = "";
      }

      // Timer bar
      var totalMs = _moveEndTime - _moveStartTime;
      var pct = totalMs > 0 ? (msLeft / totalMs) * 100 : 0;
      updateTimerBar(pct, left);

      if (left <= 0) clearInterval(_timerInterval);
    }

    tick();
    _timerInterval = setInterval(tick, 250);
  }

  function clearTimer() {
    clearInterval(_timerInterval);
    timerDisplay.textContent = "";
    timerDisplay.style.color = "";
    updateTimerBar(0);
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

  // -- State Formatting ----------------------------------------------------
  var stateLabels = {
    idle: "Waiting for Player",
    ready_prompt: "Player Ready?",
    moving: "PLAYING",
    dropping: "DROPPING!",
    post_drop: "Checking...",
    turn_end: "Turn Over"
  };

  // -- Status WebSocket (all viewers) --------------------------------------
  var statusWs = null;
  var _reconnectDelay = 3000;

  function connectStatusWs() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    statusWs = new WebSocket(proto + "//" + location.host + "/ws/status");

    statusWs.onopen = function () {
      connectionDot.className = "status-dot connected";
      _reconnectDelay = 3000;
    };

    statusWs.onmessage = function (event) {
      var msg;
      try { msg = JSON.parse(event.data); }
      catch (e) { return; }

      if (msg.type === "queue_update") {
        // Current player HUD
        if (msg.current_player) {
          playerHudName.textContent = msg.current_player;
          currentPlayerHud.classList.remove("hidden");
        } else {
          currentPlayerHud.classList.add("hidden");
        }

        if (msg.viewer_count != null) {
          viewerCount.textContent = msg.viewer_count + " viewer" + (msg.viewer_count !== 1 ? "s" : "");
        }
      }

      if (msg.type === "state_update") {
        gameStateDisplay.textContent = stateLabels[msg.state] || msg.state;

        // Timer display for spectators
        if (msg.state === "moving" && msg.state_seconds_left > 0) {
          startTimer(msg.state_seconds_left);
          if (msg.current_try && msg.max_tries) {
            tryDisplay.textContent = "Try " + msg.current_try + "/" + msg.max_tries;
          }
        } else if (msg.state === "dropping") {
          clearTimer();
          timerDisplay.textContent = "DROPPING!";
          timerDisplay.style.color = "#f59e0b";
        } else if (msg.state === "post_drop") {
          clearTimer();
          timerDisplay.textContent = "Checking...";
          timerDisplay.style.color = "#60a5fa";
        } else if (msg.state === "idle") {
          clearTimer();
          tryDisplay.textContent = "";
        }
      }

      if (msg.type === "turn_end") {
        var result = (msg.result || "").toUpperCase();
        gameStateDisplay.textContent = result + "!";
        clearTimer();
        tryDisplay.textContent = "";
        setTimeout(function () {
          gameStateDisplay.textContent = "";
        }, 3000);
      }

      // Forward events to parent frame
      notifyParent(msg.type, msg);
    };

    statusWs.onclose = function () {
      connectionDot.className = "status-dot disconnected";
      setTimeout(connectStatusWs, _reconnectDelay);
      _reconnectDelay = Math.min(_reconnectDelay * 1.5, 30000);
    };

    statusWs.onerror = function () {
      // onclose will fire after this
    };
  }

  connectStatusWs();

  // -- Initial Data Fetch --------------------------------------------------
  fetch("/api/queue/status").then(function (res) {
    if (!res.ok) return;
    return res.json();
  }).then(function (data) {
    if (!data) return;
    queueLength.textContent = "Queue: " + (data.queue_length || 0);
    if (data.current_player) {
      playerHudName.textContent = data.current_player;
      currentPlayerHud.classList.remove("hidden");
    }
    if (data.current_player_state) {
      gameStateDisplay.textContent = stateLabels[data.current_player_state] || "";
    }
  }).catch(function () { /* ignore */ });

  // -- postMessage to Parent -----------------------------------------------
  function notifyParent(type, data) {
    if (window.parent !== window) {
      window.parent.postMessage({
        source: "eclaw-embed",
        type: type,
        queue_length: data.queue_length,
        viewer_count: data.viewer_count,
        current_player: data.current_player,
        result: data.result
      }, "*");
    }
  }

  // -- Page Lifecycle (bfcache support) ------------------------------------
  window.addEventListener("pagehide", function () {
    if (streamPlayer) streamPlayer.disconnect();
    if (statusWs) { statusWs.close(); statusWs = null; }
  });

  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      streamPlayer.connect().catch(function () {});
      connectStatusWs();
    }
  });
})();
