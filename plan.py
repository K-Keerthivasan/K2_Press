"""Generate structured social post plans through the configured LLM backend."""
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


def _stamp_source(plan: dict, story: Story) -> dict:
    """Attach the source article's image + URL to the plan so the "feed" image
    source can use the article's own hero image (with a Pexels fallback)."""
    if isinstance(plan, dict):
        plan.setdefault("source_image", story.image or "")
        plan.setdefault("source_url", story.url or "")
    return plan


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


PERSONALITY_TEXT_KEYS = {
    "headline", "subhead", "heading", "body", "caption", "cta", "tweet_text",
    "hook", "take", "points", "quote", "context", "source_label", "verdict",
    "banner",
}


def _personality(brand: dict) -> str:
    return (brand.get("personality") or "").strip()


def _voice_block(brand: dict) -> str:
    personality = _personality(brand)
    if not personality:
        return ""
    return (
        "\nBRAND PERSONALITY:\n"
        f"{personality}\n"
        "Apply this voice to the actual post copy. Keep facts, schema, and source meaning intact.\n"
    )


def _merge_personality_copy(base, edited):
    """Copy only safe text fields from a personality edit result into a plan."""
    if isinstance(base, dict) and isinstance(edited, dict):
        out = dict(base)
        for key, old_value in base.items():
            if key not in edited:
                continue
            new_value = edited[key]
            if key in PERSONALITY_TEXT_KEYS and isinstance(old_value, str) and isinstance(new_value, str):
                out[key] = new_value.strip() or old_value
            elif isinstance(old_value, (dict, list)):
                out[key] = _merge_personality_copy(old_value, new_value)
        return out
    if isinstance(base, list) and isinstance(edited, list):
        return [
            _merge_personality_copy(old, edited[i]) if i < len(edited) else old
            for i, old in enumerate(base)
        ]
    return base


def _polish_personality(plan: dict, story: Story, brand: dict, model: str | None,
                        tone: str = "") -> dict:
    personality = _personality(brand)
    if not personality:
        return plan
    brand_name = brand.get("name", "the brand")
    eff_tone = (tone or brand.get("tone") or plan.get("tone") or "").strip()
    tone_line = f"\nREQUESTED TONE:\n{eff_tone}\n" if eff_tone else ""
    system = f"""You are the final copy editor for {brand_name}.
Rewrite the post copy so it sounds unmistakably on-brand, while preserving the
existing JSON shape and all source facts.

BRAND PERSONALITY:
{personality}
{tone_line}
Return ONE valid JSON object with the same structure as the input plan.

EDITING RULES:
- Rewrite only copy fields: headlines, subheads, headings, body text, captions, CTA, hooks, takes, verdicts, quotes, banners, and tweet text.
- Do not change facts, counts, ranks, formats, URLs, image_query, hashtags, slug, handles, or dm_keyword.
- Add personality through sharper framing, more specific stakes, and stronger audience relevance.
- Keep copy concise enough for social graphics.
- Do not add claims not supported by the source story."""
    user = (
        f"Source story:\nTitle: {story.title}\nSummary: {story.summary[:900]}\nURL: {story.url}\n\n"
        "Current plan JSON:\n"
        f"{json.dumps(plan, ensure_ascii=False)}"
    )
    try:
        edited = chat_json(system, user, model=model, retries=0)
    except Exception as exc:
        print(f"[plan] personality polish skipped: {exc}")
        return plan
    polished = _merge_personality_copy(plan, edited)
    if eff_tone:
        polished["tone"] = eff_tone
    return polished


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
    voice_ctx  = _voice_block(brand)

    system = f"""You are an Instagram carousel content planner for {brand_name}.
You write clear, value-first posts — no fluff, no hype.

CREATOR PROFILE:
{profile}
{voice_ctx}
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
    return _stamp_source(_polish_personality(plan, story, brand, model, eff_tone), story)


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
    voice_ctx  = _voice_block(brand)

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
{voice_ctx}
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
    return _stamp_source(_polish_personality(plan, story, brand, model, eff_tone), story)


# ── New post types (quote / comparison / breaking / linkedin / listicle) ──────

def _brand_ctx(brand: dict, tone: str) -> dict:
    """Shared brand prompt context used by the specialised planners."""
    location = brand.get("location", "")
    eff_tone = (tone or brand.get("tone") or "").strip()
    return {
        "name":      brand.get("name", "the brand"),
        "handle":    brand.get("handle", "@handle"),
        "profile":   (brand.get("profile") or "").strip() or _safe_profile(),
        "voice_ctx":  _voice_block(brand),
        "tags":      json.dumps(brand.get("hashtags", ["news"])),
        "location":  location,
        "loc_rule":  (f"- Make {location} relevance explicit when not obvious.\n"
                      if location else ""),
        "extra_ctx": _tone_directive(eff_tone),
        "eff_tone":  eff_tone,
    }


# Per-format JSON schema body + a one-line description, injected into a shared
# system prompt. Each schema is a single JSON object the model must return.
_SPECIAL_FORMATS = {
    "quote": {
        "desc": "A single bold quote/stat card (1080x1350). Pull out ONE striking "
                "statistic or quotable line from the story.",
        "schema": '''  "quote":        "<the striking stat or pull-quote, max 120 chars>",
  "context":      "<one supporting line that frames it, max 90 chars>",
  "source_label": "<short attribution e.g. 'via TechCrunch', max 40 chars>",
  "image_query":  "<2-4 word Pexels phrase that fits the mood>",''',
    },
    "comparison": {
        "desc": "A single A-vs-B comparison card (1080x1350). Compare two things "
                "from the story (tools, options, approaches, before/after).",
        "schema": '''  "headline":  "<what is being compared, max 70 chars>",
  "option_a":  {"label": "<name, max 24 chars>", "points": "<2-3 em-dash bullets, each on its own line>"},
  "option_b":  {"label": "<name, max 24 chars>", "points": "<2-3 em-dash bullets, each on its own line>"},
  "verdict":   "<the takeaway / who wins, max 90 chars>",
  "image_query": "<2-4 word Pexels phrase>",''',
    },
    "breaking": {
        "desc": "A single reactive 'breaking news / hot take' card (1080x1350). "
                "Fast, punchy, opinionated.",
        "schema": '''  "banner":      "<2-3 word label, ALL CAPS, e.g. BREAKING or HOT TAKE>",
  "headline":    "<the news or take, max 80 chars>",
  "take":        "<1-2 punchy sentences of reaction, max 170 chars>",
  "image_query": "<2-4 word Pexels phrase>",''',
    },
    "linkedin": {
        "desc": "A single square (1200x1200) LinkedIn post — a professional hot "
                "take for a business audience. Confident but not clickbait.",
        "schema": '''  "hook":        "<scroll-stopping first line, max 90 chars>",
  "take":        "<the opinion/insight, 1-2 sentences, max 180 chars>",
  "points":      "<2-3 em-dash bullets, each on its own line>",
  "cta":         "<one professional action, max 60 chars>",
  "image_query": "<2-4 word Pexels phrase>",''',
    },
}


def plan_special(
    story: Story,
    fmt: str,
    config: dict | None = None,
    model: str | None = None,
    brand: dict | None = None,
    tone: str = "",
) -> dict:
    """Plan one of the new single-card formats (quote/comparison/breaking/linkedin)."""
    if config is None:
        config = load_config()
    if brand is None:
        from brands import resolve_brand
        brand = resolve_brand(config)
    spec = _SPECIAL_FORMATS[fmt]
    ctx  = _brand_ctx(brand, tone)

    system = f"""You are a content planner for {ctx['name']}. You write sharp,
