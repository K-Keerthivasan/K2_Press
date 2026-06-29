"""postiz.py — K2 Postiz Publisher (Phase 1: engine + CLI).

A thin publishing adapter. It takes already-finished assets — carousels and
single posts from K2 Press, 9:16 MP4 Reels from K2 Reels — and hands them to a
self-hosted Postiz instance, which publishes to Instagram via the Meta Graph API.

This module renders nothing. Rendering stays in K2 Press / K2 Reels. It receives
a finished asset (or asset set), a caption, and a content type, then pushes it to
Postiz. Default behaviour is *draft*; `schedule`/`now` are opt-in and `now` is
hard-guarded behind config (`publish.allow_now`).

Postiz API facts (self-host):
  Base URL    https://{host}/public/v1
  Auth header Authorization: {api_key}        (no "Bearer" prefix)
  Rate limit  30 requests/hour (requests, not posts)
  Channels    GET  /integrations              (a "channel" is an "integration")
  Upload      POST /upload                     (multipart file=@...)
  Upload URL  POST /upload-from-url            ({ "url": "..." })
  Create post POST /posts
  Delete post DELETE /posts/:id

CLI (engine without the UI):
  python postiz.py --type carousel --asset a.png --asset b.png --caption cap.txt
  python postiz.py --type post  --asset card.png --caption "Hello 👋"
  python postiz.py --type reel  --asset reel.mp4 --caption cap.txt --dry-run
  python postiz.py --type story --asset card.png --mode draft
  python postiz.py --list-channels
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml

CONFIG_PATH = Path("config.yaml")
RATE_STATE  = Path("library/postiz_rate.json")

# Far-future placeholder so a draft never accidentally schedules a real time.
DRAFT_DATE = "2099-01-01T00:00:00.000Z"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}


class PostizError(Exception):
    """Raised for validation, config, or guard failures (clean CLI exit)."""


# ── Config ─────────────────────────────────────────────────────────────────────

_DEFAULTS = {
    "base_url": "http://localhost:5000/public/v1",
    "api_key_env": "POSTIZ_API_KEY",
    "channels": {},
    "publish": {"default_mode": "draft", "allow_now": False},
    "rate_limit": {"max_requests_per_hour": 30, "safety_margin": 2},
    "reel": {"require_9x16": True, "min_seconds": 5, "max_seconds": 90},
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(config: dict | None = None) -> dict:
    """Read the ``postiz:`` block from config.yaml (or a passed-in config),
    layered over sane defaults so a missing key never crashes."""
    if config is None:
        try:
            config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            config = {}
    cfg = _merge(_DEFAULTS, (config or {}).get("postiz") or {})
    # Env override (e.g. Dockerized app reaching Postiz via host.docker.internal
    # while the host CLI uses localhost). Mirrors the K2_LLM_BASE_URL pattern.
    env_url = os.environ.get("POSTIZ_BASE_URL", "").strip()
    if env_url:
        cfg["base_url"] = env_url
    return cfg


def api_key(cfg: dict) -> str:
    env = cfg.get("api_key_env") or "POSTIZ_API_KEY"
    key = os.environ.get(env, "").strip()
    if not key:
        raise PostizError(f"Postiz API key not set — export {env} (Postiz → Settings → Public API).")
    return key


# ── Rate limiting (30 requests/hour, accounted for, not discovered) ────────────

class RateLimiter:
    """Sliding-1-hour-window guard persisted to disk so it survives across CLI
    runs. The ceiling is ``max_requests_per_hour - safety_margin``; each Postiz
    HTTP call (every upload AND every create) consumes one slot."""

    def __init__(self, cfg: dict, state_path: Path = RATE_STATE):
        rl = cfg.get("rate_limit") or {}
        self.ceiling = max(1, int(rl.get("max_requests_per_hour", 30)) - int(rl.get("safety_margin", 2)))
        self.path = state_path

    def _load(self) -> list[float]:
        try:
            cutoff = time.time() - 3600
            return [t for t in json.loads(self.path.read_text(encoding="utf-8")) if t >= cutoff]
        except Exception:
            return []

    def _save(self, stamps: list[float]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(stamps), encoding="utf-8")

    def remaining(self) -> int:
        return max(0, self.ceiling - len(self._load()))

    def reserve(self, n: int) -> None:
        """Raise before spending any request if the batch won't fit the window."""
        used = self._load()
        if len(used) + n > self.ceiling:
            oldest = min(used) if used else time.time()
            wait_min = max(0, int((oldest + 3600 - time.time()) / 60) + 1)
            raise PostizError(
                f"Rate limit: need {n} request(s), only {self.ceiling - len(used)} left this "
                f"hour (ceiling {self.ceiling}). Retry in ~{wait_min} min."
            )

    def record(self, n: int = 1) -> None:
        stamps = self._load()
        now = time.time()
        stamps.extend([now] * n)
        self._save(stamps)


