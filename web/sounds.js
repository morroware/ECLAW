/**
 * ECLAW Sound Engine — Plays custom audio files with synthesized fallbacks.
 *
 * Custom sounds: Place audio files in /sounds/ to override the default
 * synthesized sounds. Supported formats: .mp3, .wav, .ogg, .webm
 *
 * File names:
 *   /sounds/join.mp3       — Player joined the queue
 *   /sounds/your-turn.mp3  — It's the player's turn (ready prompt)
 *   /sounds/ready.mp3      — Player confirmed ready
 *   /sounds/move.mp3       — Direction button pressed
 *   /sounds/drop.mp3       — Drop button pressed
 *   /sounds/dropping.mp3   — Claw is descending
 *   /sounds/win.mp3        — Player won!
 *   /sounds/loss.mp3       — Player lost
 *   /sounds/timer.mp3      — Timer warning beep (last 5 seconds)
 *   /sounds/next-try.mp3   — New try starting after a miss
 */
class SoundEngine {
  constructor() {
    this._ctx = null;
    this._muted = false;
    this._volume = 0.35;
    this._unlocked = false;
    /** @type {Object<string, AudioBuffer|null>} */
    this._customBuffers = {};
    this._customLoaded = false;

    // Start preloading custom sounds immediately
    this._preloadCustomSounds();
  }

  /** Lazily create and resume AudioContext (must follow user gesture). */
  _ensure() {
    if (!this._ctx) {
      this._ctx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (this._ctx.state === "suspended") {
      this._ctx.resume();
    }
    return this._ctx;
  }

  /** Unlock audio on first user interaction (call from click/touch handler). */
  unlock() {
    if (this._unlocked) return;
    this._ensure();
    // Play a silent buffer to fully unlock on iOS/Safari
    const buf = this._ctx.createBuffer(1, 1, 22050);
    const src = this._ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this._ctx.destination);
    src.start(0);
    this._unlocked = true;
  }

  get muted() { return this._muted; }
  set muted(v) { this._muted = !!v; }

  toggleMute() {
    this._muted = !this._muted;
    return this._muted;
  }

  // -- Custom Sound Loading -------------------------------------------------

  /** Preload custom audio files from /sounds/ directory. */
  async _preloadCustomSounds() {
    const soundNames = [
      "join", "your-turn", "ready", "move", "drop",
      "dropping", "win", "loss", "timer", "next-try"
    ];
    const extensions = ["mp3", "wav", "ogg", "webm"];

    const loadPromises = soundNames.map(async (name) => {
      for (const ext of extensions) {
        try {
          const res = await fetch(`/sounds/${name}.${ext}`, { method: "HEAD" });
          if (res.ok) {
            // File exists — fetch and decode it
            const audioRes = await fetch(`/sounds/${name}.${ext}`);
            const arrayBuf = await audioRes.arrayBuffer();
            const ctx = this._ensure();
            this._customBuffers[name] = await ctx.decodeAudioData(arrayBuf);
            return; // Found a file for this sound, stop checking extensions
          }
        } catch (e) {
          // Fetch failed (network error, CORS, etc.) — skip
        }
      }
      // No custom file found for this sound — will use synthesized fallback
      this._customBuffers[name] = null;
    });

    await Promise.allSettled(loadPromises);
    this._customLoaded = true;
  }

  /**
   * Play a custom audio buffer if available.
   * @returns {boolean} true if custom sound was played, false if fallback needed
   */
  _playCustom(name, volume) {
    if (this._muted) return true; // "handled" — skip sound entirely
    const buffer = this._customBuffers[name];
    if (!buffer) return false; // No custom sound — use synthesized fallback

    const ctx = this._ensure();
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    const gain = ctx.createGain();
    gain.gain.value = volume != null ? volume : this._volume;
    source.connect(gain);
    gain.connect(ctx.destination);
    source.start(0);
    return true;
  }

  // -- Internal helpers (for synthesized fallbacks) -------------------------

  _gain(vol) {
    const ctx = this._ensure();
    const g = ctx.createGain();
    g.gain.value = this._muted ? 0 : (vol != null ? vol : this._volume);
    g.connect(ctx.destination);
    return g;
  }

  _osc(type, freq, gainNode, startTime, duration) {
    const ctx = this._ensure();
    const o = ctx.createOscillator();
    o.type = type;
    o.frequency.value = freq;
    o.connect(gainNode);
    o.start(startTime);
    o.stop(startTime + duration);
  }

