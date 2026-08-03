#!/usr/bin/env bash
# Install steam_to_tuneshine onto the Steam Machine over SSH.
# Usage: ./install.sh [user@]steammachine-host
set -euo pipefail

HOST="${1:?usage: ./install.sh [user@]steammachine-host}"
HERE="$(cd "$(dirname "$0")" && pwd)"

scp "$HERE/steam_to_tuneshine.py" "$HOST:.local/bin/steam_to_tuneshine.py" 2>/dev/null || {
  ssh "$HOST" "mkdir -p .local/bin .config/tuneshine-steam .config/systemd/user"
  scp "$HERE/steam_to_tuneshine.py" "$HOST:.local/bin/steam_to_tuneshine.py"
}
ssh "$HOST" "mkdir -p .config/tuneshine-steam .config/systemd/user && chmod +x .local/bin/steam_to_tuneshine.py"
scp "$HERE/tuneshine-steam.service" "$HOST:.config/systemd/user/tuneshine-steam.service"

# config: copy example only if none exists yet
if ! ssh "$HOST" "test -f .config/tuneshine-steam/config.json"; then
  scp "$HERE/config.example.json" "$HOST:.config/tuneshine-steam/config.json"
  echo ">>> Edit ~/.config/tuneshine-steam/config.json on the Steam Machine (SGDB key!)"
fi

# Pillow makes nicer resizes than the ffmpeg fallback; best-effort
ssh "$HOST" "python3 -m pip install --user --quiet pillow 2>/dev/null || true"

ssh "$HOST" "systemctl --user daemon-reload"
echo "Installed. On the Steam Machine, test first with:"
echo "  ssh $HOST '~/.local/bin/steam_to_tuneshine.py --dry-run --once'"
echo "Then enable for real with:"
echo "  ssh $HOST 'systemctl --user enable --now tuneshine-steam'"