value-first social posts — no fluff.

CREATOR PROFILE:
{ctx['profile']}
{ctx['voice_ctx']}
{ctx['extra_ctx']}
FORMAT: {spec['desc']}

Return ONE valid JSON object (no markdown, no code fences) with EXACTLY these keys:
{{
  "slug":   "<kebab-case, max 40 chars>",
  "format": "{fmt}",
{spec['schema']}
  "caption":  "<platform caption, 2-4 sentences, no hashtags>",
  "hashtags": {ctx['tags']},
  "dm_keyword": "<one word>"
}}

RULES:
- Lead with value, not background.
- Stay factual and true to the source story; do not invent specifics.
{ctx['loc_rule']}- Body bullets use em-dash format: — point one\\n— point two"""

    user = (
        f"Story title:   {story.title}\n"
        f"Story summary: {story.summary[:900]}\n"
        f"Source URL:    {story.url}"
    )
    plan = chat_json(system, user, model=model)
    plan.setdefault("format", fmt)
    if ctx["eff_tone"]:
        plan["tone"] = ctx["eff_tone"]
    return _stamp_source(_polish_personality(plan, story, brand, model, ctx["eff_tone"]), story)


def plan_listicle(
    story: Story,
    config: dict | None = None,
    total_slides: int | None = None,
    model: str | None = None,
    brand: dict | None = None,
    tone: str = "",
) -> dict:
    """Plan a numbered Top-N listicle carousel (each content slide is a ranked item)."""
    if config is None:
        config = load_config()
    if brand is None:
        from brands import resolve_brand
        brand = resolve_brand(config)
    ctx = _brand_ctx(brand, tone)

    n_items = max(3, min(8, (total_slides - 2) if total_slides else 5))
    total   = 1 + n_items + 1

    system = f"""You are an Instagram listicle planner for {ctx['name']}.
You build punchy numbered "Top {n_items}" carousels — value first, no fluff.

