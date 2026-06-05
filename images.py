"""Image sourcing — Pexels, Unsplash, and direct URL download."""
from __future__ import annotations
import argparse
import os
import re
import hashlib
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

CACHE_DIR     = Path("image_cache")
PEXELS_SEARCH = "https://api.pexels.com/v1/search"
UNSPLASH_SRCH = "https://api.unsplash.com/search/photos"


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


# ── Pexels ──────────────────────────────────────────────────────────────────

def fetch_pexels(query: str) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    dest = CACHE_DIR / f"px-{_slugify(query)}.jpg"
    if dest.exists():
        print(f"[images] pexels cache hit  -> {dest.name}")
        return dest

    api_key = os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        raise RuntimeError("PEXELS_API_KEY not set in .env")

    resp = requests.get(
        PEXELS_SEARCH,
        headers={"Authorization": api_key},
        params={"query": query, "per_page": 1,
                "orientation": "portrait", "size": "large"},
        timeout=12,
    )
    resp.raise_for_status()
    photos = resp.json().get("photos", [])
    if not photos:
        raise RuntimeError(f"No Pexels results for '{query}'")

    img_url = photos[0]["src"].get("portrait") or photos[0]["src"]["large"]
    dest.write_bytes(requests.get(img_url, timeout=30).content)
    print(f"[images] pexels downloaded -> {dest.name}")
    return dest


# ── Unsplash ─────────────────────────────────────────────────────────────────

def fetch_unsplash(query: str) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    dest = CACHE_DIR / f"us-{_slugify(query)}.jpg"
    if dest.exists():
        print(f"[images] unsplash cache hit  -> {dest.name}")
        return dest

    api_key = os.environ.get("UNSPLASH_API_KEY", "")
    if not api_key:
        raise RuntimeError("UNSPLASH_API_KEY not set in .env")

    resp = requests.get(
        UNSPLASH_SRCH,
        headers={"Authorization": f"Client-ID {api_key}"},
        params={"query": query, "per_page": 1,
                "orientation": "portrait"},
        timeout=12,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        raise RuntimeError(f"No Unsplash results for '{query}'")

    img_url = results[0]["urls"]["regular"]
    dest.write_bytes(requests.get(img_url, timeout=30).content)
    print(f"[images] unsplash downloaded -> {dest.name}")
    return dest


# ── URL download ─────────────────────────────────────────────────────────────

def fetch_from_url(url: str, custom_name: str | None = None) -> Path:
    """Download an image from any URL and cache it locally."""
    CACHE_DIR.mkdir(exist_ok=True)
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower() or ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"

    slug = custom_name or _slugify(Path(parsed.path).stem or "url") or "img"
    uid  = hashlib.md5(url.encode()).hexdigest()[:8]
    dest = CACHE_DIR / f"url-{slug}-{uid}{ext}"

    if dest.exists():
        print(f"[images] url cache hit  -> {dest.name}")
        return dest

    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
        stream=True,
    )
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise RuntimeError(f"URL does not point to an image (content-type: {content_type})")

    dest.write_bytes(resp.content)
    print(f"[images] url downloaded -> {dest.name}")
    return dest


# ── Unified fetch ────────────────────────────────────────────────────────────

def fetch_image(query: str, source: str = "pexels") -> Path:
    """Fetch by query from pexels or unsplash."""
    if source == "unsplash":
        return fetch_unsplash(query)
    return fetch_pexels(query)


def fetch_images_for_plan(
    plan: dict, source: str = "pexels"
) -> dict[int, Path | None]:
    """Fetch one image per content slide. Returns {slide_index: Path | None}."""
    results: dict[int, Path | None] = {}
    for i, slide in enumerate(plan.get("content_slides", [])):
        query = slide.get("image_query") or "city background"
        try:
            results[i] = fetch_image(query, source)
        except Exception as exc:
            print(f"[images] WARN slide {i} ('{query}'): {exc}")
            results[i] = None
    return results


def list_cached() -> list[Path]:
    if not CACHE_DIR.exists():
        return []
    return sorted(
        f for f in CACHE_DIR.iterdir()
        if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch a single image.")
    parser.add_argument("query",   nargs="?", default="London Ontario city skyline")
    parser.add_argument("--source", default="pexels", choices=["pexels", "unsplash"])
    parser.add_argument("--url",   help="Download from a specific URL instead")
    args = parser.parse_args()

    if args.url:
        path = fetch_from_url(args.url)
    else:
        path = fetch_image(args.query, args.source)
    print(f"Saved: {path}")


if __name__ == "__main__":
    main()
