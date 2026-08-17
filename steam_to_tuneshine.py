#!/usr/bin/env python3
"""Watch the local Steam client for the running game and mirror its artwork
to a Tuneshine device.

Designed to run ON the Steam Machine (SteamOS) as a systemd user service, so
detection is bound to this device — not to Steam account presence. Handles
non-Steam shortcuts (name comes from shortcuts.vdf, artwork from SteamGridDB
by name search).

Detection: scans /proc for Steam's `reaper SteamLaunch AppId=N` wrapper, with
~/.steam/registry.vdf's RunningAppID as fallback. Both cover Steam games and
non-Steam shortcuts.

Config lives in ~/.config/tuneshine-steam/config.json:
    {
      "tuneshine_host": "tuneshine-abcd.local",
      "sgdb_api_key": "...",                  # steamgriddb.com/profile/preferences/api
      "poll_seconds": 10,
      "display_seconds": 120                  # revert to idle screen after N s; 0 = show until game ends
    }

Usage:
    steam_to_tuneshine.py            # run the watch loop (sends to Tuneshine)
    steam_to_tuneshine.py --dry-run  # detect + fetch art, write /tmp preview, send nothing
    steam_to_tuneshine.py --once     # single detection pass instead of a loop
"""
import argparse
import glob
import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

CONFIG_PATH = os.path.expanduser("~/.config/tuneshine-steam/config.json")
CACHE_DIR = os.path.expanduser("~/.cache/tuneshine-steam")
# drop <cache-key>.<any image ext> here to force artwork for a game,
# e.g. overrides/2478970.png for a Steam appid, overrides/shortcut-doom.jpg
OVERRIDE_DIR = os.path.expanduser("~/.config/tuneshine-steam/overrides")
STEAM_ROOT_CANDIDATES = [
    "~/.steam/steam",
    "~/.local/share/Steam",
]
# AppIds Steam launches for itself that are never "the game being played"
IGNORED_APPIDS = {769, 1070560, 1391110, 1628350}  # screensaver, runtimes, proton tooling


def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    if not cfg.get("tuneshine_host") or not cfg.get("sgdb_api_key"):
        sys.exit(f"config {CONFIG_PATH} needs tuneshine_host and sgdb_api_key")
    cfg.setdefault("poll_seconds", 10)
    cfg.setdefault("display_seconds", 120)
    return cfg


def steam_root() -> str:
    for c in STEAM_ROOT_CANDIDATES:
        p = os.path.expanduser(c)
        if os.path.isdir(os.path.join(p, "userdata")):
            return p
    sys.exit("could not find Steam installation")


# ---------------------------------------------------------------- detection

def running_appid_from_proc() -> int | None:
    """Find `reaper SteamLaunch AppId=N` in the process table."""
    hits = []
    for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline_path, "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if b"SteamLaunch" not in argv:
            continue
        for arg in argv:
            m = re.fullmatch(rb"AppId=(\d+)", arg)
            if m:
                appid = int(m.group(1))
                if appid not in IGNORED_APPIDS:
                    pid = int(cmdline_path.split("/")[2])
                    hits.append((pid, appid))
    if not hits:
        return None
    # newest launch wins if several linger
    return max(hits)[1]


def detect_running_appid() -> int | None:
    # Live processes only — registry.vdf's RunningAppID can go stale after a
    # crash, and "currently active" must mean a game that is running right now.
    return running_appid_from_proc()


# ------------------------------------------------------- name resolution