CREATOR PROFILE:
{ctx['profile']}
{ctx['voice_ctx']}
{ctx['extra_ctx']}
Return ONE valid JSON object (no markdown, no code fences) with EXACTLY these keys:
{{
  "slug": "<kebab-case, max 40 chars>",
  "format": "listicle",
  "slide_count": {total},
  "title_card": {{
    "headline": "<e.g. 'Top {n_items} ...', max 60 chars>",
    "subhead":  "<one hook sentence, max 90 chars>"
  }},
  "content_slides": [
    {{
      "rank":        <integer countdown position>,
      "heading":     "<the item name, ALL CAPS, 2-5 words>",
      "body":        "<1-2 em-dash lines on why it matters>",
      "image_query": "<2-4 word Pexels phrase that fits this item>"
    }}
  ],
  "outro_card": {{ "cta": "<single clear action, max 65 chars>", "handle": "{ctx['handle']}" }},
  "caption":    "<Instagram caption, 3-4 sentences, no hashtags>",
  "hashtags":   {ctx['tags']},
  "dm_keyword": "<one word>"
}}

RULES:
- content_slides MUST have EXACTLY {n_items} items, ranked {n_items} down to 1 (countdown).
- Each item is concrete and distinct; lead with the item, not background.
{ctx['loc_rule']}- Body bullets use em-dash format: — point one\\n— point two"""

    user = (
        f"Story title:   {story.title}\n"
        f"Story summary: {story.summary[:900]}\n"
        f"Source URL:    {story.url}"
    )
    plan = chat_json(system, user, model=model)
    plan.setdefault("format", "listicle")
    if ctx["eff_tone"]:
        plan["tone"] = ctx["eff_tone"]
    return _stamp_source(_polish_personality(plan, story, brand, model, ctx["eff_tone"]), story)


def plan_post(
    story: Story,
    fmt: str = "carousel",
    config: dict | None = None,
    total_slides: int | None = None,
    model: str | None = None,
    brand: dict | None = None,
    tone: str = "",
) -> dict:
    """Dispatch: carousel -> multi-slide plan; everything else -> single-card plan.

    Always returns a plan with a non-empty caption (filled from content if the
    model left it blank), so saved/published posts never need a manual regen."""
    if fmt == "cover":
        # The brand-cover card is purely static brand furniture — no LLM call.
        return {"format": "cover", "slug": "brand-cover"}
    if fmt == "carousel":
        plan = plan_story(story, config, total_slides=total_slides, model=model,
                          brand=brand, tone=tone)
        plan.setdefault("format", "carousel")
    elif fmt == "listicle":
        plan = plan_listicle(story, config, total_slides=total_slides, model=model,
                             brand=brand, tone=tone)
    elif fmt in _SPECIAL_FORMATS:
        plan = plan_special(story, fmt, config=config, model=model, brand=brand, tone=tone)
    else:
        plan = plan_single(story, fmt, config=config, model=model, brand=brand, tone=tone)
    return _ensure_caption(plan, brand=brand, config=config, model=model, tone=tone)


def _ensure_caption(plan: dict, *, brand=None, config=None, model=None, tone="") -> dict:
    """Guarantee a non-empty caption. If the generator left it blank, fill it
    (and hashtags, if missing) from the plan's own content via regen_caption."""
    if (plan.get("caption") or "").strip():
        return plan
    try:
        out = regen_caption(plan, brand=brand, config=config, model=model, tone=tone)
        if out.get("caption"):
            plan["caption"] = out["caption"]
        if out.get("hashtags") and not plan.get("hashtags"):
            plan["hashtags"] = out["hashtags"]
    except Exception as e:
        print(f"[plan] caption fallback failed: {e}")
    return plan


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
    voice_ctx  = _voice_block(brand)

    parts: list[str] = []
    tc = plan.get("title_card", {}) or {}
    parts += [tc.get("headline", ""), tc.get("subhead", "")]
    for s in plan.get("content_slides", []) or []:
        parts += [s.get("heading", ""), s.get("body", "")]
    parts += [plan.get("headline", ""), plan.get("body", ""), plan.get("caption", "")]
    content = "\n".join(p for p in parts if p)
    tone_line = f"Desired tone: {tone}\n" if tone else ""

    system = f"""You write Instagram captions for {brand_name}.
{voice_ctx}
{tone_line}Return ONE valid JSON object (no markdown) with EXACTLY these keys:
{{"caption": "<engaging 3-4 sentence caption, no hashtags>", "hashtags": {tags}}}
Rules: caption is on-brand, personality-rich, and value-first; provide 6-12 relevant hashtags (lowercase, no #)."""
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
    parser.add_argument("--model",   help="LLM model override")
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

    print("Planning with the configured LLM backend...")
    plan = plan_story(story, config,
                      total_slides=args.total_slides,
                      model=args.model)
    print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
