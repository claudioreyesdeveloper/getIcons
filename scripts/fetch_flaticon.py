#!/usr/bin/env python3
"""
Fetch one icon per label from Flaticon via the official API.

Usage:
  export FLATICON_API_KEY=...   # (in CI, set as Actions Secret)
  python scripts/fetch_flaticon_batch.py \
      --file queries.txt \
      --format png \
      --size 128 \
      --out icons/

Notes (matches Flaticon API v3 behavior):
- Auth: POST /app/authentication with API key → temporal Bearer token (≈24h).
- Search: GET /search/icons/{orderBy}?q=...&limit=...&page=...
- Download: GET /item/icon/download/{id}?format=svg|png[&size=...]

This script:
- Reads labels from --file (one per line).
- Normalizes each label to a reasonable search query (best effort).
- Searches by priority order and takes the first result per label.
- Downloads PNG (with size) or SVG to a stable filename derived from the original label.
- Avoids unsafe characters in filenames; logs success/failure per label.
"""

import argparse
import os
import sys
import time
import pathlib
import re
from typing import List, Dict
import requests

API_BASE = "https://api.flaticon.com/v3"

def get_token(api_key: str) -> str:
    url = f"{API_BASE}/app/authentication"
    # Per docs, multipart/form-data with "apikey"
    resp = requests.post(url, files={"apikey": (None, api_key)}, headers={"Accept": "application/json"})
    if resp.status_code != 200:
        raise RuntimeError(f"Auth failed: {resp.status_code} {resp.text}")
    data = resp.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"Auth response missing token: {data}")
    return token

def safe_filename(text: str) -> str:
    t = text.strip().lower()
    t = t.replace("&", "and")
    t = re.sub(r"[./]+", "-")  # dots/slashes → hyphen
    t = re.sub(r"[^a-z0-9._ -]+", "", t)
    t = re.sub(r"\s+", "-", t)
    t = re.sub(r"-+", "-", t)
    return t.strip("-") or "icon"

def normalize_query(label: str) -> str:
    raw = label.strip()
    # Domain-specific, best-effort fixes and synonyms
    fixes = {
        "e.guitar": "electric guitar",
        "e.piano": "electric piano",
        "drumkit": "drum kit",
        "sub category": "subcategory",
        "choir&vocals": "choir vocals",
        "church&christmas": "church christmas",
        "euro dance": "eurodance",
        "euro organ artist": "organ artist",
        "fm xpanded": "fm expanded",
        "japanes": "japanese",
        "vietnamise": "vietnamese",
        "bariton": "baritone",
        "sa 2": "sa2",
    }
    key = raw.lower().strip()
    # strip punctuation variants that often appear as separators
    key = key.replace("–", "-").replace("—", "-")
    # apply targeted replacements
    for k, v in fixes.items():
        if k in key:
            key = key.replace(k, v)
    # clean dotted abbreviations like "a.guitar" → "a guitar" (kept literal for search)
    key = key.replace(".", " ")
    # extra trims
    key = re.sub(r"\s+", " ", key).strip()
    return key or raw

def search_first_icon(token: str, query: str, order_by: str = "priority") -> Dict:
    url = f"{API_BASE}/search/icons/{order_by}"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    params = {"q": query, "limit": 1, "page": 1}
    r = requests.get(url, headers=headers, params=params)
    if r.status_code != 200:
        raise RuntimeError(f"Search error: {r.status_code} {r.text}")
    data = r.json().get("data", [])
    return data[0] if data else None

def download_icon(token: str, icon_id: int, fmt: str, size: int, dest_path: pathlib.Path):
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    # Try query param style first
    url_q = f"{API_BASE}/item/icon/download/{icon_id}"
    params = {"format": fmt}
    if fmt == "png" and size:
        params["size"] = size
    r = requests.get(url_q, headers=headers, params=params)
    if r.status_code == 404:
        # Fallback: path style
        url_p = f"{API_BASE}/item/icon/download/{icon_id}/{fmt}"
        r = requests.get(url_p, headers=headers, params={"size": size} if fmt == "png" and size else None)
    if r.status_code != 200:
        raise RuntimeError(f"Download error for {icon_id}: {r.status_code} {r.text}")

    # Preferred: JSON envelope with data.url
    u = None
    try:
        u = r.json()["data"]["url"]
    except Exception:
        pass

    if u:
        asset = requests.get(u, stream=True)
        asset.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in asset.iter_content(8192):
                fh.write(chunk)
        return

    # Direct binary fallback
    if "Content-Type" in r.headers and ("image/" in r.headers["Content-Type"] or "svg" in r.headers["Content-Type"]):
        dest_path.write_bytes(r.content)
        return

    raise RuntimeError(f"Unexpected download response for {icon_id}: {r.text[:500]}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="Path to a text file with one label per line")
    ap.add_argument("--format", choices=["png", "svg"], default="png", help="Download format")
    ap.add_argument("--size", type=int, default=128, help="PNG size (ignored for SVG)")
    ap.add_argument("--order", choices=["priority", "added"], default="priority", help="Search order")
    ap.add_argument("--out", default="icons", help="Output directory")
    ap.add_argument("--delay-ms", type=int, default=200, help="Delay between API calls (ms)")
    args = ap.parse_args()

    api_key = os.getenv("FLATICON_API_KEY")
    if not api_key:
        print("FLATICON_API_KEY is not set", file=sys.stderr)
        sys.exit(2)

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    token = get_token(api_key)

    labels: List[str] = []
    with open(args.file, "r", encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if s and not s.startswith("#"):
                labels.append(s)

    successes, failures = 0, 0
    for label in labels:
        q = normalize_query(label)
        filename_base = safe_filename(label)
        ext = "svg" if args.format == "svg" else "png"
        dest_path = out_dir / f"{filename_base}.{ext}"

        try:
            icon = search_first_icon(token, q, order_by=args.order)
            if not icon:
                failures += 1
                print(f"MISS | label='{label}' | query='{q}' | reason=no results", file=sys.stderr)
            else:
                icon_id = icon.get("id")
                if not icon_id:
                    failures += 1
                    print(f"MISS | label='{label}' | query='{q}' | reason=missing id", file=sys.stderr)
                else:
                    download_icon(token, icon_id, args.format, args.size, dest_path)
                    successes += 1
                    print(f"OK   | label='{label}' | query='{q}' | id={icon_id} | → {dest_path}")
        except Exception as e:
            failures += 1
            print(f"ERR  | label='{label}' | query='{q}' | {e}", file=sys.stderr)

        time.sleep(max(0.0, args.delay-ms / 1000.0))

    print(f"Done. {successes} succeeded, {failures} failed. Output: {out_dir}")

if __name__ == "__main__":
    main()
