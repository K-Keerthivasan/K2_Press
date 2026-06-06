from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

import feedparser
import yaml


CONFIG_PATH = Path("config.yaml")


@dataclass(frozen=True)
class Story:
    title: str
    summary: str
    url: str
    published: str


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")

    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    return data


def configured_feed_urls(
    config: dict,
    use_config_feeds: bool = False,
    category: str | None = None,        # None = all enabled categories
) -> list[str]:
    feeds = config.get("feeds", {})

    # Legacy flat list (backwards compat)
    if use_config_feeds and "urls" in feeds:
        urls = feeds.get("urls", [])
        return [u for u in urls if u and not u.startswith("TODO:")]

    # Category-based config
    categories = feeds.get("categories", {})
    if categories:
        urls: list[str] = []
        for cat_key, cat in categories.items():
            if not cat.get("enabled", False):
                continue
            if category and cat_key != category:
                continue
            for url in cat.get("urls", []):
                if url and not url.startswith("TODO:"):
                    urls.append(url)
        if urls:
            return urls

    # Fallback to sample
    sample_url = feeds.get("sample_url")
    return [sample_url] if sample_url else []


def list_categories(config: dict) -> dict[str, str]:
    """Return {key: display_name} for all enabled categories."""
    cats = config.get("feeds", {}).get("categories", {})
    return {k: v["name"] for k, v in cats.items() if v.get("enabled", False)}


def normalize_published(entry: dict) -> str:
    raw_value = (
        entry.get("published")
        or entry.get("updated")
        or entry.get("created")
        or ""
    )
    if not raw_value:
        return ""

    try:
        return parsedate_to_datetime(raw_value).isoformat()
    except (TypeError, ValueError, IndexError):
        return str(raw_value)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(str(value).split())


def normalize_entry(entry: dict) -> Story:
    title = clean_text(entry.get("title"))
    summary = clean_text(entry.get("summary") or entry.get("description"))
    url = clean_text(entry.get("link"))
    published = normalize_published(entry)

    return Story(
        title=title,
        summary=summary,
        url=url,
        published=published,
    )


def fetch_feed(url: str) -> list[Story]:
    parsed = feedparser.parse(url)
    # feedparser flags `bozo` for harmless issues too (e.g. an encoding declared
    # us-ascii but served as utf-8). If entries still parsed, use them; only treat
    # it as an error when there's genuinely nothing to read.
    if not parsed.entries:
        exception = getattr(parsed, "bozo_exception", None)
        raise RuntimeError(f"Could not parse feed {url}: {exception or 'no entries'}")
    if parsed.bozo:
        print(f"[feeds] note: {url} parsed with a warning "
              f"({getattr(parsed, 'bozo_exception', '')})")
    return [normalize_entry(entry) for entry in parsed.entries]


def fetch_stories(urls: Iterable[str]) -> list[Story]:
    stories: list[Story] = []
    for url in urls:
        try:
            stories.extend(fetch_feed(url))
        except Exception as exc:
            # One unreachable/broken feed shouldn't sink the whole fetch.
            print(f"[feeds] WARN skipping feed {url}: {exc}")
    return stories


def story_sort_key(story: Story) -> datetime:
    if not story.published:
        return datetime.min
    try:
        return datetime.fromisoformat(story.published)
    except ValueError:
        return datetime.min


def print_stories(stories: list[Story], limit: int) -> None:
    for index, story in enumerate(stories[:limit], start=1):
        print(f"{index}. {story.title}")
        if story.published:
            print(f"   Published: {story.published}")
        print(f"   URL: {story.url}")
        if story.summary:
            print(f"   Summary: {story.summary[:260]}")
        print()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch and print normalized RSS/Atom stories.")
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to config YAML. Defaults to config.yaml.",
    )
    parser.add_argument(
        "--config-feeds",
        action="store_true",
        help="Use configured feed URLs instead of the sample feed.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of stories to print.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print normalized stories as JSON-compatible dicts.",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    config = load_config(Path(args.config))
    urls = configured_feed_urls(config, use_config_feeds=args.config_feeds)

    if not urls:
        source = "configured feeds" if args.config_feeds else "sample feed"
        raise SystemExit(f"No {source} found in {args.config}.")

    stories = fetch_stories(urls)
    stories.sort(key=story_sort_key, reverse=True)

    if args.json:
        import json

        print(json.dumps([asdict(story) for story in stories[: args.limit]], indent=2))
        return

    print_stories(stories, args.limit)


if __name__ == "__main__":
    main()
