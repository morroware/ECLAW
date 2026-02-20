#!/usr/bin/env python3
"""
Generate all ECLAW sound effects as .mp3 files.

This script synthesises the same sounds that the Web Audio API fallback code
in web/sounds.js produces, then writes them to web/sounds/ as .mp3 files so
the browser can load real audio files instead of relying on live synthesis.

Requirements: Python 3, ffmpeg (on PATH).

Usage:
    python3 scripts/generate_sounds.py
"""

import math
import os
import random
import struct
import subprocess
import tempfile
import wave

SAMPLE_RATE = 44100
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "web", "sounds")


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _samples(duration: float) -> int:
    return int(SAMPLE_RATE * duration)


def _silence(duration: float) -> list[float]:
    return [0.0] * _samples(duration)


def _sine(freq: float, duration: float, sr: int = SAMPLE_RATE) -> list[float]:
    n = int(sr * duration)
    return [math.sin(2 * math.pi * freq * i / sr) for i in range(n)]


def _square(freq: float, duration: float, sr: int = SAMPLE_RATE) -> list[float]:
    n = int(sr * duration)
    return [1.0 if math.sin(2 * math.pi * freq * i / sr) >= 0 else -1.0 for i in range(n)]


def _triangle(freq: float, duration: float, sr: int = SAMPLE_RATE) -> list[float]:
    n = int(sr * duration)
    out = []
    for i in range(n):
        t = (freq * i / sr) % 1.0
        out.append(4.0 * abs(t - 0.5) - 1.0)
    return out


def _sawtooth(freq_start: float, freq_end: float, duration: float,
              ramp: str = "exp", sr: int = SAMPLE_RATE) -> list[float]:
    """Sawtooth wave with frequency sweep (exponential or linear)."""
    n = int(sr * duration)
    out = []
    phase = 0.0
    for i in range(n):
        t_frac = i / max(n - 1, 1)
        if ramp == "exp" and freq_start > 0 and freq_end > 0:
            freq = freq_start * ((freq_end / freq_start) ** t_frac)
        else:
            freq = freq_start + (freq_end - freq_start) * t_frac
        phase += freq / sr
        phase %= 1.0
        out.append(2.0 * phase - 1.0)
    return out


def _noise(duration: float, sr: int = SAMPLE_RATE) -> list[float]:
    n = int(sr * duration)
    return [random.random() * 2 - 1 for _ in range(n)]


def _lowpass(samples: list[float], cutoff: float, sr: int = SAMPLE_RATE) -> list[float]:
    """Simple first-order RC low-pass filter."""
    rc = 1.0 / (2.0 * math.pi * cutoff)
    dt = 1.0 / sr
    alpha = dt / (rc + dt)
    out = [0.0] * len(samples)
    out[0] = alpha * samples[0]
    for i in range(1, len(samples)):
        out[i] = out[i - 1] + alpha * (samples[i] - out[i - 1])
    return out


def _env_exp_decay(samples: list[float], vol_start: float, vol_end: float,
                   duration: float | None = None) -> list[float]:
    """Apply an exponential volume envelope (decay) over the full buffer."""
    n = len(samples)
    if n == 0:
        return samples
    vol_end = max(vol_end, 1e-6)
    out = []
    for i in range(n):
        t = i / max(n - 1, 1)
        vol = vol_start * ((vol_end / vol_start) ** t)
        out.append(samples[i] * vol)
    return out


def _env_linear(samples: list[float], vol_start: float, vol_end: float) -> list[float]:
    n = len(samples)
    if n == 0:
        return samples
    out = []
    for i in range(n):
        t = i / max(n - 1, 1)
        vol = vol_start + (vol_end - vol_start) * t
        out.append(samples[i] * vol)
    return out


def _mix(*buffers: list[float]) -> list[float]:
    """Mix (sum) multiple buffers of possibly different lengths."""
    max_len = max(len(b) for b in buffers)
    out = [0.0] * max_len
    for buf in buffers:
        for i, v in enumerate(buf):
            out[i] += v
    return out


def _pad_start(samples: list[float], duration: float) -> list[float]:
    return _silence(duration) + samples


def _clamp(samples: list[float]) -> list[float]:
    return [max(-1.0, min(1.0, s)) for s in samples]


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _write_wav(path: str, samples: list[float]):
    clamped = _clamp(samples)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        data = b"".join(struct.pack("<h", int(s * 32767)) for s in clamped)
        wf.writeframes(data)


def _wav_to_mp3(wav_path: str, mp3_path: str):
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path, "-codec:a", "libmp3lame",
         "-b:a", "128k", "-ar", str(SAMPLE_RATE), mp3_path],
        check=True, capture_output=True,
    )


def save_mp3(name: str, samples: list[float]):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    mp3_path = os.path.join(OUTPUT_DIR, f"{name}.mp3")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_wav = tmp.name
    try:
        _write_wav(tmp_wav, samples)
        _wav_to_mp3(tmp_wav, mp3_path)
    finally:
        os.unlink(tmp_wav)
    print(f"  ✓ {mp3_path}")


# ---------------------------------------------------------------------------
# Sound generators — faithful reproductions of sounds.js synthesis
# ---------------------------------------------------------------------------

def gen_join() -> list[float]:
    """Player joined the queue — two rising sine tones (880 Hz → 1100 Hz)."""
    tone1 = _env_exp_decay(_sine(880, 0.12), 0.2, 0.01)
    tone2 = _env_exp_decay(_sine(1100, 0.18), 0.2, 0.01)
    return tone1 + tone2


