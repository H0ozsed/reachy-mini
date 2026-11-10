#!/usr/bin/env bash
# Installe le SDK Reachy Mini dans .venv (Ubuntu 24.04).
set -euo pipefail
cd "$(dirname "$0")"

# 1. Paquets système : GStreamer (caméra/micro/son) + PyGObject.
#    Demande ton mot de passe sudo.
sudo apt install -y python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav

# 2. Accès au port série USB (Reachy Mini Lite). Déconnexion/reconnexion nécessaire après.
sudo usermod -aG dialout "$USER"

# 3. venv qui réutilise le PyGObject du système (évite de le compiler).
uv venv --clear --python /usr/bin/python3 --system-site-packages .venv
echo 'pygobject; sys_platform == "never"' > overrides.txt
uv pip install --python .venv/bin/python --override overrides.txt "reachy-mini[mujoco]" faster-whisper piper-tts httpx

echo "OK. Lance : .venv/bin/reachy-mini-daemon --sim   (simulation)"
echo "       ou : .venv/bin/reachy-mini-daemon         (robot branché en USB)"
