#!/usr/bin/env bash
# Puts the Fedora services and launchers back after a reinstall: ./deploy/instalar.sh
# Needs, outside the repo: whisper.cpp at ~/.local/opt/whisper.cpp/build-vk,
# models in ~/.local/share/whisper-models, Piper at ~/.local/opt/piper, and .venv (onnxruntime numpy).
set -euo pipefail
d=$(dirname "$(readlink -f "$0")")
install -Dm644 -t ~/.config/systemd/user "$d"/{whisper,fedora-assistente}.service
mkdir -p ~/.config/autostart
sed "s|^Exec=.*|Exec=$(dirname "$d")/abrir-janela.sh|" "$d/autostart.desktop" > ~/.config/autostart/fedora-assistente.desktop
install -Dm644 "$d/menu.desktop" ~/.local/share/applications/fedora-assistente.desktop
systemctl --user daemon-reload
systemctl --user enable --now whisper fedora-assistente