def parse_binary_vdf_shortcuts(path: str) -> list[dict]:
    """Minimal parser for Steam's binary shortcuts.vdf. Returns shortcut dicts."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    shortcuts, cur, i = [], None, 0

    def read_str(j):
        end = data.index(b"\0", j)
        return data[j:end].decode("utf-8", "replace"), end + 1

    while i < len(data):
        t = data[i]
        i += 1
        if t == 0x08:  # end of map
            if cur is not None and "appid" in cur:
                shortcuts.append(cur)
                cur = None
            continue
        if t not in (0x00, 0x01, 0x02):
            continue
        key, i = read_str(i)
        if t == 0x00:  # nested map
            if re.fullmatch(r"\d+", key):  # numbered shortcut entry
                cur = {}
        elif t == 0x01:
            val, i = read_str(i)
            if cur is not None:
                cur[key.lower()] = val
        elif t == 0x02:
            (val,) = struct.unpack_from("<I", data, i)
            i += 4
            if cur is not None:
                cur[key.lower()] = val
    return shortcuts


def all_shortcuts(root: str) -> dict[int, dict]:
    result = {}
    for path in glob.glob(os.path.join(root, "userdata", "*", "config", "shortcuts.vdf")):
        for sc in parse_binary_vdf_shortcuts(path):
            result[sc["appid"]] = sc
    return result


def steam_app_name(root: str, appid: int) -> str | None:
    """Read the game name from appmanifest files across all library folders."""
    libs = [os.path.join(root, "steamapps")]
    lf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    try:
        with open(lf, errors="replace") as f:
            libs += [os.path.join(p, "steamapps")
                     for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
    except OSError:
        pass
    for lib in libs:
        manifest = os.path.join(lib, f"appmanifest_{appid}.acf")
        try:
            with open(manifest, errors="replace") as f:
                m = re.search(r'"name"\s+"([^"]+)"', f.read())
            if m:
                return m.group(1)
        except OSError:
            continue
    return None


def clean_shortcut_name(name: str) -> str:
    """Shortcut names are often executable filenames — tidy for display."""
    name = re.sub(r"\.(exe|bat|cmd|sh|AppImage|app)$", "", name, flags=re.I)
    return name.replace("_", " ").strip()


def resolve_game(root: str, appid: int) -> dict:
    """Return {'appid', 'name', 'is_shortcut'} for a running appid."""
    shortcuts = all_shortcuts(root)
    if appid in shortcuts:
        raw = shortcuts[appid].get("appname", f"App {appid}")
        return {"appid": appid, "name": clean_shortcut_name(raw), "is_shortcut": True}
    name = steam_app_name(root, appid)
    return {"appid": appid, "name": name or f"App {appid}", "is_shortcut": False}


def active_account_name() -> str | None:
    """The account currently signed in to the Steam client (AutoLoginUser).
    Steam updates this immediately on user switching, unlike loginusers.vdf
    timestamps which only prove who logged in last."""
    try:
        with open(os.path.expanduser("~/.steam/registry.vdf"), errors="replace") as f:
            m = re.search(r'"AutoLoginUser"\s+"([^"]+)"', f.read())
        return m.group(1) if m else None
    except OSError:
        return None


def current_player(root: str) -> str | None:
    """Persona name of the account signed in to the Steam client right now."""
    try:
        with open(os.path.join(root, "config", "loginusers.vdf"), errors="replace") as f:
            content = f.read()
    except OSError:
        return None
    active = active_account_name()
    if active:
        for block in re.findall(r'"\d{17}"\s*\{(.*?)\}', content, re.S):
            if re.search(r'"AccountName"\s+"%s"' % re.escape(active), block):
                m = re.search(r'"PersonaName"\s+"([^"]+)"', block)
                return m.group(1) if m else active
    # blocks look like "765..." { "PersonaName" "x" ... }. Prefer MostRecent=1,
    # but newer clients omit it — fall back to the highest Timestamp.
    best, best_key = None, (-1, -1)
    for block in re.findall(r'"\d{17}"\s*\{(.*?)\}', content, re.S):
        m = re.search(r'"PersonaName"\s+"([^"]+)"', block)
        if not m:
            continue
        recent = 1 if re.search(r'"MostRecent"\s+"1"', block) else 0
        ts = re.search(r'"Timestamp"\s+"(\d+)"', block)
        key = (recent, int(ts.group(1)) if ts else 0)
        if key > best_key:
            best, best_key = m.group(1), key
    return best


# ------------------------------------------------------------- artwork

def http_get(url: str, headers: dict | None = None, timeout: int = 15) -> bytes:
    # steamgriddb.com sits behind Cloudflare, which 403s Python's default UA
    req = urllib.request.Request(
        url, headers={"User-Agent": "tuneshine-steam/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def sgdb_get(cfg: dict, path: str) -> list:
    body = http_get("https://www.steamgriddb.com/api/v2" + path,
                    headers={"Authorization": f"Bearer {cfg['sgdb_api_key']}"})
    payload = json.loads(body)
    return payload.get("data", []) if payload.get("success") else []


def rank_assets(assets: list) -> list:
    """Prefer static png/webp/jpeg, then anything else (e.g. .ico)."""
    def score(a):
        mime = a.get("mime", "")
        return (mime in ("image/png", "image/webp", "image/jpeg"),
                a.get("width", 0))
    return sorted(assets, key=score, reverse=True)


def candidate_urls(cfg: dict, game: dict) -> tuple[list[str], list[str]]:
    """Top (icon, square-grid) candidate URLs from SteamGridDB."""
    if game["is_shortcut"]:
        term = urllib.parse.quote(game["name"])
        results = sgdb_get(cfg, f"/search/autocomplete/{term}")
        if not results:
            return [], []
        gid = results[0]["id"]
        icon_path = f"/icons/game/{gid}"
        grid_path = f"/grids/game/{gid}?dimensions=512x512,1024x1024"
    else:
        icon_path = f"/icons/steam/{game['appid']}"
        grid_path = f"/grids/steam/{game['appid']}?dimensions=512x512,1024x1024"
    out = []
    for path, top_n in ((icon_path, 3), (grid_path, 2)):
        try:
            out.append([a["url"] for a in rank_assets(sgdb_get(cfg, path))[:top_n]])
        except Exception as e:
            log(f"  sgdb {path}: {e}")
            out.append([])
    return out[0], out[1]


def colorfulness(rgb: bytes) -> float:
    """Hasler–Süsstrunk colorfulness of raw rgb24 pixels. Grayscale ≈ 0."""
    import math
    n = len(rgb) // 3
    srg = srg2 = syb = syb2 = 0.0
    for i in range(0, len(rgb), 3):
        r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
        rg = r - g
        yb = (r + g) / 2 - b
        srg += rg; srg2 += rg * rg
        syb += yb; syb2 += yb * yb
    mrg, myb = srg / n, syb / n
    vrg, vyb = max(0.0, srg2 / n - mrg * mrg), max(0.0, syb2 / n - myb * myb)
    return math.sqrt(vrg + vyb) + 0.3 * math.sqrt(mrg * mrg + myb * myb)


def to_rgb64(raw: bytes) -> bytes:
    """Center-crop square + resize to 64x64 raw rgb24. Pillow, else ffmpeg."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        s = min(img.size)
        left, top = (img.width - s) // 2, (img.height - s) // 2
        return img.crop((left, top, left + s, top + s)).resize((64, 64), Image.LANCZOS).tobytes()
    except ImportError:
        pass
    return ffmpeg_convert(raw, ["-f", "rawvideo", "-pix_fmt", "rgb24"])