  _noise(gainNode, startTime, duration) {
    const ctx = this._ensure();
    const sr = ctx.sampleRate;
    const len = Math.floor(sr * duration);
    const buf = ctx.createBuffer(1, len, sr);
    const data = buf.getChannelData(0);
    for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(gainNode);
    src.start(startTime);
    src.stop(startTime + duration);
  }

  // -- Public sound effects -------------------------------------------------

  /** Player joined the queue successfully. */
  playJoinQueue() {
    if (this._playCustom("join", 0.3)) return;
    if (this._muted) return;
    const ctx = this._ensure();
    const t = ctx.currentTime;
    const g = this._gain(0.2);
    g.gain.setValueAtTime(0.2, t);
    g.gain.exponentialRampToValueAtTime(0.01, t + 0.3);
    this._osc("sine", 880, g, t, 0.12);
    this._osc("sine", 1100, g, t + 0.12, 0.18);
  }

  /** It's the player's turn — attention-getting rising chime. */
  playYourTurn() {
    if (this._playCustom("your-turn", 0.4)) return;
    if (this._muted) return;
    const ctx = this._ensure();
    const t = ctx.currentTime;
    // Three-note ascending arpeggio: C5, E5, G5
    const notes = [523.25, 659.25, 783.99];
    notes.forEach((freq, i) => {
      const g = this._gain(0.25);
      g.gain.setValueAtTime(0.25, t + i * 0.15);
      g.gain.exponentialRampToValueAtTime(0.01, t + i * 0.15 + 0.35);
      this._osc("sine", freq, g, t + i * 0.15, 0.35);
      // Add shimmer with higher harmonic
      const g2 = this._gain(0.08);
      g2.gain.setValueAtTime(0.08, t + i * 0.15);
      g2.gain.exponentialRampToValueAtTime(0.001, t + i * 0.15 + 0.3);
      this._osc("sine", freq * 2, g2, t + i * 0.15, 0.3);
    });
  }

  /** Player confirmed ready — short affirmative beep. */
  playReadyConfirm() {
    if (this._playCustom("ready", 0.3)) return;
    if (this._muted) return;
    const ctx = this._ensure();
    const t = ctx.currentTime;
    const g = this._gain(0.2);
    g.gain.setValueAtTime(0.2, t);
    g.gain.exponentialRampToValueAtTime(0.01, t + 0.15);
    this._osc("square", 880, g, t, 0.06);
    this._osc("square", 1320, g, t + 0.06, 0.09);
  }

  /** Direction button pressed — subtle click. */
  playMove() {
    if (this._playCustom("move", 0.15)) return;
    if (this._muted) return;
    const ctx = this._ensure();
    const t = ctx.currentTime;
    const g = this._gain(0.06);
    g.gain.setValueAtTime(0.06, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + 0.04);
    this._noise(g, t, 0.04);
  }

  /** Drop button pressed — dramatic descending sweep. */
  playDrop() {
    if (this._playCustom("drop", 0.4)) return;
    if (this._muted) return;
    const ctx = this._ensure();
    const t = ctx.currentTime;
    // Descending sweep
    const g = this._gain(0.3);
    g.gain.setValueAtTime(0.3, t);
    g.gain.exponentialRampToValueAtTime(0.01, t + 0.8);
    const o = ctx.createOscillator();
    o.type = "sawtooth";
    o.frequency.setValueAtTime(600, t);
    o.frequency.exponentialRampToValueAtTime(80, t + 0.7);
    // Low-pass filter for softer feel
    const filt = ctx.createBiquadFilter();
    filt.type = "lowpass";
    filt.frequency.value = 1200;
    o.connect(filt);
    filt.connect(g);
    o.start(t);
    o.stop(t + 0.8);
    // Thud at the end
    const g2 = this._gain(0.15);
    g2.gain.setValueAtTime(0.15, t + 0.5);
    g2.gain.exponentialRampToValueAtTime(0.001, t + 0.9);
    this._osc("sine", 60, g2, t + 0.5, 0.4);
  }

