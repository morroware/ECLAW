/**
 * WebRTC Stream Player — connects to MediaMTX via WHEP protocol.
 *
 * Designed for reliable internet delivery to dozens of concurrent viewers.
 * Reconnects automatically with exponential backoff on any failure.
 * Debug overlay available via ?debug query parameter.
 */
class StreamPlayer {
  constructor(videoElement, streamBaseUrl) {
    this.video = videoElement;
    this.baseUrl = streamBaseUrl; // e.g., "/stream/cam"
    this.pc = null;
    this.sessionUrl = null;
    this._reconnecting = false;
    this._backoff = 1000;
    this._frameCheckTimer = null;
    this._statusEl = null;
    this._debug = new URLSearchParams(location.search).has("debug");
    if (this._debug) this._createStatusOverlay();

    // Stream status callback — called with "connecting", "playing",
    // "reconnecting", or "failed" so the UI can show/hide a reconnect button.
    this.onStatusChange = null;
    this._status = "connecting";
  }

  _setStatus(status) {
    this._status = status;
    if (this.onStatusChange) this.onStatusChange(status);
  }

  _createStatusOverlay() {
    const el = document.createElement("div");
    el.id = "stream-debug";
    el.style.cssText =
      "position:absolute;bottom:8px;right:8px;z-index:999;" +
      "background:rgba(0,0,0,0.75);color:#0f0;font:11px/1.3 monospace;" +
      "padding:4px 8px;border-radius:4px;pointer-events:none;" +
      "max-width:90%;word-break:break-all;";
    this.video.parentNode.appendChild(el);
    this._statusEl = el;
  }

  _log(msg) {
    console.log("[stream]", msg);
    if (this._statusEl) this._statusEl.textContent = "stream: " + msg;
  }

  async connect() {
    this._log("connecting via WHEP...");
    this._setStatus("connecting");
    try {
      await this._connectWhep();
      this._backoff = 1000; // reset on successful connection
    } catch (e) {
      this._log("WHEP failed: " + e.message);
      this._setStatus("reconnecting");
      this._scheduleReconnect();
    }
  }

  async _connectWhep() {
    this.pc = new RTCPeerConnection({
      iceServers: [
        { urls: "stun:stun.l.google.com:19302" },
        { urls: "stun:stun1.l.google.com:19302" },
      ],
    });

    // Prefer H.264 for widest mobile hardware decode support.
    // Pi Camera and USB fallback both encode H.264 natively, so this
    // also avoids unnecessary transcoding in MediaMTX.
    const videoTx = this.pc.addTransceiver("video", { direction: "recvonly" });
    if (typeof RTCRtpReceiver.getCapabilities === "function") {
      try {
        const caps = RTCRtpReceiver.getCapabilities("video").codecs;
        const h264 = caps.filter(c => c.mimeType === "video/H264");
        const rest = caps.filter(c => c.mimeType !== "video/H264");
        if (h264.length > 0) {
          videoTx.setCodecPreferences([...h264, ...rest]);
        }
      } catch (_) {
        // setCodecPreferences not supported — browser will negotiate normally
      }
    }
    this.pc.addTransceiver("audio", { direction: "recvonly" });

    this.pc.ontrack = (event) => {
      this._log("track: " + event.track.kind);
      if (event.track.kind === "video") {
        this.video.srcObject = event.streams[0];
        // Force attributes in JS — mobile browsers can ignore HTML attributes
        // on dynamically-assigned MediaStreams.
        this.video.muted = true;
        this.video.playsInline = true;
        this._tryPlay();
        this._startFrameCheck();
      }
    };

    this.pc.oniceconnectionstatechange = () => {
      if (!this.pc) return;
      const state = this.pc.iceConnectionState;
      this._log("ICE: " + state);
      if (state === "failed") {
        this._setStatus("reconnecting");
        this._scheduleReconnect();
      } else if (state === "disconnected") {
        // Temporary blip — give 10s to recover before reconnecting
        setTimeout(() => {
          if (this.pc && this.pc.iceConnectionState !== "connected") {
            this._setStatus("reconnecting");
            this._scheduleReconnect();
          }
        }, 10000);
      } else if (state === "connected") {
        this._reconnecting = false;
        this._backoff = 1000;
      }
    };

    const offer = await this.pc.createOffer();
    await this.pc.setLocalDescription(offer);
    this._log("gathering ICE candidates...");

    // Wait for ICE gathering — generous 5s timeout for mobile networks
    await new Promise((resolve) => {
      if (this.pc.iceGatheringState === "complete") return resolve();
      const check = () => {
        if (this.pc.iceGatheringState === "complete") {
          this.pc.removeEventListener("icegatheringstatechange", check);
          resolve();
        }
      };
      this.pc.addEventListener("icegatheringstatechange", check);
      setTimeout(resolve, 5000);
    });

    this._log("WHEP POST...");
    const res = await fetch(this.baseUrl + "/whep", {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: this.pc.localDescription.sdp,
    });

    if (res.status !== 201) {
      throw new Error("WHEP " + res.status);
    }

    this.sessionUrl = res.headers.get("Location");
    const answerSdp = await res.text();
    await this.pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
    this._log("SDP exchanged, waiting for media...");
  }

