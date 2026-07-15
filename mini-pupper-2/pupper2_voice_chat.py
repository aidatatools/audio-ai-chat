#!/usr/bin/env python3
"""
Reliable streaming voice chat with Mini Pupper 2 (OpenAI Realtime API).

Hardware (Mini Pupper 2 / Ubuntu 24.04 arm64):
  - Mics   = I2S voiceHAT  -> ALSA card 1 (capture only), VERY low amplitude
  - Speaker = bcm2835 PWM  -> ALSA card 0 (playback only), mixer control 'PCM'
  - ~/.asoundrc: default playback -> plughw:0,0, capture -> plughw:1,0

Why this version does not stutter:
  - Playback jitter buffer: accumulate ~300 ms before playing, so network
    bursts don't starve the audio callback (the usual cause of choppiness).
  - Thread-safe leftover handling (no deque mutation inside the callback).
  - 100 ms blocks: easier for a Pi-class ARM board to keep up.
  - Half-duplex echo control: mic is muted while the robot is talking, so the
    speaker (inches from the mics) can't make Pupper interrupt itself.
  - Auto-reconnect: Realtime sessions cap at 60 min; this reconnects cleanly.

Env:
  OPENAI_API_KEY   required
  MIC_GAIN         software mic boost (default 20; raise if it can't hear you)
  PUPPER_VOICE     realtime voice name (default "marin")
  FULL_DUPLEX      set to 1 to allow barge-in (needs real AEC or headphones)
"""

import asyncio
import base64
import json
import os
import queue
import subprocess
import sys
import threading

import numpy as np
import sounddevice as sd
import websockets

# ---------------------------------------------------------------- config
MODEL = "gpt-realtime-2"
URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"
RATE = 24000                 # Realtime API: PCM16 mono @ 24 kHz
BLOCK = 2400                 # 100 ms — stable on ARM
PREBUFFER_BLOCKS = 3         # ~300 ms of audio buffered before playback starts
MIC_GAIN = float(os.environ.get("MIC_GAIN", 20))
VOICE = os.environ.get("PUPPER_VOICE", "marin")
FULL_DUPLEX = os.environ.get("FULL_DUPLEX", "0") == "1"

INSTRUCTIONS = (
    "You are Mini Pupper, a small, cheerful robot dog. "
    "Keep replies short and conversational, one or two sentences. "
    "Reply in the same language the user speaks."
)

# ---------------------------------------------------------------- state
mic_q: "queue.Queue[bytes]" = queue.Queue(maxsize=100)
spk_q: "queue.Queue[np.ndarray]" = queue.Queue()

# True while assistant audio is playing -> used to gate the mic (half-duplex)
speaking = threading.Event()
# Playback starts only after the prebuffer has filled; reset when drained
playing = threading.Event()
_leftover = np.zeros(0, dtype=np.int16)


# ---------------------------------------------------------------- helpers
def max_volume():
    """bcm2835 card 0 exposes 'PCM' (not 'Headphone') on Ubuntu 24.04."""
    subprocess.run(
        ["amixer", "-c", "0", "sset", "PCM", "100%", "unmute"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def drain_speaker():
    """Discard queued assistant audio (barge-in / turn cut-off)."""
    global _leftover
    with spk_q.mutex:
        spk_q.queue.clear()
    _leftover = np.zeros(0, dtype=np.int16)
    playing.clear()
    speaking.clear()


# ---------------------------------------------------------------- audio callbacks
def mic_callback(indata, frames, time_info, status):
    """Boost quiet I2S mics, gate while speaking (half-duplex), send PCM16."""
    if not FULL_DUPLEX and speaking.is_set():
        return  # don't feed our own voice back to the API
    audio = indata[:, 0].astype(np.float32) * MIC_GAIN
    np.clip(audio, -1.0, 1.0, out=audio)
    pcm = (audio * 32767).astype(np.int16).tobytes()
    try:
        mic_q.put_nowait(pcm)
    except queue.Full:
        pass  # drop rather than block the audio thread


def spk_callback(outdata, frames, time_info, status):
    """Play assistant audio with a prebuffer so bursts don't cause underruns."""
    global _leftover
    out = np.zeros(frames, dtype=np.int16)

    # Wait until enough audio is queued before starting (fills the jitter buffer)
    if not playing.is_set():
        if spk_q.qsize() >= PREBUFFER_BLOCKS:
            playing.set()
            speaking.set()
        else:
            outdata[:] = out.reshape(-1, 1)
            return

    buf = _leftover
    while len(buf) < frames:
        try:
            buf = np.concatenate([buf, spk_q.get_nowait()])
        except queue.Empty:
            break

    n = min(len(buf), frames)
    out[:n] = buf[:n]
    _leftover = buf[n:]

    # Ran dry and nothing left -> assistant finished; re-arm prebuffer
    if n < frames and spk_q.empty():
        playing.clear()
        speaking.clear()

    outdata[:] = out.reshape(-1, 1)


# ---------------------------------------------------------------- ws coroutines
async def send_mic(ws):
    loop = asyncio.get_running_loop()
    while True:
        chunk = await loop.run_in_executor(None, mic_q.get)
        await ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(chunk).decode(),
        }))


async def receive(ws):
    async for raw in ws:
        ev = json.loads(raw)
        t = ev.get("type", "")

        if t in ("response.output_audio.delta", "response.audio.delta"):
            pcm = np.frombuffer(base64.b64decode(ev["delta"]), dtype=np.int16)
            spk_q.put(pcm)

        elif t == "input_audio_buffer.speech_started":
            if FULL_DUPLEX:
                drain_speaker()  # barge-in only when echo is handled elsewhere

        elif t == "conversation.item.input_audio_transcription.completed":
            print(f"\nYou: {ev.get('transcript', '').strip()}")

        elif t in ("response.output_audio_transcript.done",
                   "response.audio_transcript.done"):
            print(f"Pupper: {ev.get('transcript', '').strip()}\n")

        elif t == "error":
            print("API error:", ev.get("error"), file=sys.stderr)


async def session(ws):
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": INSTRUCTIONS,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": RATE},
                    "turn_detection": {"type": "semantic_vad"},
                    "transcription": {"model": "gpt-realtime-whisper"},
                },
                "output": {
                    "format": {"type": "audio/pcm"},
                    "voice": VOICE,
                },
            },
        },
    }))
    await asyncio.gather(send_mic(ws), receive(ws))


async def main():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("Set OPENAI_API_KEY first.")

    max_volume()
    headers = {"Authorization": f"Bearer {key}"}

    with sd.InputStream(samplerate=RATE, channels=1, dtype="float32",
                        blocksize=BLOCK, callback=mic_callback), \
         sd.OutputStream(samplerate=RATE, channels=1, dtype="int16",
                         blocksize=BLOCK, callback=spk_callback):

        mode = "full-duplex (barge-in)" if FULL_DUPLEX else "half-duplex (echo-safe)"
        print(f"Mini Pupper listening — {mode}. Ctrl+C to stop.\n")

        backoff = 1
        while True:  # auto-reconnect loop
            try:
                async with websockets.connect(
                    URL, additional_headers=headers, max_size=None
                ) as ws:
                    backoff = 1
                    await session(ws)
            except (websockets.ConnectionClosed, OSError) as e:
                drain_speaker()
                print(f"[reconnecting in {backoff}s: {e}]", file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye.")