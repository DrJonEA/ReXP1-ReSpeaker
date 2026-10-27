#!/usr/bin/env python3
"""
Wake word -> whisper.cpp -> action dispatcher, for Raspberry Pi 5 + reSpeaker Flex.

Pipeline:
  1. openWakeWord listens continuously (low CPU) for the wake word.
  2. On detection: report the speaker's direction (reSpeaker DOA over USB),
     astromech chirp, then record until the speaker goes quiet.
  3. whisper.cpp (via pywhispercpp) transcribes the recording.
  4. The text is matched against COMMANDS (regex -> shell command or Pico message) and the
     first match runs.

The Pico 2 firmware (../firmware) is sent one JSON line per state change over USB serial:
  {"mode":"listen","deg":180}   wake word heard, deg = speaker direction (omitted if unknown)
  {"mode":"think"}              transcribing / processing the command
  {"mode":"on"} / {"mode":"off"}  light on or off, also sent to end "think" for other commands

Setup (in a venv):
  sudo apt install python3-venv python3-dev build-essential cmake libportaudio2
  python3 -m venv ~/voice && source ~/voice/bin/activate
  pip install numpy sounddevice openwakeword pywhispercpp ttastromech pyusb pyserial
  python -c "import openwakeword.utils as u; u.download_models()"

Run:
  python voice_cmd.py            # records from INPUT_DEVICE (the Flex's PipeWire source)
  python voice_cmd.py --list     # list audio devices ('>' marks default input, '<' default output)
  python voice_cmd.py --debug    # show live mic level and wake score
  python voice_cmd.py --pico /dev/ttyACM0   # Pico serial port (default: auto-detect, 'none' to disable)
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from openwakeword.model import Model as WakeModel
from pywhispercpp.model import Model as WhisperModel

try:
    from ttastromech import TTAstromech
except ImportError:             # fall back to plain beeps
    TTAstromech = None

try:
    import serial
    import serial.tools.list_ports
except ImportError:             # pyserial not installed - no Pico
    serial = None

# Direction of arrival comes from Seeed's USB control example in the lib submodule.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib/reSpeaker_Flex/python_control"))
try:
    import respeaker_get_doa as respeaker
except ImportError:             # pyusb not installed, or submodule not checked out
    respeaker = None

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
#WAKE_WORD = "hey_jarvis"        # built-in openWakeWord model; or a path to your own .onnx
#WAKE_WORD = "../models/Hey_RexP_20261023_091505.onnx"
WAKE_WORD = "../models/Hey_RexP_rung1_full_20261023_091506.onnx"
WAKE_THRESHOLD = 0.5            # raise if you get false triggers, lower if it misses you
WHISPER_MODEL = "base.en"       # "tiny.en" = faster, "base.en" = more accurate, "small.en" = slow on a Pi
WHISPER_THREADS = 4
# whisper.cpp pads every clip to 30 s (audio_ctx 1500) before encoding, so a 2 s
# command costs as much as a 30 s one. Each unit is 20 ms; 384 = 7.7 s, which must
# cover MAX_COMMAND_SECONDS. Smaller is faster; 0 restores the full 30 s window.
WHISPER_AUDIO_CTX = 384

# Input device: part of a device name, or an index. The PipeWire source for the
# Flex works; ALSA "default" (via the pipewire-alsa bridge) delivers audio that the
# wake model never fires on. Falls back to the system default if not found.
INPUT_DEVICE = "alsa_input.usb-Seeed_Studio_reSpeaker_Flex"

# Serial port of the Pico 2 running ../firmware. None = first USB device with the
# Raspberry Pi vendor ID.
PICO_PORT = None
PICO_VID = 0x2E8A

RATE = 16000                    # both openWakeWord and Whisper want 16 kHz mono
CHUNK = 1280                    # 80 ms - the frame size openWakeWord expects

MAX_COMMAND_SECONDS = 6.0       # hard limit on how long a command can be
SILENCE_SECONDS = 0.8           # stop recording after this much quiet
SILENCE_RMS = 300               # int16 RMS below this counts as quiet; tune for your room
MIN_SPEECH_SECONDS = 0.3

# Astromech feedback sounds - each letter is a syllable. d-p are short (~80 ms),
# a/b/c are long (~0.6 s), so keep the wake chirp short or it delays recording.
WAKE_CHIRP = "deki"             # played after the wake word, before recording
DONE_CHIRP = "tuv"              # played when recording stops

# Regex (matched against lower-cased text, punctuation stripped) -> command to run.
# Use a list for the command (no shell), a string to run through the shell, or a
# dict to send to the Pico as JSON.
COMMANDS = [
    (r"\b(lights?|lamp) on\b",          {"mode": "on"}),
    (r"\b(lights?|lamp) off\b",         {"mode": "off"}),
    (r"\bwhat time is it\b|\bthe time\b", "date '+%H:%M'"),
    (r"\b(volume|turn it) up\b",        ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "10%+"]),
    (r"\b(volume|turn it) down\b",      ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "10%-"]),
    (r"\breboot\b",                     ["echo", "TODO: confirm before rebooting"]),
    (r"\battention\b",                     ["echo", "TODO: Todo Stand at Attention "]),
]

# Words to bias Whisper towards - list the words your commands use.
WHISPER_PROMPT = "lights on, lights off, lamp, volume up, volume down, what time is it, reboot."


# ----------------------------------------------------------------------------
# Audio helpers
# ----------------------------------------------------------------------------
def beep(freq=880, dur=0.12, volume=0.3, blocking=True):
    t = np.linspace(0, dur, int(RATE * dur), endpoint=False)
    tone = (volume * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    try:
        sd.play(tone, RATE, blocking=blocking)
    except Exception as e:  # never let feedback sounds kill the loop
        print(f"[beep failed: {e}]", file=sys.stderr)


class Chirper:
    """Astromech sounds via ttastromech's syllables, played through sounddevice
    (ttastromech's own speak() shells out to aplay, which may pick a different device)."""
    RATE = 22050                # ttastromech's wavs are 22.05 kHz mono int16

    def __init__(self):
        self.r2 = TTAstromech() if TTAstromech else None

    def chirp(self, phrase, fallback_freq, blocking=True):
        if self.r2 is None:
            beep(fallback_freq, blocking=blocking)
            return
        syl = self.r2.syllabary
        data, prev = b"", ""
        for ltr in phrase.lower():
            if not ltr.isalpha():
                continue
            # repeated letters use the alternate "1" syllable if there is one
            data += syl.get(ltr + "1", syl[ltr]) if ltr == prev else syl[ltr]
            prev = ltr
        audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            sd.play(audio, self.RATE, blocking=blocking)
        except Exception as e:  # never let feedback sounds kill the loop
            print(f"[chirp failed: {e}]", file=sys.stderr)


def open_respeaker():
    """Open the Flex's USB control interface for DOA readings, or None if unavailable."""
    if respeaker is None:
        print("DOA disabled: pyusb or lib/reSpeaker_Flex not available")
        return None
    try:
        dev = respeaker.find()
    except Exception as e:
        print(f"DOA disabled: {e}")
        return None
    if dev is None:
        print("DOA disabled: reSpeaker not found on USB")
        return None
    if read_doa(dev) is None:
        print("DOA disabled: can't read from the reSpeaker over USB "
              "(usually a permission problem - see 'Speaker direction' in setup.md)")
        return None
    return dev


def read_doa(dev):
    """Returns (angle 0-359 degrees, speech detected) or None."""
    if dev is None:
        return None
    try:
        r = dev.read("DOA_VALUE")   # [status, angle lo, angle hi, speech, ...]
    except Exception as e:          # never let DOA kill the loop
        print(f"[DOA read failed: {e}]", file=sys.stderr)
        return None
    angle = r[1] + r[2] * 256
    # The reSpeaker measures the opposite way round to the LED ring, so invert it.
    return (360 - angle) % 360, bool(r[3])


class Pico:
    """JSON lines to the Pico 2 firmware over USB serial. Never raises: if the Pico is
    missing or unplugged, messages are dropped and the port is reopened on the next send."""

    def __init__(self, port=None, debug=False):
        self.port, self.debug = port, debug
        self.ser = None
        self.light = {"mode": "off"}   # last light state; the firmware starts off
        if serial is None:
            print("Pico disabled: pyserial not installed")
            self.port = "none"
        elif self.open():
            print(f"Pico on {self.ser.port}")
        else:
            print("Pico not found - will keep trying")

    def find(self):
        if self.port:
            return self.port
        for p in serial.tools.list_ports.comports():
            if p.vid == PICO_VID:
                return p.device
        return None

    def open(self):
        port = self.find()
        if port is None:
            return False
        try:
            self.ser = serial.Serial(port, 115200, timeout=0, write_timeout=1)
        except Exception as e:
            print(f"[Pico open {port} failed: {e}]", file=sys.stderr)
            self.ser = None
        return self.ser is not None

    def send(self, msg: dict):
        if self.port == "none" or (self.ser is None and not self.open()):
            return
        if msg.get("mode") in ("on", "off"):
            self.light = msg
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        try:
            # Drain what the firmware prints so its USB output never backs up.
            pending = self.ser.read(self.ser.in_waiting or 0)
            if self.debug and pending:
                print(f"  pico> {pending.decode(errors='replace').strip()}")
            self.ser.write(line.encode())
            if self.debug:
                print(f"  pico< {line.strip()}")
        except Exception as e:
            print(f"[Pico write failed: {e}]", file=sys.stderr)
            self.ser.close()
            self.ser = None

    def restore_light(self):
        """Leave "think" mode by re-sending the current light state."""
        self.send(self.light)


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))