  /**
   * Monitor video decode. If no frames arrive within 10s, tear down and
   * retry WebRTC from scratch (the next attempt may negotiate a different
   * codec path or hit a fresh keyframe).
   *
   * If the video is paused with a valid srcObject, autoplay was blocked
   * (common on iPhone Safari). In that case, signal "autoplay_blocked"
   * instead of reconnecting, so the UI can show a tap-to-play overlay.
   */
  _startFrameCheck() {
    if (this._frameCheckTimer) clearInterval(this._frameCheckTimer);
    let checks = 0;
    this._frameCheckTimer = setInterval(() => {
      checks++;
      const w = this.video.videoWidth;
      const h = this.video.videoHeight;
      const ready = this.video.readyState;

      if (this._debug) {
        this._log("check #" + checks + ": " + w + "x" + h +
          " ready=" + ready + " paused=" + this.video.paused);
      }

      if (w > 0 && h > 0 && ready >= 2) {
        this._log("playing " + w + "x" + h);
        this._setStatus("playing");
        clearInterval(this._frameCheckTimer);
        this._frameCheckTimer = null;
        return;
      }

      // After 3s, surface autoplay-blocked early so UI can react
      if (checks === 3 && this.video.paused && this.video.srcObject) {
        this._log("autoplay appears blocked");
        this._setStatus("autoplay_blocked");
        // Don't return — keep checking in case user taps
      }

      // 10s without decoded frames
      if (checks >= 10) {
        clearInterval(this._frameCheckTimer);
        this._frameCheckTimer = null;

        if (this.video.paused && this.video.srcObject) {
          // Autoplay blocked — WebRTC connection is fine, the browser
          // just won't render without a user gesture. Don't reconnect.
          this._log("autoplay blocked, waiting for user interaction");
          this._setStatus("autoplay_blocked");
        } else {
          // Genuine connection issue — reconnect WebRTC.
          this._log("no frames after 10s, retrying WebRTC...");
          this._setStatus("reconnecting");
          this._scheduleReconnect();
        }
      }
    }, 1000);
  }

  _scheduleReconnect() {
    if (this._reconnecting) return;
    this._reconnecting = true;
    const delay = this._backoff;
    this._log("reconnecting in " + (delay / 1000) + "s...");
    setTimeout(() => {
      this._reconnecting = false;
      this.disconnect();
      this._backoff = Math.min(this._backoff * 2, 30000);
      this.connect();
    }, delay);
  }

  async reconnect() {
    this.disconnect();
    this._backoff = 1000;
    await this.connect();
  }

  disconnect() {
    if (this._frameCheckTimer) {
      clearInterval(this._frameCheckTimer);
      this._frameCheckTimer = null;
    }
    if (this.pc) {
      this.pc.close();
      this.pc = null;
    }
    if (this.sessionUrl) {
      fetch(this.sessionUrl, { method: "DELETE", keepalive: true }).catch(() => {});
      this.sessionUrl = null;
    }
  }

  /**
   * Force video playback — mobile browsers (especially Safari) silently
   * ignore the autoplay attribute on dynamically-assigned MediaStreams.
   * Retries on loadedmetadata AND canplay to cover Safari timing quirks,
   * plus a delayed fallback for iOS devices where events fire too early.
   */
  _tryPlay() {
    const v = this.video;
    const attempt = () => {
      const p = v.play();
      if (p && typeof p.catch === "function") {
        p.catch((err) => this._log("play() rejected: " + err.message));
      }
    };
    attempt();
    v.addEventListener("loadedmetadata", () => {
      this._log("metadata loaded, playing...");
      attempt();
    }, { once: true });
    v.addEventListener("canplay", () => {
      this._log("canplay, attempting play...");
      attempt();
    }, { once: true });

    // iOS Safari fallback: retry after a short delay in case the above
    // events fired before the decoder was truly ready.
    setTimeout(() => {
      if (v.paused && v.srcObject) {
        this._log("still paused after 1.5s, retrying play...");
        attempt();
      }
    }, 1500);
  }

  /**
   * Attempt to start playback after user interaction (tap overlay).
   * Returns a promise that resolves true if playback started.
   */
  userPlay() {
    const v = this.video;
    const p = v.play();
    if (p && typeof p.then === "function") {
      return p.then(() => {
        this._startFrameCheck();
        return true;
      }).catch(() => false);
    }
    this._startFrameCheck();
    return Promise.resolve(true);
  }
}
