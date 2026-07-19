"""video_reels.py — K2 Press "video_reels" mode (Phase 1: engine + CLI + manifest).

Turns a YouTube gameplay/trailer video into branded 9:16 content in the JKR-style
hook → title → CTA format, shipped as a 3-card **video carousel** or a single
stitched **reel**. It is a K2 Press *extension*: it reuses K2 Press's config, the
Playwright HTML/CSS→PNG renderer ([[render]]), the Ollama/Hermes wrapper
([[llm]]), and the yt-dlp + ffmpeg helpers in [[video]]. Delivery is via
[[postiz]] as a draft.

Editable by design — generation auto-fills a JSON **manifest** (copy + clip
picks). A human edits the manifest, then re-renders. Two-step CLI:

  python video_reels.py --rss <feed> --mode carousel --emit-manifest m.json
  # ...edit m.json (swap clips, rewrite text, toggle highlights)...
  python video_reels.py --from-manifest m.json --send      # render + draft to Postiz

Rights-gated: yt-dlp downloads only when the source is cleared (config allowlist
or manifest source.rights_cleared = true).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

import video as V                      # extract_clip, probe_duration, _ffmpeg, _run, W, H
from render import render_overlay_to_bytes
from brands import resolve_brand, theme_css, active_key

CONFIG_PATH = Path("config.yaml")
VR_DIR      = Path("video_cache/reels")          # branded card clips + reels
OVL_DIR     = Path("video_cache/overlays")       # rendered overlay PNGs
MANIFEST_DIR = Path("library/manifests")

CARD_ORDER = ["hook", "title", "cta"]


class VideoReelsError(Exception):
    """Validation / rights / config failures (clean CLI exit)."""


# ── config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def _vr_cfg(config: dict) -> dict:
    base = {"default_clip_seconds": 4.0, "scene_threshold": 0.4, "allowlist": []}
    base.update((config or {}).get("video_reels") or {})
    return base


# ── 1. RSS poll + source metadata ──────────────────────────────────────────────

def latest_video(rss_url: str) -> dict:
    """Return the newest entry from a YouTube channel RSS feed."""
    import feedparser
    feed = feedparser.parse(rss_url)
    if not feed.entries:
        raise VideoReelsError(f"No entries in feed: {rss_url}")
    e = feed.entries[0]
    vid = getattr(e, "yt_videoid", None) or _video_id_from_url(getattr(e, "link", ""))
    return {
        "video_id": vid,
        "title": getattr(e, "title", ""),
        "url": getattr(e, "link", "") or f"https://www.youtube.com/watch?v={vid}",
        "channel_rss": rss_url,
        "channel_id": getattr(feed.feed, "yt_channelid", "") if feed.feed else "",
    }


def _video_id_from_url(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/watch\?v=)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else ""


def video_meta(url: str) -> dict:
    """Title / description / duration via yt-dlp — info only, NO download."""
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "title": info.get("title", ""),
        "description": info.get("description", "") or "",
        "duration": float(info.get("duration") or 0),
        "channel_id": info.get("channel_id", ""),
        "url": info.get("webpage_url", url),
    }


# ── 2. rights gate ──────────────────────────────────────────────────────────────

def is_cleared(source: dict, config: dict) -> bool:
    """Cleared if the manifest source says so, OR the channel/url is on the
    config allowlist. The allowlist holds channel ids, RSS urls, or video urls."""
    if source.get("rights_cleared"):
        return True
    allow = set(_vr_cfg(config).get("allowlist") or [])
    for k in ("channel_id", "channel_rss", "url", "video_id"):
        if source.get(k) and source[k] in allow:
            return True
    return False


# ── 3. clip selection (different moment per card) ───────────────────────────────

def select_clips(method: str, duration: float, n: int, *, clip_seconds: float,
                 source_path: Path | None = None, threshold: float = 0.4,
                 manual: list | None = None) -> list[dict]:
    """Return N {start, duration} dicts — always distinct moments."""
    method = method or "even_intervals"
    dur = max(clip_seconds * n, float(duration or 0))

    if method == "manual":
        if not manual or len(manual) < n:
            raise VideoReelsError("manual clip method needs a {start,duration} per card.")
        return [{"method": "manual", "start": float(m["start"]),
                 "duration": float(m.get("duration", clip_seconds))} for m in manual[:n]]

    if method == "scene_cut":
        cuts = _scene_cuts(source_path, threshold) if source_path else []
        if len(cuts) >= n:
            step = max(1, len(cuts) // n)
            picks = [cuts[i * step] for i in range(n)]
            return [{"method": "scene_cut", "start": round(max(0.0, t), 2),
                     "duration": clip_seconds} for t in picks]
        # fall through to even spacing if detection came up short

    # even_intervals (default): N centres evenly spaced, clip centred on each
    out = []
    for i in range(n):
        centre = dur * (i + 1) / (n + 1)
        start = max(0.0, centre - clip_seconds / 2)
        if duration:
            start = min(start, max(0.0, duration - clip_seconds))
        out.append({"method": "even_intervals", "start": round(start, 2),
                    "duration": clip_seconds})
    return out


def _scene_cuts(path: Path, threshold: float) -> list[float]:
    import subprocess
    proc = subprocess.run(
        [V._ffmpeg(), "-i", str(path), "-filter:v",
         f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return [round(float(m), 3) for m in re.findall(r"pts_time:([0-9.]+)", proc.stderr or "")]


# ── 4. Ollama copy generation ───────────────────────────────────────────────────

def gen_card_copy(title: str, description: str, brand: dict, *, model: str | None = None) -> dict:
    """Ask the LLM for the three cards' copy (hook / title / cta) as JSON, written
    in THIS brand's voice — driven by the brand's personality + profile from config
    (no hardcoded tone; a gaming page and a B2B agency read differently)."""
    from llm import chat_json
    bname   = brand.get("name", "the brand")
    handle  = brand.get("handle", "")
    persona = (brand.get("personality") or "").strip()
    profile = (brand.get("profile") or "").strip()
    voice = ""
    if persona:
        voice += f"\nBRAND VOICE (write in this voice):\n{persona}\n"
    if profile:
        voice += f"\nBRAND / NICHE & AUDIENCE:\n{profile}\n"
    system = (
        f"You write short-form 9:16 video copy for {bname} ({handle}).\n"
        f"{voice}"
        "You are given a YouTube video's title and description. Produce copy for a "
        "3-card branded clip — a HOOK card, a TITLE card, and a CTA card — IN THIS BRAND'S "
        "VOICE and for its audience.\n"
        "Rules: hook is a scroll-stopping opener; title names the subject; CTA invites a "
        "follow. Keep each line short (fits a phone screen). Match the brand's tone above — "
        "do NOT default to a generic hype/gaming voice unless that is the brand. Pick 1-3 "
        "highlight keywords per card (exact substrings of that card's text). Return ONLY valid JSON:\n"
        '{"hook": {"lead": "<2-4 word kicker>", "body": "<one punchy sentence>", '
        '"highlight": ["..."], "emoji": "<one emoji>"}, '
        '"title": {"title": "<subject>", "subtitle": "<short descriptor>", "highlight": ["..."]}, '
        '"cta": {"body": "<follow CTA, may include the handle>", "highlight": ["..."]}}'
    )
    user = f"Title: {title}\nDescription: {description[:900]}"
    res = chat_json(system, user, model=model)
    # Guard shape so a flaky model can't break the manifest.
    res.setdefault("hook", {}); res.setdefault("title", {}); res.setdefault("cta", {})
    return res


# ── 5. manifest ─────────────────────────────────────────────────────────────────

def build_manifest(source: dict, copy: dict, clips: list[dict], *, mode: str,
                   brand: dict, brand_key: str, aspect: str = "9:16") -> dict:
    h, t, c = copy.get("hook", {}), copy.get("title", {}), copy.get("cta", {})
    # Per-card crop defaults: hook letterboxes, title/cta fill (cover-crop), centred.
    def _crop(fit): return {"fit": fit, "zoom": 1.0, "x": 0.5, "y": 0.5}
    return {
        "source": {
            "channel_rss": source.get("channel_rss", ""),
            "video_id": source.get("video_id", ""),
            "url": source.get("url", ""),
            "channel_id": source.get("channel_id", ""),
            "title": source.get("title", ""),
            "rights_cleared": bool(source.get("rights_cleared", False)),
        },
        "output_mode": mode,
        "aspect": aspect,                 # 9:16 | 4:5 | 1:1 | 16:9
        "brand": {"key": brand_key, "palette": brand_key,
                  "handle": brand.get("handle", ""), "font": "Calibri"},
        "audio": {"use_source_audio": True, "music_path": None, "rights_cleared": False},
        "cards": [
            {"id": "hook", "template": "hook", "clip": clips[0], "crop": _crop("fit"),
             "text": {"lead": h.get("lead", "Check this out:"), "body": h.get("body", source.get("title", "")),
                      "highlight": h.get("highlight", []), "emoji": h.get("emoji", "")}},
            {"id": "title", "template": "title", "clip": clips[1], "crop": _crop("fill"),
             "text": {"title": t.get("title", source.get("title", "")), "subtitle": t.get("subtitle", ""),
                      "highlight": t.get("highlight", [])}},
            {"id": "cta", "template": "cta", "clip": clips[2], "crop": _crop("fill"),
             "text": {"body": c.get("body", f"Follow {brand.get('handle','')} for more"),
                      "highlight": c.get("highlight", [])}},
        ],
    }


def emit_manifest(*, rss: str = "", video_id: str = "", url: str = "", mode: str = "carousel",
                  clip_method: str = "even_intervals", cards: int = 3, out_path: str,
                  aspect: str = "9:16", model: str | None = None, brand_key: str | None = None,
                  config: dict | None = None) -> dict:
    config = config or load_config()
    vr = _vr_cfg(config)
    bkey = brand_key or active_key(config) or "default"
    brand = resolve_brand(config, bkey)

    # Resolve the source.
    if rss:
        source = latest_video(rss)
    elif video_id or url:
        u = url or f"https://www.youtube.com/watch?v={video_id}"
        source = {"video_id": video_id or _video_id_from_url(u), "url": u, "channel_rss": ""}
    else:
        raise VideoReelsError("provide --rss or --video-id.")

    meta = video_meta(source["url"])
    source.setdefault("title", meta["title"])
    source["channel_id"] = source.get("channel_id") or meta["channel_id"]
    duration = meta["duration"]

    # scene_cut needs the bytes; only allowed when cleared.
    source_path = None
    if clip_method == "scene_cut":
        if not is_cleared(source, config):
            raise VideoReelsError("scene_cut needs the video downloaded, but the source "
                                  "is not rights-cleared (allowlist or source.rights_cleared).")
        source_path = V.download_full(source["url"], rights_cleared=True)

    clips = select_clips(clip_method, duration, cards,
                         clip_seconds=float(vr["default_clip_seconds"]),
                         source_path=source_path, threshold=float(vr["scene_threshold"]))
    copy = gen_card_copy(source.get("title", ""), meta["description"], brand, model=model)
    manifest = build_manifest(source, copy, clips, mode=mode, brand=brand,
                              brand_key=bkey, aspect=aspect)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


# ── 6. highlight helpers ────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _highlight_html(text: str, words: list[str], handle: str = "") -> str:
    """Escape text, substitute {handle}, then wrap highlight words in spans
    (alternating teal/green)."""
    text = (text or "").replace("{handle}", handle)
    html = _esc(text)
    for i, w in enumerate(words or []):
        w = (w or "").replace("{handle}", handle).strip()
        if not w:
            continue
        cls = "hl" if i % 2 == 0 else "hl alt"
        html = re.sub(re.escape(_esc(w)),
                      lambda m: f'<span class="{cls}">{m.group(0)}</span>', html, count=1)
    return html


# ── 7. overlay render + ffmpeg composite ────────────────────────────────────────

def render_card_overlay(card: dict, brand: dict, dest: Path, *,
                        width: int = V.W, height: int = V.H) -> Path:
    handle = brand.get("handle", "")
    logo = Path(brand.get("logo_path", "static/logo.png")).resolve().as_uri()
    txt = card.get("text", {})
    hl = txt.get("highlight", [])
    base = {"theme_css": theme_css(brand), "logo_path": logo, "handle": handle, "brand": brand}
    tmpl = {"hook": "vr_hook.html", "title": "vr_title.html", "cta": "vr_cta.html"}[card["template"]]

    if card["template"] == "hook":
        vars_ = {**base, "lead": txt.get("lead", ""), "emoji": txt.get("emoji", ""),
                 "body_html": _highlight_html(txt.get("body", ""), hl, handle)}
    elif card["template"] == "title":
        vars_ = {**base, "subtitle": txt.get("subtitle", ""),
                 "title_html": _highlight_html(txt.get("title", ""), hl, handle),
                 "subtitle_html": _highlight_html(txt.get("subtitle", ""), hl, handle)}
    else:  # cta
        vars_ = {**base, "body_html": _highlight_html(txt.get("body", ""), hl, handle)}

    png = render_overlay_to_bytes(tmpl, vars_, width, height)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(png)
    return dest


def composite_card(source_path: Path, start: float, duration: float,
                   overlay_png: Path, out_path: Path, *, template: str,
                   width: int = V.W, height: int = V.H, crop: dict | None = None,
                   bg: str = "0A0F1E", use_audio: bool = True) -> Path:
    """Trim [start, start+duration] from the source, frame it to width×height
    per the card's crop settings, composite the overlay PNG, encode H.264.

    crop = {fit: "fill"|"fit", zoom: >=1, x: 0..1, y: 0..1, dim: 0..1}
      fill = cover-crop (zoom in + pick focal point); fit = letterbox (whole frame).
      x/y = focal point (0,0 = top-left; 0.5,0.5 = centre; 0.5,1 = bottom-centre).
    Defaults: hook letterboxes, title fills, cta fills + dims for legibility.
    """
    crop = crop or {}
    fit  = (crop.get("fit") or ("fit" if template == "hook" else "fill")).lower()
    zoom = max(1.0, float(crop.get("zoom", 1.0)))
    fx   = min(1.0, max(0.0, float(crop.get("x", 0.5))))
    fy   = min(1.0, max(0.0, float(crop.get("y", 0.5))))
    dim  = float(crop["dim"]) if "dim" in crop else (0.45 if template == "cta" else 0.0)

    if fit == "fit":
        base = (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x{bg},setsar=1[b]")
    else:  # fill (cover-crop) with optional zoom + focal point
        sw, sh = int(width * zoom), int(height * zoom)
        base = (f"[0:v]scale={sw}:{sh}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}:(iw-{width})*{fx:.3f}:(ih-{height})*{fy:.3f},setsar=1[b]")
    parts, last = [base], "b"
    if dim > 0:
        parts.append(f"[{last}]drawbox=x=0:y=0:w=iw:h=ih:color=0x000000@{dim:.3f}:t=fill[d]"); last = "d"
    parts.append(f"[{last}][1:v]overlay=0:0[vout]")

    cmd = [V._ffmpeg(), "-y",
           "-ss", f"{max(0.0, start):.3f}", "-i", str(source_path),   # input seek = trim
           "-i", str(overlay_png),
           "-filter_complex", ";".join(parts), "-map", "[vout]"]
    cmd += (["-map", "0:a?"] if use_audio else ["-an"])
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart",
            "-t", f"{max(0.5, duration):.3f}", str(out_path)]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    V._run(cmd)
    return out_path


def _concat_reel(card_mp4s: list[Path], out_path: Path) -> Path:
    """Concatenate the branded card clips into one continuous reel."""
    listfile = out_path.with_suffix(".txt")
    listfile.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in card_mp4s), encoding="utf-8")
    try:
        V._run([V._ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart",
                str(out_path)])
    finally:
        listfile.unlink(missing_ok=True)
    return out_path


# ── 8. render from manifest ─────────────────────────────────────────────────────

def render_from_manifest(path: str, *, send: bool = False, config: dict | None = None) -> dict:
    config = config or load_config()
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    source = manifest["source"]
    mode = manifest.get("output_mode", "carousel")
    bkey = manifest.get("brand", {}).get("key") or active_key(config) or "default"
    brand = resolve_brand(config, bkey)
    use_audio = bool(manifest.get("audio", {}).get("use_source_audio", True))

    if not is_cleared(source, config):
        raise VideoReelsError(
            "Source not rights-cleared — refusing to download. Set source.rights_cleared "
            "true in the manifest (you assert rights), or add the channel to "
            "config.yaml video_reels.allowlist.")

    aspect = manifest.get("aspect", "9:16")
    w, h = V.dims_for_aspect(aspect)
    bg = V._hex((brand.get("theme") or {}).get("navy", "#0A0F1E"))   # letterbox colour
    vid = source.get("video_id") or _video_id_from_url(source.get("url", ""))
    stem = f"{bkey}_{vid or 'vr'}"
    cards = manifest.get("cards", [])
    card_mp4s: list[Path] = []

    # Step 1: download the FULL video once at max resolution; cards are trimmed
    # locally from it (no per-card re-download, no quality cap).
    full = V.download_full(source["url"], rights_cleared=True)

    for n, card in enumerate(cards):
        clip = card.get("clip", {})
        start = float(clip.get("start", 0))
        dur = float(clip.get("duration", 4.0))
        overlay = render_card_overlay(card, brand, OVL_DIR / f"{stem}_{n}_{card['id']}.png",
                                      width=w, height=h)
        out = composite_card(full, start, dur, overlay,
                             VR_DIR / f"{stem}_{n}_{card['id']}.mp4",
                             template=card["template"], width=w, height=h,
                             crop=card.get("crop"), bg=bg, use_audio=use_audio)
        card_mp4s.append(out)

    if mode == "reel":
        assets = [_concat_reel(card_mp4s, VR_DIR / f"{stem}_reel.mp4")]
    else:
        assets = card_mp4s

    result = {"mode": mode, "brand": bkey, "assets": [str(a) for a in assets],
              "rights_cleared": True}

    if send:
        import postiz
        ptype = "reel" if mode == "reel" else "carousel"
        caption = _caption_from(manifest, brand)
        res = postiz.publish(type=ptype, assets=[str(a) for a in assets],
                             caption=caption, channel=bkey, mode="draft", config=config)
        result["postiz"] = {"post_id": res.get("post_id"), "post_type": res.get("post_type")}
    return result


def _caption_from(manifest: dict, brand: dict) -> str:
    cards = {c["id"]: c.get("text", {}) for c in manifest.get("cards", [])}
    hook = cards.get("hook", {}).get("body", "")
    cta = cards.get("cta", {}).get("body", "").replace("{handle}", brand.get("handle", ""))
    src = manifest.get("source", {}).get("title", "")
    return "\n\n".join(p for p in [hook, cta, f"Source: {src}" if src else ""] if p).strip()


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="video-reels",
                                 description="YouTube trailer → branded 9:16 carousel/reel → Postiz draft.")
    ap.add_argument("--rss", default="", help="YouTube channel RSS feed url")
    ap.add_argument("--video-id", default="", help="explicit YouTube video id")
    ap.add_argument("--url", default="", help="explicit YouTube video url")
    ap.add_argument("--mode", choices=["carousel", "reel"], default="carousel")
    ap.add_argument("--aspect", choices=["9:16", "4:5", "1:1", "16:9"], default="9:16",
                    help="output aspect ratio")
    ap.add_argument("--clip-method", choices=["even_intervals", "scene_cut", "manual"],
                    default="even_intervals")
    ap.add_argument("--cards", type=int, default=3)
    ap.add_argument("--brand", default="", help="brand key (default: active brand)")
    ap.add_argument("--model", default="", help="LLM model override for copy gen")
    ap.add_argument("--emit-manifest", default="", help="generate + write manifest, then stop")
    ap.add_argument("--from-manifest", default="", help="render from an edited manifest")
    ap.add_argument("--send", action="store_true", help="push the render to Postiz as a draft")
    ap.add_argument("--dry-run", action="store_true", help="resolve source + build manifest in memory, no write/post")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    try:
        if args.from_manifest:
            res = render_from_manifest(args.from_manifest, send=args.send)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        # emit (or dry-run) a manifest
        if args.dry_run:
            m = emit_manifest(rss=args.rss, video_id=args.video_id, url=args.url, mode=args.mode,
                              clip_method=args.clip_method, cards=args.cards, aspect=args.aspect,
                              out_path=str(Path(MANIFEST_DIR) / "_dryrun.json"),
                              model=args.model or None, brand_key=args.brand or None)
            Path(MANIFEST_DIR / "_dryrun.json").unlink(missing_ok=True)
            print(json.dumps(m, ensure_ascii=False, indent=2))
            return 0

        out = args.emit_manifest or str(MANIFEST_DIR / "manifest.json")
        m = emit_manifest(rss=args.rss, video_id=args.video_id, url=args.url, mode=args.mode,
                          clip_method=args.clip_method, cards=args.cards, aspect=args.aspect,
                          out_path=out, model=args.model or None, brand_key=args.brand or None)
        print(f"Manifest written → {out}")
        print(f"  source: {m['source']['title']}  ({m['source']['url']})")
        print(f"  rights_cleared: {m['source']['rights_cleared']}  mode: {m['output_mode']}")
        for c in m["cards"]:
            print(f"  [{c['id']}] clip start={c['clip']['start']}s dur={c['clip']['duration']}s")
        print("Edit the manifest, then: python video_reels.py --from-manifest "
              f"{out} --send")
        return 0
    except VideoReelsError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