def record_command(stream):
    """Read from the open stream until silence or the time limit.
    Returns (int16 audio, info) where info holds the speech timing within the clip, in seconds."""
    frames, quiet_time, speech_time, total = [], 0.0, 0.0, 0.0
    first_speech = None
    block_s = CHUNK / RATE
    while total < MAX_COMMAND_SECONDS:
        block, _ = stream.read(CHUNK)
        block = block[:, 0]
        frames.append(block.copy())
        if rms(block) >= SILENCE_RMS:
            if first_speech is None:
                first_speech = total
            speech_time += block_s
            quiet_time = 0.0
        else:
            quiet_time += block_s
        total += block_s
        if speech_time >= MIN_SPEECH_SECONDS and quiet_time >= SILENCE_SECONDS:
            break
    audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
    info = {"duration": total, "speech": speech_time, "trailing_quiet": quiet_time,
            "first_speech": first_speech, "backlog": stream.read_available / RATE}
    return audio, info


class Timings:
    """Per-command timing breakdown plus running stats across all commands."""

    def __init__(self):
        self.history = {}           # metric -> list of ms values, one per command

    def report(self, t, rec, input_latency):
        ms = lambda a, b: (t[b] - t[a]) * 1000
        # The last block read is (almost) live, so end of speech was trailing_quiet
        # seconds of audio plus the input buffer latency before recording stopped.
        # Any backlog still queued means the clip ended that much earlier than "now".
        eos_to_rec_end = (rec["trailing_quiet"] + input_latency + rec["backlog"]) * 1000
        post_rec = ms("rec_end", "think")
        whisper = ms("think", "transcribed")
        norm = ms("transcribed", "text")
        latency = eos_to_rec_end + ms("rec_end", "text")
        rows = [
            ("wake predict (trigger frame)", ms("predict", "wake")),
            ("DOA read", ms("wake", "doa")),
            ("wake chirp + pico listen", ms("doa", "chirped")),
            ("recording", ms("chirped", "rec_end")),
            ("done chirp + pico think", post_rec),
            ("whisper transcribe", whisper),
            ("normalise", norm),
            ("dispatch + pico restore", ms("text", "dispatched")),
            ("drain buffered audio", ms("dispatched", "drained")),
            ("total (wake -> listening)", ms("wake", "drained")),
        ]
        audio_s = rec["duration"]
        first = rec["first_speech"]
        print("  timings:")
        for name, v in rows:
            print(f"    {name:<30}{v:8.0f} ms")
        print(f"    {'':<30}clip {audio_s * 1000:.0f} ms: speech {rec['speech'] * 1000:.0f} ms, "
              f"first speech at {'-' if first is None else f'{first * 1000:.0f}'} ms, "
              f"trailing quiet {rec['trailing_quiet'] * 1000:.0f} ms")
        if audio_s:
            print(f"    {'':<30}whisper RTF {whisper / 1000 / audio_s:.2f} (transcribe time / clip length)")
        print(f"  latency (end of speech -> text): {latency:.0f} ms")
        print(f"    = silence wait {rec['trailing_quiet'] * 1000:.0f} + input buffer {input_latency * 1000:.0f}"
              f" + backlog {rec['backlog'] * 1000:.0f} + done chirp/pico {post_rec:.0f}"
              f" + whisper {whisper:.0f} + normalise {norm:.0f} ms")
        print(f"  latency (end of speech -> action done): {latency + ms('text', 'dispatched'):.0f} ms")

        for key, v in (("latency", latency), ("whisper", whisper),
                       ("recording", ms("chirped", "rec_end")), ("total", ms("wake", "drained"))):
            self.history.setdefault(key, []).append(v)
        n = len(self.history["latency"])
        print(f"  stats over {n} command{'s' if n != 1 else ''} (ms):")
        print(f"    {'':<30}{'min':>7}{'mean':>7}{'median':>7}{'max':>7}")
        for key, vals in self.history.items():
            print(f"    {key:<30}{min(vals):7.0f}{statistics.mean(vals):7.0f}"
                  f"{statistics.median(vals):7.0f}{max(vals):7.0f}")


