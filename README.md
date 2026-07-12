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
- Board: Raspberry Pi 4B (MangDang Mini Pupper 2)

[pupper_voice_chat.py](mini-pupper-2/pupper_voice_chat.py) streams audio both ways over the OpenAI **Realtime API** (`gpt-realtime-2`) instead of a discrete STT/chat/TTS pipeline, giving lower latency and barge-in (the robot stops talking as soon as it detects you speaking). Server-side semantic VAD decides turn boundaries, and replies come back in whatever language the user spoke.

Hardware quirks the script works around:

- Mics are an I2S voiceHAT on ALSA card 1 (capture-only, very low native amplitude — boosted in software via `MIC_GAIN`)
- Speaker is the bcm2835 PWM output on ALSA card 0 (playback-only, volume controlled via the `PCM` mixer, not `Headphone`)
- `~/.asoundrc` should map default playback -> `plughw:0,0` and capture -> `plughw:1,0`

Env vars: `OPENAI_API_KEY` (required), `MIC_GAIN` (software mic boost, default `20`), `PUPPER_VOICE` (Realtime API voice, default `marin`).

### How to run

```bash
# Install deps in your venv:
pip install openai sounddevice websockets numpy
sudo apt install -y libportaudio2
export OPENAI_API_KEY=sk-...
# Run it with:
python pupper_voice_chat.py
```

## Reference

<https://openrobot.aidatatools.com/>

<https://jasonchuang.substack.com/p/open-duck-mini-v2-mini-pupper-2-chat>
