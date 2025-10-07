#!/usr/bin/env python3
"""
Fetch icons from Flaticon via the official API.

Requirements:
  - Python 3.9+
  - pip install requests

Environment:
  - FLATICON_API_KEY: your private API key (kept secret)
Usage examples:
  - python scripts/fetch_flaticon.py --query "api" --limit 10 --format png --size 128 --out icons/
  - python scripts/fetch_flaticon.py --query "user" --limit 5 --format svg --out icons-svgs/

Notes (per Flaticon API v3 docs):
  - Obtain a temporal bearer token via POST /app/authentication with your API key.
  - Search icons via GET /search/icons/{orderBy}?q=... (orderBy: priority|added).
  - Download via GET /item/icon/download/{id} with format, size, color, iconType parameters.
    The docs list 'format' as a required parameter (documented as “path” in the spec);
    in practice, many clients pass it as a query string (?format=svg|png). If one style
    404s in your account, this script falls back to the other.
References:
  - API home: https://api.flaticon.com/v3/docs/index.html
"""

import argparse
import os
import sys
import time
import pathlib
import re
import requests
from typing import List, Dict

API_BASE = "https://api.flaticon.com/v3"

def get_token(api_key: str) -> str:
    # POST /app/authentication → { token, expires }
    # Content type must be multipart/form-data per docs.
    url = f"{API_BASE}/app/authentication"
    resp = requests.post(url, files={"apikey": (None, api_key)}, headers={"Accept": "application/json"})
    if resp.status_code != 200:
        raise RuntimeError(f"Auth failed: {resp.status_code} {resp.text}")
    data = resp.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"Auth response missing token: {data}")
    return token

def search_icons(token: str, query: str, order_by: str, limit: int) -> List[Dict]:
    # GET /search/icons/{orderBy}?q=...&limit=...
    url = f"{API_BASE}/search/icons/{order_by}"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    out = []
    page = 1
    while len(out) < limit:
        params = {"q": query, "limit": min(100, limit - len(out)), "page": page}
        r = requests.get(url, headers=headers, params=params)
        if r.status_code != 200:
            raise RuntimeError(f"Search error: {r.status_code} {r.text}")
        payload = r.json()
        items = payload.get("data", [])
        if not items:
            break
        out.extend(items)
        meta = payload.get("metadata", {})
        if meta.get("page", 1) * 1 >= meta.get("total", meta.get("count", 0)) or len(items) == 0:
            break
        page += 1
        time.sleep(0.2)  # be polite
    return out[:limit]

def safe_filename(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-") or "icon"

def download_icon(token: str, icon_id: int, fmt: str, size: int, dest_dir: pathlib.Path):
    """
    Try download endpoint first, then fall back to CDN PNG if available in search results (for PNG only).
    Primary route (per docs):
      GET /item/icon/download/{id}?format=svg|png&size=...&color=...&iconType=...
    Some tenants may require format in the path; if the query version 404s, try /{id}/{format}.
    """
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    # Query-parameter style
    url_q = f"{API_BASE}/item/icon/download/{icon_id}"
    params = {"format": fmt}
    if fmt == "png" and size:
        params["size"] = size
    r = requests.get(url_q, headers=headers, params=params)
    if r.status_code == 404:
        # Path-parameter style fallback
        url_p = f"{API_BASE}/item/icon/download/{icon_id}/{fmt}"
        r = requests.get(url_p, headers=headers, params={"size": size} if fmt == "png" and size else None)
    if r.status_code != 200:
        raise RuntimeError(f"Download error for {icon_id}: {r.status_code} {r.text}")
    # API typically returns a JSON with a 'data' object and a 'url' to the asset
    try:
        u = r.json()["data"]["url"]
    except Exception:
        # Some accounts may get a direct file response; handle it.
        if "Content-Type" in r.headers and ("image/" in r.headers["Content-Type"] or "svg" in r.headers["Content-Type"]):
            # Save binary payload directly.
            ext = "svg" if "svg" in r.headers["Content-Type"] else "png"
            out = dest_dir / f"{icon_id}.{ext}"
            out.write_bytes(r.content)
            return str(out)
        raise RuntimeError(f"Unexpected download response for {icon_id}: {r.text[:500]}")
    # Fetch the asset from the returned CDN URL
    asset = requests.get(u, stream=True)
    asset.raise_for_status()
    ext = "svg" if fmt == "svg" else "png"
    out = dest_dir / f"{icon_id}.{ext}"
    with open(out, "wb") as fh:
        for chunk in asset.iter_content(8192):
            fh.write(chunk)
    return str(out)

def main():
    p = argparse.ArgumentParser(description="Fetch Flaticon icons via official API.")
    p.add_argument("--query", required=True, help="Search term, e.g., 'api'")
    p.add_argument("--limit", type=int, default=10, help="Max icons to fetch")
    p.add_argument("--format", choices=["png", "svg"], default="png", help="Download format")
    p.add_argument("--size", type=int, default=128, help="PNG size (16,24,32,64,128,256,512). Ignored for SVG.")
    p.add_argument("--order", choices=["priority", "added"], default="priority", help="Search order")
    p.add_argument("--out", default="icons", help="Output directory")
    args = p.parse_args()

    api_key = os.getenv("FLATICON_API_KEY")
    if not api_key:
        print("FLATICON_API_KEY is not set", file=sys.stderr)
        sys.exit(2)

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    token = get_token(api_key)
    results = search_icons(token, args.query, args.order, args.limit)

    print(f"Found {len(results)} results; downloading up to {args.limit}…")
    downloaded = 0
    for item in results:
        icon_id = item.get("id")
        if not icon_id:
            continue
        try:
            path = download_icon(token, icon_id, args.format, args.size, out_dir)
            downloaded += 1
            print(f"✔ {icon_id} → {path}")
        except Exception as e:
            print(f"✖ {icon_id} → {e}", file=sys.stderr)
        time.sleep(0.15)

    print(f"Done. Downloaded {downloaded} file(s) to {out_dir}")

if __name__ == "__main__":
    main()
