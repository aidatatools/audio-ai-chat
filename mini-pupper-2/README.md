# Mini Pupper 2 voice chat

Talk to Mini Pupper 2 through the OpenAI **Realtime API** (`gpt-realtime-2.1`). Audio streams both ways, and you can **interrupt Pupper while it's talking** (barge-in).

Use [pupper2_voice_chat_v2.py](pupper2_voice_chat_v2.py). The older scripts ([pupper2_voice_chat.py](pupper2_voice_chat.py) and [pupper_voice_chat.py](pupper_voice_chat.py)) are kept only for reference.

- OS: Ubuntu 24.04 (arm64)
- Board: Raspberry Pi 4B 2GB RAM
- Mics: I2S voiceHAT, ALSA card 1 (capture only, very quiet, so it gets a software boost)
- Speaker: bcm2835 PWM, ALSA card 0 (playback only, `PCM` mixer)

## How to run

### 1. System packages

```bash
sudo apt update
sudo apt install -y python3-venv libportaudio2 alsa-utils
```

`libportaudio2` is needed by `sounddevice`. `alsa-utils` provides `amixer`, which the script uses to set the speaker volume.

### 2. ALSA defaults

Create `~/.asoundrc` so the default playback device is card 0 and the default capture device is card 1:

```
pcm.!default {
    type asym
    playback.pcm "plughw:0,0"
    capture.pcm  "plughw:1,0"
}
ctl.!default {
    type hw
    card 0
}
```

Check the cards with `aplay -l` and `arecord -l`.

### 3. Python environment

Ubuntu 24.04 blocks system-wide `pip install`, so use a venv:

```bash
cd ~/audio-ai-chat/mini-pupper-2
python3 -m venv .venv
source .venv/bin/activate
pip install numpy sounddevice "websockets>=14"
```

The script calls `websockets.connect(..., additional_headers=...)`, which older `websockets` releases don't support.

### 4. API key

```bash
export OPENAI_API_KEY=sk-...
```

To avoid retyping it, put your `export` lines in [env.sh](env.sh) (don't commit it) and run `source env.sh`.

### 5. Check audio devices (optional)

```bash
python pupper2_voice_chat_v2.py --list
```

This prints the `sounddevice` device list and exits. You only need it if the defaults pick the wrong device (see `MIC_DEVICE` / `SPK_DEVICE` below).

### 6. Run

```bash
python pupper2_voice_chat_v2.py
```

At startup the script shows the devices and sample rates it chose, then starts listening:

```
Mic:     default @ 48000 Hz, 1 ch -> resampled to 24000
Speaker: default @ 48000 Hz, 1 ch <- resampled from 24000, volume 80%
Mini Pupper listening — half-duplex + local barge-in (ratio 3.0, min 0.02, 2 blocks). Ctrl+C to stop.
```

Start talking. Transcripts print as `You: ...` / `Pupper: ...`. To interrupt Pupper, just talk over it; `[barge-in]` is printed when an interruption is detected. Press **Ctrl+C** to quit.

Examples with overrides:

```bash
# quieter speaker, which makes barge-in easier
SPK_VOLUME=60 python pupper2_voice_chat_v2.py

# different voice, plain half-duplex (no interrupting)
PUPPER_VOICE=alloy BARGE_IN=0 python pupper2_voice_chat_v2.py
```

## Configuration (environment variables)

| Variable         | Default        | Meaning                                                                               |
| ---------------- | -------------- | ------------------------------------------------------------------------------------- |
| `OPENAI_API_KEY` | —              | **Required.**                                                                         |
| `MIC_GAIN`       | `20`           | Software mic boost. Raise it if Pupper doesn't hear you; lower it if the audio clips. |
| `PUPPER_VOICE`   | `marin`        | Realtime API voice name.                                                              |
| `SPK_VOLUME`     | `80`           | Speaker volume in %. Lower volume means less echo and easier barge-in.                |
| `BARGE_IN`       | `1`            | `1` = you can interrupt Pupper; `0` = plain half-duplex.                              |
| `BARGE_RATIO`    | `3.0`          | How much louder than the expected echo your voice must be (about 10 dB).              |
| `BARGE_MIN_RMS`  | `0.02`         | Minimum mic level that counts as speech.                                              |
| `BARGE_BLOCKS`   | `2`            | Number of consecutive 100 ms loud blocks needed to trigger barge-in.                  |
| `BARGE_DEBUG`    | `0`            | `1` = print mic, echo, and threshold levels while Pupper talks.                       |
| `FULL_DUPLEX`    | `0`            | `1` = no mic gating at all. Use only with headphones or real echo cancellation.       |
| `MIC_DEVICE`     | system default | `sounddevice` index or name substring for the mic.                                    |
| `SPK_DEVICE`     | system default | `sounddevice` index or name substring for the speaker.                                |

## How barge-in works

The speaker sits a few centimetres from the mics, so everything Pupper says is picked up again. Without handling this, Pupper would answer its own voice.

1. While Pupper talks, mic audio is **not sent** to the API, but it is still measured locally.
2. The script knows what the speaker is playing. From the first ~500 ms of each reply, it learns how loud the echo normally is at the mic.
3. If the mic stays louder than that expected echo × `BARGE_RATIO` for `BARGE_BLOCKS` blocks, the sound must be you. The script then:
   - stops local playback right away,
   - sends `response.cancel` to stop generating,
   - sends `conversation.item.truncate`, so the model knows how much of its reply you actually heard,
   - opens the mic and sends the last ~300 ms of buffered audio first, so your first words aren't lost.

Other features: a playback jitter buffer (~300 ms), a 0.6 s echo hangover after each reply, automatic resampling when the hardware doesn't support 24 kHz, and auto-reconnect with backoff (Realtime sessions are capped at 60 minutes).

## Tuning and troubleshooting

- **Pupper interrupts itself.** Raise `BARGE_RATIO` (e.g. `4`) or `BARGE_BLOCKS` (e.g. `3`), or lower `SPK_VOLUME`.
- **Talking over Pupper doesn't interrupt it.** Lower `BARGE_RATIO` (e.g. `2`) or `SPK_VOLUME`, or speak closer to the mics. Run with `BARGE_DEBUG=1` and compare `mic=` against `thr=` while you speak.
- **Pupper never hears you.** Increase `MIC_GAIN`, and check with `arecord -D plughw:1,0 -f S16_LE -r 48000 -d 3 t.wav && aplay t.wav`.
- **`No usable input/output rate`.** Run `--list` and set `MIC_DEVICE` / `SPK_DEVICE` explicitly.
- **No sound from the speaker.** Run `amixer -c 0 sget PCM` and make sure the output is unmuted. The script sets the volume at startup.
- **`Set OPENAI_API_KEY first.`** Export the key in the same shell, or `source env.sh`.