# ── Postiz HTTP client ─────────────────────────────────────────────────────────

class PostizClient:
    def __init__(self, base_url: str, key: str, limiter: RateLimiter | None = None):
        self.base = base_url.rstrip("/")
        self.h = {"Authorization": key}          # no "Bearer" prefix
        self.limiter = limiter

    def _spend(self, n: int = 1) -> None:
        if self.limiter:
            self.limiter.record(n)

    def integrations(self) -> list[dict]:
        self._spend()
        r = requests.get(f"{self.base}/integrations", headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json()

    def upload_from_url(self, url: str) -> dict:
        self._spend()
        r = requests.post(f"{self.base}/upload-from-url",
                          headers={**self.h, "Content-Type": "application/json"},
                          json={"url": url}, timeout=120)
        r.raise_for_status()
        return r.json()

    def upload_file(self, path: str) -> dict:
        self._spend()
        with open(path, "rb") as f:
            r = requests.post(f"{self.base}/upload", headers=self.h,
                              files={"file": f}, timeout=180)
        r.raise_for_status()
        return r.json()

    def create_post(self, integration_id: str, content: str, media: list[dict],
                    post_type: str, mode: str = "draft", date: str | None = None,
                    is_trial_reel: bool = False, collaborators: list | None = None) -> dict:
        settings = {"__type": "instagram", "post_type": post_type}
        if post_type == "reel":
            settings["is_trial_reel"] = is_trial_reel
            settings["collaborators"] = collaborators or []
        body = {
            "type": mode,                                  # draft | schedule | now
            "date": date or DRAFT_DATE,
            "shortLink": False,
            "tags": [],
            "posts": [{
                "integration": {"id": integration_id},
                "value": [{"content": content,
                           "image": [{"id": m["id"], "path": m["path"]} for m in media]}],
                "settings": settings,
            }],
        }
        self._spend()
        r = requests.post(f"{self.base}/posts",
                          headers={**self.h, "Content-Type": "application/json"},
                          json=body, timeout=60)
        r.raise_for_status()
        return r.json()

    def delete_post(self, post_id: str) -> dict:
        self._spend()
        r = requests.delete(f"{self.base}/posts/{post_id}", headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json() if r.text else {"ok": True}


# ── Channel resolution ─────────────────────────────────────────────────────────

def resolve_integration(client: PostizClient, cfg: dict, channel: str = "instagram") -> dict:
    """Map the logical channel name (config ``channels.<channel>``) to the Postiz
    integration dict. Matches by name/id/internalId, else falls back to the first
    Instagram integration (matches both 'instagram' and 'instagram-standalone')."""
    logical = (cfg.get("channels") or {}).get(channel, channel)
    items = client.integrations()
    ig = [i for i in items if (i.get("identifier") or i.get("provider") or "").lower().startswith("instagram")]
    pool = ig or items
    for i in pool:
        if logical in (i.get("name"), i.get("id"), i.get("internalId")):
            return i
    if ig:
        return ig[0]
    raise PostizError(
        f"No Instagram integration found for logical channel '{logical}'. "
        f"Available: {[i.get('name') for i in items]}"
    )


def resolve_channel(client: PostizClient, cfg: dict, channel: str = "instagram") -> str:
    return resolve_integration(client, cfg, channel)["id"]


# ── Asset validation ───────────────────────────────────────────────────────────

def _probe_video(path: Path) -> tuple[float, int, int]:
    """(duration_s, width, height) via ffprobe. Returns zeros if ffprobe absent."""
    import shutil, subprocess
    exe = shutil.which("ffprobe") or "ffprobe"
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True,
        )
        data = json.loads(proc.stdout or "{}")
        dur = float((data.get("format") or {}).get("duration") or 0)
        st = (data.get("streams") or [{}])[0]
        return dur, int(st.get("width") or 0), int(st.get("height") or 0)
    except Exception:
        return 0.0, 0, 0


def validate_assets(post_type: str, assets: list[str], cfg: dict) -> list[Path]:
    """Reject bad asset sets early with a clear message. Returns Paths on success."""
    paths = [Path(a) for a in assets]
    for p in paths:
        if not p.exists():
            raise PostizError(f"Asset not found: {p}")

    if post_type == "carousel":
        if not (2 <= len(paths) <= 10):
            raise PostizError(f"Carousel needs 2–10 items, got {len(paths)}.")
        # IG (and Postiz) publish mixed-media carousels — images and/or 9:16 video.
        for p in paths:
            if p.suffix.lower() not in (IMAGE_EXTS | VIDEO_EXTS):
                raise PostizError(f"Carousel asset must be an image or video: {p.name}")

    elif post_type == "post":
        if len(paths) != 1 or paths[0].suffix.lower() not in IMAGE_EXTS:
            raise PostizError("Single post needs exactly one image.")

    elif post_type == "story":
        if len(paths) != 1 or paths[0].suffix.lower() not in (IMAGE_EXTS | VIDEO_EXTS):
            raise PostizError("Story needs exactly one image or video.")

    elif post_type == "reel":
        if len(paths) != 1 or paths[0].suffix.lower() not in VIDEO_EXTS:
            raise PostizError("Reel needs exactly one MP4 video.")
        rcfg = cfg.get("reel") or {}
        dur, w, h = _probe_video(paths[0])
        if dur:  # only enforce when ffprobe could read the file
            lo, hi = float(rcfg.get("min_seconds", 5)), float(rcfg.get("max_seconds", 90))
            if not (lo <= dur <= hi):
                raise PostizError(f"Reel duration {dur:.1f}s outside {lo:.0f}–{hi:.0f}s.")
        if rcfg.get("require_9x16", True) and w and h:
            ratio = w / h
            if abs(ratio - 9 / 16) > 0.02:
                raise PostizError(f"Reel must be 9:16 ({w}×{h} ≈ {ratio:.3f}, need 0.5625).")
        else:
            print("  [!] ffprobe unavailable - skipped reel dimension/duration check.")
    else:
        raise PostizError(f"Unknown content type: {post_type}")
    return paths


def validate_urls(post_type: str, urls: list[str]) -> None:
    """Count + extension checks for asset URLs (used by the /upload-from-url path,
    where the bytes live behind PUBLIC_BASE_URL rather than on local disk)."""
    def ext(u: str) -> str:
        return ("." + u.rsplit("?", 1)[0].rsplit(".", 1)[-1]).lower() if "." in u else ""
    if post_type == "carousel":
        if not (2 <= len(urls) <= 10):
            raise PostizError(f"Carousel needs 2–10 items, got {len(urls)}.")
        if any(ext(u) not in (IMAGE_EXTS | VIDEO_EXTS) for u in urls):
            raise PostizError("Carousel assets must be images or videos.")
    elif post_type == "post":
        if len(urls) != 1 or ext(urls[0]) not in IMAGE_EXTS:
            raise PostizError("Single post needs exactly one image URL.")
    elif post_type == "story":
        if len(urls) != 1 or ext(urls[0]) not in (IMAGE_EXTS | VIDEO_EXTS):
            raise PostizError("Story needs exactly one image or video URL.")
    elif post_type == "reel":
        if len(urls) != 1 or ext(urls[0]) not in VIDEO_EXTS:
            raise PostizError("Reel needs exactly one MP4 URL.")
    else:
        raise PostizError(f"Unknown content type: {post_type}")


POST_TYPE_MAP = {"carousel": "post", "post": "post", "reel": "reel", "story": "story"}


# ── High-level publish ─────────────────────────────────────────────────────────

def publish(*, type: str, assets: list[str], caption: str, channel: str = "instagram",
            mode: str = "", date: str | None = None, trial_reel: bool = False,
            asset_urls: list[str] | None = None, config: dict | None = None,
            dry_run: bool = False) -> dict:
    """Validate → resolve channel → upload → build payload → guard → POST.

    Pass ``asset_urls`` to use POST /upload-from-url instead of multipart upload
    (preferred when K2 already serves the assets over HTTP). ``mode`` defaults to
    config ``publish.default_mode``. Returns a result dict; raises PostizError on
    any guard/validation failure.
    """
    cfg = load_config(config)
    mode = (mode or (cfg.get("publish") or {}).get("default_mode", "draft")).lower()
    post_type = POST_TYPE_MAP.get(type)
    if not post_type:
        raise PostizError(f"Unknown --type '{type}' (carousel|post|reel|story).")

    # Guards (run before any network I/O).
    if mode == "now" and not (cfg.get("publish") or {}).get("allow_now", False):
        raise PostizError("mode 'now' refused — set postiz.publish.allow_now: true to allow.")
    if mode == "schedule" and not date:
        raise PostizError("mode 'schedule' requires --date (ISO8601).")
    if mode not in ("draft", "schedule", "now"):
        raise PostizError(f"Unknown --mode '{mode}' (draft|schedule|now).")

    if asset_urls:
        validate_urls(type, asset_urls)
        paths = []
        names = [u.rsplit("/", 1)[-1].rsplit("?", 1)[0] for u in asset_urls]
    else:
        paths = validate_assets(type, assets, cfg)
        names = [p.name for p in paths]
    n_assets = len(asset_urls) if asset_urls else len(paths)
    n_requests = 1 + n_assets + 1            # integrations + uploads + create

    limiter = RateLimiter(cfg)

    if dry_run:
        body_media = [{"id": f"<upload:{n}>", "path": f"<path:{n}>"} for n in names]
        payload = {
            "type": mode, "date": date or DRAFT_DATE, "shortLink": False, "tags": [],
            "posts": [{"integration": {"id": "<resolved-at-runtime>"},
                       "value": [{"content": caption, "image": body_media}],
                       "settings": ({"__type": "instagram", "post_type": post_type}
                                    | ({"is_trial_reel": trial_reel, "collaborators": []}
                                       if post_type == "reel" else {}))}],
        }
        return {"dry_run": True, "mode": mode, "post_type": post_type,
                "requests_needed": n_requests, "requests_remaining": limiter.remaining(),
                "payload": payload}

    limiter.reserve(n_requests)
    client = PostizClient(cfg["base_url"], api_key(cfg), limiter)

    integ = resolve_integration(client, cfg, channel)
    integration_id = integ["id"]
    identifier = (integ.get("identifier") or integ.get("provider") or "").lower()
    # Instagram Standalone exposes only post_type post|story — it has no "reel".
    # A 9:16 video posted as "post" is published as a Reel by Instagram itself.
    if post_type == "reel" and "standalone" in identifier:
        post_type = "post"

    media: list[dict] = []
    if asset_urls:
        for u in asset_urls:
            media.append(client.upload_from_url(u))
    else:
        for p in paths:
            media.append(client.upload_file(str(p)))

    res = client.create_post(integration_id, caption, media, post_type,
                             mode=mode, date=date, is_trial_reel=trial_reel)
    # Postiz returns a list of created posts (one per integration); be tolerant
    # of either a list or a dict response shape.
    item = res[0] if isinstance(res, list) and res else res
    post_id = item.get("postId") or item.get("id") or item.get("releaseId") if isinstance(item, dict) else None
    return {"dry_run": False, "mode": mode, "post_type": post_type,
            "integration_id": integration_id, "post_id": post_id,
            "uploaded": len(media), "response": res}


# ── CLI ────────────────────────────────────────────────────────────────────────

def _read_caption(value: str | None) -> str:
    if not value:
        return ""
    p = Path(value)
    if p.exists() and p.is_file():
        return p.read_text(encoding="utf-8")
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="k2publish", description="Push finished assets to Postiz (Instagram).")
    ap.add_argument("--type", choices=["carousel", "post", "reel", "story"])
    ap.add_argument("--asset", action="append", default=[], help="asset path; repeat for carousel")
    ap.add_argument("--asset-url", action="append", default=[], help="asset URL (uses /upload-from-url)")
    ap.add_argument("--caption", default="", help="caption text, or a path to a caption file")
    ap.add_argument("--channel", default="instagram", help="logical channel name from config")
    ap.add_argument("--mode", default="", choices=["", "draft", "schedule", "now"])
    ap.add_argument("--date", default=None, help="ISO8601, required for --mode schedule")
    ap.add_argument("--trial-reel", action="store_true", help="reels only")
    ap.add_argument("--dry-run", action="store_true", help="validate + build payload, do not POST")
    ap.add_argument("--list-channels", action="store_true", help="list Postiz integrations and exit")
    args = ap.parse_args(argv)

    # Windows consoles default to cp1252; captions/JSON carry emoji + unicode.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Standalone CLI: load .env so POSTIZ_API_KEY is available (app.py loads it
    # itself; calling again here is harmless/idempotent).
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    try:
        cfg = load_config()
        if args.list_channels:
            client = PostizClient(cfg["base_url"], api_key(cfg), RateLimiter(cfg))
            for i in client.integrations():
                print(f"  {i.get('id')}  {i.get('name')}  [{i.get('identifier') or i.get('provider')}]")
            return 0

        if not args.type:
            ap.error("--type is required (carousel|post|reel|story)")
        if not (args.asset or args.asset_url):
            ap.error("at least one --asset or --asset-url is required")

        result = publish(
            type=args.type,
            assets=args.asset,
            asset_urls=args.asset_url or None,
            caption=_read_caption(args.caption),
            channel=args.channel,
            mode=args.mode,
            date=args.date,
            trial_reel=args.trial_reel,
            config=cfg if False else None,   # let publish re-read for freshness
            dry_run=args.dry_run,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not args.dry_run and not result.get("post_id"):
            print("  ⚠ no post id returned — check the Postiz response above.", file=sys.stderr)
        return 0
    except PostizError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:500]
        except Exception:
            pass
        print(f"✗ Postiz HTTP {getattr(e.response, 'status_code', '?')}: {body}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
