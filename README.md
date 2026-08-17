# tuneshine-steammachine

Show the artwork of whatever is being played **on your Steam Machine** on a
[Tuneshine](https://www.tuneshine.rocks/) display.

- **Shows what's actually playing on the device** — being online on your
  laptop or another PC doesn't trigger anything.
- **Non-Steam games work too**: anything you've added to Steam as a shortcut
  (GOG classics, emulators, launchers) gets proper artwork from
  [SteamGridDB](https://www.steamgriddb.com).
- **Picks nice artwork automatically** — and if you disagree with a pick, you
  can override it per game with any image file.
- **Cleans up after itself**: when you stop playing (or the machine goes to
  sleep or shuts down), the Tuneshine returns to its normal music/idle
  display.
- The Tuneshine app also shows who's playing and what, like it does for music.
<img width="768" height="1024" alt="7BF4AA61-7424-43DA-BA7E-8B2D3A2F06F3_1_105_c" src="https://github.com/user-attachments/assets/4b504b81-1240-49c0-8bb9-47914ba99a13" />

## Setup (the easy way)

```bash
./setup.sh
```

An interactive wizard that auto-discovers the Tuneshine on your network,
walks you through enabling SSH on the Steam Machine (one copy-paste in
Konsole), installs your SSH key, validates your SteamGridDB API key, installs
everything, runs an on-device dry run, and only goes live after you confirm.
Re-run just the dry run anytime with `./setup.sh --test`.

## Setup (manual)

1. **SteamGridDB API key**: log in at steamgriddb.com → Profile →
   [Preferences → API](https://www.steamgriddb.com/profile/preferences/api).
2. **Prepare the Steam Machine** (Desktop Mode → Konsole):
   ```bash
   passwd                                        # set a password
   sudo hostnamectl set-hostname steammachine    # (optional) avoid mDNS collision with a Steam Deck
   sudo systemctl enable --now sshd
   ```
3. From this repo: `./install.sh deck@steammachine.local` (adjust user/host).
4. Edit `~/.config/tuneshine-steam/config.json` on the machine: Tuneshine
   host/IP and your SteamGridDB key. Optional: `display_seconds` — how long
   the artwork stays up before the Tuneshine reverts to its idle screen
   (default 60; 0 = show for the whole play session).
5. Test without touching the Tuneshine:
   `~/.local/bin/steam_to_tuneshine.py --dry-run` while a game runs —
   previews land in `/tmp/tuneshine-preview-*.webp`.
6. Go live: `systemctl --user enable --now tuneshine-steam`.

Logs: `journalctl --user -u tuneshine-steam -f`.

## How it works

A small Python watcher runs on the Steam Machine as a systemd user service
and polls every few seconds (locally only — no network traffic unless
something changes):

- **Game detection**: scans `/proc` for Steam's launcher wrapper
  (`reaper SteamLaunch AppId=N`), so only games with a live process count —
  stale state after a crash can't linger. Works for Steam games and non-Steam
  shortcuts alike.
- **Names**: Steam games are resolved via their `appmanifest_*.acf`; shortcut
  names come from `shortcuts.vdf` (binary VDF parser included).
- **Player**: the persona name of the account currently signed in to the
  Steam client (`AutoLoginUser` in `registry.vdf`, updated on user
  switching). Shown in the Tuneshine mobile app; the panel itself only shows
  artwork.
- **Artwork selection**: the top SteamGridDB icons are scored for
  colorfulness (Hasler–Süsstrunk) and the most colorful wins — near-grayscale
  logo icons lose, and square 512/1024 grids only compete when *every* icon
  is dull. Steam CDN header (center-cropped) is the last resort.
- **Per-game overrides**: drop any image into
  `~/.config/tuneshine-steam/overrides/` named `<appid>.<ext>` (Steam) or
  `shortcut-<slugified-name>.<ext>` (non-Steam) — it beats every heuristic.
  The log line `using override …` confirms it.
- **Conversion & caching**: images are center-cropped and converted to 64×64
  lossless WebP (Pillow if available, else ffmpeg — SteamOS ships without
  pip) and cached in `~/.cache/tuneshine-steam`, so each game hits
  SteamGridDB only once.
- **Display**: sent to the Tuneshine's local HTTP API (spec:
  `http://<tuneshine>/openapi.json`), where a locally-pushed image overrides
  the cloud/music artwork until it's deleted. After `display_seconds`
  (default 60, 0 = never) the watcher deletes it again so the panel returns
  to its idle/music screen mid-session; each game launch — and each wake
  from sleep — starts a fresh display window.
- **Cleanup**: on game exit (debounced to survive loading-screen gaps) and on
  service stop the image is deleted; a D-Bus listener catches system suspend
  and clears before sleep, re-sending on resume; on startup the watcher
  removes a stale image of its own (e.g. after a hard crash).

## Extras

- `send_image.py` — standalone helper to push any image file to the Tuneshine
  (`--clear` reverts to idle).
- Survives SteamOS updates: everything lives in the home directory
  (`~/.local/bin`, `~/.config/systemd/user`).

## Notes

- **Network**: the Steam Machine and the Tuneshine must be on the same local
  network — artwork is delivered directly over your LAN, not via the cloud.
  (Strictly, they only need to reach each other over HTTP: separate
  subnets/VLANs work if routed, but `.local` hostnames and the wizard's
  auto-discovery use mDNS, which doesn't cross subnets — there, use the
  Tuneshine's IP in the config and give it a DHCP reservation.) Internet
  access is only needed to download artwork.
- If the Steam Machine is hard-powered-off mid-game the Tuneshine keeps the
  last game art until the machine boots again (startup reconciliation) or a
  manual `curl -X DELETE http://<tuneshine>/image`.
