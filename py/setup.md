# reSpeaker Flex on Raspberry Pi 5 (Ubuntu 26.04) – Setup

Setup for using a Seeed reSpeaker Flex (XMOS XVF3800) as both speaker and microphone on a Raspberry Pi 5 running Ubuntu 26.04 with PipeWire. Then a wake word → whisper.cpp → action pipeline (`voice_cmd.py`) runs on top of it.

## 1. Hardware

- Connect the reSpeaker Flex to the Pi with a USB-C cable. The non-XIAO versions ship with USB firmware, which is what this setup uses. The XIAO ESP32S3 versions ship with I2S firmware.
- The Flex has no built-in speaker. Plug headphones or **powered** speakers into the audio output connector on the core board.
- The mic port is on the side with the Seeed Studio logo printed on it. Point that side towards the speaker.
- Use the official 5A Pi 5 power supply. USB audio devices can misbehave when power is marginal.

## 2. Check the device is detected

```bash
aplay -l                     # playback devices – the Flex should appear as a USB card
arecord -l                   # capture devices
pactl list short sinks       # PipeWire outputs
pactl list short sources     # PipeWire inputs
```

If the sink is listed but you hear nothing, the problem is almost always the card profile (step 3).

## 3. Switch the card to the analog duplex profile (the key fix)

By default PipeWire picked the **digital S/PDIF** profile (`output:iec958-stereo+input:iec958-stereo`), which sends audio somewhere that doesn't reach the analog output.

Find the card name:

```bash
pactl list short cards
```

The second column is the name, something like `alsa_card.usb-Seeed_..._XVF3800...-00`. The index number in the first column also works.

Check the available profiles and the active one:

```bash
pactl list cards | grep -i -E "output:|Active Profile"
```

Set the analog duplex profile:

```bash
pactl set-card-profile <card-name-or-index> output:analog-stereo+input:analog-stereo
```

WirePlumber normally remembers this choice across reboots.

## 4. Set the Flex as default output and unmute

Changing the profile creates new sink and source names, so list them again:

```bash
pactl list short sinks
pactl list short sources
```

```bash
pactl set-default-sink   <alsa_output...analog-stereo>
pactl set-sink-mute      <alsa_output...analog-stereo> 0
pactl set-sink-volume    <alsa_output...analog-stereo> 100%
```

Test playback:

```bash
pw-play /usr/share/sounds/alsa/Front_Center.wav
pw-top            # in a second terminal: the Flex node should show activity
```

## 5. Set up and test the microphone

Use the `alsa_input...analog-stereo` source, **not** the sink's `.monitor`.

```bash
pactl set-default-source <alsa_input...analog-stereo>
pactl set-source-mute    <alsa_input...analog-stereo> 0
pactl set-source-volume  <alsa_input...analog-stereo> 100%
```

Record a few seconds and play it back:

```bash
pw-record --rate 16000 --channels 2 test.wav    # speak, then Ctrl+C
pw-play test.wav
```

For live input levels, use `pw-top`, or `sudo apt install pavucontrol` and open its **Input Devices** tab.

You can do the same with `wpctl`, which shows whether the default is saved:

```bash
wpctl status            # under Audio -> Sources, '*' marks the current default
wpctl set-default 55    # the ID of "reSpeaker Flex XVF3800 C48K2Ch Analog Stereo" under Sources
```

Use the ID listed under **Sources**, not the one under **Sinks**. IDs can differ between machines. A `*` alone only means PipeWire picked that device automatically. After `set-default`, the source also appears under **Settings → Default Configured Devices** as `Audio/Source`, which means the choice is saved across reboots.

Notes:
- The XVF3800 does echo cancellation, noise suppression and beamforming on the chip. The two channels can be different processing outputs. If one sounds better, use that one. To split a recording: `sox test.wav left.wav remix 1` and `sox test.wav right.wav remix 2`.
- Because echo cancellation runs on the chip, you can play and record at the same time.

## 6. Troubleshooting (bypass PipeWire)

If PipeWire gives silence, test ALSA directly to find out whether the fault is in the hardware or in PipeWire:

```bash
systemctl --user stop pipewire pipewire-pulse wireplumber

# Playback
speaker-test -D plughw:CARD=<name>,DEV=0 -c 2 -r 48000 -t sine

# Capture
arecord -D plughw:CARD=<name>,DEV=0 -f S16_LE -r 16000 -c 2 -d 5 raw.wav

systemctl --user start pipewire pipewire-pulse wireplumber
pw-play raw.wav
```

- **ALSA works but PipeWire doesn't:** recheck the profile (step 3) and the defaults (steps 4 and 5).
- **ALSA is silent too:** run `alsamixer -c <card>` (F6 selects the card, F4 shows capture controls) and look for muted (`MM`) or zero controls. Then check `dmesg | tail -30` for USB errors.
- **Still quiet when everything is unmuted:** the XVF3800 has its own output gain, which you can set with Seeed's `xvf_host` tool (see the Seeed wiki).

## 7. Voice command software

The files in this folder:

| File | Purpose |
|---|---|
| `voice_cmd.py` | Listens for a wake word (openWakeWord), reports the speaker's direction, records the command, transcribes it with whisper.cpp (pywhispercpp), and runs the matching action |
| `requirements.txt` | Python dependencies |
| `install.sh` | Installs system packages, creates `.venv`, installs requirements, downloads the models |

Install:

```bash
cd <this folder>
bash install.sh
```

`install.sh` installs `openwakeword` with `--no-deps`. Otherwise openwakeword pulls in `tflite-runtime`, which has no wheels for recent Python versions. The script uses the ONNX backend instead. `pywhispercpp` may compile from source, which takes a few minutes.

