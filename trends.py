"""Google Trends story source (pytrends).

Surfaces *recent trending topics* as story-shaped dicts the rest of the pipeline
(plan_post / batch / bulk) can consume, so trends become an alternative to the
RSS feeds. pytrends is an unofficial client, so every call is wrapped — Google
will occasionally rate-limit (HTTP 429) and we degrade to a clear error rather
than crashing a fetch.

Each returned story looks like a ranked feed story:
    {title, summary, url, published, score, reason}
The ``summary`` is written as guidance for the LLM planner (why it's trending +
related entities), and ``url`` is a Google News search link so "Source ↗" works.
"""
from __future__ import annotations

import html
import re
import time
from urllib.parse import quote_plus

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", text or "")).split())

# Brand → trending profile. ``realtime`` regions are the handful pytrends'
# realtime endpoint supports; ``daily`` regions use full country names. Category
# codes: e=entertainment, b=business, t=sci/tech, s=sports, h=top stories.
BRAND_TRENDS = {
    "k2": {
        "realtime_region": "US",
        "realtime_cats":   ["b", "t"],          # business + tech
        "daily_region":    "canada",
        "label":           "business & tech",
        # Google News RSS fallback (reliable when pytrends' endpoints 404).
        "news_region":     ("en-CA", "CA"),
        "news_topics":     ["BUSINESS", "TECHNOLOGY"],
        "news_query":      "",
    },
    "jkr": {
        "realtime_region": "US",
        "realtime_cats":   ["e"],               # entertainment (games + film)
        "daily_region":    "united_states",
        "label":           "entertainment",
        "news_region":     ("en-US", "US"),
        "news_topics":     ["ENTERTAINMENT"],
        "news_query":      "video games OR gaming OR film OR movie",
    },
}
_DEFAULT = {"realtime_region": "US", "realtime_cats": ["all"],
            "daily_region": "united_states", "label": "trending",
            "news_region": ("en-US", "US"), "news_topics": ["NATION"], "news_query": ""}

# Tiny in-process cache so repeated fetches (e.g. both UI tabs) don't hammer
# Google within a short window.
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TTL = 600  # seconds


def _news_url(term: str) -> str:
    return f"https://news.google.com/search?q={quote_plus(term)}"


def _story(term: str, entities: str, label: str, rank: int, n: int) -> dict:
    summary = (
        f"'{term}' is trending on Google right now ({label}). "
        + (f"Related topics: {entities}. " if entities else "")
        + "Write a timely, on-brand post reacting to why this is trending and "
          "what it means for the audience — stay factual and don't invent specifics."
    )
    # Synthetic score so it slots into the existing score-pill UI (top trend = ~95).
    score = max(50, 96 - int(rank * (46 / max(1, n))))
    return {
        "title":     term,
        "summary":   summary,
        "url":       _news_url(term),
        "published": "",
        "image":     "",       # pytrends terms have no article image
        "score":     score,
        "reason":    f"🔥 Trending on Google · {label}",
    }


def _news_story(title: str, summary: str, url: str, published: str,
                label: str, rank: int, n: int, image: str = "") -> dict:
    score = max(50, 96 - int(rank * (46 / max(1, n))))
    # Google News summaries are HTML lists of related-article links — strip tags,
    # and if nothing substantive survives, give the planner topic guidance instead.
    clean = _strip_html(summary)
    if len(clean) < 50:
        clean = (f"'{title}' is a top trending {label} story right now. Write a "
                 "timely, on-brand post reacting to it — stay factual, don't invent specifics.")
    return {
        "title":     title,
        "summary":   clean[:900],
        "url":       url,
        "published": published or "",
        "image":     image or "",
        "score":     score,
        "reason":    f"🔥 Trending · {label} (Google News)",
    }