DULL_ICON_THRESHOLD = 15.0  # below this, an icon is near-grayscale


def score_candidates(urls: list[str], kind: str) -> tuple[bytes | None, float]:
    best, best_score = None, -1.0
    for url in urls:
        try:
            raw = http_get(url)
            c = colorfulness(to_rgb64(raw))
        except Exception as e:
            log(f"  {kind} {url.rsplit('/', 1)[-1]}: {e}")
            continue
        log(f"  {kind} {url.rsplit('/', 1)[-1]}: colorfulness {c:.1f}")
        if c > best_score:
            best, best_score = raw, c
    return best, best_score


def fetch_artwork(cfg: dict, game: dict) -> bytes | None:
    """Icon-priority selection: the most colorful icon wins (icons are drawn
    for small sizes); square grids only compete when every icon is
    near-grayscale (e.g. a b&w logo). Steam CDN header as last resort."""
    icons, grids = candidate_urls(cfg, game)
    best, best_score = score_candidates(icons, "icon")
    if best is not None and best_score >= DULL_ICON_THRESHOLD:
        return best
    grid_best, grid_score = score_candidates(grids, "grid")
    if grid_score > best_score:
        best = grid_best
    if best is not None:
        return best
    if not game["is_shortcut"]:  # last resort: Steam CDN header (will be center-cropped)
        try:
            return http_get("https://cdn.cloudflare.steamstatic.com"
                            f"/steam/apps/{game['appid']}/header.jpg")
        except Exception as e:
            log(f"  steam cdn: {e}")
    return None


