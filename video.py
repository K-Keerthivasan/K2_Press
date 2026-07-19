"""Video / Reels pipeline.

Two stages, usable independently:
  1. extract_clip()  — pull a [start,end] segment from a YouTube URL (yt-dlp +
     ffmpeg). Gated behind an explicit rights_cleared flag (off by default).
  2. composite()     — turn a clip + a Script variant into a branded vertical
     (1080x1920) MP4: cover-crop, brand colour grade, word-timed burned
     captions (ASS), a hook card (start) and an end card (logo + handle + CTA).

ffmpeg/ffprobe must be on PATH. yt-dlp is only needed for extract_clip().
"""
from __future__ import annotations
import hashlib
import shutil
import subprocess
from pathlib import Path

CACHE      = Path("video_cache")
CLIPS_DIR  = CACHE / "clips"
OUT_DIR    = CACHE / "out"
TMP_DIR    = CACHE / "tmp"
W, H       = 1080, 1920          # default 9:16 (reel/story)

# Supported output aspect ratios → (width, height). 1080-wide IG ratios + landscape.
ASPECTS = {
    "9:16": (1080, 1920),   # reel / story
    "4:5":  (1080, 1350),   # IG portrait feed
    "1:1":  (1080, 1080),   # square
    "16:9": (1920, 1080),   # landscape
}


def dims_for_aspect(aspect: str) -> tuple[int, int]:
    return ASPECTS.get(aspect or "9:16", (W, H))

RIGHTS_MSG = (
    "YouTube extraction requires explicit rights confirmation. Pass "
    "rights_cleared=True to proceed. By doing so you assert that your use of "
    "this material qualifies as fair use / is otherwise licensed under "
    "applicable law. K2 Press does not verify fair-use claims."
)


# ── tool resolution ───────────────────────────────────────────────────────────

