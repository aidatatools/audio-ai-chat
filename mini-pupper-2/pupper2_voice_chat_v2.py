#!/usr/bin/env python3
"""
Streaming voice chat with Mini Pupper 2 (OpenAI Realtime API) — barge-in edition.

Hardware (Mini Pupper 2 / Ubuntu 24.04 arm64):
  - Mics    = I2S voiceHAT -> ALSA card 1 (capture only), VERY low amplitude
  - Speaker = bcm2835 PWM  -> ALSA card 0 (playback only), mixer control 'PCM'

How barge-in works here (no AEC needed):
  - While Pupper talks, mic audio is NOT sent to the API (so the server never
    hears the echo), but it is still analysed locally.
  - We know what the speaker is playing, so we keep its recent loudness as an
    echo reference and learn the speaker->mic coupling while Pupper talks.
  - If mic loudness exceeds (coupling x speaker loudness x BARGE_RATIO) for
    BARGE_BLOCKS consecutive 100 ms blocks, it's you, not the echo:
      1. local playback stops immediately
      2. response.cancel stops generation
      3. conversation.item.truncate tells the model how much you actually heard
      4. the mic opens and the last ~300 ms (pre-roll) is sent first, so your
         first words aren't lost
  - Server-side interrupt_response stays off; the client decides.

Also: device-rate probing + resampling (48k <-> 24k), jitter buffer, echo
hangover, short-reply playback fix, auto-reconnect, clean Ctrl+C.

Env:
  OPENAI_API_KEY   required
  MIC_GAIN         software mic boost (default 20)
  PUPPER_VOICE     realtime voice name (default "marin")
  SPK_VOLUME       speaker volume % (default 80; lower = easier barge-in)
  BARGE_IN         1 = local barge-in (default), 0 = plain half-duplex
  BARGE_RATIO      how much louder than the echo you must be (default 3.0 ≈ 10 dB)
  BARGE_MIN_RMS    absolute mic level needed to count as speech (default 0.02)
  BARGE_BLOCKS     consecutive 100 ms blocks needed to trigger (default 2)
  BARGE_DEBUG      1 = print mic/echo levels while muted (for tuning)
  FULL_DUPLEX      1 = raw full duplex, no gating (only with headphones / AEC)
  MIC_DEVICE       optional: sounddevice index or name substring for the mic
  SPK_DEVICE       optional: sounddevice index or name substring for the speaker
"""

import asyncio
import base64
import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time

import numpy as np
import sounddevice as sd
import websockets

# ---------------------------------------------------------------- config
MODEL = "gpt-realtime-2.1"
URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"
RATE = 24000                 # Realtime API: PCM16 mono @ 24 kHz
BLOCK_SEC = 0.1              # 100 ms blocks — stable on ARM
PREBUFFER_BLOCKS = 3         # ~300 ms of audio buffered before playback starts
HANGOVER_SEC = 0.6           # keep mic muted this long after playback ends (echo tail)
PREROLL_BLOCKS = 3           # mic audio kept while muted, sent on barge-in (~300 ms)
ECHO_WINDOW_SEC = 0.5        # echo reference = loudest speaker block in this window
REF_ACTIVE = 0.005           # speaker RMS above this counts as "playing something"
WARMUP_BLOCKS = 5            # blocks of playback used to learn coupling before arming

MIC_GAIN = float(os.environ.get("MIC_GAIN", 20))
VOICE = os.environ.get("PUPPER_VOICE", "marin")
SPK_VOLUME = int(os.environ.get("SPK_VOLUME", 80))
FULL_DUPLEX = os.environ.get("FULL_DUPLEX", "0") == "1"
BARGE_IN = os.environ.get("BARGE_IN", "1") == "1" and not FULL_DUPLEX
BARGE_RATIO = float(os.environ.get("BARGE_RATIO", 3.0))
BARGE_MIN_RMS = float(os.environ.get("BARGE_MIN_RMS", 0.02))
BARGE_BLOCKS = int(os.environ.get("BARGE_BLOCKS", 2))
BARGE_DEBUG = os.environ.get("BARGE_DEBUG", "0") == "1"