def to_webp_64(raw: bytes) -> bytes:
    """Center-crop square + resize to 64x64 lossless WebP. Pillow, else ffmpeg."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        s = min(img.size)
        left, top = (img.width - s) // 2, (img.height - s) // 2
        img = img.crop((left, top, left + s, top + s)).resize((64, 64), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "WEBP", lossless=True)
        return buf.getvalue()
    except ImportError:
        pass
    return ffmpeg_convert(raw, ["-c:v", "libwebp", "-lossless", "1", "-f", "webp"])


def sniff_suffix(raw: bytes) -> str:
    """ffmpeg picks demuxers by file extension for weak-magic formats (.ico!),
    so name the temp file to match the content."""
    if raw[:4] == b"\x00\x00\x01\x00":
        return ".ico"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ".img"


def ffmpeg_convert(raw: bytes, out_args: list[str]) -> bytes:
    # input via temp file, not pipe: some demuxers (e.g. .ico) need seekable input
    with tempfile.NamedTemporaryFile(suffix=sniff_suffix(raw)) as f:
        f.write(raw)
        f.flush()
        proc = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", f.name,
             "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=64:64", "-frames:v", "1",
             *out_args, "pipe:1"],
            capture_output=True, check=True)
    return proc.stdout


def cache_key(game: dict) -> str:
    return ("shortcut-" + re.sub(r"\W+", "-", game["name"].lower())
            if game["is_shortcut"] else str(game["appid"]))


def artwork_for(cfg: dict, game: dict) -> bytes | None:
    key = cache_key(game)
    overrides = glob.glob(os.path.join(OVERRIDE_DIR, f"{key}.*"))
    if overrides:
        log(f"  using override {os.path.basename(overrides[0])}")
        with open(overrides[0], "rb") as f:
            return to_webp_64(f.read())
    cache = os.path.join(CACHE_DIR, f"{key}.webp")
    if os.path.exists(cache):
        with open(cache, "rb") as f:
            return f.read()
    raw = fetch_artwork(cfg, game)
    if raw is None:
        return None
    webp = to_webp_64(raw)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache, "wb") as f:
        f.write(webp)
    return webp


# ------------------------------------------------------------ tuneshine

_last_good_ip: str | None = None


def tuneshine_ip(cfg: dict, cached_only: bool = False) -> str:
    """Resolve the Tuneshine host to an IPv4 address at call time, so a .local
    hostname in the config survives DHCP changes and avoids slow IPv6 lookups.
    Falls back to the last successful resolution when lookup fails; with
    cached_only, skips the lookup entirely — during suspend prep the network
    dies ~1s after PrepareForSleep, and a stalling mDNS query eats the window."""
    global _last_good_ip
    host = cfg["tuneshine_host"]
    try:
        socket.inet_aton(host)
        return host  # already an IP
    except OSError:
        pass
    if cached_only and _last_good_ip:
        return _last_good_ip
    try:
        infos = socket.getaddrinfo(host, 80, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        if _last_good_ip:
            return _last_good_ip
        raise
    _last_good_ip = infos[0][4][0]
    return _last_good_ip


def tuneshine_send(cfg: dict, webp: bytes, metadata: dict) -> None:
    boundary = "tuneshinesteamboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="metadata"\r\n\r\n'
        f"{json.dumps(metadata)}\r\n".encode()
        + f"--{boundary}\r\n"
          'Content-Disposition: form-data; name="image"; filename="game.webp"\r\n'
          "Content-Type: image/webp\r\n\r\n".encode() + webp + b"\r\n"
        + f"--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        f"http://{tuneshine_ip(cfg)}/image", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=15).read()


def tuneshine_clear(cfg: dict, timeout: int = 15, cached_only: bool = False) -> None:
    req = urllib.request.Request(
        f"http://{tuneshine_ip(cfg, cached_only)}/image", method="DELETE")
    urllib.request.urlopen(req, timeout=timeout).read()


def tuneshine_state(cfg: dict) -> dict:
    with urllib.request.urlopen(f"http://{tuneshine_ip(cfg)}/state", timeout=10) as r:
        return json.load(r)


def reconcile_startup(cfg: dict, dry_run: bool) -> None:
    """After a hard crash nothing sends the clear, so a stale game image can
    outlive us on the device. On startup: if the Tuneshine is showing OUR
    image but no game is running, remove it."""
    try:
        st = tuneshine_state(cfg)
    except Exception as e:
        log(f"startup reconcile skipped (device unreachable): {e}")
        return
    local = st.get("localMetadata") or {}
    if (st.get("imageSource") == "local"
            and local.get("serviceName") == "Steam Machine"
            and detect_running_appid() is None):
        log("startup: clearing stale game image from a previous run")
        if not dry_run:
            tuneshine_clear(cfg)


# ------------------------------------------------------------ suspend

SLEEP_MONITOR_CMDS = [
    ["gdbus", "monitor", "--system", "--dest", "org.freedesktop.login1"],
    ["dbus-monitor", "--system",
     "type='signal',interface='org.freedesktop.login1.Manager',member='PrepareForSleep'"],
]


def sleep_signals(cmd: list[str]):
    """Yield True on suspend-imminent, False on resume, from a D-Bus monitor."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True)
    pending = False
    for line in proc.stdout:
        if "PrepareForSleep" in line:
            pending = True
        if pending and ("true" in line or "false" in line):
            yield "true" in line
            pending = False


