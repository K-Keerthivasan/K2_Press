"""Score and rank stories for relevance to the content profile."""
from __future__ import annotations
import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path

from feeds import Story, fetch_stories, configured_feed_urls, load_config
from llm import chat_json

PROFILE_PATH = Path("content_profile.md")


@dataclass
class ScoredStory:
    score: int
    reason: str
    title: str
    summary: str
    url: str
    published: str


def _load_profile() -> str:
    return PROFILE_PATH.read_text(encoding="utf-8")


def _score_one(story: Story, profile: str, brand_name: str = "the brand",
               model: str | None = None) -> ScoredStory:
    system = (
        f"You are a content relevance judge for {brand_name}, which posts Instagram content.\n\n"
        f"CREATOR PROFILE:\n{profile}\n\n"
        "Score the story 0-100 for how well it fits as a post for this brand and its "
        "audience. Reward stories that map onto the niche AND can be turned into an engaging "
        "post; penalise off-topic material. Return ONLY valid JSON:\n"
        '{"score": <int 0-100>, "reason": "<one sentence why it does or does not fit>"}'
    )
    user = (
        f"Title: {story.title}\n"
        f"Summary: {story.summary[:600]}\n"
        f"URL: {story.url}"
    )
    result = chat_json(system, user, model=model)
    return ScoredStory(
        score=int(result.get("score", 0)),
        reason=result.get("reason", ""),
        title=story.title,
        summary=story.summary,
        url=story.url,
        published=story.published,
    )


def rank_stories(stories: list[Story], top_n: int = 5, model: str | None = None,
                 brand: dict | None = None, should_cancel=None) -> list[ScoredStory]:
    if brand:
        profile    = (brand.get("profile") or "").strip() or _safe_profile()
        brand_name = brand.get("name", "the brand")
    else:
        profile    = _safe_profile()
        brand_name = "K2 Digital Media"
    scored: list[ScoredStory] = []
    for s in stories:
        if should_cancel and should_cancel():
            print(f"[filter] cancelled — scored {len(scored)} before stopping")
            break
        try:
            scored.append(_score_one(s, profile, brand_name, model))
        except Exception as exc:
            print(f"[filter] WARN  '{s.title[:60]}': {exc}")
    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[:top_n]


def _safe_profile() -> str:
    try:
        return _load_profile()
    except OSError:
        return "A social media brand."


def main() -> None:
    parser = argparse.ArgumentParser(description="Score and rank feed stories by relevance.")
    parser.add_argument("--limit",       type=int, default=20, help="Stories to fetch per feed")
    parser.add_argument("--top",         type=int, default=5,  help="Top N stories to return")
    parser.add_argument("--config-feeds", action="store_true", help="Use configured feed URLs")
    parser.add_argument("--json",        action="store_true",  help="Print as JSON")
    args = parser.parse_args()

    config = load_config()
    urls = configured_feed_urls(config, use_config_feeds=args.config_feeds)
    if not urls:
        raise SystemExit("No feed URLs found. Check config.yaml.")

    print(f"Fetching from {len(urls)} feed(s)...")
    stories = fetch_stories(urls)[: args.limit]
    print(f"Scoring {len(stories)} stories with Ollama...\n")

    ranked = rank_stories(stories, args.top)

    if args.json:
        print(json.dumps([asdict(r) for r in ranked], indent=2))
        return

    for i, r in enumerate(ranked, 1):
        print(f"{i}. [{r.score:>3}/100] {r.title}")
        print(f"         {r.reason}")
        print(f"         {r.url}\n")


if __name__ == "__main__":
    main()