def _google_news_stories(prof: dict, count: int) -> list[dict]:
    """Reliable fallback: top/trending headlines from Google News RSS.

    Returns real articles (title + summary + source URL), which the LLM planner
    can turn into a post directly. Used when pytrends' endpoints are unavailable
    (Google frequently 404s them) — and as a generally sturdier trending source.
    """
    from urllib.parse import quote_plus
    from feeds import fetch_stories, story_sort_key

    hl, gl = prof.get("news_region", ("en-US", "US"))
    ceid   = f"{gl}:{hl.split('-')[0]}"
    urls = [f"https://news.google.com/rss/headlines/section/topic/{t}"
            f"?hl={hl}&gl={gl}&ceid={ceid}" for t in prof.get("news_topics", [])]
    if prof.get("news_query"):
        urls.append("https://news.google.com/rss/search?q="
                    f"{quote_plus(prof['news_query'])}&hl={hl}&gl={gl}&ceid={ceid}")

    stories = fetch_stories(urls)
    # Freshest first, de-dup by title.
    stories.sort(key=story_sort_key, reverse=True)
    out, seen = [], set()
    label = prof.get("label", "trending")
    for s in stories:
        key = (s.title or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((s.title, s.summary, s.url, s.published, s.image))
        if len(out) >= count:
            break
    return [_news_story(t, sm, u, p, label, i, len(out), image=img)
            for i, (t, sm, u, p, img) in enumerate(out)]


def trending_stories(brand_key: str | None = None, count: int = 15) -> list[dict]:
    """Return up to ``count`` trending topics for a brand as story dicts.

    Tries Google Trends (pytrends) first, then falls back to Google News RSS
    top headlines (reliable). Raises only if both sources come up empty.
    """
    prof = BRAND_TRENDS.get(brand_key or "", _DEFAULT)
    ckey = f"{brand_key}:{count}"
    hit  = _CACHE.get(ckey)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]

    try:
        from pytrends.request import TrendReq
    except ImportError:
        TrendReq = None

    stories: list[dict] = []
    seen: set[str] = set()
    errors: list[str] = []

    pt = None
    if TrendReq is not None:
        try:
            pt = TrendReq(hl="en-US", tz=300, timeout=(10, 25))
        except Exception as exc:
            errors.append(f"trendreq: {exc}")

    # 1) Realtime trending (category-filtered) — the freshest signal.
    for cat in (prof["realtime_cats"] if pt is not None else []):
        try:
            df = pt.realtime_trending_searches(pn=prof["realtime_region"], cat=cat)
        except Exception as exc:
            errors.append(f"realtime/{cat}: {exc}")
            continue
        for _, row in df.iterrows():
            title = str(row.get("title", "")).strip()
            if not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            # entityNames is normally a list, but pytrends can hand back a NaN
            # (float) or string — coerce defensively so one bad row can't sink
            # the whole fetch (this loop runs outside the realtime try/except).
            raw_ents = row.get("entityNames")
            try:
                entities = ", ".join(list(raw_ents)[:4]) if isinstance(raw_ents, (list, tuple)) else ""
            except Exception:
                entities = ""
            stories.append((title, entities))

    # 2) Daily trending searches fallback (terms only) if realtime came up short.
    if pt is not None and len(stories) < count:
        try:
            df = pt.trending_searches(pn=prof["daily_region"])
            for term in df[0].tolist():
                term = str(term).strip()
                if term and term.lower() not in seen:
                    seen.add(term.lower())
                    stories.append((term, ""))
        except Exception as exc:
            errors.append(f"daily: {exc}")

    out = [_story(t, e, prof["label"], i, min(count, len(stories)))
           for i, (t, e) in enumerate(stories[:count])]

    # 3) Google News RSS fallback — reliable when pytrends 404s / rate-limits.
    if len(out) < count:
        try:
            for ns in _google_news_stories(prof, count):
                k = ns["title"].strip().lower()
                if k in seen:
                    continue
                seen.add(k)
                out.append(ns)
                if len(out) >= count:
                    break
        except Exception as exc:
            errors.append(f"news: {exc}")

    if not out:
        detail = "; ".join(errors) or "no results"
        raise RuntimeError(
            "No trending stories available right now (Google Trends and Google "
            f"News both returned nothing). Try an RSS category. Detail: {detail}"
        )

    out = out[:count]
    _CACHE[ckey] = (time.time(), out)
    return out


if __name__ == "__main__":
    import json, sys
    bk = sys.argv[1] if len(sys.argv) > 1 else "k2"
    print(json.dumps(trending_stories(bk, 10), indent=2, ensure_ascii=False))