class SleepInhibitor:
    """Best-effort logind delay lock. Without one, logind may suspend before
    our clear request finishes; holding it guarantees a grace window between
    PrepareForSleep(true) and the actual suspend. The lock lives in a
    `systemd-inhibit ... cat` child; closing cat's stdin releases it."""

    def __init__(self):
        self._proc = None

    def take(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        try:
            self._proc = subprocess.Popen(
                ["systemd-inhibit", "--what=sleep", "--mode=delay",
                 "--who=tuneshine-steam", "--why=clearing Tuneshine display",
                 "cat"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except OSError:
            self._proc = None

    def release(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.stdin.close()
            self._proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self._proc.kill()
        self._proc = None


def clear_for_suspend(cfg: dict) -> bool:
    # NetworkManager downs the link ~1s after PrepareForSleep regardless of our
    # delay lock, so go straight at the cached IP with a short timeout — the
    # request must be on the wire within that first second to win.
    try:
        tuneshine_clear(cfg, timeout=3, cached_only=True)
        return True
    except Exception as e:
        log(f"  clear on suspend failed: {e}")
        return False


def sleep_monitor(cfg: dict, state: dict, lock: threading.Lock, dry_run: bool) -> None:
    """Clear the Tuneshine when the machine suspends; force a re-send on resume.
    ExecStopPost never fires for suspend, so without this the image lingers.
    While `suspending` is set, run_pass sends nothing — otherwise the poll loop
    could re-send the artwork in the window between our clear and the suspend."""
    inhibitor = SleepInhibitor()
    inhibitor.take()
    for cmd in SLEEP_MONITOR_CMDS:
        try:
            while True:
                for sleeping in sleep_signals(cmd):
                    if sleeping:
                        with lock:
                            state["suspending"] = True
                            state["misses"] = 0
                            if state.get("shown") is not None:
                                log("suspending — clearing Tuneshine")
                                # on failure keep `shown`: the device still has
                                # the image, so resume must not skip fixing it
                                if dry_run or clear_for_suspend(cfg):
                                    state["shown"] = None
                        inhibitor.release()  # let the suspend proceed
                    else:
                        with lock:
                            if not state.get("suspending"):
                                continue  # duplicate resume signal
                            state["suspending"] = False
                            state["misses"] = 0
                            state["expired"] = None  # wake = fresh display window
                        inhibitor.take()
                        log("resumed — will re-send on next poll if a game is running")
                time.sleep(5)  # monitor process died; restart it
        except OSError:
            continue  # try the next monitor tool
    log("no D-Bus monitor tool found — suspend clearing disabled")


# ----------------------------------------------------------------- main

def run_pass(cfg: dict, root: str, state: dict, dry_run: bool) -> None:
    if state.get("suspending"):
        return  # pre-suspend window: a send here would undo the suspend clear
    appid = detect_running_appid()
    if appid is None:
        # debounce: launches (Proton setup) and level loads can briefly leave no
        # AppId process; require 3 consecutive misses before declaring the game over
        state["misses"] = state.get("misses", 0) + 1
        if state["misses"] >= 3:
            if state.get("shown"):
                log("game ended — clearing Tuneshine")
                if not dry_run:
                    tuneshine_clear(cfg)
                state["shown"] = None
            state["expired"] = None  # next launch gets a fresh display window
        return
    state["misses"] = 0
    if state.get("shown") == appid:
        secs = cfg["display_seconds"]
        if secs and time.monotonic() - state["sent_at"] >= secs:
            log(f"display window over ({secs}s) — clearing Tuneshine")
            if not dry_run:
                tuneshine_clear(cfg)
            state["shown"] = None
            state["expired"] = appid
        return
    if state.get("expired") == appid:
        return  # this game already had its display window
    game = resolve_game(root, appid)
    player = current_player(root)
    log(f"detected: {game['name']} (appid {appid}, "
        f"{'shortcut' if game['is_shortcut'] else 'steam'}), player: {player}")
    webp = artwork_for(cfg, game)
    if webp is None:
        log("  no artwork found — leaving Tuneshine as-is")
        state["expired"] = appid  # don't retry every poll
        return
    metadata = {"trackName": game["name"], "serviceName": "Steam Machine"}
    if player:
        metadata["artistName"] = player
    if dry_run:
        preview = f"/tmp/tuneshine-preview-{appid}.webp"
        with open(preview, "wb") as f:
            f.write(webp)
        log(f"  dry-run: would send {metadata}; preview at {preview}")
    else:
        tuneshine_send(cfg, webp, metadata)
        log(f"  sent to Tuneshine: {metadata}")
    state["shown"] = appid
    state["sent_at"] = time.monotonic()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="never contact the Tuneshine; write previews to /tmp")
    p.add_argument("--once", action="store_true", help="single pass, no loop")
    p.add_argument("--clear", action="store_true",
                   help="delete the local image from the Tuneshine and exit")
    args = p.parse_args()

    cfg = load_config()
    if args.clear:
        tuneshine_clear(cfg)
        log("cleared")
        return
    root = steam_root()
    state = {"shown": None}
    lock = threading.Lock()
    log(f"watching Steam at {root}; Tuneshine at {cfg['tuneshine_host']}"
        + (" [DRY RUN]" if args.dry_run else ""))
    reconcile_startup(cfg, args.dry_run)
    if not args.once:
        threading.Thread(target=sleep_monitor, args=(cfg, state, lock, args.dry_run),
                         daemon=True).start()
    while True:
        try:
            with lock:
                run_pass(cfg, root, state, args.dry_run)
        except Exception as e:
            log(f"pass failed: {e}")
        if args.once:
            break
        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    main()
