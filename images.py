"""Image sourcing — Pexels, Unsplash, and direct URL download."""
from __future__ import annotations
import argparse
import os
import re
import hashlib
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from dotenv import load_dotenv

load_dotenv()

CACHE_DIR     = Path("image_cache")
PEXELS_SEARCH = "https://api.pexels.com/v1/search"
UNSPLASH_SRCH = "https://api.unsplash.com/search/photos"
GOOGLE_SEARCH = "https://www.googleapis.com/customsearch/v1"

# Default "slight filter" baked into web/Google images on download. Overridable
# globally (config.yaml -> images.filter) or per brand (brand.image_filter).
DEFAULT_FILTER = {"enabled": True, "saturation": 0.85, "brightness": 0.95, "contrast": 1.05}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


# ── Slight filter (Pillow) ────────────────────────────────────────────────────

def _filter_params(brand_key: str | None = None) -> dict | None:
    """Merge global + brand filter settings. Returns None if disabled.

    ``brand_key`` picks a specific brand's image_filter override; without it the
    current active brand is used.
    """
    try:
        import yaml
        from brands import resolve_brand
        with open("config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        glob  = (cfg.get("images", {}) or {}).get("filter", {}) or {}
        brand = resolve_brand(cfg, brand_key).get("image_filter", {}) or {}
        params = {**DEFAULT_FILTER, **glob, **brand}
        return params if params.get("enabled", True) else None
    except Exception as exc:                       # never let config break a fetch
        print(f"[images] filter config load failed: {exc}")
        return None


def _apply_filter(path: Path, params: dict | None) -> None:
    """Bake a mild colour/brightness/contrast adjustment into the file in place."""
    if not params:
        return
    try:
        from PIL import Image, ImageEnhance
    except ImportError:
        print("[images] Pillow not installed — skipping bake-in filter "
              "(pip install Pillow). Render-time CSS filter still applies.")
        return
    try:
        img = Image.open(path)
        fmt = img.format                            # remember original encoder
        img = img.convert("RGB")
        img = ImageEnhance.Color(img).enhance(float(params.get("saturation", 1.0)))
        img = ImageEnhance.Brightness(img).enhance(float(params.get("brightness", 1.0)))
        img = ImageEnhance.Contrast(img).enhance(float(params.get("contrast", 1.0)))
        save_kw = {"quality": 90} if fmt in ("JPEG", "WEBP") else {}
        img.save(path, format=fmt or "JPEG", **save_kw)
        print(f"[images] filtered -> {path.name}")
    except Exception as exc:
        print(f"[images] filter failed for {path.name}: {exc}")


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


# ── Google (Programmable Search / Custom Search JSON API) ────────────────────

def google_results(query: str, count: int = 10) -> list[dict]:
    """Relevance-ranked Google image results: [{thumb, url, title}]."""
    def _unset(v: str) -> bool:
        return not v or "your_" in v or v.endswith("_here")
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    cx      = os.environ.get("GOOGLE_CSE_ID", "")
    if _unset(api_key) or _unset(cx):
        raise RuntimeError(
            "GOOGLE_API_KEY / GOOGLE_CSE_ID not set in .env. Create a Programmable "
            "Search Engine (image search ON) and an API key for the Custom Search API."
        )
    resp = requests.get(
        GOOGLE_SEARCH,
        params={"key": api_key, "cx": cx, "q": query, "searchType": "image",
                "num": max(1, min(count, 10)), "safe": "active", "imgSize": "large"},
        timeout=12,
    )
    if resp.status_code == 403:
        raise RuntimeError("Google Custom Search rejected the request (quota or key/cx). "
                           "Free tier is 100 searches/day.")
    resp.raise_for_status()
    items = resp.json().get("items", [])
    out = []
    for it in items:
        out.append({
            "url":   it.get("link", ""),
            "thumb": (it.get("image", {}) or {}).get("thumbnailLink") or it.get("link", ""),
            "title": it.get("title", ""),
        })
    return [r for r in out if r["url"]]


def fetch_google(query: str) -> Path:
    """Download the top Google image result for a query (filter baked in)."""
    results = google_results(query, count=5)
    if not results:
        raise RuntimeError(f"No Google image results for '{query}'")
    return fetch_from_url(results[0]["url"], custom_name=f"g-{_slugify(query)}")


# ── Article images (RSS entry image → page og:image → Pexels fallback) ───────

def _og_image(page_url: str) -> str:
    """Scrape an article page for its og:image / twitter:image (no new deps)."""
    try:
        resp = requests.get(page_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[images] og:image fetch failed for {page_url}: {exc}")
        return ""
    html = resp.text
    patterns = (
        r'<meta[^>]+property=["\']og:image(?::url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::url)?["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    )
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            url = m.group(1).strip()
            if url.startswith("//"):
                return "https:" + url
            if url.startswith("/"):
                return urljoin(page_url, url)
            return url
    return ""


def fetch_article_image(image_url: str = "", page_url: str = "",
                        fallback_query: str = "", fallback_source: str = "pexels") -> Path:
    """Source the article's own image, then fall back to a query-based source.

    1. The image URL extracted from the RSS entry (if any).
    2. The article page's og:image / twitter:image.
    3. ``fallback_query`` via Pexels (or ``fallback_source``).
    """
    if image_url:
        try:
            return fetch_from_url(image_url, custom_name=f"feed-{_slugify(page_url or image_url)}")
        except Exception as exc:
            print(f"[images] feed image failed ({image_url}): {exc}")
    if page_url:
        og = _og_image(page_url)
        if og:
            try:
                return fetch_from_url(og, custom_name=f"og-{_slugify(page_url)}")
            except Exception as exc:
                print(f"[images] og:image failed ({og}): {exc}")
    if fallback_query:
        eff = "pexels" if fallback_source == "feed" else fallback_source
        print(f"[images] feed -> falling back to {eff} for '{fallback_query}'")
        return fetch_image(fallback_query, eff)
    raise RuntimeError("No article image and no fallback query available.")


# ── Multi-result candidate search (for the picker grid) ──────────────────────

def search_images(query: str, source: str = "pexels", count: int = 10) -> list[dict]:
    """Return up to `count` candidate images [{thumb, url, title}] for a query.

    Used by the UI picker so the user can choose instead of auto-taking #1.
    """
    if source == "google":
        return google_results(query, count)

    if source == "unsplash":
        api_key = os.environ.get("UNSPLASH_API_KEY", "")
        if not api_key:
            raise RuntimeError("UNSPLASH_API_KEY not set in .env")
        resp = requests.get(
            UNSPLASH_SRCH,
            headers={"Authorization": f"Client-ID {api_key}"},
            params={"query": query, "per_page": count, "orientation": "portrait"},
            timeout=12,
        )
        resp.raise_for_status()
        return [{"url": r["urls"]["regular"], "thumb": r["urls"]["thumb"],
                 "title": (r.get("alt_description") or query)}
                for r in resp.json().get("results", [])]

    # default: pexels
    api_key = os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        raise RuntimeError("PEXELS_API_KEY not set in .env")
    resp = requests.get(
        PEXELS_SEARCH,
        headers={"Authorization": api_key},
        params={"query": query, "per_page": count, "orientation": "portrait", "size": "large"},
        timeout=12,
    )
    resp.raise_for_status()
    out = []
    for p in resp.json().get("photos", []):
        src = p.get("src", {})
        out.append({"url": src.get("portrait") or src.get("large") or src.get("original"),
                    "thumb": src.get("tiny") or src.get("small") or src.get("medium"),
                    "title": p.get("alt") or query})
    return [r for r in out if r["url"]]


# ── URL download ─────────────────────────────────────────────────────────────

def _looks_like_image(data: bytes) -> bool:
    """Sniff common image magic bytes (JPEG/PNG/GIF/WEBP/BMP/SVG)."""
    return (
        data[:3] == b"\xff\xd8\xff"                       # JPEG
        or data[:8] == b"\x89PNG\r\n\x1a\n"               # PNG
        or data[:6] in (b"GIF87a", b"GIF89a")             # GIF
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")  # WEBP
        or data[:2] == b"BM"                              # BMP
        or data.lstrip()[:5].lower() == b"<?xml"          # SVG (xml decl)
        or data.lstrip()[:4].lower() == b"<svg"           # SVG
    )


def fetch_from_url(url: str, custom_name: str | None = None,
                   do_filter: bool = True) -> Path:
    """Download an image from any URL and cache it locally.

    Accepts URLs whose server returns a non-image content-type (e.g.
    ``application/octet-stream``) as long as the bytes are a real image.
    Web images get the slight bake-in filter unless ``do_filter`` is False.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    parsed = urlparse(url)

    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*,*/*"},
        timeout=30,
    )
    resp.raise_for_status()
    data         = resp.content
    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()

    if not content_type.startswith("image/") and not _looks_like_image(data):
        raise RuntimeError(
            f"URL did not return an image (content-type: {content_type or 'none'}). "
            "Use a direct image link, not a webpage."
        )

    # Pick extension: URL path first, then content-type, default .jpg.
    ct_ext = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp",
        "image/svg+xml": ".svg",
    }
    ext = Path(parsed.path).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"}:
        ext = ct_ext.get(content_type, ".jpg")

    slug = custom_name or _slugify(Path(parsed.path).stem or "url") or "img"
    uid  = hashlib.md5(url.encode()).hexdigest()[:8]
    dest = CACHE_DIR / f"url-{slug}-{uid}{ext}"

    if dest.exists():
        print(f"[images] url cache hit  -> {dest.name}")
        return dest

    dest.write_bytes(data)
    print(f"[images] url downloaded -> {dest.name}")
    if do_filter:
        _apply_filter(dest, _filter_params())
    return dest


# ── Unified fetch ────────────────────────────────────────────────────────────

def fetch_image(query: str, source: str = "pexels") -> Path:
    """Fetch by query from pexels, unsplash, or google."""
    if source == "unsplash":
        return fetch_unsplash(query)
    if source == "google":
        return fetch_google(query)
    return fetch_pexels(query)


def fetch_images_for_plan(
    plan: dict, source: str = "pexels"
) -> dict[int, Path | None]:
    """Fetch one image per content slide. Returns {slide_index: Path | None}.

    With ``source="feed"`` the lead slide gets the source article's own image
    (RSS entry → og:image → Pexels), and the remaining slides fall back to
    Pexels using each slide's ``image_query``.
    """
    results: dict[int, Path | None] = {}
    src_img = plan.get("source_image", "")
    src_url = plan.get("source_url", "")
    for i, slide in enumerate(plan.get("content_slides", [])):
        query = slide.get("image_query") or "city background"
        try:
            if source == "feed" and i == 0:
                results[i] = fetch_article_image(src_img, src_url, fallback_query=query)
            else:
                eff = "pexels" if source == "feed" else source
                results[i] = fetch_image(query, eff)
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
    parser.add_argument("--source", default="pexels", choices=["pexels", "unsplash", "google"])
    parser.add_argument("--url",   help="Download from a specific URL instead")
    args = parser.parse_args()

    if args.url:
        path = fetch_from_url(args.url)
    else:
        path = fetch_image(args.query, args.source)
    print(f"Saved: {path}")


if __name__ == "__main__":
    main()
