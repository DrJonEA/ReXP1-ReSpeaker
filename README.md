# ReXP1-ReSpeaker
Early work to bring Seeed's reSpeaker Flex to ReXP1 control. A Raspberry Pi 5 listens for a wake word, works out which direction the speaker is in, transcribes the spoken command with whisper.cpp and acts on it. A Raspberry Pi Pico 2 drives an LED ring that points at whoever is speaking and shows what the voice system is doing.

The project has two parts:
- `py`: the Python voice command system, running on the Pi 5
- `firmware`: the Pico 2 firmware for the LED ring

## Cloning and Building
This is a Pico SDK based project in C++. Where libraries are included they are normally included as submodules. When cloning please recurse submodules.

I have a blog that explains how to clone repos and build my projects on
[drjonea.co.uk](https://drjonea.co.uk/2025/12/15/building-my-projects-from-repo/).

The firmware target is `WB2812B`, built from the `firmware` folder.

The Python side has its own install script. See [py/setup.md](py/setup.md).

## BOM

- Voice brain: Raspberry Pi 5, running Ubuntu 26.04
- Microphone array: [Seeed reSpeaker Flex](https://www.seeedstudio.com/reSpeaker-Flex-XVF3800-Circular-4-with-XIAO-ESP32S3-p-6739.html?sensecap_affiliate=6mdKM3D&referring_service=link) (XMOS XVF3800), USB firmware version, on USB-C to the Pi
- [Speeed Grove Speaker Plus](https://www.seeedstudio.com/Grove-Speaker-Plus-p-4592.html?sensecap_affiliate=6mdKM3D&referring_service=link)
- LED brain: Raspberry Pi Pico 2, on USB to the Pi
- LEDs: 24 LED WS2812B ring on GP18

## Voice Command System (py)
`voice_cmd.py` runs a wake word, transcribe and act loop:

1. [openWakeWord](https://github.com/dscripka/openWakeWord) listens for the wake word "Hey RexP". The custom models are in the `models` folder.
2. On the wake word, the direction of arrival (DOA) is read from the reSpeaker over USB and an astromech chirp is played. Recording then runs until the speaker goes quiet.
3. [whisper.cpp](https://github.com/ggerganov/whisper.cpp) (via pywhispercpp) transcribes the recording.
4. The text is matched against a list of regular expressions in `COMMANDS`. The first match runs a shell command or sends a message to the Pico.

Commands include lights on and off, the time, volume up and down. New commands are added as `(regex, command)` entries in `COMMANDS`.

[py/setup.md](py/setup.md) covers getting the reSpeaker working with PipeWire, installing, running and tuning.

## LED Ring (firmware)
The Pico 2 reads one JSON message per line over USB serial (stdio) and shows the state of the voice system on the ring:

| Message | Ring |
|---|---|
| `{"mode":"listen","deg":180}` | Wake word heard. One green LED points to the speaker. `deg` is optional |
| `{"mode":"think"}` | Transcribing and processing. Three blue LEDs turn round the ring |
| `{"mode":"on"}` | Light on. The whole ring is white |
| `{"mode":"off"}` | Light off. The ring is dark |

`voice_cmd.py` finds the Pico automatically by its USB vendor ID. After a command that isn't for the light, it sends the current light state again so the ring leaves "think".

You can test the firmware by hand from a serial terminal by typing the messages.

## Folders
- `firmware`: the Pico 2 LED ring firmware
- `py`: the Python voice command system and its setup notes
- `models`: custom openWakeWord models for "Hey RexP"
- `lib`: libraries as submodules: [reSpeaker_Flex](https://github.com/respeaker/reSpeaker_Flex) (USB control and DOA), [PicoLED](https://github.com/ForsakenNGS/PicoLED) and [tiny-json](https://github.com/rafagafe/tiny-json)