# Rates to try on each device, in order of preference (24k = no resampling)
CANDIDATE_RATES = (RATE, 48000, 44100, 32000, 16000)

INSTRUCTIONS = (
    "You are Mini Pupper, a small, cheerful robot dog. "
    "Keep replies short and conversational, one or two sentences. "
    "Reply in the same language the user speaks. "
    "When speaking Chinese, always use Traditional Chinese as used in Taiwan "
    "(台灣繁體中文), never Simplified Chinese."
)


def _device_from_env(name):
    """Env value -> sounddevice device spec (int index, name substring, or None)."""
    v = os.environ.get(name, "").strip()
    if not v:
        return None
    return int(v) if v.isdigit() else v


MIC_DEVICE = _device_from_env("MIC_DEVICE")
SPK_DEVICE = _device_from_env("SPK_DEVICE")


# ---------------------------------------------------------------- resamplers
class Passthrough:
    def process(self, x):
        return x

    def reset(self):
        pass


class BlockMeanDecimator:
    """Integer-ratio downsampling by averaging groups of k samples."""

    def __init__(self, k):
        self.k = k

    def process(self, x):
        n = len(x) - len(x) % self.k
        return x[:n].reshape(-1, self.k).mean(axis=1)

    def reset(self):
        pass


class LinearResampler:
    """Streaming linear-interpolation resampler for mono float32."""

    def __init__(self, src_rate, dst_rate):
        self.step = src_rate / dst_rate
        self.reset()

    def reset(self):
        self.pos = 0.0
        self.prev = np.zeros(0, dtype=np.float32)

    def process(self, x):
        buf = np.concatenate([self.prev, x.astype(np.float32)])
        last = len(buf) - 1
        n_out = 0 if last < self.pos else int((last - self.pos) / self.step) + 1
        idx = self.pos + np.arange(n_out) * self.step
        out = np.interp(idx, np.arange(len(buf)), buf).astype(np.float32)
        next_pos = self.pos + n_out * self.step
        consumed = min(int(next_pos), len(buf))
        self.prev = buf[consumed:]
        self.pos = next_pos - consumed
        return out


