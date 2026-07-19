"""Content-script generation — 3-5 platform-specific script variants per topic/story.

Produces voiceover/caption/ad-copy scripts with beat timestamps and word-timed
captions so the video pipeline (compositing) can overlay them directly. Reuses
the same LLM backend (``llm.chat_json``) and brand voice as the post planner.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from feeds import Story, load_config
from llm import chat_json

PROFILE_PATH = Path("content_profile.md")

# Platform → tone guidance baked into the prompt. Keeps IG ≠ TikTok ≠ Shorts ≠ LinkedIn.
PLATFORMS = {
    "instagram_reel": "Instagram Reel. Polished, aspirational, value-first. Hook in the first 2s. "
                      "Conversational but on-brand. 1-2 light emoji max.",
    "tiktok":        "TikTok. Fast, casual, native, slightly irreverent. Pattern-interrupt hook, "
                     "punchy lines, trend-aware. No corporate tone.",
    "youtube_short": "YouTube Short. Clear, informative, retention-driven. Strong curiosity hook, "
                     "tight pacing, payoff at the end.",
    "linkedin":      "LinkedIn video. Professional, credible, insight-led. Confident point of view "
                     "for a business audience. No hype, no slang.",
}

# Content angle → what the script should do.
CONTENT_TYPES = {
    "educational": "Teach one concrete, useful idea the audience can act on.",
    "proof":       "Show a result/case/number that builds credibility.",
    "testimonial": "Frame it as a client/customer win in an authentic voice.",
    "story":       "Tell a short narrative with a hook, tension, and resolution.",
    "tip":         "Deliver a sharp, quick actionable tip.",
}

DEFAULT_DURATION = 30


def _safe_profile() -> str:
    try:
        return PROFILE_PATH.read_text(encoding="utf-8")
    except OSError:
        return "A social media brand."


def _voice_block(brand: dict) -> str:
    personality = (brand.get("personality") or "").strip()
    if not personality:
        return ""
    return f"\nBRAND PERSONALITY:\n{personality}\nApply this voice to the script copy.\n"


def _normalize_variant(raw: dict, idx: int, platform: str, content_type: str,
                       duration: int) -> dict:
    """Coerce a model variant into the canonical script schema."""
    def _num_list(v):
        out = []
        if isinstance(v, list):
            for x in v:
                try:
                    out.append(round(float(x), 2))
                except (TypeError, ValueError):
                    continue
        return out

    captions = []
    raw_caps = raw.get("captions")
    if isinstance(raw_caps, list):
        for c in raw_caps:
            if isinstance(c, dict) and c.get("text"):
                try:
                    t = round(float(c.get("time", 0)), 2)
                except (TypeError, ValueError):
                    t = 0.0
                captions.append({"time": t, "text": str(c["text"]).strip()})

    # Ordered, editable segments (label/time/text).
    segments = []
    raw_segs = raw.get("segments")
    if isinstance(raw_segs, list):
        for s in raw_segs:
            if isinstance(s, dict) and s.get("text"):
                try:
                    t = round(float(s.get("time", 0)), 2)
                except (TypeError, ValueError):
                    t = 0.0
                segments.append({"label": str(s.get("label") or "Beat").strip(),
                                 "time": t, "text": str(s["text"]).strip()})
    segments.sort(key=lambda s: s["time"])

    voice_over = str(raw.get("voice_over") or raw.get("script") or "").strip()
    if not voice_over and segments:
        voice_over = " ".join(s["text"] for s in segments)

    beats = _num_list(raw.get("beat_timestamps"))
    if not beats:
        beats = [s["time"] for s in segments] or [c["time"] for c in captions]

    return {
        "id":             f"script_{idx:03d}",
        "angle":          str(raw.get("angle") or f"Variant {idx}").strip(),
        "platform":       platform,
        "content_type":   content_type,
        "hook":           str(raw.get("hook") or "").strip(),
        "segments":       segments,
        "voice_over":     voice_over,
        "duration_seconds": int(raw.get("duration_seconds") or duration),
        "beat_timestamps": beats,
        "captions":       captions,
        "cta":            str(raw.get("cta") or "").strip(),
        "caption_text":   str(raw.get("caption_text") or "").strip(),
    }


def generate_scripts(
    *,
    story: Story | None = None,
    topic: str = "",
    keywords: list[str] | None = None,
    brand: dict | None = None,
    config: dict | None = None,
    platform: str = "instagram_reel",
    content_type: str = "educational",
    num_variants: int = 3,
    duration: int = DEFAULT_DURATION,
    model: str | None = None,
    outline: list[str] | None = None,   # user's own beats/segments (one per point)
    hook: str = "",                     # user's own opening line (optional)
    cta: str = "",                      # user's own closing CTA (optional)
) -> list[dict]:
    """Generate ``num_variants`` script variants for a story or free topic."""
    if config is None:
        config = load_config()
    if brand is None:
        from brands import resolve_brand
        brand = resolve_brand(config)

    num_variants = max(1, min(int(num_variants or 3), 5))
    platform = platform if platform in PLATFORMS else "instagram_reel"
    content_type = content_type if content_type in CONTENT_TYPES else "educational"
    duration = max(8, min(int(duration or DEFAULT_DURATION), 90))

    brand_name = brand.get("name", "the brand")
    handle     = brand.get("handle", "@handle")
    profile    = (brand.get("profile") or "").strip() or _safe_profile()
    voice_ctx  = _voice_block(brand)
    plat_tone  = PLATFORMS[platform]
    ct_goal    = CONTENT_TYPES[content_type]
    kw_line    = (", ".join(keywords) if keywords else "")

    system = f"""You are a short-form video scriptwriter for {brand_name}.
