#!/usr/bin/env bash
# Interactive setup wizard: connects your Steam Machine to your Tuneshine.
#
#   ./setup.sh          full guided setup
#   ./setup.sh --test   just re-run the on-device dry-run (after setup)
#
# The wizard never sends images to the Tuneshine. It only reads /health and
# runs dry-runs; the service goes live only after you explicitly say yes.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
note()  { printf '    %s\n' "$*"; }
die()   { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

ask() {  # ask "Prompt" "default" -> answer
  local ans
  read -rp "$1${2:+ [$2]}: " ans
  echo "${ans:-${2:-}}"
}

# ---------------------------------------------------------------- helpers

discover_tuneshine() {
  avahi-browse -rpt _tuneshine._tcp 2>/dev/null | awk -F';' '/^=/ {print $7";"$8; exit}'
}

tuneshine_ok() {  # $1 = host or ip
  curl -s -4 -m 4 "http://$1/health" 2>/dev/null | grep -q alive
}

sgdb_key_ok() {  # $1 = key
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
      -H "Authorization: Bearer $1" -A "tuneshine-steam-setup" \
      "https://www.steamgriddb.com/api/v2/grids/steam/70")
  [ "$code" = "200" ]
}

ssh_port_open() {  # $1 = host
  timeout 3 bash -c "echo > /dev/tcp/$1/22" 2>/dev/null
}

remote() { ssh -o BatchMode=yes -o ConnectTimeout=5 "$SM_TARGET" "$@"; }

run_remote_dryrun() {
  step "Dry run on the Steam Machine (nothing is sent to the Tuneshine)"
  note "If a game is running there right now, you'll see it detected below."
  local out
  out=$(remote '~/.local/bin/steam_to_tuneshine.py --dry-run --once' 2>&1) || true
  printf '%s\n' "$out" | sed 's/^/    | /'
  local preview
  preview=$(printf '%s\n' "$out" | grep -o '/tmp/tuneshine-preview-[0-9]*\.webp' | head -1 || true)
  if [ -n "$preview" ]; then
    scp -q "$SM_TARGET:$preview" /tmp/ && bold "    Preview copied to ${preview} on THIS machine — open it to check."
  else
    note "No game detected. Launch one on the Steam Machine and run: ./setup.sh --test"
  fi
}

# ------------------------------------------------------------ test-only mode

if [ "${1:-}" = "--test" ]; then
  SM_TARGET=$(cat "$HERE/.sm-target" 2>/dev/null) || die "run ./setup.sh first"
  run_remote_dryrun
  exit 0
fi

# ---------------------------------------------------------------- 0. deps

step "Checking prerequisites on this PC"
for dep in ssh scp curl python3 avahi-browse ssh-copy-id; do
  command -v "$dep" >/dev/null || die "missing dependency: $dep"
done
note "all present"

# ---------------------------------------------------------- 1. tuneshine

step "Looking for your Tuneshine on the network"
TS_INFO=$(discover_tuneshine || true)
TS_HOST="${TS_INFO%%;*}"; TS_IP="${TS_INFO##*;}"
if [ -z "$TS_INFO" ]; then
  TS_HOST=$(ask "Could not auto-discover it. Enter the Tuneshine hostname or IP")
  TS_IP="$TS_HOST"
else
  note "found: $TS_HOST ($TS_IP)"
fi
if tuneshine_ok "$TS_HOST"; then
  TUNESHINE="$TS_HOST"   # stable across DHCP changes
elif tuneshine_ok "$TS_IP"; then
  TUNESHINE="$TS_IP"
  note "hostname didn't resolve from here; using IP $TS_IP (may change with DHCP)"
else
  die "Tuneshine at $TS_HOST/$TS_IP did not answer /health — is it powered on?"
fi
bold "    Tuneshine: $TUNESHINE"

# ------------------------------------------------------- 2. steam machine

step "Connecting to the Steam Machine"
SM_HOST=""
for cand in steammachine.local steamdeck.local; do
  if ssh_port_open "$cand"; then SM_HOST="$cand"; break; fi
done
SM_HOST=$(ask "Steam Machine hostname or IP" "${SM_HOST:-steammachine.local}")
until ssh_port_open "$SM_HOST"; do
  cat <<'EOF'

    Can't reach SSH there yet. On the Steam Machine, switch to Desktop Mode,
    open Konsole, and paste:

        passwd                                        # set a password
        sudo hostnamectl set-hostname steammachine    # optional: avoid Steam Deck name clash
        sudo systemctl enable --now sshd

EOF
  ans=$(ask "Press Enter to retry, or type a different host" "$SM_HOST")
  SM_HOST="$ans"
done
SM_USER=$(ask "Username on the Steam Machine" "deck")
SM_TARGET="$SM_USER@$SM_HOST"

if ! remote true 2>/dev/null; then
  note "Installing your SSH key (you'll type the Steam Machine password once)."
  [ -f ~/.ssh/id_ed25519.pub ] || [ -f ~/.ssh/id_rsa.pub ] || \
      ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
  ssh-copy-id "$SM_TARGET"
  remote true || die "SSH still failing after ssh-copy-id"
fi
echo "$SM_TARGET" > "$HERE/.sm-target"
bold "    SSH to $SM_TARGET: ok"

# ------------------------------------------------------------ 3. sgdb key

step "SteamGridDB API key (for game artwork, incl. non-Steam games)"
SGDB_KEY=$(remote "python3 -c \"import json;print(json.load(open('.config/tuneshine-steam/config.json'))['sgdb_api_key'])\"" 2>/dev/null || true)
if [ -n "$SGDB_KEY" ] && sgdb_key_ok "$SGDB_KEY"; then
  note "found a working key already configured on the Steam Machine — keeping it"
else
  note "Get one at: https://www.steamgriddb.com/profile/preferences/api"
  command -v xdg-open >/dev/null && xdg-open "https://www.steamgriddb.com/profile/preferences/api" 2>/dev/null || true
  while :; do
    SGDB_KEY=$(ask "Paste your API key")
    sgdb_key_ok "$SGDB_KEY" && break
    note "that key was rejected by steamgriddb.com — try again"
  done
fi
bold "    key verified"

# ------------------------------------------------- 4. install + configure

step "Installing onto the Steam Machine"
"$HERE/install.sh" "$SM_TARGET" >/dev/null
python3 -c "
import json, sys
print(json.dumps({
    'tuneshine_host': sys.argv[1],
    'sgdb_api_key': sys.argv[2],
    'poll_seconds': 10,
}, indent=2))" "$TUNESHINE" "$SGDB_KEY" > /tmp/tuneshine-steam-config.json
scp -q /tmp/tuneshine-steam-config.json "$SM_TARGET:.config/tuneshine-steam/config.json"
rm -f /tmp/tuneshine-steam-config.json
bold "    installed and configured"

# ------------------------------------------------------------ 5. dry run

run_remote_dryrun

# ------------------------------------------------------------- 6. enable

step "Go live?"
note "Enabling the service means: whenever a game runs on the Steam Machine,"
note "its artwork appears on the Tuneshine, and it goes dark when the game ends."
if [ "$(ask "Enable now? (y/N)" "n")" = "y" ]; then
  remote "systemctl --user daemon-reload && systemctl --user enable --now tuneshine-steam"
  bold "    LIVE. Have fun!"
else
  note "Not enabled. When ready:  ssh $SM_TARGET 'systemctl --user enable --now tuneshine-steam'"
fi
note "Logs:     ssh $SM_TARGET 'journalctl --user -u tuneshine-steam -f'"
note "Disable:  ssh $SM_TARGET 'systemctl --user disable --now tuneshine-steam'"
