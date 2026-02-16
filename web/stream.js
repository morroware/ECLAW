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
  }

  async connect() {
    try {
      await this._connectWhep();
    } catch (e) {
      console.warn("WHEP unavailable, trying MJPEG fallback:", e.message);
      try {
        await this._connectMjpeg();
      } catch (e2) {
        console.warn("MJPEG also unavailable:", e2.message);
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
      this.video.srcObject = event.streams[0];
    };

    this.pc.oniceconnectionstatechange = () => {
      const state = this.pc.iceConnectionState;
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
    console.log("Stream connected via WHEP");
  }

  async _connectMjpeg() {
    // Verify the MJPEG endpoint is available with a snapshot probe
    const probe = await fetch("/api/stream/snapshot");
    if (!probe.ok) {
      throw new Error(`MJPEG not available: ${probe.status}`);
    }

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
      console.warn("MJPEG stream error, reconnecting in 3s...");
      setTimeout(() => {
        if (this._mjpegImg) {
          this._mjpegImg.src = "/api/stream/mjpeg?" + Date.now();
        }
      }, 3000);
    };

    this.video.style.display = "none";
    this.video.parentNode.insertBefore(img, this.video.nextSibling);
    this._mjpegImg = img;

    console.log("Stream connected via MJPEG fallback");
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
}