def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError("ffmpeg failed:\n" + "\n".join(tail))


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [_ffprobe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


# ── 1. extraction (yt-dlp) ────────────────────────────────────────────────────

def extract_clip(youtube_url: str, start: float, end: float, *,
                 rights_cleared: bool = False, out_dir: Path = CLIPS_DIR) -> Path:
    """Download the [start,end] segment of a YouTube video as a trimmed MP4."""
    if not rights_cleared:
        raise PermissionError(RIGHTS_MSG)
    start, end = float(start), float(end)
    if end <= start:
        raise ValueError("end timestamp must be greater than start.")

    out_dir.mkdir(parents=True, exist_ok=True)
    uid    = hashlib.md5(f"{youtube_url}|{start}|{end}".encode()).hexdigest()[:10]
    target = out_dir / f"clip_{uid}.mp4"
    if target.exists():
        return target

    import yt_dlp
    from yt_dlp.utils import download_range_func

    # A progressive (single-file) format + download_range_func grabs just the
    # [start,end] section over HTTP range requests in ~1-2s. Avoid DASH bv+ba
    # merges and force_keyframes_at_cuts — both can stall the ranged download.
    raw_tmpl = out_dir / f"raw_{uid}.%(ext)s"
    opts = {
        "format": "best[height<=1080][ext=mp4]/best[ext=mp4]/best",
        "outtmpl": str(raw_tmpl),
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": False,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([youtube_url])

    raws = sorted(out_dir.glob(f"raw_{uid}.*"))
    if not raws:
        raise RuntimeError("yt-dlp produced no output (video unavailable or blocked).")
    raw = raws[0]

    # Re-encode to an exact-duration, clean-boundary MP4 (the ranged download
    # starts at ~start, so we trim from 0 for the requested length).
    try:
        _run([_ffmpeg(), "-y", "-i", str(raw), "-t", f"{end - start:.3f}",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
              "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target)])
    finally:
        raw.unlink(missing_ok=True)
    return target


def download_full(youtube_url: str, *, rights_cleared: bool = False,
                  out_dir: Path = CLIPS_DIR) -> Path:
    """Download the WHOLE video at the highest available resolution, once.

    Unlike extract_clip (a fast, ≤1080p ranged grab), this pulls bestvideo+
    bestaudio with no resolution cap so downstream 9:16 crops keep their detail.
    Cached per video url. Container may be mp4/mkv/webm — ffmpeg reads all three;
    the per-card composite re-encodes to H.264 anyway.
    """
    if not rights_cleared:
        raise PermissionError(RIGHTS_MSG)
    out_dir.mkdir(parents=True, exist_ok=True)
    uid = hashlib.md5(youtube_url.encode()).hexdigest()[:10]
    existing = sorted(out_dir.glob(f"full_{uid}.*"))
    existing = [p for p in existing if p.suffix.lower() in (".mp4", ".mkv", ".webm")]
    if existing:
        return existing[0]

    import yt_dlp
    opts = {
        "format": "bestvideo+bestaudio/best",          # max resolution, merged
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / f"full_{uid}.%(ext)s"),
        "quiet": True, "no_warnings": True, "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([youtube_url])

    produced = [p for p in sorted(out_dir.glob(f"full_{uid}.*"))
                if p.suffix.lower() in (".mp4", ".mkv", ".webm")]
    if not produced:
        raise RuntimeError("yt-dlp produced no output (video unavailable or blocked).")
    return produced[0]


# ── 2. compositing (ffmpeg) ───────────────────────────────────────────────────

def _hex(c: str) -> str:
    """'#0A0F1E' → '0A0F1E' for ffmpeg colour args."""
    return (c or "#000000").lstrip("#")


def _ass_ts(t: float) -> str:
    t = max(0.0, float(t))
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_color(hex_str: str) -> str:
    """#RRGGBB → ASS &HBBGGRR& (no alpha)."""
    h = _hex(hex_str)
    if len(h) != 6:
        h = "FFFFFF"
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}"


def _build_ass(captions: list[dict], vcfg: dict, duration: float, path: Path) -> bool:
    """Write an ASS subtitle file from word/phrase-timed captions. Returns False
    if there's nothing to burn."""
    caps = [c for c in (captions or []) if c.get("text")]
    if not caps:
        return False
    caps = sorted(caps, key=lambda c: float(c.get("time", 0)))

    ccfg      = vcfg.get("captions", {}) or {}
    font_size = int(ccfg.get("font_size", 54))
    primary   = _ass_color(ccfg.get("text_color", "#FFFFFF"))
    pos       = float(ccfg.get("position", 0.74))
    margin_v  = max(20, int(H - pos * H))
    box       = float(ccfg.get("box_opacity", 0.55))
    # ASS BorderStyle 3 = opaque box; alpha is inverted (00=opaque, FF=transparent).
    back_alpha = int((1.0 - max(0.0, min(box, 1.0))) * 255)
    back_col   = f"&H{back_alpha:02X}000000"
    border     = 3 if box > 0 else 1

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV",
        f"Style: Cap,Calibri,{font_size},{primary},&H00000000,{back_col},"
        f"-1,{border},2,0,2,80,80,{margin_v}",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for i, c in enumerate(caps):
        t0 = float(c.get("time", 0))
        t1 = float(caps[i + 1]["time"]) if i + 1 < len(caps) else duration
        if t1 <= t0:
            t1 = min(t0 + 2.5, duration)
        text = str(c["text"]).replace("\n", " ").replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_ass_ts(t0)},{_ass_ts(t1)},Cap,,0,0,0,,{text}")

    path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _font() -> str | None:
    for p in (r"C:\Windows\Fonts\calibrib.ttf", r"C:\Windows\Fonts\arialbd.ttf",
              r"C:\Windows\Fonts\arial.ttf"):
        if Path(p).exists():
            return p
    return None


def _card_png(kind: str, text_lines: list[str], vcfg: dict, brand: dict,
              dest: Path) -> Path:
    """Render a full-frame branded card (hook or end) as a PNG via Pillow."""
    from PIL import Image, ImageDraw, ImageFont
    ccfg   = vcfg.get(f"{kind}_card", {}) or {}
    bg     = ccfg.get("bg", "#0A0F1E")
    fg     = ccfg.get("text_color", "#FFFFFF")
    accent = ccfg.get("accent", brand.get("theme", {}).get("accent", "#00B4C8"))

    img  = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    font_path = _font()

    def _f(size):
        try:
            return ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        except Exception:
            return ImageFont.load_default()

    # accent bar
    draw.rectangle([(W // 2 - 70, H // 2 - 220), (W // 2 + 70, H // 2 - 206)], fill=accent)

    # logo on the end card if available
    y = H // 2 - 180
    if kind == "end":
        logo = brand.get("logo_path") or brand.get("logo")
        if logo and Path(logo).exists():
            try:
                lg = Image.open(logo).convert("RGBA")
                lg.thumbnail((300, 300))
                img.paste(lg, (W // 2 - lg.width // 2, y), lg)
                y += lg.height + 30
            except Exception:
                pass

    big = _f(74)
    for line in text_lines:
        # naive word-wrap to the frame width
        words, cur, wrapped = line.split(), "", []
        for wd in words:
            trial = (cur + " " + wd).strip()
            if draw.textlength(trial, font=big) > W - 160 and cur:
                wrapped.append(cur); cur = wd
            else:
                cur = trial
        if cur:
            wrapped.append(cur)
        for wl in wrapped:
            tw = draw.textlength(wl, font=big)
            draw.text((W // 2 - tw / 2, y), wl, font=big, fill=fg)
            y += 92

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def composite(clip_path: Path, script: dict, brand: dict, *,
              out_path: Path | None = None) -> Path:
    """Composite a branded vertical reel from a clip + a Script variant."""
    clip_path = Path(clip_path)
    if not clip_path.exists():
        raise FileNotFoundError(f"Clip not found: {clip_path}")
    vcfg = (brand.get("video") or {})

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    uid = hashlib.md5(f"{clip_path}|{script.get('id','')}".encode()).hexdigest()[:10]
    if out_path is None:
        out_path = OUT_DIR / f"reel_{uid}.mp4"

    duration = probe_duration(clip_path) or float(script.get("duration_seconds", 30))

    # captions → ASS (relative path keeps the Windows drive-colon out of the filter)
    ass_path = TMP_DIR / f"cap_{uid}.ass"
    has_caps = _build_ass(script.get("captions", []), vcfg, duration, ass_path)

    # hook + end cards
    hook_cfg = vcfg.get("hook_card", {}) or {}
    end_cfg  = vcfg.get("end_card", {}) or {}
    hook_dur = float(hook_cfg.get("duration", 2.0)) if hook_cfg.get("enabled", True) else 0
    end_dur  = float(end_cfg.get("duration", 3.0)) if end_cfg.get("enabled", True) else 0

    inputs = ["-i", str(clip_path)]
    overlays = []
    idx = 1
    if hook_dur > 0:
        hook_png = _card_png("hook", [script.get("hook") or script.get("angle", "")],
                             vcfg, brand, TMP_DIR / f"hook_{uid}.png")
        inputs += ["-i", str(hook_png)]
        overlays.append((idx, 0, hook_dur)); idx += 1
    if end_dur > 0:
        handle = brand.get("handle", "")
        end_png = _card_png("end", [handle, end_cfg.get("cta", "")],
                            vcfg, brand, TMP_DIR / f"end_{uid}.png")
        inputs += ["-i", str(end_png)]
        overlays.append((idx, max(0, duration - end_dur), duration)); idx += 1

    # base: cover-crop to 9:16 → colour grade → captions
    grade   = vcfg.get("color_grade", {}) or {}
    g_col   = _hex(grade.get("overlay", "#0A0F1E"))
    g_op    = float(grade.get("opacity", 0.15))
    chain = [f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"]
    if g_op > 0:
        chain.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=0x{g_col}@{g_op:.3f}:t=fill")
    if has_caps:
        chain.append(f"subtitles={ass_path.as_posix()}")
    label = "base"
    filter_parts = [",".join(chain) + f"[{label}]"]

    # overlay the cards on top for their windows
    for n, (in_idx, t0, t1) in enumerate(overlays):
        out_lbl = "vout" if n == len(overlays) - 1 else f"ov{n}"
        filter_parts.append(
            f"[{label}][{in_idx}:v]overlay=0:0:enable='between(t,{t0:.3f},{t1:.3f})'[{out_lbl}]")
        label = out_lbl
    if not overlays:
        # rename base → vout
        filter_parts[0] = ",".join(chain) + "[vout]"
    vmap = "[vout]"

    cmd = [_ffmpeg(), "-y", *inputs, "-filter_complex", ";".join(filter_parts),
           "-map", vmap, "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
           "-t", f"{duration:.3f}", str(out_path)]
    _run(cmd)
    return out_path


def list_outputs() -> list[Path]:
    if not OUT_DIR.exists():
        return []
    return sorted(OUT_DIR.glob("*.mp4"))
