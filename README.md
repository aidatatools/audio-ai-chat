# audio-ai-chat

AI chat with human voices and languages for physical AI robots.

Two open-source robot platforms — [Open Duck Mini v2](https://github.com/apirrone/Open_Duck_Mini) and [Mini Pupper 2](https://github.com/mangdangroboticclub/mini-pupper-2) — turned from silent, script-executing hardware into voice-interactive companions.

## Open Duck Mini v2

- OS: Raspberry Pi OS (Bookworm) (Debian GNU/Linux 12 )
- Linux Kernerl: 6.12.25+rpt-rpi-2712
- Board: Raspberry Pi 5 16GB RAM

[duck_voice_chat.py](open-duck-mini-v2/duck_voice_chat.py) runs a turn-based pipeline:

```
mic (arecord + energy-based VAD) -> OpenAI STT -> chat model -> OpenAI TTS -> speaker (aplay)
```

Audio in/out goes through `arecord`/`aplay` over ALSA instead of `sounddevice`, avoiding a portaudio/apt dependency tangle on the Pi. A simple RMS-based voice-activity detector calibrates to the ambient noise floor so you don't need to press a key to talk — it starts recording once you start speaking and stops after ~1s of trailing silence. Conversation history is kept across turns.

Defaults: `gpt-4o-mini-transcribe` (STT), `gpt-5.4-mini` (chat), `gpt-4o-mini-tts` with the `alloy` voice (TTS). Mic/speaker ALSA device names (`plughw:card,device`, from `arecord -l` / `aplay -l`) and model choices are set in the `CONFIG` block at the top of the script. Say "goodbye" (or Ctrl-C) to end the session.

### How to run

```bash
# Install deps in your venv:
pip install openai
export OPENAI_API_KEY="sk-..."
# Run it with:
python duck_voice_chat.py

```

## Mini Pupper 2

- OS: Ubuntu 24.04.4
- Board: Raspberry Pi 4B 2GB RAM

[pupper2_voice_chat.py](mini-pupper-2/pupper2_voice_chat.py) streams audio both ways over the OpenAI **Realtime API** (`gpt-realtime-2`) instead of a discrete STT/chat/TTS pipeline, giving lower latency. Server-side semantic VAD decides turn boundaries, and replies come back in whatever language the user spoke.

Hardware quirks the script works around:

- Mics are an I2S voiceHAT on ALSA card 1 (capture-only, very low native amplitude — boosted in software via `MIC_GAIN`)
- Speaker is the bcm2835 PWM output on ALSA card 0 (playback-only, volume controlled via the `PCM` mixer, not `Headphone`)
- `~/.asoundrc` should map default playback -> `plughw:0,0` and capture -> `plughw:1,0`

### Use `pupper2_voice_chat.py`, not `pupper_voice_chat.py`

The original script ([pupper_voice_chat.py](mini-pupper-2/pupper_voice_chat.py)) is left in the repo for reference, but it stutters and feeds back on real hardware. `pupper2_voice_chat.py` fixes both, plus resilience over long sessions:

- **Playback jitter buffer** — audio doesn't start playing until ~300 ms (`PREBUFFER_BLOCKS`) has queued up. The old script started playing the instant any audio arrived, so a normal network burst would starve the speaker callback mid-word, causing the stutter.
- **Thread-safe leftover handling** — the old speaker callback pushed unplayed remainder samples back with `spk_q.queue.appendleft(...)`, mutating the queue's internal deque directly from the audio callback thread while the main thread was also reading it. That's a data race. The new version tracks the remainder in a plain `_leftover` array owned solely by the callback thread.
- **Half-duplex echo suppression** — on this board the speaker sits inches from the mics, so anything Pupper says gets picked back up and sent to the API as if the user said it, making Pupper interrupt itself. The new script mutes the mic (via a `speaking` event) for the duration of playback. Set `FULL_DUPLEX=1` to restore barge-in if you add real AEC or run over headphones.
- **100 ms audio blocks** (`BLOCK = 2400` vs. the old 50 ms) — larger blocks give the Pi 4B's ARM cores more slack per callback, which matters once you're also running the prebuffer/echo-gating logic.
- **Auto-reconnect** — Realtime API sessions are capped at 60 minutes; the old script just crashed when the socket closed. The new one reconnects with exponential backoff and clears the speaker buffer on disconnect so stale audio doesn't play after a reconnect.

Env vars: `OPENAI_API_KEY` (required), `MIC_GAIN` (software mic boost, default `20`), `PUPPER_VOICE` (Realtime API voice, default `marin`), `FULL_DUPLEX` (set `1` to allow barge-in, default off).

### How to run

```bash
# Install deps in your venv:
pip install openai sounddevice websockets numpy
sudo apt install -y libportaudio2
export OPENAI_API_KEY=sk-...
# Run it with:
python pupper2_voice_chat.py
```

## Reference

<https://openrobot.aidatatools.com/>

<https://jasonchuang.substack.com/p/open-duck-mini-v2-mini-pupper-2-chat>
