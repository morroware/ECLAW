/**
 * WebRTC Stream Player — connects to MediaMTX via WHEP protocol,
 * with MJPEG fallback when MediaMTX is not running.
 */
class StreamPlayer {
  constructor(videoElement, streamBaseUrl) {
    this.video = videoElement;
    this.baseUrl = streamBaseUrl; // e.g., "/stream/cam"
    this.pc = null;
    this.sessionUrl = null;
    this._reconnecting = false;
    this._mjpegImg = null;
    this._statusEl = null;
    this._createStatusOverlay();
  }

  /** Temporary diagnostic overlay — shows stream status on-screen. */
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
    this._setStatus("init");
  }

  _setStatus(msg) {
    if (this._statusEl) this._statusEl.textContent = "stream: " + msg;
    console.log("[stream]", msg);
  }

  async connect() {
    this._setStatus("connecting (WHEP)...");
    try {
      await this._connectWhep();
    } catch (e) {
      this._setStatus("WHEP failed: " + e.message + " — trying MJPEG...");
      try {
        await this._connectMjpeg();
      } catch (e2) {
        this._setStatus("ALL FAILED. WHEP: " + e.message + " | MJPEG: " + e2.message);
        throw e2;
      }
    }
  }

  async _connectWhep() {
    this.pc = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });

    this.pc.addTransceiver("video", { direction: "recvonly" });
    this.pc.addTransceiver("audio", { direction: "recvonly" });

    this.pc.ontrack = (event) => {
      this._setStatus("WHEP: got track, assigning to video...");
      this.video.srcObject = event.streams[0];
      // Ensure attributes are set in JS for mobile browsers that
      // ignore HTML attributes on dynamically-assigned streams.
      this.video.muted = true;
      this.video.playsInline = true;
      this._tryPlay();
    };

    this.pc.oniceconnectionstatechange = () => {
      const state = this.pc.iceConnectionState;
      this._setStatus("ICE: " + state);
      if (state === "failed") {
        console.warn("Stream failed, reconnecting in 3s...");
        if (!this._reconnecting) {
          this._reconnecting = true;
          setTimeout(() => this.reconnect(), 3000);
        }
      } else if (state === "disconnected") {
        // "disconnected" is often transient; give it time to self-recover
        console.warn("Stream disconnected, will reconnect in 10s if not recovered...");
        if (!this._reconnecting) {
          this._reconnecting = true;
          setTimeout(() => {
            if (this.pc && this.pc.iceConnectionState !== "connected") {
              this.reconnect();
            } else {
              this._reconnecting = false;
            }
          }, 10000);
        }
      } else if (state === "connected") {
        this._reconnecting = false;
      }
    };

    const offer = await this.pc.createOffer();
    await this.pc.setLocalDescription(offer);

    this._setStatus("WHEP: gathering ICE candidates...");

    // Wait for ICE gathering to complete (or timeout)
    await new Promise((resolve) => {
      if (this.pc.iceGatheringState === "complete") {
        resolve();
      } else {
        const check = () => {
          if (this.pc.iceGatheringState === "complete") {
            this.pc.removeEventListener("icegatheringstatechange", check);
            resolve();
          }
        };
        this.pc.addEventListener("icegatheringstatechange", check);
        setTimeout(resolve, 2000); // Timeout fallback
      }
    });

    this._setStatus("WHEP: POST to " + this.baseUrl + "/whep ...");

    const res = await fetch(this.baseUrl + "/whep", {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: this.pc.localDescription.sdp,
    });

    if (res.status !== 201) {
      throw new Error(`WHEP failed: ${res.status}`);
    }

    this.sessionUrl = res.headers.get("Location");
    const answerSdp = await res.text();
    await this.pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
    this._setStatus("WHEP: SDP exchanged, waiting for ICE...");
  }

  async _connectMjpeg() {
    this._setStatus("MJPEG: probing /api/stream/snapshot...");

    // Verify the MJPEG endpoint is available with a snapshot probe
    const probe = await fetch("/api/stream/snapshot");
    if (!probe.ok) {
      throw new Error(`MJPEG not available: ${probe.status}`);
    }

    this._setStatus("MJPEG: snapshot OK, starting stream...");

    // Hide the <video> element and insert an <img> for MJPEG
    const img = document.createElement("img");
    img.id = "mjpeg-stream";
    img.src = "/api/stream/mjpeg";
    img.style.width = "100%";
    img.style.height = "100%";
    img.style.objectFit = "contain";
    img.style.position = "absolute";
    img.style.top = "0";
    img.style.left = "0";

    // Auto-reconnect on MJPEG stream error (e.g., server restart)
    img.onerror = () => {
      this._setStatus("MJPEG: stream error, reconnecting in 3s...");
      setTimeout(() => {
        if (this._mjpegImg) {
          this._mjpegImg.src = "/api/stream/mjpeg?" + Date.now();
        }
      }, 3000);
    };

    img.onload = () => {
      this._setStatus("MJPEG: receiving frames");
    };

    this.video.style.display = "none";
    this.video.parentNode.insertBefore(img, this.video.nextSibling);
    this._mjpegImg = img;

    this._setStatus("MJPEG: connected");
  }

  async reconnect() {
    this.disconnect();
    try {
      await this.connect();
      this._reconnecting = false;
    } catch (e) {
      console.error("Reconnect failed:", e);
      setTimeout(() => this.reconnect(), 5000);
    }
  }

  disconnect() {
    if (this.pc) {
      this.pc.close();
      this.pc = null;
    }
    if (this.sessionUrl) {
      fetch(this.sessionUrl, { method: "DELETE" }).catch(() => {});
      this.sessionUrl = null;
    }
    if (this._mjpegImg) {
      this._mjpegImg.src = "";
      this._mjpegImg.remove();
      this._mjpegImg = null;
      this.video.style.display = "";
    }
  }

  /**
   * Force video playback — mobile browsers (especially Safari) can
   * silently ignore the autoplay attribute on MediaStream changes.
   * Retries on loadedmetadata if the initial play() is rejected.
   */
  _tryPlay() {
    const video = this.video;

    const attemptPlay = () => {
      const p = video.play();
      if (p && typeof p.catch === "function") {
        p.catch((err) => {
          this._setStatus("play() rejected: " + err.message);
        });
      }
    };

    // Attempt immediately
    attemptPlay();

    // Also attempt once metadata is ready (covers Safari timing edge cases)
    video.addEventListener(
      "loadedmetadata",
      () => {
        this._setStatus("WHEP: metadata loaded, playing...");
        attemptPlay();
      },
      { once: true }
    );
  }
}