def gen_your_turn() -> list[float]:
    """Ascending C5-E5-G5 arpeggio with shimmer harmonics."""
    notes = [523.25, 659.25, 783.99]
    parts = []
    for i, freq in enumerate(notes):
        offset = i * 0.15
        # Main tone
        main = _env_exp_decay(_sine(freq, 0.35), 0.25, 0.01)
        main = _pad_start(main, offset)
        # Shimmer (octave above, quieter)
        shimmer = _env_exp_decay(_sine(freq * 2, 0.3), 0.08, 0.001)
        shimmer = _pad_start(shimmer, offset)
        parts.extend([main, shimmer])
    return _mix(*parts)


def gen_ready() -> list[float]:
    """Short affirmative beep — two quick square wave tones."""
    tone1 = _env_exp_decay(_square(880, 0.06), 0.2, 0.01)
    tone2 = _env_exp_decay(_square(1320, 0.09), 0.2, 0.01)
    return tone1 + tone2


def gen_move() -> list[float]:
    """Subtle click — short noise burst."""
    return _env_exp_decay(_noise(0.04), 0.06, 0.001)


def gen_drop() -> list[float]:
    """Dramatic descending sweep with thud."""
    # Sawtooth 600→80 Hz over 0.7s through lowpass at 1200 Hz
    sweep = _sawtooth(600, 80, 0.8, ramp="exp")
    sweep = _lowpass(sweep, 1200)
    sweep = _env_exp_decay(sweep, 0.3, 0.01)
    # Thud: sine at 60 Hz starting at t=0.5
    thud = _env_exp_decay(_sine(60, 0.4), 0.15, 0.001)
    thud = _pad_start(thud, 0.5)
    return _mix(sweep, thud)


def gen_win() -> list[float]:
    """Triumphant fanfare — C5, E5, G5, C6 with triangle harmonics + sparkle."""
    fanfare = [
        (523.25, 0.0, 0.2),
        (659.25, 0.15, 0.2),
        (783.99, 0.3, 0.2),
        (1046.5, 0.45, 0.6),
    ]
    parts = []
    for freq, offset, dur in fanfare:
        # Main sine tone: hold then decay
        main = _sine(freq, dur)
        # Envelope: hold at 0.25 for 70% then decay
        hold_n = int(len(main) * 0.7)
        env_main = [0.25] * hold_n
        decay_n = len(main) - hold_n
        for i in range(decay_n):
            t = i / max(decay_n - 1, 1)
            env_main.append(0.25 * ((0.01 / 0.25) ** t))
        main = [s * e for s, e in zip(main, env_main)]
        main = _pad_start(main, offset)
        # Triangle harmonic at 1.5x freq
        bright = _env_exp_decay(_triangle(freq * 1.5, dur), 0.1, 0.001)
        bright = _pad_start(bright, offset)
        parts.extend([main, bright])
    # Sparkle noise at end
    sparkle = _env_exp_decay(_noise(0.4), 0.05, 0.001)
    sparkle = _pad_start(sparkle, 0.8)
    parts.append(sparkle)
    return _mix(*parts)


def gen_loss() -> list[float]:
    """Sad descending tone — E4→C4 with minor third undertone."""
    # Frequency sweep sine: 329.63 → 261.63 over 0.6s, total 0.8s
    n = _samples(0.8)
    sweep = []
    for i in range(n):
        t = i / max(n - 1, 1)
        sweep_dur = 0.6 / 0.8  # sweep happens in first 0.6s of 0.8s
        if t < sweep_dur:
            frac = t / sweep_dur
            freq = 329.63 * ((261.63 / 329.63) ** frac)
        else:
            freq = 261.63
        sweep.append(math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
    sweep = _env_exp_decay(sweep, 0.18, 0.01)
    # Minor third undertone at 196 Hz starting at t=0.1
    undertone = _env_exp_decay(_sine(196, 0.6), 0.08, 0.001)
    undertone = _pad_start(undertone, 0.1)
    return _mix(sweep, undertone)


def gen_timer() -> list[float]:
    """Timer warning beep — short square wave pulse.
    We generate the higher urgency version (1000 Hz) since it works for all
    countdown states."""
    return _env_exp_decay(_square(1000, 0.1), 0.2, 0.001)


def gen_next_try() -> list[float]:
    """Quick double-beep — 660 Hz then 880 Hz."""
    beep1 = _sine(660, 0.08)
    gap = _silence(0.04)
    beep2 = _sine(880, 0.1)
    combined = beep1 + gap + beep2
    return _env_exp_decay(combined, 0.15, 0.01)


def gen_dropping() -> list[float]:
    """Mechanical descending whir — sawtooth 200→120 Hz through lowpass."""
    sweep = _sawtooth(200, 120, 2.0, ramp="linear")
    sweep = _lowpass(sweep, 800)
    # Envelope: hold at 0.12 until 1.5s, then decay
    n = len(sweep)
    hold_end = int(SAMPLE_RATE * 1.5)
    out = []
    for i in range(n):
        if i < hold_end:
            vol = 0.12
        else:
            t = (i - hold_end) / max(n - hold_end - 1, 1)
            vol = 0.12 * ((0.001 / 0.12) ** t)
        out.append(sweep[i] * vol)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SOUNDS = {
    "join": gen_join,
    "your-turn": gen_your_turn,
    "ready": gen_ready,
    "move": gen_move,
    "drop": gen_drop,
    "dropping": gen_dropping,
    "win": gen_win,
    "loss": gen_loss,
    "timer": gen_timer,
    "next-try": gen_next_try,
}


def main():
    print(f"Generating {len(SOUNDS)} sound effects → {os.path.abspath(OUTPUT_DIR)}/")
    for name, generator in SOUNDS.items():
        samples = generator()
        save_mp3(name, samples)
    print("Done.")


if __name__ == "__main__":
    main()
