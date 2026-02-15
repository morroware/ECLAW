/**
 * Control WebSocket — authenticated bidirectional channel for players.
 */
class ControlSocket {
  constructor(token) {
    this.token = token;
    this.ws = null;
    this.reconnectDelay = 1000;
    this.latencyMs = 0;
    this.onStateChange = null;
    this.onReadyPrompt = null;
    this.onTurnEnd = null;
    this.onAuthOk = null;
    this.onError = null;
    this.onControlAck = null;
    this.onConnect = null;
    this.onDisconnect = null;
    this._shouldReconnect = true;
  }

  connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    this.ws = new WebSocket(`${proto}//${location.host}/ws/control`);

    this.ws.onopen = () => {
      this.ws.send(JSON.stringify({ type: "auth", token: this.token }));
      this.reconnectDelay = 1000;
      if (this.onConnect) this.onConnect();
    };

    this.ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);

      switch (msg.type) {
        case "auth_ok":
          if (this.onAuthOk) this.onAuthOk(msg);
          break;
        case "error":
          if (this.onError) this.onError(msg);
          break;
        case "state_update":
          if (this.onStateChange) this.onStateChange(msg);
          break;
        case "ready_prompt":
          if (this.onReadyPrompt) this.onReadyPrompt(msg);
          break;
        case "turn_end":
          if (this.onTurnEnd) this.onTurnEnd(msg);
          break;
        case "control_ack":
          if (this.onControlAck) this.onControlAck(msg);
          break;
        case "latency_ping":
          this.ws.send(JSON.stringify({
            type: "latency_pong",
            server_time: msg.server_time,
          }));
          this.latencyMs = Math.round((Date.now() / 1000 - msg.server_time) * 1000);
          break;
      }
    };

    this.ws.onclose = () => {
      if (this.onDisconnect) this.onDisconnect();
      if (this._shouldReconnect) {
        setTimeout(() => this.connect(), this.reconnectDelay);
        this.reconnectDelay = Math.min(this.reconnectDelay * 2, 10000);
      }
    };

    this.ws.onerror = () => {
      // onclose will fire after this
    };
  }

  send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  keydown(key) { this.send({ type: "keydown", key }); }
  keyup(key) { this.send({ type: "keyup", key }); }
  drop() { this.send({ type: "drop" }); }
  readyConfirm() { this.send({ type: "ready_confirm" }); }

  disconnect() {
    this._shouldReconnect = false;
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}
