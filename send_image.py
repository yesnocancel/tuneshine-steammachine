#!/usr/bin/env python3
"""Send an arbitrary image to a Tuneshine device.

Usage: send_image.py IMAGE [--track NAME] [--artist NAME] [--host HOST]
       send_image.py --clear [--host HOST]
"""
import argparse
import io
import json
import subprocess
import sys
import urllib.request

from PIL import Image


def discover_host() -> str | None:
    """Find a Tuneshine on the LAN via mDNS (needs avahi-browse)."""
    try:
        out = subprocess.run(["avahi-browse", "-rpt", "_tuneshine._tcp"],
                             capture_output=True, text=True, timeout=6).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        parts = line.split(";")
        if line.startswith("=") and len(parts) > 7:
            return parts[6]  # e.g. tuneshine-abcd.local
    return None


def to_webp_64(path: str) -> bytes:
    img = Image.open(path).convert("RGB")
    img = img.resize((64, 64), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "WEBP", lossless=True)
    return buf.getvalue()


def send(host: str, webp: bytes, metadata: dict) -> None:
    boundary = "tuneshineboundary"
    parts = [
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="metadata"\r\n\r\n'
        f"{json.dumps(metadata)}\r\n".encode(),
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="image"; filename="image.webp"\r\n'
        "Content-Type: image/webp\r\n\r\n".encode() + webp + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(
        f"http://{host}/image",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(resp.read().decode())


def clear(host: str) -> None:
    req = urllib.request.Request(f"http://{host}/image", method="DELETE")
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(resp.read().decode())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("image", nargs="?", help="path to any image (PNG/JPEG/WebP/etc.)")
    p.add_argument("--track", default="", help="track name metadata")
    p.add_argument("--artist", default="", help="artist name metadata")
    p.add_argument("--host", default=None,
                   help="Tuneshine hostname/IP (default: mDNS auto-discovery)")
    p.add_argument("--clear", action="store_true", help="clear the local image (revert to idle)")
    args = p.parse_args()

    if args.host is None:
        args.host = discover_host()
        if args.host is None:
            p.error("no Tuneshine found via mDNS — pass --host")
        print(f"discovered Tuneshine at {args.host}")

    if args.clear:
        clear(args.host)
        return
    if not args.image:
        p.error("image path required unless --clear")

    metadata = {k: v for k, v in [("trackName", args.track), ("artistName", args.artist)] if v}
    send(args.host, to_webp_64(args.image), metadata)


if __name__ == "__main__":
    main()
