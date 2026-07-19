"""Score and rank stories for relevance to the content profile."""
from __future__ import annotations
import argparse
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

from feeds import Story, fetch_stories, configured_feed_urls, load_config
from llm import chat_json

PROFILE_PATH = Path("content_profile.md")

# ── Near-duplicate detection ────────────────────────────────────────────────────
# Different outlets often cover the same real-world event with very differently
# WORDED headlines (so simple title matching misses most duplicates), but their
# title+summary usually restate the same concrete facts/entities. Comparing on
# significant word overlap of title+summary — not just the title — reliably
# separates "same event, different outlet" (~0.5+) from genuinely different
# stories (~0.0), calibrated against real headline pairs.
_DEDUPE_THRESHOLD = 0.42
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on", "for", "and",
    "or", "with", "its", "it's", "this", "that", "new", "after", "before", "says", "say",
    "said", "how", "why", "what", "will", "has", "have", "into", "from", "by", "as", "at",
    "be", "been", "not", "no", "but", "so", "up", "out", "over", "under", "than", "you",
    "your", "now", "just", "could", "would", "should", "report", "reportedly", "confirms",
    "confirmed", "according", "announced", "announces", "week", "today",
}


def _sig_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _title_similarity(a: str, b: str) -> float:
    """Overlap coefficient (intersection / smaller set) — more forgiving than
    Jaccard when headline lengths differ a lot, which they usually do."""
    wa, wb = _sig_words(a), _sig_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def dedupe_similar_stories(stories: list, threshold: float = _DEDUPE_THRESHOLD) -> list:
    """Drop near-duplicate coverage of the same event (different outlets, same
    story) so the same headline doesn't get generated into multiple posts.
    Expects items sorted best-first; keeps the first (highest-ranked) of each
    cluster. Works on Story or ScoredStory — anything with .title/.summary."""
    kept: list = []
    kept_text: list[str] = []
    for s in stories:
        text = f"{s.title} {(s.summary or '')[:300]}"
        dup = next((kt for kt in kept_text if _title_similarity(text, kt) >= threshold), None)
        if dup is not None:
            print(f"[filter] near-duplicate dropped: '{s.title[:60]}'")
            continue
        kept.append(s)
        kept_text.append(text)
    return kept


@dataclass
class ScoredStory:
    score: int
    reason: str
    title: str
    summary: str
    url: str
    published: str
    image: str = ""


def _load_profile() -> str:
    return PROFILE_PATH.read_text(encoding="utf-8")


def _build_score_system(profile: str, brand_name: str = "the brand",
                        brand: dict | None = None) -> str:
    guidance = (brand.get("scoring_guidance") or "").strip() if brand else ""
    system = (
        f"You are a content relevance judge for {brand_name}, which posts Instagram content.\n\n"
        f"CREATOR PROFILE:\n{profile}\n\n"
        "Score the story 0-100 for how well it fits as a post for this brand and its "
        "audience. Reward stories that map onto the niche AND can be turned into an engaging "
        "post; penalise off-topic material."
    )
    if guidance:
        system += f"\n\nBRAND SCORING GUIDANCE:\n{guidance}"
    system += "\n\nReturn ONLY valid JSON:\n"
    system += '{"score": <int 0-100>, "reason": "<one sentence why it does or does not fit>"}'
    return system


def _score_batch(stories: list[Story], profile: str, brand_name: str = "the brand",
                 model: str | None = None, brand: dict | None = None) -> list[ScoredStory]:
    """Score multiple stories in a single LLM call for speed. Falls back to individual
    scoring if batch parsing fails."""
    if not stories:
        return []
    
    # Build batch request with all stories
    story_text = "\n\n".join([
        f"[{i}] Title: {s.title}\nSummary: {s.summary[:400]}"
        for i, s in enumerate(stories)
    ])
    
    system = _build_score_system(profile, brand_name, brand)
    system = system.replace(
        'Return ONLY valid JSON:\n{"score": <int 0-100>, "reason": "<one sentence why it does or does not fit>"}',
        'Return ONLY valid JSON array of scoring objects:\n[{"index": <int>, "score": <int 0-100>, "reason": "<one sentence>"}]'
    )
    
    user = f"Score each story (indexed 0-{len(stories)-1}):\n\n{story_text}"
    
    try:
        result = chat_json(system, user, model=model)
        # Handle both list and dict responses
        results = result if isinstance(result, list) else result.get("results", [])
        
        scored = []
        for idx, s in enumerate(stories):
            score_data = next((r for r in results if r.get("index") == idx), None)
            if score_data is None:
                # Fallback: unscored stories get a default
                score_data = {"score": 50, "reason": "batch parsing incomplete"}
            
            scored.append(ScoredStory(
                score=int(score_data.get("score", 50)),
                reason=score_data.get("reason", ""),
                title=s.title,
                summary=s.summary,
                url=s.url,
                published=s.published,
                image=s.image,
            ))
        return scored
    except Exception as exc:
        print(f"[filter] batch scoring failed, falling back to individual: {exc}")
        # Fallback to single-story scoring
        return [_score_one(s, profile, brand_name, model, brand=brand) for s in stories]


def _score_one(story: Story, profile: str, brand_name: str = "the brand",
               model: str | None = None, brand: dict | None = None) -> ScoredStory:
    """Score a single story (fallback when batch fails)."""
    system = _build_score_system(profile, brand_name, brand)
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
        image=story.image,
    )


def rank_stories(stories: list[Story], top_n: int = 5, model: str | None = None,
                 brand: dict | None = None, should_cancel=None, batch_size: int = 10) -> list[ScoredStory]:
    """Rank and score stories. Uses batching to reduce LLM API calls from N to N/batch_size."""
    if brand:
        profile    = (brand.get("profile") or "").strip() or _safe_profile()
        brand_name = brand.get("name", "the brand")
    else:
        profile    = _safe_profile()
        brand_name = "the brand"
    
    scored: list[ScoredStory] = []
    
    # Process stories in batches to reduce API calls
    for batch_start in range(0, len(stories), batch_size):
        if should_cancel and should_cancel():
            print(f"[filter] cancelled — scored {len(scored)} before stopping")
            break
        
        batch_end = min(batch_start + batch_size, len(stories))
        batch = stories[batch_start:batch_end]
        
        try:
            batch_scores = _score_batch(batch, profile, brand_name, model, brand=brand)
            scored.extend(batch_scores)
            print(f"[filter] scored batch {batch_start//batch_size + 1} ({len(batch)} stories)")
        except Exception as exc:
            print(f"[filter] WARN  batch scoring failed: {exc}")
    
    scored.sort(key=lambda x: x.score, reverse=True)
    # Different outlets often cover the same real event — keep the best-scored
    # story per cluster so the same headline doesn't turn into multiple posts.
    scored = dedupe_similar_stories(scored)
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
