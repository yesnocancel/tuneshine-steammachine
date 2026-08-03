# tuneshine-steam

Show the artwork of whatever is being played **on the Steam Machine** on a
[Tuneshine](https://www.tuneshine.rocks/) display.

- **Device-bound and live**: the watcher runs *on* the Steam Machine and only
  reports games with a live process (`/proc` scan for Steam's
  `reaper SteamLaunch AppId=N` wrapper). Being online on another PC does
  nothing, and stale state (e.g. after a crash) can't linger.
- **Active player**: the metadata's artist line is the persona name of
  whoever is signed in to the Steam client *right now* (`AutoLoginUser` in
  `registry.vdf`, which Steam updates on user switching). Visible in the
  Tuneshine mobile app; the panel itself only shows the artwork.
- **Non-Steam games work**: shortcut names come from `shortcuts.vdf`; artwork
  is found on [SteamGridDB](https://www.steamgriddb.com) by name search
  (e.g. classics installed from GOG, emulators, launchers).
- **Artwork**: the top SteamGridDB icons are scored for colorfulness
  (Hasler–Süsstrunk) and the most colorful wins — near-grayscale logo icons
  lose, and square 512/1024 grids only compete when *every* icon is dull.
  Steam CDN header (center-cropped) is the last resort. Converted to 64×64
  lossless WebP (Pillow, or ffmpeg fallback), cached in
  `~/.cache/tuneshine-steam`.
- **Per-game overrides**: to force artwork for a game, drop any image into
  `~/.config/tuneshine-steam/overrides/` named `<appid>.<ext>` (Steam) or
  `shortcut-<slugified-name>.<ext>` (non-Steam) — it beats every heuristic.
  The log line `using override …` confirms it; the slug appears in the cache
  filename if unsure.
- **Metadata**: game name as track, player (Steam persona of the active
  account, optionally mapped to a friendly name) as artist, service
  "Steam Machine".
- When the game closes, the local image is deleted so the Tuneshine reverts to
  its idle screen (dark/blank, unless music is playing — then album art).

## Setup (the easy way)

```bash
./setup.sh
```

An interactive wizard that auto-discovers the Tuneshine via mDNS, walks you
through enabling SSH on the Steam Machine (one copy-paste in Konsole),
installs your SSH key, validates your SteamGridDB API key, finds the Steam
accounts on the machine and asks for display names, installs everything,
runs an on-device dry run, and only goes live after you confirm.
Re-run just the dry run anytime with `./setup.sh --test`.

## Setup (manual)

1. **SteamGridDB API key**: log in at steamgriddb.com → Profile →
   [Preferences → API](https://www.steamgriddb.com/profile/preferences/api).
2. **Prepare the Steam Machine** (Desktop Mode → Konsole):
   ```bash
   passwd                                        # set a password
   sudo hostnamectl set-hostname steammachine    # avoid mDNS collision with a Steam Deck
   sudo systemctl enable --now sshd
   ```
3. From this repo: `./install.sh deck@steammachine.local` (adjust user/host).
4. Edit `~/.config/tuneshine-steam/config.json` on the machine: Tuneshine
   host/IP, SGDB key, persona→name mapping.
5. Test without touching the Tuneshine:
   `~/.local/bin/steam_to_tuneshine.py --dry-run` while a game runs —
   previews land in `/tmp/tuneshine-preview-*.webp`.
6. Go live: `systemctl --user enable --now tuneshine-steam`.

Logs: `journalctl --user -u tuneshine-steam -f`.

## Extras

- `send_image.py` — standalone helper to push any image file to the Tuneshine
  (`--clear` reverts to idle).
- Survives SteamOS updates: everything lives in the home directory
  (`~/.local/bin`, `~/.config/systemd/user`, pip `--user`).

## Notes

- Tuneshine local API spec: `http://<tuneshine>/openapi.json`. Local images
  override cloud art until `DELETE /image`.
- If the Steam Machine is hard-powered-off mid-game the Tuneshine may keep the
  last game art until the next music play or a manual
  `curl -X DELETE http://<tuneshine>/image` (the service's ExecStopPost handles
  normal shutdowns best-effort).