  /** Win celebration — triumphant fanfare. */
  playWin() {
    if (this._playCustom("win", 0.5)) return;
    if (this._muted) return;
    const ctx = this._ensure();
    const t = ctx.currentTime;
    // Fanfare: C5, E5, G5, C6 with harmonics
    const fanfare = [
      { freq: 523.25, time: 0, dur: 0.2 },
      { freq: 659.25, time: 0.15, dur: 0.2 },
      { freq: 783.99, time: 0.3, dur: 0.2 },
      { freq: 1046.5, time: 0.45, dur: 0.6 },
    ];
    fanfare.forEach(({ freq, time, dur }) => {
      const g = this._gain(0.25);
      g.gain.setValueAtTime(0.25, t + time);
      g.gain.setValueAtTime(0.25, t + time + dur * 0.7);
      g.gain.exponentialRampToValueAtTime(0.01, t + time + dur);
      this._osc("sine", freq, g, t + time, dur);
      // Add brightness with triangle wave
      const g2 = this._gain(0.1);
      g2.gain.setValueAtTime(0.1, t + time);
      g2.gain.exponentialRampToValueAtTime(0.001, t + time + dur);
      this._osc("triangle", freq * 1.5, g2, t + time, dur);
    });
    // Sparkle noise at the end
    const gn = this._gain(0.05);
    gn.gain.setValueAtTime(0.05, t + 0.8);
    gn.gain.exponentialRampToValueAtTime(0.001, t + 1.2);
    this._noise(gn, t + 0.8, 0.4);
  }

  /** Loss — gentle descending tone. */
  playLoss() {
    if (this._playCustom("loss", 0.3)) return;
    if (this._muted) return;
    const ctx = this._ensure();
    const t = ctx.currentTime;
    // Sad descending: E4 → C4
    const g = this._gain(0.18);
    g.gain.setValueAtTime(0.18, t);
    g.gain.exponentialRampToValueAtTime(0.01, t + 0.8);
    const o = ctx.createOscillator();
    o.type = "sine";
    o.frequency.setValueAtTime(329.63, t);
    o.frequency.exponentialRampToValueAtTime(261.63, t + 0.6);
    o.connect(g);
    o.start(t);
    o.stop(t + 0.8);
    // Minor third undertone
    const g2 = this._gain(0.08);
    g2.gain.setValueAtTime(0.08, t + 0.1);
    g2.gain.exponentialRampToValueAtTime(0.001, t + 0.7);
    this._osc("sine", 196, g2, t + 0.1, 0.6);
  }

  /** Timer warning — short beep, call every second during countdown. */
  playTimerWarning(secondsLeft) {
    if (this._playCustom("timer", 0.25)) return;
    if (this._muted) return;
    const ctx = this._ensure();
    const t = ctx.currentTime;
    // Higher pitch as time runs out
    const freq = secondsLeft <= 3 ? 1000 : 700;
    const vol = secondsLeft <= 3 ? 0.2 : 0.12;
    const g = this._gain(vol);
    g.gain.setValueAtTime(vol, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + 0.1);
    this._osc("square", freq, g, t, 0.1);
  }

  /** New try starting (after a miss). */
  playNextTry() {
    if (this._playCustom("next-try", 0.3)) return;
    if (this._muted) return;
    const ctx = this._ensure();
    const t = ctx.currentTime;
    // Quick double-beep
    const g = this._gain(0.15);
    g.gain.setValueAtTime(0.15, t);
    g.gain.exponentialRampToValueAtTime(0.01, t + 0.25);
    this._osc("sine", 660, g, t, 0.08);
    this._osc("sine", 880, g, t + 0.12, 0.1);
  }

  /** Dropping state — mechanical descending whir. */
  playDropping() {
    if (this._playCustom("dropping", 0.3)) return;
    if (this._muted) return;
    const ctx = this._ensure();
    const t = ctx.currentTime;
    // Mechanical whir
    const g = this._gain(0.12);
    g.gain.setValueAtTime(0.12, t);
    g.gain.setValueAtTime(0.12, t + 1.5);
    g.gain.exponentialRampToValueAtTime(0.001, t + 2.0);
    const o = ctx.createOscillator();
    o.type = "sawtooth";
    o.frequency.setValueAtTime(200, t);
    o.frequency.linearRampToValueAtTime(120, t + 2.0);
    const filt = ctx.createBiquadFilter();
    filt.type = "lowpass";
    filt.frequency.value = 800;
    o.connect(filt);
    filt.connect(g);
    o.start(t);
    o.stop(t + 2.0);
  }
}
