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

[pupper2_voice_chat_v2.py](mini-pupper-2/pupper2_voice_chat_v2.py) streams audio both ways over the OpenAI **Realtime API** (`gpt-realtime-2.1`) instead of a discrete STT/chat/TTS pipeline, giving lower latency. Server-side semantic VAD decides turn boundaries, and replies come back in whatever language the user spoke. You can interrupt Pupper mid-reply (local barge-in). Full setup, configuration and tuning are in [mini-pupper-2/README.md](mini-pupper-2/README.md).

Hardware quirks the script works around:

- Mics are an I2S voiceHAT on ALSA card 1 (capture-only, very low native amplitude — boosted in software via `MIC_GAIN`)
- Speaker is the bcm2835 PWM output on ALSA card 0 (playback-only, volume controlled via the `PCM` mixer, not `Headphone`)
- `~/.asoundrc` should map default playback -> `plughw:0,0` and capture -> `plughw:1,0`

### Use `pupper2_voice_chat_v2.py`

The two earlier scripts are left in the repo for reference only:

- [pupper_voice_chat.py](mini-pupper-2/pupper_voice_chat.py): the original. It stutters and feeds back on real hardware.
- [pupper2_voice_chat.py](mini-pupper-2/pupper2_voice_chat.py): fixes the stutter and feedback, but mutes the mic while Pupper talks, so you can't interrupt it.

`pupper2_voice_chat_v2.py` keeps all of `pupper2_voice_chat.py`'s fixes and adds:

- **Local barge-in without AEC:** while Pupper talks, mic audio isn't sent to the API but is still measured locally. The script learns how loud the speaker echo normally is at the mic. When your voice is clearly louder than that echo, it stops playback, cancels the response, truncates the reply to what you actually heard, and sends ~300 ms of pre-roll so your first words aren't lost.
- **Device-rate probing and resampling:** the script works when the hardware doesn't support 24 kHz directly, for example by running the device at 48 kHz and resampling.
- **Echo hangover:** the mic stays muted for 0.6 s after playback ends, so the echo tail isn't sent to the API.
- **Short-reply playback fix:** short replies no longer get stuck in the jitter buffer.
- **Clean Ctrl+C.**

What `pupper2_voice_chat.py` fixed over the original, all still in v2:

- **Playback jitter buffer** — audio doesn't start playing until ~300 ms (`PREBUFFER_BLOCKS`) has queued up. The old script started playing the instant any audio arrived, so a normal network burst would starve the speaker callback mid-word, causing the stutter.
- **Thread-safe leftover handling** — the old speaker callback pushed unplayed remainder samples back with `spk_q.queue.appendleft(...)`, mutating the queue's internal deque directly from the audio callback thread while the main thread was also reading it. That's a data race. The new version tracks the remainder in a plain `_leftover` array owned solely by the callback thread.
- **Half-duplex echo suppression** — on this board the speaker sits inches from the mics, so anything Pupper says gets picked back up and sent to the API as if the user said it, making Pupper interrupt itself. The new script mutes the mic (via a `speaking` event) for the duration of playback. v2 keeps this gating but adds local barge-in on top.
- **100 ms audio blocks** (`BLOCK = 2400` vs. the old 50 ms) — larger blocks give the Pi 4B's ARM cores more slack per callback, which matters once you're also running the prebuffer/echo-gating logic.
- **Auto-reconnect** — Realtime API sessions are capped at 60 minutes; the old script just crashed when the socket closed. The new one reconnects with exponential backoff and clears the speaker buffer on disconnect so stale audio doesn't play after a reconnect.

Main env vars: `OPENAI_API_KEY` (required), `MIC_GAIN` (software mic boost, default `20`), `PUPPER_VOICE` (Realtime API voice, default `marin`), `SPK_VOLUME` (speaker %, default `80`), `BARGE_IN` (`0` disables interrupting). `BARGE_RATIO`, `BARGE_MIN_RMS`, `BARGE_BLOCKS` and `BARGE_DEBUG` tune barge-in. `FULL_DUPLEX=1` turns off mic gating entirely (headphones or AEC only). See the [full table](mini-pupper-2/README.md#configuration-environment-variables).

### How to run

```bash
sudo apt install -y python3-venv libportaudio2 alsa-utils
cd mini-pupper-2
python3 -m venv .venv && source .venv/bin/activate
pip install numpy sounddevice "websockets>=14"
export OPENAI_API_KEY=sk-...
python pupper2_voice_chat_v2.py
```

Step-by-step setup (including `~/.asoundrc`) and troubleshooting are in [mini-pupper-2/README.md](mini-pupper-2/README.md#how-to-run).

## Reference

<https://openrobot.aidatatools.com/>

<https://jasonchuang.substack.com/p/open-duck-mini-v2-mini-pupper-2-chat>
