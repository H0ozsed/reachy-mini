#!/usr/bin/env bash
# Reachy Mini Wireless : le son et la caméra arrivent par WebRTC.
# Il faut l'élément GStreamer « webrtcsrc », à compiler depuis gst-plugins-rs (doc officielle).
# À lancer après install.sh. Demande ton mot de passe sudo. Compilation : 10-20 min.
set -euo pipefail

# 1. Paquets système (liste de la doc Reachy Mini)
sudo apt-get update
sudo apt-get install -y \
  libgstreamer-plugins-bad1.0-dev libgstreamer-plugins-base1.0-dev libgstreamer1.0-dev \
  libglib2.0-dev libssl-dev libgirepository1.0-dev libcairo2-dev libportaudio2 \
  libnice10 gstreamer1.0-nice gstreamer1.0-alsa gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-tools python3-gi python3-gi-cairo pkg-config build-essential

# 2. Rust (installateur officiel rustup.rs)
if ! command -v cargo >/dev/null && [ ! -x "$HOME/.cargo/bin/cargo" ]; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
fi
source "$HOME/.cargo/env"

# 3. Compile le plugin WebRTC (version épinglée par la doc)
SRC="$HOME/.cache/gst-plugins-rs"
[ -d "$SRC" ] || git clone https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git "$SRC"
cd "$SRC"
git fetch --tags -q
git checkout -q 0.14.5
cargo install cargo-c
sudo mkdir -p /opt/gst-plugins-rs
sudo chown "$USER" /opt/gst-plugins-rs
cargo cinstall -p gst-plugin-webrtc --prefix=/opt/gst-plugins-rs --release

# 4. Rend le plugin visible pour GStreamer
LIGNE='export GST_PLUGIN_PATH=/opt/gst-plugins-rs/lib/x86_64-linux-gnu:$GST_PLUGIN_PATH'
grep -qxF "$LIGNE" ~/.bashrc || echo "$LIGNE" >> ~/.bashrc
export GST_PLUGIN_PATH=/opt/gst-plugins-rs/lib/x86_64-linux-gnu

# 5. Vérification
gst-inspect-1.0 webrtcsrc >/dev/null && echo "webrtcsrc OK"
gst-inspect-1.0 nicesrc >/dev/null && echo "nicesrc OK"