def make_resampler(src_rate, dst_rate):
    if src_rate == dst_rate:
        return Passthrough()
    if src_rate > dst_rate and src_rate % dst_rate == 0:
        return BlockMeanDecimator(src_rate // dst_rate)
    return LinearResampler(src_rate, dst_rate)


# ---------------------------------------------------------------- barge-in detector
class BargeDetector:
    """Energy-based double-talk detector.

    update(mic_rms, ref_rms) -> True when the mic is clearly louder than the
    expected echo for `blocks` consecutive blocks. The speaker->mic coupling is
    learned while the speaker plays; it adapts down quickly and up slowly, so
    quiet talking over Pupper doesn't inflate the echo estimate much.
    """

    def __init__(self, ratio, min_rms, blocks, warmup=WARMUP_BLOCKS):
        self.ratio = ratio
        self.min_rms = min_rms
        self.blocks = blocks
        self.warmup = warmup
        self.coupling = None
        self.learned = 0
        self.hits = 0
        self.last_thresh = min_rms

    def update(self, mic_rms, ref_rms):
        if ref_rms > REF_ACTIVE:
            r = mic_rms / ref_rms
            if self.learned < self.warmup:
                # still learning the echo level: never trigger yet
                self.coupling = r if self.coupling is None else 0.7 * self.coupling + 0.3 * r
                self.learned += 1
                self.hits = 0
                self.last_thresh = float("inf")
                return False
            echo_est = self.coupling * ref_rms
        else:
            r = None
            echo_est = 0.0

        thresh = max(self.min_rms, echo_est * self.ratio)
        self.last_thresh = thresh
        if mic_rms > thresh:
            self.hits += 1
        else:
            self.hits = 0
            if r is not None:  # adapt coupling on echo-only blocks
                alpha = 0.2 if r < self.coupling else 0.02
                self.coupling += alpha * (r - self.coupling)

        if self.hits >= self.blocks:
            self.hits = 0
            return True
        return False


# ---------------------------------------------------------------- device probing
def probe(kind, device):
    """Return (rate, channels) the device accepts, preferring 24 kHz mono."""
    check = sd.check_input_settings if kind == "input" else sd.check_output_settings
    dtype = "float32" if kind == "input" else "int16"
    for rate in CANDIDATE_RATES:
        for ch in (1, 2):
            try:
                check(device=device, samplerate=rate, channels=ch, dtype=dtype)
                return rate, ch
            except Exception:
                continue
    sys.exit(f"No usable {kind} rate/channel combination on device {device!r}. "
             f"Run with --list and set {'MIC' if kind == 'input' else 'SPK'}_DEVICE.")


def device_name(kind, device):
    try:
        return sd.query_devices(device, kind)["name"]
    except Exception:
        return str(device)


# ---------------------------------------------------------------- state
mic_q: "queue.Queue[bytes]" = queue.Queue(maxsize=100)
spk_q: "queue.Queue[tuple]" = queue.Queue()      # (item_id, int16 pcm @ device rate)

speaking = threading.Event()          # speaker is actively playing a reply
playing = threading.Event()           # prebuffer filled, playback running
response_active = threading.Event()   # response.created .. response.done
mic_open = threading.Event()          # set on barge-in: mic open until next reply
barge_flag = threading.Event()        # tells the speaker thread to stop now

_unmute_at = 0.0                      # echo hangover deadline (monotonic)
_leftover = np.zeros(0, dtype=np.int16)

# playback position of the item currently being played (speaker thread writes)
_play_item = None
_play_samples = 0

# echo reference: recent (time, rms) of speaker blocks
_ref_lock = threading.Lock()
_ref_hist = collections.deque(maxlen=32)

# mic pre-roll while muted (mic thread only)
_preroll = collections.deque(maxlen=PREROLL_BLOCKS)

# asyncio side (set in main / receive)
_loop = None
barge_evt = None                      # asyncio.Event
current_response_id = None
current_item_id = None

detector = BargeDetector(BARGE_RATIO, BARGE_MIN_RMS, BARGE_BLOCKS)
spk_rate_global = RATE
mic_resampler = Passthrough()
spk_resampler = Passthrough()


# ---------------------------------------------------------------- helpers
def set_volume(pct):
    """bcm2835 card 0 exposes 'PCM' (not 'Headphone') on Ubuntu 24.04."""
    subprocess.run(
        ["amixer", "-c", "0", "sset", "PCM", f"{pct}%", "unmute"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def echo_ref():
    now = time.monotonic()
    with _ref_lock:
        return max((r for t, r in _ref_hist if now - t <= ECHO_WINDOW_SEC), default=0.0)


def drain_speaker():
    """Discard queued assistant audio (reconnect / full-duplex barge-in)."""
    global _leftover
    with spk_q.mutex:
        spk_q.queue.clear()
    _leftover = np.zeros(0, dtype=np.int16)
    spk_resampler.reset()
    playing.clear()
    speaking.clear()
    response_active.clear()


def mic_muted():
    if FULL_DUPLEX or mic_open.is_set():
        return False
    return (response_active.is_set() or speaking.is_set()
            or time.monotonic() < _unmute_at)


# ---------------------------------------------------------------- audio callbacks
def mic_callback(indata, frames, time_info, status):
    audio = mic_resampler.process(indata[:, 0].astype(np.float32)) * MIC_GAIN
    mic_rms = float(np.sqrt(np.mean(audio * audio))) if len(audio) else 0.0
    np.clip(audio, -1.0, 1.0, out=audio)
    pcm = (audio * 32767).astype(np.int16).tobytes()

    if not mic_muted():
        _preroll.clear()
        try:
            mic_q.put_nowait(pcm)
        except queue.Full:
            pass
        return

    if not BARGE_IN:
        return  # plain half-duplex: drop

    # Muted: analyse locally, keep a short pre-roll
    _preroll.append(pcm)
    ref = echo_ref()
    triggered = detector.update(mic_rms, ref)
    if BARGE_DEBUG:
        print(f"[dbg] mic={mic_rms:.3f} ref={ref:.3f} "
              f"thr={detector.last_thresh:.3f} hits={detector.hits}", file=sys.stderr)

    if triggered:
        barge_flag.set()       # speaker thread stops playback
        mic_open.set()         # stop gating until the next reply starts
        for chunk in _preroll:
            try:
                mic_q.put_nowait(chunk)
            except queue.Full:
                break
        _preroll.clear()
        if _loop is not None and barge_evt is not None:
            _loop.call_soon_threadsafe(barge_evt.set)


def spk_callback(outdata, frames, time_info, status):
    global _leftover, _unmute_at, _play_item, _play_samples
    out = np.zeros(frames, dtype=np.int16)

    if barge_flag.is_set():
        barge_flag.clear()
        with spk_q.mutex:
            spk_q.queue.clear()
        _leftover = np.zeros(0, dtype=np.int16)
        playing.clear()
        speaking.clear()
        _unmute_at = 0.0

    if not playing.is_set():
        qn = spk_q.qsize()
        if qn >= PREBUFFER_BLOCKS or (qn > 0 and not response_active.is_set()):
            playing.set()
            speaking.set()
        else:
            outdata[:] = out[:, None]
            return

    buf = _leftover
    while len(buf) < frames:
        try:
            item, chunk = spk_q.get_nowait()
        except queue.Empty:
            break
        if item != _play_item:
            _play_item, _play_samples = item, 0
        buf = np.concatenate([buf, chunk])

    n = min(len(buf), frames)
    out[:n] = buf[:n]
    _leftover = buf[n:]
    _play_samples += n

    rms = float(np.sqrt(np.mean((out.astype(np.float32) / 32768.0) ** 2)))
    with _ref_lock:
        _ref_hist.append((time.monotonic(), rms))

    if n < frames and spk_q.empty():
        playing.clear()
        speaking.clear()
        _unmute_at = time.monotonic() + HANGOVER_SEC

    outdata[:] = out[:, None]


# ---------------------------------------------------------------- ws coroutines
async def send_mic(ws):
    loop = asyncio.get_running_loop()

    def get_chunk():
        try:
            return mic_q.get(timeout=0.2)   # timeout -> Ctrl+C exits cleanly
        except queue.Empty:
            return None

    while True:
        chunk = await loop.run_in_executor(None, get_chunk)
        if chunk is None:
            continue
        await ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(chunk).decode(),
        }))


