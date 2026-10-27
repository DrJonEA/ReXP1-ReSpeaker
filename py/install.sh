#!/usr/bin/env bash
# Set up the voice command environment on the Raspberry Pi.
# Run from this folder:  ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== System packages =="
sudo apt update
sudo apt install -y python3-venv python3-dev build-essential cmake libportaudio2 ffmpeg libusb-1.0-0

echo "== USB access to the reSpeaker for direction of arrival (plugdev group) =="
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2886", GROUP="plugdev", MODE="0660"' \
    | sudo tee /etc/udev/rules.d/99-respeaker.rules > /dev/null
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG plugdev "$USER"

echo "== Python virtual environment (.venv) =="
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel

echo "== Python packages (pywhispercpp may compile from source - takes a few minutes) =="
pip install -r requirements.txt
pip install --no-deps "openwakeword>=0.6"

echo "== Downloading openWakeWord models =="
python -c "import openwakeword.utils as u; u.download_models()"

echo "== Pre-fetching whisper.cpp model (base.en) =="
python -c "from pywhispercpp.model import Model; Model('base.en', print_realtime=False, print_progress=False)"

echo
echo "Done. To run:"
echo "  source .venv/bin/activate"
echo "  python voice_cmd.py"