# ----------------------------------------------------------------------------
# Command dispatch
# ----------------------------------------------------------------------------
def normalise(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\[.*?\]|\(.*?\)", " ", text)     # drop [BLANK_AUDIO], (music) etc.
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def dispatch(text: str, pico: Pico) -> bool:
    for pattern, cmd in COMMANDS:
        if re.search(pattern, text):
            if isinstance(cmd, dict):
                print(f"  -> match {pattern!r}: sending {cmd} to Pico")
                pico.send(cmd)
                return True
            print(f"  -> match {pattern!r}: running {cmd}")
            try:
                result = subprocess.run(cmd, shell=isinstance(cmd, str),
                                        capture_output=True, text=True, timeout=30)
                if result.stdout.strip():
                    print(f"     {result.stdout.strip()}")
                if result.returncode != 0:
                    print(f"     exit {result.returncode}: {result.stderr.strip()}")
            except Exception as e:
                print(f"     failed: {e}")
            return True
    return False


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list audio devices and exit")
    ap.add_argument("--device", default=INPUT_DEVICE, help=f"input device name/index (default: {INPUT_DEVICE})")
    ap.add_argument("--debug", action="store_true", help="print live mic level and wake score")
    ap.add_argument("--pico", default=PICO_PORT,
                    help="Pico serial port (default: auto-detect, 'none' to disable)")
    args = ap.parse_args()
    if args.device is not None and args.device.isdigit():
        args.device = int(args.device)   # sounddevice treats a str as a name match

    if args.list:
        print(sd.query_devices())
        return

    try:
        sd.query_devices(args.device, "input")
    except ValueError as e:
        print(f"Input device {args.device!r} not found ({e}) - using system default")
        args.device = None

    print("Loading wake word model...")
    wake = WakeModel(wakeword_models=[WAKE_WORD], inference_framework="onnx")
    wake_key = list(wake.models.keys())[0]

    chirper = Chirper()
    if chirper.r2 is None:
        print("ttastromech not installed - using beeps")

    doa_dev = open_respeaker()
    pico = Pico(args.pico, debug=args.debug)
    pico.restore_light()

    print(f"Loading whisper.cpp model {WHISPER_MODEL}...")
    whisper = WhisperModel(WHISPER_MODEL, n_threads=WHISPER_THREADS,
                           print_realtime=False, print_progress=False,
                           audio_ctx=WHISPER_AUDIO_CTX,
                           single_segment=True, no_timestamps=True, max_tokens=32,
                           initial_prompt=WHISPER_PROMPT, language="en")

    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                        blocksize=CHUNK, device=args.device) as stream:
        dev = sd.query_devices(args.device, "input")
        print(f"Input device: [{dev['index']}] {dev['name']}")
        print(f"Listening for '{wake_key}'... (Ctrl+C to quit)")
        input_latency = stream.latency    # seconds of audio buffered in the input path
        timings = Timings()
        peak_rms, peak_score, n = 0.0, 0.0, 0
        while True:
            block, overflowed = stream.read(CHUNK)
            t = {"predict": time.perf_counter()}
            scores = wake.predict(block[:, 0])
            if args.debug:
                # one line every ~0.5 s with the loudest level and best score seen
                peak_rms = max(peak_rms, rms(block[:, 0]))
                peak_score = max(peak_score, scores.get(wake_key, 0))
                n += 1
                if n == 6:
                    print(f"  level {peak_rms:6.0f}  score {peak_score:.2f}"
                          f"{'  OVERFLOW' if overflowed else ''}")
                    peak_rms, peak_score, n = 0.0, 0.0, 0
            if scores.get(wake_key, 0) < WAKE_THRESHOLD:
                continue
            t["wake"] = time.perf_counter()

            doa = read_doa(doa_dev)    # before the chirp, so we get the speaker not ourselves
            t["doa"] = time.perf_counter()
            where = f" from {doa[0]}\N{DEGREE SIGN}" if doa else " (direction unavailable)"
            print(f"\nWake word detected ({scores[wake_key]:.2f}){where}")
            pico.send({"mode": "listen", "deg": doa[0]} if doa else {"mode": "listen"})
            chirper.chirp(WAKE_CHIRP, 880)
            t["chirped"] = time.perf_counter()
            audio, rec = record_command(stream)
            t["rec_end"] = time.perf_counter()
            chirper.chirp(DONE_CHIRP, 660, blocking=False)   # plays while we transcribe
            pico.send({"mode": "think"})
            t["think"] = time.perf_counter()

            segments = whisper.transcribe(audio.astype(np.float32) / 32768.0)
            t["transcribed"] = time.perf_counter()
            text = normalise(" ".join(s.text for s in segments))
            t["text"] = time.perf_counter()
            print(f"  heard: '{text}'  ({len(audio) / RATE:.1f}s audio)")

            light = pico.light
            if text and not dispatch(text, pico):
                print("  -> no matching command")
            if pico.light is light:     # no light command - leave "think" mode
                pico.restore_light()
            t["dispatched"] = time.perf_counter()

            # Clear wake-word state and drain audio buffered while we were busy,
            # so the chirp or the command itself doesn't re-trigger.
            wake.reset()
            while stream.read_available >= CHUNK:
                stream.read(CHUNK)
            t["drained"] = time.perf_counter()
            timings.report(t, rec, input_latency)
            print(f"Listening for '{wake_key}'...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")