async def barge_watcher(ws):
    """Runs cancel + truncate when the mic thread detects barge-in."""
    global current_item_id
    while True:
        await barge_evt.wait()
        barge_evt.clear()
        print("[barge-in]", file=sys.stderr)

        if response_active.is_set():
            await ws.send(json.dumps({"type": "response.cancel"}))

        if current_item_id:
            played = _play_samples if _play_item == current_item_id else 0
            await ws.send(json.dumps({
                "type": "conversation.item.truncate",
                "item_id": current_item_id,
                "content_index": 0,
                "audio_end_ms": int(played * 1000 / spk_rate_global),
            }))
            current_item_id = None

        spk_resampler.reset()


async def receive(ws):
    global current_response_id, current_item_id
    async for raw in ws:
        ev = json.loads(raw)
        t = ev.get("type", "")

        if t == "response.created":
            current_response_id = ev.get("response", {}).get("id")
            response_active.set()
            mic_open.clear()          # new reply -> gate the mic again

        elif t == "response.done":
            response_active.clear()

        elif t in ("response.output_audio.delta", "response.audio.delta"):
            if mic_open.is_set() and not FULL_DUPLEX:
                continue              # late audio from a cancelled reply
            current_item_id = ev.get("item_id", current_item_id)
            pcm = np.frombuffer(base64.b64decode(ev["delta"]), dtype=np.int16)
            up = spk_resampler.process(pcm.astype(np.float32))
            spk_q.put((current_item_id,
                       np.clip(np.round(up), -32768, 32767).astype(np.int16)))

        elif t == "input_audio_buffer.speech_started":
            if FULL_DUPLEX:
                drain_speaker()

        elif t == "conversation.item.input_audio_transcription.completed":
            print(f"\nYou: {ev.get('transcript', '').strip()}")

        elif t in ("response.output_audio_transcript.done",
                   "response.audio_transcript.done"):
            print(f"Pupper: {ev.get('transcript', '').strip()}\n")

        elif t == "error":
            err = ev.get("error") or {}
            # cancel racing with a just-finished response is harmless
            if "no active response" not in str(err.get("message", "")).lower():
                print("API error:", err, file=sys.stderr)


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
                    # client handles barge-in; server never cancels on (echo) VAD
                    "turn_detection": {"type": "semantic_vad",
                                       "interrupt_response": FULL_DUPLEX},
                    "transcription": {"model": "gpt-realtime-whisper"},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": RATE},
                    "voice": VOICE,
                },
            },
        },
    }))
    await asyncio.gather(send_mic(ws), receive(ws), barge_watcher(ws))


