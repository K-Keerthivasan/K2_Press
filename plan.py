"""Generate a structured carousel post plan for a single story via Ollama."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import yaml

from feeds import Story, fetch_stories, configured_feed_urls, load_config
from llm import chat_json

PROFILE_PATH = Path("content_profile.md")


def _profile() -> str:
    return PROFILE_PATH.read_text(encoding="utf-8")


def _safe_profile() -> str:
    try:
        return _profile()
    except OSError:
        return "A social media brand."


def _tone_directive(tone: str) -> str:
    """A system-prompt block that bends the generated content toward a chosen
    stance (e.g. 'positive', 'negative', 'critical') without inventing facts."""
    tone = (tone or "").strip()
    if not tone:
        return ""
    return (
        "\nTONE & PERSPECTIVE:\n"
        f"- Take a {tone} stance toward this topic.\n"
        "- Let that perspective shape the headline, the framing, and which points\n"
        "  you emphasise — while staying factual and true to the source story.\n"
    )


def _auto_content_cards(story: Story, config: dict) -> int:
    words     = len(story.summary.split())
    threshold = config.get("slides", {}).get("short_story", {}).get("max_summary_words", 220)
    if words <= threshold:
        return config.get("slides", {}).get("short_story", {}).get("content_cards", 2)
    return config.get("slides", {}).get("long_story", {}).get("content_cards", 4)


def plan_story(
    story: Story,
    config: dict | None = None,
    total_slides: int | None = None,   # explicit override (3-10)
    model: str | None = None,
    brand: dict | None = None,
    tone: str = "",                    # per-post stance (positive/negative/…)
) -> dict:
    if config is None:
        config = load_config()
    if brand is None:
        from brands import resolve_brand
        brand = resolve_brand(config)

    if total_slides is not None:
        n_content = max(1, min(8, total_slides - 2))
    else:
        n_content = _auto_content_cards(story, config)
        # cap to max
        max_c = config.get("slides", {}).get("max_content_cards", 8)
        n_content = min(n_content, max_c)

    total      = 1 + n_content + 1
    brand_name = brand.get("name", "the brand")
    handle     = brand.get("handle", "@handle")
    profile    = (brand.get("profile") or "").strip() or _safe_profile()
    tags       = json.dumps(brand.get("hashtags", ["news"]))
    location   = brand.get("location", "")
    loc_rule   = (f"- Make {location} relevance explicit when it is not obvious.\n"
                  if location else "")
    # Per-post tone wins; fall back to a brand-level default tone if set.
    eff_tone   = (tone or brand.get("tone") or "").strip()
    extra_ctx  = _tone_directive(eff_tone)

    system = f"""You are an Instagram carousel content planner for {brand_name}.
You write clear, value-first posts — no fluff, no hype.

CREATOR PROFILE:
{profile}
{extra_ctx}
Return ONE valid JSON object (no markdown, no code fences) with EXACTLY these keys:
{{
  "slug": "<kebab-case, max 40 chars>",
  "slide_count": {total},
  "title_card": {{
    "headline": "<punchy, lead with value, max 65 chars>",
    "subhead":  "<one compelling hook sentence, max 90 chars>"
  }},
  "content_slides": [
    {{
      "heading":     "<ALL CAPS, 2-5 words>",
      "body":        "<start each bullet with an em-dash —, 2-3 concrete points, each on its own line>",
      "image_query": "<2-4 word Pexels/Unsplash search phrase that visually fits this slide>"
    }}
  ],
  "outro_card": {{
    "cta":    "<single clear action, max 65 chars>",
    "handle": "{handle}"
  }},
  "caption":    "<Instagram caption, 3-4 sentences, no hashtags>",
  "hashtags":   {tags},
  "dm_keyword": "<one word>"
}}

RULES:
- content_slides MUST have EXACTLY {n_content} items.
- Every slide leads with value first, not background context.
- CTA is ONE action only (follow / DM / save / share).
{loc_rule}- image_query must be a Pexels-compatible phrase (e.g. "city skyline night").
- Body bullets use em-dash format: — point one\\n— point two"""

    user = (
        f"Story title:   {story.title}\n"
        f"Story summary: {story.summary[:900]}\n"
        f"Source URL:    {story.url}"
    )

    plan = chat_json(system, user, model=model)
    if eff_tone:
        plan["tone"] = eff_tone
    return plan


# ── Single-card formats (square / story / x) ─────────────────────────────────

def plan_single(
    story: Story,
    fmt: str,                    # "square" | "story" | "x"
    config: dict | None = None,
    model: str | None = None,
    brand: dict | None = None,
    tone: str = "",              # per-post stance (positive/negative/…)
) -> dict:
    """Plan a single-card post. Returns format-appropriate JSON."""
    if config is None:
        config = load_config()
    if brand is None:
        from brands import resolve_brand
        brand = resolve_brand(config)

    brand_name = brand.get("name", "the brand")
    handle     = brand.get("handle", "@handle")
    profile    = (brand.get("profile") or "").strip() or _safe_profile()
    tags       = json.dumps(brand.get("hashtags", ["news"]))
    location   = brand.get("location", "")
    loc_rule   = (f"- Make {location} relevance explicit when it is not obvious.\n"
                  if location else "")
    eff_tone   = (tone or brand.get("tone") or "").strip()
    extra_ctx  = _tone_directive(eff_tone)

    fmt_notes = {
        "square": "A single square (1080x1080) Instagram feed post. One bold headline plus "
                  "2-3 em-dash bullet points. Punchy and self-contained.",
        "story":  "A vertical 1080x1920 Instagram/Facebook story. One headline, 1-2 short "
                  "lines, and a 'cta' field with a 2-4 word tap action (e.g. 'DM us GROW').",
        "x":      "A landscape 1600x900 image for X/Twitter. Headline + 2-3 short bullets. "
                  "Also write 'tweet_text': the actual post text under 270 chars, with 1-2 "
                  "hashtags inline.",
    }
    note = fmt_notes.get(fmt, fmt_notes["square"])

    extra_keys = ""
    if fmt == "story":
        extra_keys = '  "cta": "<2-4 word tap action>",\n'
    if fmt == "x":
        extra_keys = '  "tweet_text": "<the X post text, under 270 chars, 1-2 inline hashtags>",\n'

    system = f"""You are a content planner for {brand_name}. You write clear, value-first
