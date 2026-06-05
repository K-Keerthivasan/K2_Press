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
) -> dict:
    if config is None:
        config = load_config()

    if total_slides is not None:
        n_content = max(1, min(8, total_slides - 2))
    else:
        n_content = _auto_content_cards(story, config)
        default_total = config.get("slides", {}).get("default_total", 4)
        # cap to max
        max_c = config.get("slides", {}).get("max_content_cards", 8)
        n_content = min(n_content, max_c)

    total = 1 + n_content + 1
    handle  = config.get("brand", {}).get("handle", "@k2digitalmedia_")
    profile = _profile()

    system = f"""You are an Instagram carousel content planner for K2 Digital Media, a full-service
digital agency (web, video, marketing, IT) based in London, Ontario, Canada.
You write clear, value-first posts — no fluff, no hype.

CREATOR PROFILE:
{profile}

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
  "hashtags":   ["london", "ontario", "digitalmedia"],
  "dm_keyword": "<one word>"
}}

RULES:
- content_slides MUST have EXACTLY {n_content} items.
- Every slide leads with value first, not background context.
- CTA is ONE action only (follow / DM / save / share).
- Make local London ON relevance explicit when it is not obvious.
- image_query must be a Pexels-compatible phrase (e.g. "city skyline night").
- Body bullets use em-dash format: — point one\\n— point two"""

    user = (
        f"Story title:   {story.title}\n"
        f"Story summary: {story.summary[:900]}\n"
        f"Source URL:    {story.url}"
    )

    return chat_json(system, user, model=model)


# ── Single-card formats (square / story / x) ─────────────────────────────────

def plan_single(
    story: Story,
    fmt: str,                    # "square" | "story" | "x"
    config: dict | None = None,
    model: str | None = None,
) -> dict:
    """Plan a single-card post. Returns format-appropriate JSON."""
    if config is None:
        config = load_config()

    handle  = config.get("brand", {}).get("handle", "@k2digitalmedia_")
    profile = _profile()

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

    system = f"""You are a content planner for K2 Digital Media, a digital agency (web, video,
marketing, IT) in London, Ontario. You write clear, value-first social posts — no fluff.

CREATOR PROFILE:
{profile}

FORMAT: {note}

Return ONE valid JSON object (no markdown, no code fences) with EXACTLY these keys:
{{
  "slug": "<kebab-case, max 40 chars>",
  "format": "{fmt}",
  "headline": "<punchy, value-first, max 70 chars>",
  "body": "<2-3 points, each starting with an em-dash, each on its own line>",
  "image_query": "<2-4 word Pexels/Unsplash phrase that fits this post>",
{extra_keys}  "caption": "<platform caption, 2-4 sentences, no hashtags>",
  "hashtags": ["london", "ontario", "digitalmedia"],
  "dm_keyword": "<one word>"
}}

RULES:
- Lead with value, not background.
- One clear CTA only.
- Make local London ON relevance explicit when not obvious.
- Do not invent facts beyond the source story."""

    user = (
        f"Story title:   {story.title}\n"
        f"Story summary: {story.summary[:900]}\n"
        f"Source URL:    {story.url}"
    )
    plan = chat_json(system, user, model=model)
    plan.setdefault("format", fmt)
    return plan


def plan_post(
    story: Story,
    fmt: str = "carousel",
    config: dict | None = None,
    total_slides: int | None = None,
    model: str | None = None,
) -> dict:
    """Dispatch: carousel -> multi-slide plan; everything else -> single-card plan."""
    if fmt == "carousel":
        plan = plan_story(story, config, total_slides=total_slides, model=model)
        plan.setdefault("format", "carousel")
        return plan
    return plan_single(story, fmt, config=config, model=model)


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
