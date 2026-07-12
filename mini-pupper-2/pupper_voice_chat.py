#!/usr/bin/env python3
"""
Streaming voice chat with Mini Pupper 2 using the OpenAI Realtime API.

Hardware notes (Mini Pupper 2 / Ubuntu 24.04):
  - Mics  = I2S voiceHAT  -> ALSA card 1 (capture only), VERY low amplitude
  - Speaker = bcm2835 PWM -> ALSA card 0 (playback only), mixer control 'PCM'
  - ~/.asoundrc should map default playback->plughw:0,0, capture->plughw:1,0

Env:
  OPENAI_API_KEY   required
  MIC_GAIN         software mic boost (default 20; raise if it can't hear you)
  PUPPER_VOICE     realtime voice name (default "marin")
"""

import asyncio
import base64
import json
import os
import queue
import subprocess
import sys

import numpy as np
import sounddevice as sd
import websockets

MODEL = "gpt-realtime-2"
URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"
RATE = 24000          # Realtime API requires 24 kHz PCM16 mono
BLOCK = 1200          # 50 ms chunks
MIC_GAIN = float(os.environ.get("MIC_GAIN", 20))
VOICE = os.environ.get("PUPPER_VOICE", "marin")

INSTRUCTIONS = (
    "You are Mini Pupper, a small, cheerful robot dog. "
    "Keep replies short and conversational, one or two sentences. "
    "Reply in the same language the user speaks."
)

mic_q: "queue.Queue[bytes]" = queue.Queue()
spk_q: "queue.Queue[np.ndarray]" = queue.Queue()


def max_volume():
    """bcm2835 card 0 exposes 'PCM' (not 'Headphone') on Ubuntu 24.04."""
    subprocess.run(["amixer", "-c", "0", "sset", "PCM", "100%", "unmute"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def mic_callback(indata, frames, time_info, status):
    """Boost the quiet I2S mics, clip safely, hand off as PCM16 bytes."""
    audio = indata[:, 0].astype(np.float32) * MIC_GAIN
    np.clip(audio, -1.0, 1.0, out=audio)
    mic_q.put((audio * 32767).astype(np.int16).tobytes())


def spk_callback(outdata, frames, time_info, status):
    """Drain assistant audio; output silence when the queue is empty."""
    need = frames
    out = np.zeros(frames, dtype=np.int16)
    pos = 0
    while pos < need:
        try:
            chunk = spk_q.get_nowait()
        except queue.Empty:
            break
        take = min(len(chunk), need - pos)
        out[pos:pos + take] = chunk[:take]
        pos += take
        if take < len(chunk):                 # push the remainder back
            spk_q.queue.appendleft(chunk[take:])
    outdata[:] = out.reshape(-1, 1)


def drain_speaker():
    """Barge-in: dump queued assistant audio when the user starts talking."""
    with spk_q.mutex:
        spk_q.queue.clear()


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

        # Assistant audio arrives as base64 PCM16 @ 24 kHz
        if t in ("response.output_audio.delta", "response.audio.delta"):
            pcm = np.frombuffer(base64.b64decode(ev["delta"]), dtype=np.int16)
            spk_q.put(pcm)

        # User started speaking -> stop talking over them
        elif t == "input_audio_buffer.speech_started":
            drain_speaker()

        elif t == "conversation.item.input_audio_transcription.completed":
            print(f"\nYou: {ev.get('transcript', '').strip()}")

        elif t in ("response.output_audio_transcript.done",
                   "response.audio_transcript.done"):
            print(f"Pupper: {ev.get('transcript', '').strip()}\n")

        elif t == "error":
            print("API error:", ev.get("error"), file=sys.stderr)


async def main():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("Set OPENAI_API_KEY first.")

    max_volume()

    async with websockets.connect(
        URL, additional_headers={"Authorization": f"Bearer {key}"}
    ) as ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": INSTRUCTIONS,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": RATE},
                        # server decides when you've finished a sentence
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

        with sd.InputStream(samplerate=RATE, channels=1, dtype="float32",
                            blocksize=BLOCK, callback=mic_callback), \
             sd.OutputStream(samplerate=RATE, channels=1, dtype="int16",
                             blocksize=BLOCK, callback=spk_callback):

            print("Mini Pupper is listening — speak to it. Ctrl+C to stop.\n")
            await asyncio.gather(send_mic(ws), receive(ws))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye.")