social posts — no fluff.

CREATOR PROFILE:
{profile}
{extra_ctx}
FORMAT: {note}

Return ONE valid JSON object (no markdown, no code fences) with EXACTLY these keys:
{{
  "slug": "<kebab-case, max 40 chars>",
  "format": "{fmt}",
  "headline": "<punchy, value-first, max 70 chars>",
  "body": "<2-3 points, each starting with an em-dash, each on its own line>",
  "image_query": "<2-4 word Pexels/Unsplash phrase that fits this post>",
{extra_keys}  "caption": "<platform caption, 2-4 sentences, no hashtags>",
  "hashtags": {tags},
  "dm_keyword": "<one word>"
}}

RULES:
- Lead with value, not background.
- One clear CTA only.
{loc_rule}- Do not invent facts beyond the source story."""

    user = (
        f"Story title:   {story.title}\n"
        f"Story summary: {story.summary[:900]}\n"
        f"Source URL:    {story.url}"
    )
    plan = chat_json(system, user, model=model)
    plan.setdefault("format", fmt)
    if eff_tone:
        plan["tone"] = eff_tone
    return plan


def plan_post(
    story: Story,
    fmt: str = "carousel",
    config: dict | None = None,
    total_slides: int | None = None,
    model: str | None = None,
    brand: dict | None = None,
    tone: str = "",
) -> dict:
    """Dispatch: carousel -> multi-slide plan; everything else -> single-card plan."""
    if fmt == "carousel":
        plan = plan_story(story, config, total_slides=total_slides, model=model,
                          brand=brand, tone=tone)
        plan.setdefault("format", "carousel")
        return plan
    if fmt == "cover":
        # The brand-cover card is purely static brand furniture — no LLM call.
        return {"format": "cover", "slug": "brand-cover"}
    return plan_single(story, fmt, config=config, model=model, brand=brand, tone=tone)


def regen_caption(plan: dict, brand: dict | None = None, config: dict | None = None,
                  model: str | None = None, tone: str = "") -> dict:
    """Regenerate just the caption + hashtags from an existing plan's content."""
    if config is None:
        config = load_config()
    if brand is None:
        from brands import resolve_brand
        brand = resolve_brand(config)
    brand_name = brand.get("name", "the brand")
    tags       = json.dumps(brand.get("hashtags", ["news"]))

    parts: list[str] = []
    tc = plan.get("title_card", {}) or {}
    parts += [tc.get("headline", ""), tc.get("subhead", "")]
    for s in plan.get("content_slides", []) or []:
        parts += [s.get("heading", ""), s.get("body", "")]
    parts += [plan.get("headline", ""), plan.get("body", ""), plan.get("caption", "")]
    content = "\n".join(p for p in parts if p)
    tone_line = f"Desired tone: {tone}\n" if tone else ""

    system = f"""You write Instagram captions for {brand_name}.
{tone_line}Return ONE valid JSON object (no markdown) with EXACTLY these keys:
{{"caption": "<engaging 3-4 sentence caption, no hashtags>", "hashtags": {tags}}}
Rules: caption is on-brand and value-first; provide 6-12 relevant hashtags (lowercase, no #)."""
    user = f"Post content:\n{content[:1400]}"
    res  = chat_json(system, user, model=model)
    tags_out = res.get("hashtags", [])
    if isinstance(tags_out, str):
        tags_out = [t.strip().lstrip("#") for t in tags_out.split() if t.strip()]
    return {"caption": res.get("caption", ""), "hashtags": tags_out}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a carousel plan for one story.")
    parser.add_argument("--story-index",  type=int, default=0)
    parser.add_argument("--total-slides", type=int, default=None,
                        help="Total slides 3-10 (overrides auto-detect)")
    parser.add_argument("--title",   help="Story title (skips feed fetch)")
    parser.add_argument("--summary", help="Story summary")
    parser.add_argument("--url",     default="")
    parser.add_argument("--model",   help="Ollama model override")
    parser.add_argument("--config-feeds", action="store_true")
    args = parser.parse_args()

    config = load_config()

    if args.title and args.summary:
        story = Story(title=args.title, summary=args.summary,
                      url=args.url, published="")
    else:
        urls = configured_feed_urls(config, use_config_feeds=args.config_feeds)
        if not urls:
            raise SystemExit("No feed URLs found.")
        stories = fetch_stories(urls)
        story   = stories[args.story_index]
        print(f"Story [{args.story_index}]: {story.title}\n")

    print("Planning with Ollama...")
    plan = plan_story(story, config,
                      total_slides=args.total_slides,
                      model=args.model)
    print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