async def main():
    global mic_resampler, spk_resampler, spk_rate_global, _loop, barge_evt

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("Set OPENAI_API_KEY first.")

    _loop = asyncio.get_running_loop()
    barge_evt = asyncio.Event()

    mic_rate, mic_ch = probe("input", MIC_DEVICE)
    spk_rate, spk_ch = probe("output", SPK_DEVICE)
    spk_rate_global = spk_rate
    mic_resampler = make_resampler(mic_rate, RATE)
    spk_resampler = make_resampler(RATE, spk_rate)

    print(f"Mic:     {device_name('input', MIC_DEVICE)} @ {mic_rate} Hz, {mic_ch} ch"
          f"{'' if mic_rate == RATE else f' -> resampled to {RATE}'}")
    print(f"Speaker: {device_name('output', SPK_DEVICE)} @ {spk_rate} Hz, {spk_ch} ch"
          f"{'' if spk_rate == RATE else f' <- resampled from {RATE}'}, volume {SPK_VOLUME}%")

    set_volume(SPK_VOLUME)
    headers = {"Authorization": f"Bearer {key}"}

    with sd.InputStream(device=MIC_DEVICE, samplerate=mic_rate, channels=mic_ch,
                        dtype="float32", blocksize=int(mic_rate * BLOCK_SEC),
                        callback=mic_callback), \
         sd.OutputStream(device=SPK_DEVICE, samplerate=spk_rate, channels=spk_ch,
                         dtype="int16", blocksize=int(spk_rate * BLOCK_SEC),
                         callback=spk_callback):

        if FULL_DUPLEX:
            mode = "full-duplex (no gating — headphones/AEC only)"
        elif BARGE_IN:
            mode = (f"half-duplex + local barge-in "
                    f"(ratio {BARGE_RATIO}, min {BARGE_MIN_RMS}, {BARGE_BLOCKS} blocks)")
        else:
            mode = "half-duplex (echo-safe, no barge-in)"
        print(f"Mini Pupper listening — {mode}. Ctrl+C to stop.\n")

        backoff = 1
        while True:
            try:
                async with websockets.connect(
                    URL, additional_headers=headers, max_size=None
                ) as ws:
                    backoff = 1
                    await session(ws)
            except (websockets.ConnectionClosed, OSError) as e:
                drain_speaker()
                mic_open.clear()
                print(f"[reconnecting in {backoff}s: {e}]", file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    if "--list" in sys.argv:
        print(sd.query_devices())
        sys.exit(0)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye.")