You write tight, spoken-word scripts for vertical social video.

CREATOR PROFILE:
{profile}
{voice_ctx}
PLATFORM: {plat_tone}
CONTENT GOAL: {ct_goal}
TARGET DURATION: ~{duration} seconds.

Return ONE valid JSON object (no markdown, no code fences) with EXACTLY this shape:
{{
  "scripts": [
    {{
      "angle":            "<short name for this script's angle, max 40 chars>",
      "hook":             "<the spoken opening line, must grab attention in the first 2s>",
      "segments":         [{{"label": "<Hook | Beat 1 | Beat 2 | … | CTA>", "time": <seconds this segment starts, ascending from 0>, "text": "<the spoken line(s) for this segment>"}}],
      "voice_over":       "<the full voiceover = all segment texts read in order>",
      "duration_seconds": {duration},
      "beat_timestamps":  [<seconds for each beat/scene change, starting at 0, ascending, last ≈ {duration}>],
      "captions":         [{{"time": <seconds>, "text": "<on-screen caption phrase, 2-6 words>"}}],
      "cta":              "<one clear closing call to action>",
      "caption_text":     "<the social post caption, 1-3 sentences, no hashtags>"
    }}
  ]
}}

RULES:
- Produce EXACTLY {num_variants} distinct script variants — each a genuinely different angle.
- Break each script into ordered "segments" (Hook first, CTA last); segment times ascend from 0 to ~{duration}. voice_over is those segment texts joined.
- voice_over must be spoken-word (what a narrator says), not bullet points.
- captions must cover the whole script, synced to beat_timestamps; each caption is short enough to read on a phone.
- beat_timestamps and caption times are ascending and within 0..{duration}.
- Stay factual and true to the source; do not invent specific numbers or quotes.
- Handle for the brand is {handle}."""

    # The user may supply their own structure: an outline (their beats/segments),
    # a hook line, and/or a CTA. When given, the script must follow it.
    struct = ""
    if outline:
        pts = "\n".join(f"  {i+1}. {p}" for i, p in enumerate(outline) if p)
        if pts:
            struct += ("\nMY OUTLINE — follow these as the ordered segments (one segment per point, "
                       f"plus a Hook first and a CTA last):\n{pts}\n")
    if hook:
        struct += f"\nUSE THIS HOOK (opening line): {hook}\n"
    if cta:
        struct += f"\nUSE THIS CTA (closing line): {cta}\n"

    if story is not None:
        user = (
            f"Source story:\nTitle: {story.title}\nSummary: {story.summary[:900]}\nURL: {story.url}\n"
            f"{('Keywords: ' + kw_line) if kw_line else ''}{struct}"
        )
    else:
        user = (
            f"Topic: {topic or 'an on-brand topic for this audience'}\n"
            f"{('Keywords: ' + kw_line) if kw_line else ''}{struct}"
        )

    result = chat_json(system, user, model=model)
    raw_scripts = result.get("scripts")
    if not isinstance(raw_scripts, list) or not raw_scripts:
        # Some models return a bare list or a single object — be forgiving.
        if isinstance(result, list):
            raw_scripts = result
        elif result.get("voice_over") or result.get("hook"):
            raw_scripts = [result]
        else:
            raise RuntimeError("Script generation returned no scripts.")

    return [
        _normalize_variant(rs if isinstance(rs, dict) else {}, i + 1,
                           platform, content_type, duration)
        for i, rs in enumerate(raw_scripts[:num_variants])
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate content-script variants.")
    parser.add_argument("--topic", default="why website speed matters for local business")
    parser.add_argument("--platform", default="instagram_reel", choices=list(PLATFORMS))
    parser.add_argument("--content-type", default="educational", choices=list(CONTENT_TYPES))
    parser.add_argument("--num", type=int, default=3)
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION)
    parser.add_argument("--model")
    args = parser.parse_args()

    scripts = generate_scripts(
        topic=args.topic, platform=args.platform, content_type=args.content_type,
        num_variants=args.num, duration=args.duration, model=args.model,
    )
    print(json.dumps(scripts, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
