"""Download new Telnyx call recordings into a local folder. Telnyx keeps its own copies."""
from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

API = "https://api.telnyx.com/v2/recordings"


def parse_since(text: str) -> datetime:
    match = re.fullmatch(r"(\d+)([hd])", text)
    if not match:
        raise argparse.ArgumentTypeError("use hours or days, like 6h or 2d")
    unit = {"h": "hours", "d": "days"}[match[2]]
    return datetime.now(timezone.utc) - timedelta(**{unit: int(match[1])})


def sync(api: httpx.Client, plain: httpx.Client, since: datetime, out: Path, dry_run: bool = False) -> list[Path]:
    """Save every mp3 recorded since `since` that isn't already in `out`."""
    resp = api.get(API, params={"filter[created_at][gte]": since.isoformat(), "page[size]": 250})
    resp.raise_for_status()
    saved = []
    for rec in resp.json()["data"]:
        url = (rec.get("download_urls") or {}).get("mp3")
        if not url:
            continue
        created = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00"))
        path = out / f"{created:%Y%m%d-%H%M%S}_{rec['id']}.mp3"
        if path.exists():
            continue
        print(("would save " if dry_run else "saving ") + path.name)
        if not dry_run:
            # The link is pre-signed, so it is fetched without the Telnyx key.
            audio = plain.get(url)
            audio.raise_for_status()
            out.mkdir(parents=True, exist_ok=True)
            path.write_bytes(audio.content)
        saved.append(path)
    return saved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=parse_since, default=parse_since("2d"), help="how far back, like 6h or 2d")
    parser.add_argument("--out", type=Path, default=Path("recordings"))
    parser.add_argument("--dry-run", action="store_true", help="list what would be saved")
    args = parser.parse_args()
    load_dotenv()
    key = os.environ.get("TELNYX_API")
    if not key:
        raise SystemExit("TELNYX_API is not set")
    with httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=30) as api, \
            httpx.Client(timeout=60, follow_redirects=True) as plain:
        saved = sync(api, plain, args.since, args.out, args.dry_run)
    print(f"{len(saved)} recording(s)")


if __name__ == "__main__":
    main()
