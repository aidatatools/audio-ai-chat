#!/usr/bin/env python3
"""
duck_voice_chat.py — Talk to the Open Duck Mini V2 (Raspberry Pi 5) and it talks back.

Pipeline (turn-based):
    mic (arecord + VAD) -> OpenAI STT -> chat model -> OpenAI TTS -> speaker (aplay)

Why this design:
    - Audio I/O uses arecord/aplay via subprocess, which you've already confirmed work.
      This avoids installing sounddevice / portaudio and the apt dependency tangle.
    - Only new dependency is the pure-Python `openai` package.
    - Keeps a running message history, so it actually holds a conversation.
    - Hands-free: simple energy-based voice-activity detection starts/stops recording,
      so you don't press any key to talk.

Setup (needs working network/DNS on the Pi):
    pip install openai
    export OPENAI_API_KEY="sk-..."

Run:
    python3 duck_voice_chat.py
    # say "goodbye" (or Ctrl-C) to quit

Tune the CONFIG block for your device indices and models.
"""

import math
import os
import subprocess
import sys
import tempfile
import time
import wave
from array import array

from openai import OpenAI

# ----------------------------- CONFIG ---------------------------------------
MIC_DEVICE = "plughw:3,0"      # H540 headset mic  (from `arecord -l`)
SPK_DEVICE = "plughw:0,0"      # HifiBerry DAC     (from `aplay -l`)
# Tip: if USB card numbers shift, use name form e.g. "plughw:CARD=H540,0"

STT_MODEL  = "gpt-4o-mini-transcribe"   # or "whisper-1"
CHAT_MODEL = "gpt-5.4-mini"             # set to whatever chat model you have access to
TTS_MODEL  = "gpt-4o-mini-tts"          # or "tts-1"
TTS_VOICE  = "alloy"                    # alloy, ash, coral, echo, fable, onyx, nova, sage, shimmer

REC_RATE   = 16000             # 16 kHz mono is plenty for speech STT
SILENCE_SECS = 1.0             # stop recording after this much trailing silence
MAX_UTTER_SECS = 20            # hard cap on one utterance
START_TIMEOUT_SECS = 20        # if no speech detected within this, loop back

SYSTEM_PROMPT = (
    "You are the voice of a small friendly bipedal robot duck called Duck Mini. "
    "You are chatting out loud, so keep replies short, natural, and conversational "
    "-- usually one or two sentences. Avoid lists, markdown, or emoji. "
    "If the user says goodbye, say a brief farewell."
)
# ----------------------------------------------------------------------------

client = OpenAI()   # reads OPENAI_API_KEY from env

CHUNK = 1024                    # frames per read
BYTES_PER_FRAME = 2            # S16_LE mono


def rms(frame_bytes: bytes) -> float:
    """Root-mean-square amplitude of a raw S16_LE mono chunk (pure stdlib)."""
    samples = array("h")
    samples.frombytes(frame_bytes)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _read_exact(stream, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        part = stream.read(n - len(buf))
        if not part:
            break
        buf += part
    return buf


def record_until_silence() -> bytes | None:
    """Stream from the mic, wait for speech, capture until trailing silence.
    Returns raw PCM16 mono bytes, or None if nothing was said."""
    cmd = ["arecord", "-D", MIC_DEVICE, "-f", "S16_LE", "-r", str(REC_RATE),
           "-c", "1", "-t", "raw", "-q"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    chunk_bytes = CHUNK * BYTES_PER_FRAME
    chunks_per_sec = REC_RATE / CHUNK

    try:
        # --- calibrate ambient noise for ~1s ---
        ambient = []
        for _ in range(int(chunks_per_sec)):
            data = _read_exact(proc.stdout, chunk_bytes)
            if len(data) < chunk_bytes:
                return None
            ambient.append(rms(data))
        noise_floor = sum(ambient) / len(ambient)
        threshold = max(noise_floor * 3.0, 300.0)   # 300 = practical minimum

        print("  (listening...)")
        frames = []
        started = False
        silent_chunks = 0
        elapsed_chunks = 0
        silence_limit = int(SILENCE_SECS * chunks_per_sec)
        start_limit = int(START_TIMEOUT_SECS * chunks_per_sec)
        max_chunks = int(MAX_UTTER_SECS * chunks_per_sec)

        while True:
            data = _read_exact(proc.stdout, chunk_bytes)
            if len(data) < chunk_bytes:
                break
            level = rms(data)
            elapsed_chunks += 1

            if not started:
                if level > threshold:
                    started = True
                    frames.append(data)
                elif elapsed_chunks > start_limit:
                    return None            # nobody spoke
                continue

            frames.append(data)
            if level > threshold:
                silent_chunks = 0
            else:
                silent_chunks += 1
                if silent_chunks > silence_limit:
                    break                  # trailing silence -> done
            if len(frames) > max_chunks:
                break                      # safety cap
        return b"".join(frames) if frames else None
    finally:
        proc.terminate()
        proc.wait()


def pcm_to_wav(pcm: bytes) -> str:
    """Write raw PCM16 mono to a temp WAV file, return its path."""
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(REC_RATE)
        w.writeframes(pcm)
    return path


def transcribe(wav_path: str) -> str:
    with open(wav_path, "rb") as f:
        tr = client.audio.transcriptions.create(model=STT_MODEL, file=f)
    return (tr.text or "").strip()


def chat(messages) -> str:
    resp = client.chat.completions.create(model=CHAT_MODEL, messages=messages)
    return resp.choices[0].message.content.strip()


def speak(text: str):
    """TTS the reply and play it through the HifiBerry speaker."""
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with client.audio.speech.with_streaming_response.create(
            model=TTS_MODEL, voice=TTS_VOICE, input=text, response_format="wav"
        ) as response:
            response.stream_to_file(path)
        subprocess.run(["aplay", "-D", SPK_DEVICE, "-q", path], check=False)
    finally:
        os.unlink(path)


def main():
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY first:  export OPENAI_API_KEY=sk-...")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    print("Duck Mini voice chat. Speak after '(listening...)'. Ctrl-C to quit.\n")

    while True:
        pcm = record_until_silence()
        if not pcm:
            continue

        t0 = time.time()
        user_text = transcribe(pcm_to_wav_and_cleanup(pcm))
        if not user_text:
            continue
        print(f"You: {user_text}")

        messages.append({"role": "user", "content": user_text})
        reply = chat(messages)
        messages.append({"role": "assistant", "content": reply})
        print(f"Duck: {reply}   [{time.time() - t0:.1f}s]\n")

        speak(reply)

        if any(w in user_text.lower() for w in ("goodbye", "bye", "quit", "stop talking")):
            break


def pcm_to_wav_and_cleanup(pcm: bytes) -> str:
    """Convenience: write WAV, caller transcribes, we leave cleanup to OS temp."""
    return pcm_to_wav(pcm)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")