`install.sh` creates and fills its own venv at `py/.venv`. It doesn't change the venv your terminal is using. If another venv was active when you ran it, that venv stays active afterwards. Running `voice_cmd.py` then fails with `ModuleNotFoundError: No module named 'openwakeword'`, because openwakeword is installed separately and only into `py/.venv`. Run `deactivate`, then activate `py/.venv` as shown below.

Run:

```bash
deactivate 2>/dev/null         # leave any other venv first
source .venv/bin/activate
python voice_cmd.py            # records from the Flex's PipeWire source (INPUT_DEVICE)
python voice_cmd.py --list     # list audio devices
python voice_cmd.py --debug    # show live mic level and wake score
```

Say the wake word, wait for the astromech chirp, then speak the command. `WAKE_WORD` at the top of `voice_cmd.py` chooses the model. It can be a built-in openWakeWord model such as `hey_jarvis`, or one of the custom models in `../models/`.

### Troubleshooting: no wake word detection

The Flex's input appears several times in `python voice_cmd.py --list`:

| Device | What it is |
|---|---|
| `reSpeaker Flex ... (hw:0,0)` | Raw ALSA hardware. It only runs at 48 kHz, so the script can't open it at 16 kHz |
| `default` (128 in) | The PipeWire ALSA bridge. It follows PipeWire's default source |
| `alsa_output...analog-stereo.monitor` | A loopback of the speaker output, not the mic |
| `alsa_input.usb-Seeed_Studio_reSpeaker_Flex...analog-stereo` | **The mic through PipeWire. Use this one** |

Recording through ALSA `default` picks up sound, but the wake word never triggers, even with the Flex set as the default source. So `INPUT_DEVICE` in `voice_cmd.py` opens the `alsa_input...` source by name. It falls back to the system default if that source isn't found. The startup line `Input device: [n] ...` shows which device was opened. `--device` overrides it with part of a device name or an index from `--list`. Use a name rather than an index, because indexes can change after a reboot.

To check whether audio is reaching the wake model, run `python voice_cmd.py --debug`. It prints the loudest level and the best wake score about twice a second:

| What you see | Meaning |
|---|---|
| Level stays near 0–50 while you talk | Wrong device, or the mic is muted |
| Level rises when you talk, score stays near 0 | Audio arrives, but the model doesn't recognise it. Check the device (see above), speak clearly, and check that the wake phrase matches `WAKE_WORD` |
| Score peaks at 0.2–0.4 | Lower `WAKE_THRESHOLD` |
| `OVERFLOW` | Audio frames are being dropped |

Normal speech reads about 500–5000. Use the level readings to set `SILENCE_RMS`.

### Speaker direction

When the wake word triggers, the script reads the reSpeaker's direction of arrival (DOA) over USB and prints it, for example `Wake word detected (0.87) from 135°`. The angle is 0–359°, measured around the mic array. It uses Seeed's `respeaker_get_doa.py` from `lib/reSpeaker_Flex/python_control`, so check out the submodule first: `git submodule update --init`.

USB control needs permission. `install.sh` adds a udev rule that gives the `plugdev` group access to Seeed devices (vendor ID `2886`) and adds you to that group. Log out and back in, or unplug and replug the Flex, for it to take effect. If you see `DOA disabled` or `Access denied` at startup, the permission isn't active yet. The rest of the script still works without DOA.

### Pico 2 LED ring

The script sends the Pico 2 firmware in `../firmware` one JSON line per state change over USB serial:

| When | Message |
|---|---|
| Wake word heard | `{"mode":"listen","deg":180}`, where `deg` is the speaker direction. It is left out if DOA is unavailable |
| Command recorded, transcribing | `{"mode":"think"}` |
| "Lights on" / "lights off" | `{"mode":"on"}` / `{"mode":"off"}` |
| Any other command, or nothing heard | The current light state again, to end `think` |

The Pico is found automatically by its USB vendor ID (`2E8A`). Use `--pico /dev/ttyACM0` to pick a port, or `--pico none` to disable it. If the Pico isn't connected, the script carries on and tries again on the next message. Opening the port needs the `dialout` group, which `install.sh` adds you to (log out and back in). `--debug` shows the messages sent (`pico<`) and what the firmware prints (`pico>`).

### Tuning (constants at the top of `voice_cmd.py`)

| Setting | Effect |
|---|---|
| `WAKE_THRESHOLD` | Raise it if the wake word triggers by mistake, lower it if it misses you |
| `WHISPER_MODEL` | `tiny.en` is faster, `base.en` is the default, `small.en` is slow on a Pi |
| `WHISPER_AUDIO_CTX` | How much audio Whisper encodes, in 20 ms units (384 is about 7.7 s). Smaller is faster, but it must cover `MAX_COMMAND_SECONDS`. 0 restores the full 30 s window |
| `SILENCE_RMS` | Lower it if recording cuts off early, raise it if recording never stops |
| `SILENCE_SECONDS`, `MAX_COMMAND_SECONDS` | How long to wait for quiet, and the hard limit on command length |

### Adding custom actions

Add a `(regex, command)` entry to `COMMANDS`. The regex is matched against the lower-cased transcript with punctuation removed. The first match wins, so put specific patterns first.

```python
COMMANDS = [
    (r"\battention\b", ["/home/pi/scripts/attention.sh"]),          # list: runs directly, no shell
    (r"\bding\b",      "aplay /home/pi/sounds/ding.wav && echo ok"), # string: runs via the shell
    ...
]
```

Also add the new words to `WHISPER_PROMPT`. This matters most for single-word commands, which Whisper can otherwise mishear.

The script prints `heard: '...'` each time. If a command doesn't fire, compare that text with your regex. Commands time out after 30 seconds. For longer tasks, run them in the background.
