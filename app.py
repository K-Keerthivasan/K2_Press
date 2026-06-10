"""K2 Digital Media — carousel generator with live editor and canvas design tool."""
from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="K2 Digital Media")

for _d in ("outputs", "image_cache", "library", "library/plans", "library/stories"):
    Path(_d).mkdir(parents=True, exist_ok=True)

app.mount("/static",      StaticFiles(directory="static"),      name="static")
app.mount("/outputs",     StaticFiles(directory="outputs"),      name="outputs")
app.mount("/image_cache", StaticFiles(directory="image_cache"),  name="images")
app.mount("/assets",      StaticFiles(directory="Assets"),       name="assets")

_executor = ThreadPoolExecutor(max_workers=3)

# ── Session state ─────────────────────────────────────────────────────────────
_session: dict[str, Any] = {
    "stories":      [],
    "plan":         None,
    "image_paths":  {},   # {str(slide_idx): str path}
    "rendered_dir": None,
    "used_urls":    set(),  # stories already generated this session — not re-served
    "bulk_progress": None,  # {done,total,current,brand,running} during a bulk run
}


def _set_progress(done: int, total: int, current: str = "", brand: str = "",
                  running: bool = True) -> None:
    """Publish bulk-run progress so the UI can poll it via /api/session, and
    heartbeat the busy lock so a long run can't trip the stale-lock expiry."""
    _session["bulk_progress"] = {
        "done": done, "total": total, "current": current,
        "brand": brand, "running": running,
    }
    if running:
        _busy["since"] = time.time()

def _mark_used(*urls: str | None) -> None:
    """Remember stories we've generated from so a re-fetch won't surface them
    again this session. Reset on brand switch (or via /api/session/reset)."""
    seen = _session.setdefault("used_urls", set())
    for u in urls:
        if u:
            seen.add(u)


# Single-flight guard: only one heavy LLM job (fetch/plan/batch) at a time so a
# page refresh + re-click can't stack overlapping Ollama runs.
_busy: dict[str, Any] = {"job": None, "since": 0.0}
# Long enough for a big (25–50 post) cross-brand bulk run. The job heartbeats
# `_busy["since"]` per item so the stale-lock expiry can't trip mid-run.
_BUSY_TIMEOUT = 3600  # seconds; stale lock auto-expires so we can never deadlock
_cancel: dict[str, bool] = {"flag": False}  # cooperative cancel for loop jobs


def _acquire(job: str) -> None:
    """Claim the job slot, or raise 409 if another job is genuinely in flight."""
    cur = _busy["job"]
    if cur and (time.time() - _busy["since"]) < _BUSY_TIMEOUT:
        raise HTTPException(409, f"A '{cur}' job is already running. Wait for it to finish.")
    _busy["job"] = job
    _busy["since"] = time.time()
    _cancel["flag"] = False  # fresh job starts uncancelled


def _release() -> None:
    _busy["job"] = None
    _busy["since"] = 0.0
    _cancel["flag"] = False


def _cancelled() -> bool:
    return _cancel["flag"]


def _cfg() -> dict:
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def _run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(_executor, fn, *args)


# ── Thread helpers ─────────────────────────────────────────────────────────────

def _sync_list_models():
    from llm import list_models
    return list_models()


# Sentinel category that pulls from Google Trends instead of the RSS feeds.
TRENDING_CAT = "__trending__"


def _sync_fetch_stories(limit, top_n, category, model, exclude=(), brand_key=None):
    from feeds import fetch_stories, configured_feed_urls, load_config
    from filter import rank_stories
    from brands import resolve_brand, brand_feeds_config, active_key
    config = load_config()
    # Resolve the brand explicitly from the per-request key so the worker is immune
    # to the global active-brand being flipped by a concurrent request (otherwise a
    # second column's fetch could make this one return the wrong brand's stories).
    bkey   = brand_key or active_key(config)
    brand  = resolve_brand(config, bkey)

    # 🔥 Trending: skip RSS + LLM ranking; Google already ranks these.
    if category == TRENDING_CAT:
        from trends import trending_stories
        stories = trending_stories(bkey, count=max(top_n, limit))
        stories = [s for s in stories if s["url"] not in exclude]
        return stories[:max(top_n, 1)] if top_n else stories

    scoped = brand_feeds_config(config, brand)
    urls = configured_feed_urls(scoped, category=category or None)
    if not urls:
        urls_all = configured_feed_urls(scoped)
        if not urls_all:
            raise ValueError(f"No feed URLs configured for brand '{brand.get('name','')}'.")
        urls = urls_all
    stories = fetch_stories(urls)
    # Drop stories already generated this session so a re-fetch surfaces fresh ones.
    if exclude:
        stories = [s for s in stories if s.url not in exclude]
    stories = stories[:limit]
    ranked  = rank_stories(stories, top_n, model=model, brand=brand,
                           should_cancel=_cancelled)
    return [vars(r) for r in ranked]


def _sync_generate_plan(story_dict, total_slides, model, tone=""):
    from feeds import Story
    from plan import plan_story
    from brands import resolve_brand
    story  = Story(**story_dict)
    config = _cfg()
    brand  = resolve_brand(config)
    return plan_story(story, config, total_slides=total_slides, model=model,
                      brand=brand, tone=tone)


def _sync_regen_caption(plan, tone):
    from plan import regen_caption
    from brands import resolve_brand
    config = _cfg()
    return regen_caption(plan, brand=resolve_brand(config), config=config, tone=tone)


def _sync_fetch_images(plan, source):
    from images import fetch_images_for_plan
    raw = fetch_images_for_plan(plan, source=source)
    return {str(k): str(v) if v else None for k, v in raw.items()}


def _sync_swap_pexels(query, source):
    from images import fetch_image
    p = fetch_image(query, source)
    return str(p)


def _sync_search_images(query, source, count):
    from images import search_images
    return search_images(query, source, count)


def _sync_fetch_url(url, name, do_filter=True):
    from images import fetch_from_url
    p = fetch_from_url(url, name, do_filter=do_filter)
    return str(p)


# Renders are grouped per brand: outputs/<brand>/<date>_<slug>_<format>/.
# When a caller passes save=False (automation / preview), the render lands in a
# throwaway outputs/_tmp/<brand> that is wiped at the start of each unsaved run,
# so nothing accumulates on disk unless a run explicitly asks to keep it.
TMP_OUT = Path("outputs/_tmp")


def _out_root_for(brand_key: str, save: bool) -> Path:
    base = Path("outputs") if save else TMP_OUT
    return base / (brand_key or "default")


def _clear_tmp() -> None:
    import shutil
    shutil.rmtree(TMP_OUT, ignore_errors=True)


def _sync_render_carousel(plan, image_paths, save=True):
    from render import generate_carousel
    from brands import active_key
    bkey = active_key(_cfg()) or "default"
    if not save:
        _clear_tmp()
    out_root  = _out_root_for(bkey, save)
    int_paths = {int(k): (Path(v) if v else None) for k, v in image_paths.items()}
    return str(generate_carousel(plan, int_paths, out_root=out_root))


def _sync_preview_slide(template, variables):
    from render import render_slide_to_bytes
    return render_slide_to_bytes(template, variables)


def _sync_preview_fmt(template, variables, width, height):
    """Preview render at an explicit canvas size (single-card formats vary:
    square 1080², linkedin 1200², x 1600×900)."""
    from render import render_slide_to_bytes
    return render_slide_to_bytes(template, variables, width, height)


def _base_vars(plan):
    from render import _base_vars as rbv
    return rbv(plan)


def _slide_vars(plan, slide_idx):
    from render import _uri
    from brands import brand_template
    bv    = _base_vars(plan)
    brand = bv.get("brand", {})
    n     = len(plan.get("content_slides", []))
    if slide_idx == 0:
        # Image-forward brands lead the title with the first content image.
        timg = None
        if brand.get("image_forward"):
            raw = _session["image_paths"].get("0")
            timg = _uri(raw) if raw else None
        return brand_template(brand, "title", "title.html"), {**bv, "background_image": timg}
    if 1 <= slide_idx <= n:
        i   = slide_idx - 1
        sl  = plan["content_slides"][i]
        raw = _session["image_paths"].get(str(i))
        return brand_template(brand, "content", "content.html"), {
            **bv,
            "slide":            sl,
            "slide_number":     slide_idx,
            "background_image": _uri(raw) if raw else None,
        }
    return brand_template(brand, "outro", "outro.html"), {**bv, "background_image": None}


EDITABLE_FILES = {
    "title.html":        Path("templates/title.html"),
    "content.html":      Path("templates/content.html"),
    "outro.html":        Path("templates/outro.html"),
    "cover.html":        Path("templates/cover.html"),
    "jkr_title.html":    Path("templates/jkr_title.html"),
    "jkr_content.html":  Path("templates/jkr_content.html"),
    "jkr_outro.html":    Path("templates/jkr_outro.html"),
    "square.html":       Path("templates/square.html"),
    "story.html":        Path("templates/story.html"),
    "xpost.html":        Path("templates/xpost.html"),
    "quote.html":        Path("templates/quote.html"),
    "comparison.html":   Path("templates/comparison.html"),
    "breaking.html":     Path("templates/breaking.html"),
    "linkedin.html":     Path("templates/linkedin.html"),
    "listicle_content.html": Path("templates/listicle_content.html"),
    "brand.css":         Path("static/brand.css"),
}


def _fmt_allowed(fmt: str, brand_key: str, config: dict) -> bool:
    """A format may restrict itself to certain brands (e.g. LinkedIn -> K2 only)
    via formats.<fmt>.brands in config.yaml. No list = available everywhere."""
    allowed = (config.get("formats", {}).get(fmt, {}) or {}).get("brands")
    return (not allowed) or (brand_key in allowed)


def _render_item(story_dict, fmt, *, config, brand, brand_key, out_root,
                 model, source, total_slides, tone):
    """Plan + fetch images + render ONE (story, format) pair for a given brand.

    Returns a result dict (ok True/False). Shared by the single-brand batch and
    the cross-brand bulk runs so both honour brand_key + format restrictions."""
    from feeds import Story
    from plan import plan_post
    from images import fetch_images_for_plan, fetch_image
    from render import generate_post, CAROUSEL_FORMATS

    story = Story(**story_dict)
    if not _fmt_allowed(fmt, brand_key, config):
        return {"title": story.title, "format": fmt, "brand": brand_key,
                "ok": False, "skipped": True,
                "error": f"'{fmt}' is not available for {brand.get('name', brand_key)}"}
    try:
        plan = plan_post(story, fmt, config=config, total_slides=total_slides,
                         model=model, brand=brand, tone=tone)
        # Multi-image (carousel/listicle) vs single-card image fetch.
        if fmt in CAROUSEL_FORMATS:
            img_paths = fetch_images_for_plan(plan, source=source)
        else:
            img_paths = None
            q = plan.get("image_query")
            if q:
                try:
                    img_paths = {0: fetch_image(q, source)}
                except Exception as ie:
                    print(f"[bulk] image fail '{q}': {ie}")
                    img_paths = None
        out_dir = generate_post(plan, fmt, img_paths, out_root=out_root,
                                brand_key=brand_key)
        files   = sorted(p.name for p in Path(out_dir).glob("*.png"))
        return {
            "title":       story.title,
            "format":      fmt,
            "brand":       brand_key,
            "slug":        plan.get("slug", ""),
            "rel":         Path(out_dir).relative_to("outputs").as_posix(),
            "files":       files,
            "caption":     plan.get("caption", ""),
            "image_query": plan.get("image_query", ""),
            "plan":        plan,
            "story":       story_dict,
            "ok":          True,
        }
    except Exception as e:
        return {"title": story.title, "format": fmt, "brand": brand_key,
                "story": story_dict, "ok": False, "error": str(e)}


def _sync_batch_run(items, model, source, save=True, tone=""):
    """Single-brand batch: plan + fetch images + render every (story, format)
    pair for the active brand. Output lands in outputs/<brand>/ (or _tmp)."""
    from brands import resolve_brand, active_key

    config    = _cfg()
    brand     = resolve_brand(config)
    brand_key = active_key(config) or "default"
    if not save:
        _clear_tmp()
    out_root  = _out_root_for(brand_key, save)
    results   = []
    total     = sum(len(it.get("formats", ["carousel"])) for it in items)
    done      = 0
    _set_progress(0, total, brand=brand_key)

    cancelled = False
    for item in items:
        if _cancelled():
            cancelled = True
            break
        item_tone = item.get("tone", tone)
        for fmt in item.get("formats", ["carousel"]):
            if _cancelled():
                cancelled = True
                break
            _set_progress(done, total, current=(item.get("story") or {}).get("title", ""),
                          brand=brand_key)
            results.append(_render_item(
                item["story"], fmt, config=config, brand=brand, brand_key=brand_key,
                out_root=out_root, model=model, source=source,
                total_slides=item.get("total_slides"), tone=item_tone))
            done += 1

    _set_progress(done, total, brand=brand_key, running=False)
    return {"batch_dir": out_root.as_posix(), "saved": save,
            "results": results, "cancelled": cancelled}


def _sync_bulk_run(groups, model, source, save=True, tone=""):
    """Cross-brand bulk run. ``groups`` = [{brand, items:[{story,formats,
    total_slides,tone}]}]. Each group renders for its OWN brand via an explicit
    brand_key, so K2 + JKR posts come out of one job with no global-brand desync.
    Brands run sequentially (timing is not a concern for this workflow)."""
    from brands import resolve_brand, list_brands

    config = _cfg()
    known  = list_brands(config)
    if not save:
        _clear_tmp()
    results: list[dict] = []
    total = sum(len(it.get("formats", ["carousel"]))
                for grp in groups for it in grp.get("items", []))
    done  = 0
    _set_progress(0, total)

    cancelled = False
    dirs: set[str] = set()
    for grp in groups:
        bkey = grp.get("brand")
        if bkey not in known:
            continue
        brand    = resolve_brand(config, bkey)
        out_root = _out_root_for(bkey, save)
        dirs.add(out_root.as_posix())
        for item in grp.get("items", []):
            if _cancelled():
                cancelled = True
                break
            item_tone = item.get("tone", tone)
            for fmt in item.get("formats", ["carousel"]):
                if _cancelled():
                    cancelled = True
                    break
                _set_progress(done, total,
                              current=(item.get("story") or {}).get("title", ""),
                              brand=bkey)
                results.append(_render_item(
                    item["story"], fmt, config=config, brand=brand, brand_key=bkey,
                    out_root=out_root, model=model, source=source,
                    total_slides=item.get("total_slides"), tone=item_tone))
                done += 1
            if cancelled:
                break
        if cancelled:
            break

    _set_progress(done, total, running=False)
    return {"batch_dirs": sorted(dirs), "saved": save,
            "results": results, "cancelled": cancelled}


def _sync_rerender_one(plan, fmt, image_paths, brand_key, save=True):
    """Re-render a single post (used after the user swaps a suggested image)."""
    from render import generate_post
    if not save:
        _clear_tmp()
    out_root  = _out_root_for(brand_key or "default", save)
    int_paths = {int(k): (Path(v) if v else None) for k, v in (image_paths or {}).items()}
    out_dir   = generate_post(plan, fmt, int_paths, out_root=out_root,
                              brand_key=brand_key)
    files = sorted(p.name for p in Path(out_dir).glob("*.png"))
    return {"rel": Path(out_dir).relative_to("outputs").as_posix(), "files": files}


# ── API: models ───────────────────────────────────────────────────────────────
@app.get("/api/models")
async def api_models():
    models = await _run(_sync_list_models)
    return {"models": models or ["qwen3:8b", "gemma:latest"]}


@app.put("/api/model")
async def api_set_model(body: dict = Body(...)):
    from llm import set_model
    set_model(body.get("model", ""))
    return {"ok": True}


# ── API: formats ───────────────────────────────────────────────────────────────
@app.get("/api/formats")
async def api_formats():
    fmts = _cfg().get("formats", {})
    return {
        "formats": {k: v.get("name", k) for k, v in fmts.items()},
        # Brand restrictions so the UI can hide brand-locked formats (e.g. LinkedIn → K2).
        "restrict": {k: v["brands"] for k, v in fmts.items() if v.get("brands")},
    }


# ── API: batch ───────────────────────────────────────────────────────────────
@app.post("/api/batch/run")
async def api_batch_run(body: dict = Body(...)):
    items  = body.get("items", [])
    model  = body.get("model") or None
    source = body.get("source", "pexels")
    save   = body.get("save", True)
    tone   = body.get("tone", "")
    if not items:
        raise HTTPException(400, "No items selected.")
    _apply_brand(body.get("brand"))
    _acquire("batch")
    t0 = time.time()
    try:
        out = await _run(_sync_batch_run, items, model, source, save, tone)
        out["elapsed"] = round(time.time() - t0, 1)
        _mark_used(*[(it.get("story") or {}).get("url") for it in items])
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _release()


# ── API: bulk (cross-brand) ────────────────────────────────────────────────────
@app.post("/api/bulk/run")
async def api_bulk_run(body: dict = Body(...)):
    """Generate posts for multiple brands in one job. Body:
    {groups:[{brand, items:[{story,formats,total_slides,tone}]}], model, source, save, tone}."""
    groups = body.get("groups", [])
    model  = body.get("model") or None
    source = body.get("source", "pexels")
    save   = body.get("save", True)
    tone   = body.get("tone", "")
    groups = [g for g in groups if g.get("items")]
    if not groups:
        raise HTTPException(400, "No stories selected for any brand.")
    _acquire("bulk")
    t0 = time.time()
    try:
        out = await _run(_sync_bulk_run, groups, model, source, save, tone)
        out["elapsed"] = round(time.time() - t0, 1)
        _mark_used(*[(it.get("story") or {}).get("url")
                     for g in groups for it in g.get("items", [])])
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _release()


@app.post("/api/bulk/rerender")
async def api_bulk_rerender(body: dict = Body(...)):
    """Re-render one post after swapping its image. Body:
    {plan, format, brand, image_paths:{idx:path}, save?}."""
    plan = body.get("plan")
    fmt  = body.get("format", "carousel")
    if not plan:
        raise HTTPException(400, "plan required")
    bkey = body.get("brand") or _active_brand_key()
    save = body.get("save", True)
    imgs = body.get("image_paths", {})
    try:
        out = await _run(_sync_rerender_one, plan, fmt, imgs, bkey, save)
        return out
    except Exception as e:
        raise HTTPException(500, str(e))


# ── API: categories ───────────────────────────────────────────────────────────
@app.get("/api/categories")
async def api_categories(brand: str = ""):
    """Categories for a brand. ``brand`` resolves explicitly (no global switch),
    so the bulk tab can read each brand's feeds without changing the active one."""
    from feeds import list_categories, load_config
    from brands import resolve_brand, brand_feeds_config
    config = load_config()
    b = resolve_brand(config, brand or None)
    scoped = brand_feeds_config(config, b)
    return {"categories": list_categories(scoped)}


# ── API: cancel ────────────────────────────────────────────────────────────────
@app.post("/api/cancel")
async def api_cancel():
    """Ask the running loop job (fetch/batch) to stop after the current item."""
    job = _busy["job"]
    if job:
        _cancel["flag"] = True
        return {"ok": True, "cancelling": job}
    return {"ok": False, "cancelling": None}


# ── API: session ──────────────────────────────────────────────────────────────
@app.post("/api/session/reset")
async def api_session_reset(body: dict = Body(default={})):
    """Forget which stories were already generated this session (the dedup set),
    so previously-used stories can be served again on the next fetch."""
    n = len(_session.get("used_urls") or ())
    _session["used_urls"] = set()
    return {"ok": True, "cleared": n}


# ── API: brands ────────────────────────────────────────────────────────────────
@app.get("/api/brands")
async def api_brands():
    from brands import list_brands, active_key
    config = _cfg()
    return {"brands": list_brands(config), "active": active_key(config)}


@app.get("/api/brand")
async def api_brand():
    from brands import resolve_brand, active_key
    config = _cfg()
    b = resolve_brand(config)
    t = b.get("theme", {})
    return {
        "key":     active_key(config),
        "name":    b.get("name", ""),
        "short":   b.get("short", b.get("name", "")),
        "handle":  b.get("handle", ""),
        "tagline": b.get("tagline", ""),
        "pitch":    b.get("pitch", ""),
        "services": b.get("services", ""),
        "location": b.get("location", ""),
        "website":  b.get("website", ""),
        "category": b.get("category", ""),
        "logo":    "/" + b.get("logo_path", "static/logo.png"),
        "shape":   b.get("logo_shape", "round"),
        "accent":  t.get("accent", "#00B4C8"),
        "accent2": t.get("accent2", "#00C896"),
        "navy":    t.get("navy", "#0A0F1E"),
        "text":    t.get("text", "#FFFFFF"),
    }


@app.get("/api/brand-logo")
async def api_brand_logo(brand: str = ""):
    """Redirect to a specific brand's logo static URL (used by the bulk tab to
    show each brand's mark without switching the active brand)."""
    from fastapi.responses import RedirectResponse
    from brands import resolve_brand
    b = resolve_brand(_cfg(), brand or None)
    return RedirectResponse("/" + b.get("logo_path", "static/logo.png"))


@app.put("/api/brand")
async def api_set_brand(body: dict = Body(...)):
    from brands import set_active, resolve_brand, active_key, list_brands
    key = body.get("brand", "")
    config = _cfg()
    if key not in list_brands(config):
        raise HTTPException(404, f"Unknown brand '{key}'")
    set_active(key)
    # Switching brand means different feeds/voice — clear story/plan context.
    _session["stories"]     = []
    _session["plan"]        = None
    _session["image_paths"] = {}
    _session["used_urls"]   = set()
    b = resolve_brand(config)
    return {
        "ok":     True,
        "active": active_key(config),
        "name":   b.get("name", ""),
        "logo":   "/" + b.get("logo_path", "static/logo.png"),
        "shape":  b.get("logo_shape", "round"),
        "accent": b.get("theme", {}).get("accent", "#00B4C8"),
    }


# ── API: library (save/load generated content) ──────────────────────────────────
LIB_PLANS   = Path("library/plans")
LIB_STORIES = Path("library/stories")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "item"


def _active_brand_key() -> str:
    from brands import active_key
    return active_key(_cfg()) or ""


def _apply_brand(key: str | None) -> None:
    """Make the UI-selected brand authoritative for this action (and onward
    renders), so the dropdown can never desync from what the server uses."""
    from brands import set_active, list_brands
    if key and key in list_brands(_cfg()):
        set_active(key)


def _lib_list(folder: Path) -> list[dict]:
    # rglob: saved items live in per-brand subfolders (library/plans/<brand>/…).
    items = []
    for f in sorted(folder.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            meta = json.loads(f.read_text(encoding="utf-8")).get("meta", {})
        except Exception:
            meta = {}
        items.append({"id": f.stem, **meta})
    return items


def _lib_path(folder: Path, fid: str) -> Path | None:
    """Locate a saved item by id across the brand subfolders (or legacy root)."""
    fid = _slug_id(fid)
    return next(iter(folder.rglob(f"{fid}.json")), None)


@app.post("/api/library/plan")
async def api_lib_save_plan(body: dict = Body(default={})):
    plan = body.get("plan") or _session.get("plan")
    if not plan:
        raise HTTPException(400, "No plan to save.")
    name = body.get("name") or plan.get("title_card", {}).get("headline") \
        or plan.get("headline") or plan.get("slug", "plan")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bkey  = _active_brand_key() or "default"
    fid   = f"{_slug(name)}-{stamp}"
    meta  = {"name": name, "brand": bkey,
             "slug": plan.get("slug", ""), "format": plan.get("format", "carousel"),
             "tone": plan.get("tone", ""),
             "caption": plan.get("caption", ""),
             "when": datetime.now().isoformat(timespec="seconds")}
    dest  = LIB_PLANS / bkey
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{fid}.json").write_text(
        json.dumps({"meta": meta, "plan": plan}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "id": fid, "meta": meta}


@app.get("/api/library/plans")
async def api_lib_plans():
    return {"plans": _lib_list(LIB_PLANS)}


@app.get("/api/library/plan/{fid}")
async def api_lib_get_plan(fid: str):
    f = _lib_path(LIB_PLANS, fid)
    if not f:
        raise HTTPException(404, fid)
    data = json.loads(f.read_text(encoding="utf-8"))
    _session["plan"]        = data.get("plan")
    _session["image_paths"] = {}
    return data


@app.delete("/api/library/plan/{fid}")
async def api_lib_del_plan(fid: str):
    f = _lib_path(LIB_PLANS, fid)
    if f:
        f.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/library/stories")
async def api_lib_save_stories(body: dict = Body(default={})):
    stories = _session.get("stories") or []
    if not stories:
        raise HTTPException(400, "No fetched stories to save.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bkey  = _active_brand_key()
    name  = body.get("name") or f"{bkey or 'stories'} · {len(stories)} stories"
    fid   = f"{_slug(bkey)}-{stamp}"
    meta  = {"name": name, "brand": bkey, "count": len(stories),
             "when": datetime.now().isoformat(timespec="seconds")}
    dest  = LIB_STORIES / (bkey or "default")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{fid}.json").write_text(
        json.dumps({"meta": meta, "stories": stories}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "id": fid, "meta": meta}


@app.get("/api/library/stories")
async def api_lib_stories():
    return {"stories": _lib_list(LIB_STORIES)}


@app.get("/api/library/stories/{fid}")
async def api_lib_get_stories(fid: str):
    f = _lib_path(LIB_STORIES, fid)
    if not f:
        raise HTTPException(404, fid)
    data = json.loads(f.read_text(encoding="utf-8"))
    _session["stories"] = data.get("stories", [])
    return data


@app.delete("/api/library/stories/{fid}")
async def api_lib_del_stories(fid: str):
    f = _lib_path(LIB_STORIES, fid)
    if f:
        f.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/library/clear")
async def api_lib_clear(body: dict = Body(default={})):
    """Empty the saved library. ``kind`` = 'plans' | 'stories' | 'all' (default);
    optional ``brand`` limits the wipe to one brand's subfolder."""
    import shutil
    kind  = body.get("kind", "all")
    brand = _slug_id(body.get("brand", "")) if body.get("brand") else ""
    folders = {"plans": [LIB_PLANS], "stories": [LIB_STORIES],
               "all": [LIB_PLANS, LIB_STORIES]}.get(kind, [LIB_PLANS, LIB_STORIES])
    removed = 0
    for folder in folders:
        targets = [folder / brand] if brand else [folder]
        for t in targets:
            for f in t.rglob("*.json") if t.exists() else []:
                try:
                    f.unlink(); removed += 1
                except OSError:
                    pass
            # Drop now-empty brand subfolders, but keep the top-level folder.
            if brand and (folder / brand).exists():
                shutil.rmtree(folder / brand, ignore_errors=True)
    return {"ok": True, "removed": removed}


def _slug_id(fid: str) -> str:
    """Sanitise a library id from the URL to a safe filename stem."""
    return re.sub(r"[^A-Za-z0-9._-]", "", fid)


# ── API: stories ──────────────────────────────────────────────────────────────
@app.get("/api/stories")
async def api_get_stories():
    return {"stories": _session["stories"]}


@app.post("/api/stories/fetch")
async def api_fetch_stories(
    limit:    int = 20,
    top:      int = 5,
    category: str = "",
    model:    str = "",
    brand:    str = "",
):
    # Resolve per-request (no global switch) so two columns fetching different
    # brands in the bulk tab can't clobber each other's active brand mid-flight.
    from brands import list_brands
    bkey = brand if brand and brand in list_brands(_cfg()) else None
    _acquire("fetch stories")
    t0 = time.time()
    try:
        exclude = set(_session.get("used_urls") or ())
        ranked = await _run(_sync_fetch_stories, limit, top, category or None,
                            model or None, exclude, bkey)
        _session["stories"] = ranked
        elapsed = round(time.time() - t0, 1)
        return {"stories": ranked, "elapsed": elapsed, "excluded": len(exclude)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        _release()


# ── API: plan ─────────────────────────────────────────────────────────────────
@app.post("/api/plan/generate")
async def api_generate_plan(body: dict = Body(...)):
    story       = body.get("story", {})
    total       = body.get("total_slides") or None
    model       = body.get("model") or None
    tone        = body.get("tone", "")
    _apply_brand(body.get("brand"))
    _acquire("generate plan")
    t0 = time.time()
    try:
        plan = await _run(_sync_generate_plan, story, total, model, tone)
        _session["plan"]        = plan
        _session["image_paths"] = {}
        _mark_used(story.get("url"))
        elapsed = round(time.time() - t0, 1)
        return {"plan": plan, "elapsed": elapsed}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _release()


@app.get("/api/plan")
async def api_get_plan():
    if not _session["plan"]:
        raise HTTPException(404, "No plan loaded.")
    return {"plan": _session["plan"]}


@app.post("/api/plan/caption")
async def api_regen_caption(body: dict = Body(default={})):
    plan = _session.get("plan")
    if not plan:
        raise HTTPException(400, "No plan loaded.")
    _apply_brand(body.get("brand"))
    _acquire("caption")
    try:
        out = await _run(_sync_regen_caption, plan, body.get("tone", ""))
        plan["caption"]  = out.get("caption", plan.get("caption", ""))
        plan["hashtags"] = out.get("hashtags", plan.get("hashtags", []))
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _release()


@app.get("/api/session")
async def api_get_session():
    """Snapshot of server-side state so the UI can restore after a refresh."""
    busy = _busy["job"] if (_busy["job"] and (time.time() - _busy["since"]) < _BUSY_TIMEOUT) else None
    return {
        "plan":        _session.get("plan"),
        "stories":     _session.get("stories", []),
        "image_paths": _session.get("image_paths", {}),
        "busy":        busy,
        "progress":    _session.get("bulk_progress"),
    }


@app.post("/api/plan/dummy")
async def api_load_dummy():
    """Load the built-in preview plan into the session (for template/layout testing)."""
    _session["plan"]        = _dummy_plan()
    _session["image_paths"] = {}
    return {"plan": _session["plan"]}


@app.put("/api/plan")
async def api_update_plan(plan: dict = Body(...)):
    _session["plan"] = plan
    return {"ok": True}


# ── API: images ────────────────────────────────────────────────────────────────
@app.post("/api/images/fetch")
async def api_fetch_images(body: dict = Body(default={})):
    plan   = _session.get("plan")
    source = body.get("source", "pexels")
    if not plan:
        raise HTTPException(400, "No plan loaded.")
    t0 = time.time()
    try:
        paths = await _run(_sync_fetch_images, plan, source)
        _session["image_paths"] = paths
        elapsed = round(time.time() - t0, 1)
        return {"image_paths": paths, "elapsed": elapsed}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/images/swap/{slide_idx}")
async def api_swap_image(slide_idx: int, body: dict = Body(...)):
    query  = body.get("query", "")
    source = body.get("source", "pexels")
    if not query:
        raise HTTPException(400, "query required")
    try:
        path = await _run(_sync_swap_pexels, query, source)
        _session["image_paths"][str(slide_idx)] = path
        return {"path": path, "filename": Path(path).name}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/images/search")
async def api_search_images(body: dict = Body(...)):
    """Return a grid of candidate images for a query so the user can pick one."""
    query  = body.get("query", "")
    source = body.get("source", "pexels")
    count  = int(body.get("count", 10))
    if not query:
        raise HTTPException(400, "query required")
    try:
        results = await _run(_sync_search_images, query, source, count)
        return {"results": results, "source": source}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/images/from-url")
async def api_image_from_url(body: dict = Body(...)):
    url   = body.get("url", "")
    name  = body.get("name", "")
    sidx  = body.get("slide_idx")
    do_filter = body.get("filter", True)   # bake the slight filter into web images
    if not url:
        raise HTTPException(400, "url required")
    try:
        path = await _run(_sync_fetch_url, url, name or None, do_filter)
        if sidx is not None:
            _session["image_paths"][str(sidx)] = path
        return {"path": path, "filename": Path(path).name}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/images/{slide_idx}")
async def api_clear_image(slide_idx: int):
    _session["image_paths"].pop(str(slide_idx), None)
    return {"ok": True}


@app.get("/api/images/cache")
async def api_list_cache():
    from images import list_cached
    files = list_cached()
    return {"files": [{"name": f.name, "url": f"/image_cache/{f.name}"} for f in files]}


@app.post("/api/images/cache/clear")
async def api_clear_cache():
    """Delete every cached background image and forget the session's image refs.

    Two-segment path (.../cache/clear) so it can't collide with the single-segment
    DELETE /api/images/{slide_idx} route.
    """
    cache = Path("image_cache")
    removed, freed = 0, 0
    for f in cache.glob("*"):
        if f.is_file():
            try:
                freed += f.stat().st_size
                f.unlink()
                removed += 1
            except OSError:
                pass
    _session["image_paths"] = {}
    return {"ok": True, "removed": removed, "freed_mb": round(freed / 1_000_000, 2)}


# ── API: preview ──────────────────────────────────────────────────────────────
@app.get("/api/preview/{slide_idx}")
async def api_preview(slide_idx: int, brand: str = ""):
    _apply_brand(brand)
    plan = _session.get("plan") or _dummy_plan()
    template, variables = _slide_vars(plan, slide_idx)
    try:
        png = await _run(_sync_preview_slide, template, variables)
        return Response(content=png, media_type="image/png")
    except Exception as e:
        raise HTTPException(500, str(e))


# ── API: render ───────────────────────────────────────────────────────────────
@app.post("/api/render")
async def api_render(body: dict = Body(default={})):
    plan = _session.get("plan")
    if not plan:
        raise HTTPException(400, "No plan loaded.")
    _apply_brand(body.get("brand"))
    save = body.get("save", True)
    t0 = time.time()
    try:
        out_dir = await _run(_sync_render_carousel, plan,
                             _session.get("image_paths", {}), save)
        _session["rendered_dir"] = out_dir
        elapsed = round(time.time() - t0, 1)
        files   = sorted(Path(out_dir).glob("*.png"))
        rel     = Path(out_dir).relative_to(Path("outputs")).as_posix()
        return {
            "output_dir": out_dir,
            "rel":        rel,
            "saved":      save,
            "files":      [f.name for f in files],
            "elapsed":    elapsed,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── API: templates ─────────────────────────────────────────────────────────────
@app.get("/api/templates")
async def api_list_templates():
    return {"files": list(EDITABLE_FILES.keys())}


@app.get("/api/template/{filename}")
async def api_get_template(filename: str):
    if filename not in EDITABLE_FILES:
        raise HTTPException(404, filename)
    return {"filename": filename, "content": EDITABLE_FILES[filename].read_text(encoding="utf-8")}


@app.put("/api/template/{filename}")
async def api_save_template(filename: str, body: dict = Body(...)):
    if filename not in EDITABLE_FILES:
        raise HTTPException(404, filename)
    path   = EDITABLE_FILES[filename]
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    path.write_text(body.get("content", ""), encoding="utf-8")
    return {"ok": True}


# Single-card template filename -> format key (so the Templates tab can preview
# them with representative content at the format's real dimensions).
SINGLE_TEMPLATE_FMT = {
    "square.html": "square", "story.html": "story", "xpost.html": "x",
    "quote.html": "quote", "comparison.html": "comparison",
    "breaking.html": "breaking", "linkedin.html": "linkedin",
}


def _dummy_single_plan(fmt: str) -> dict:
    """A filled-in single-card plan so each new template previews with real-looking
    content instead of empty fields."""
    p = {"slug": "preview", "format": fmt,
         "headline": "Your Headline Goes Here",
         "body": "— First key point\n— Second key point\n— Third key point",
         "image_query": "city skyline", "caption": "Preview caption.",
         "hashtags": ["preview"], "dm_keyword": "INFO"}
    if fmt == "quote":
        p.update({"quote": "73% of local buyers check you out online first.",
                  "context": "If your site isn't ready, you lose them before hello.",
                  "source_label": "via industry data"})
    elif fmt == "comparison":
        p.update({"headline": "DIY Website vs Pro Build",
                  "option_a": {"label": "DIY Builder", "points": "— Cheap upfront\n— Generic templates\n— Your time sunk"},
                  "option_b": {"label": "Pro Build", "points": "— Built to convert\n— Owned + scalable\n— You stay focused"},
                  "verdict": "Pro pays for itself in leads."})
    elif fmt == "breaking":
        p.update({"banner": "HOT TAKE", "headline": "AI search is rewriting SEO",
                  "take": "If your content isn't structured for it, you're invisible by next quarter."})
    elif fmt == "linkedin":
        p.update({"hook": "Most local sites fail in the first 5 seconds.",
                  "take": "Speed and clarity beat clever design — every time.",
                  "points": "— Load under 2s\n— One clear CTA\n— Proof above the fold",
                  "cta": "Book a free audit"})
    return p


@app.post("/api/template/preview/{filename}")
async def api_template_preview(filename: str, body: dict = Body(default={})):
    _apply_brand(body.get("brand"))
    plan = _session.get("plan") or _dummy_plan()
    try:
        if filename == "cover.html":
            template  = "cover.html"
            variables = {**_base_vars(plan), "category": body.get("category", "")}
            png = await _run(_sync_preview_slide, template, variables)
        elif filename in SINGLE_TEMPLATE_FMT:
            from render import format_config
            fmt   = SINGLE_TEMPLATE_FMT[filename]
            dplan = _dummy_single_plan(fmt)
            fc    = format_config(fmt)
            variables = {**_base_vars(dplan), "post": dplan, "background_image": None}
            png = await _run(_sync_preview_fmt, filename, variables,
                             fc.get("width", 1080), fc.get("height", 1080))
        elif filename == "listicle_content.html":
            slide = {"rank": 3, "heading": "THE THIRD PICK",
                     "body": "— Why it earns the spot\n— What makes it stand out",
                     "image_query": "spotlight"}
            variables = {**_base_vars(_dummy_plan()), "slide": slide,
                         "slide_number": 3, "background_image": None}
            png = await _run(_sync_preview_slide, filename, variables)
        else:
            idx_map = {"title.html": 0, "brand.css": 0,
                       "content.html": 1,
                       "outro.html": 1 + len(plan.get("content_slides", []))}
            idx = idx_map.get(filename, 0)
            template, variables = _slide_vars(plan, idx)
            png = await _run(_sync_preview_slide, template, variables)
        return Response(content=png, media_type="image/png")
    except Exception as e:
        raise HTTPException(500, str(e))


def _dummy_plan():
    return {
        "slug": "preview",
        "slide_count": 4,
        "title_card": {
            "headline": "3 Ways K2 Digital Media Grows London Businesses",
            "subhead":  "Local strategy, real results — no fluff.",
        },
        "content_slides": [
            {"heading": "LOCAL INSIGHT", "body": "— We know London's market inside out.\n— Your competitors don't get this playbook.", "image_query": "London Ontario city"},
            {"heading": "FULL STACK", "body": "— Web, video, marketing, IT — one team.\n— No hand-offs, no finger-pointing.", "image_query": "digital team working"},
        ],
        "outro_card": {"cta": "Follow for weekly London business insight", "handle": "@k2digitalmedia_"},
        "caption": "London's local business scene is moving fast.",
        "hashtags": ["london", "ontario", "digitalmedia"],
        "dm_keyword": "GROW",
    }


# ── Frontend ──────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    # Never cache the UI — otherwise the browser can serve a stale build (e.g.
    # an old brand switcher) and quietly use the wrong brand.
    return HTMLResponse(FRONTEND_HTML, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    })


FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>K2 Digital Media</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/xml/xml.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/css/css.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/htmlmixed/htmlmixed.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/fabric.js/5.3.1/fabric.min.js"></script>
<style>
@font-face{font-family:'Roboto Mono';font-weight:400;font-display:swap;src:url('/static/fonts/RobotoMono-400.woff2') format('woff2');}
@font-face{font-family:'Roboto Mono';font-weight:500;font-display:swap;src:url('/static/fonts/RobotoMono-500.woff2') format('woff2');}
@font-face{font-family:'Roboto Mono';font-weight:700;font-display:swap;src:url('/static/fonts/RobotoMono-700.woff2') format('woff2');}
:root{--navy:#0A0F1E;--teal:#00B4C8;--green:#00C896;--panel:#111827;--border:#1e2a3a;--text:#e4e8f0;--muted:#6b7a96;--red:#e05252;--yellow:#f5c542;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
html{height:100%;-webkit-text-size-adjust:100%;}
body{font-family:Calibri,Arial,sans-serif;background:var(--navy);color:var(--text);height:100vh;height:100dvh;display:flex;flex-direction:column;overflow:hidden;font-size:15px;}
/* Header */
header{display:flex;align-items:center;gap:16px;padding:0 20px;height:52px;background:var(--panel);border-bottom:1px solid var(--border);flex-shrink:0;}
/* Mobile hamburger + slide-in drawer. On desktop the drawer is just an inline
   flex row holding nav + the right-hand controls; the hamburger/overlay hide. */
.hamburger{display:none;background:none;border:none;color:#fff;font-size:22px;line-height:1;cursor:pointer;padding:4px 8px;border-radius:8px;}
.hamburger:hover{background:var(--border);}
.nav-drawer{display:flex;align-items:center;flex:1;gap:16px;min-width:0;}
.nav-overlay{display:none;}
.logo{display:flex;align-items:center;gap:10px;}
.logo img{width:34px;height:34px;border-radius:50%;}
.logo-text{font-size:17px;font-weight:900;color:#fff;letter-spacing:-0.3px;}
.logo-text span{color:var(--teal);}
nav{display:flex;gap:3px;margin-left:16px;}
.nav-btn{padding:5px 14px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-family:inherit;font-weight:600;background:transparent;color:var(--muted);transition:all .15s;}
.nav-btn:hover{color:#fff;}
.nav-btn.active{background:var(--teal);color:#000;}
.header-right{margin-left:auto;display:flex;align-items:center;gap:10px;}
.model-sel{background:#0d1828;border:1px solid var(--border);border-radius:6px;color:#fff;padding:4px 8px;font-size:12px;font-family:inherit;cursor:pointer;}
.model-sel:focus{outline:none;border-color:var(--teal);}
/* Main layout */
.main{display:flex;flex:1;overflow:hidden;}
.tab{display:none;flex:1;overflow:hidden;}
.tab.active{display:flex;}
/* ═══ STORIES TAB ══════════════════════════════════════════════════════════ */
#tab-stories{flex-direction:column;}
.stories-toolbar{display:flex;align-items:center;gap:8px;padding:12px 18px;border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap;}
/* Keep a label glued to its control so they wrap together, not as loose items. */
.tb-group{display:inline-flex;align-items:center;gap:5px;flex-shrink:0;margin:0;}
.tb-spacer{flex:1;}
.cat-tabs{display:flex;gap:4px;overflow-x:auto;}
.cat-btn{padding:4px 12px;border:1px solid var(--border);border-radius:20px;background:transparent;color:var(--muted);cursor:pointer;font-size:12px;font-family:inherit;font-weight:600;white-space:nowrap;transition:all .15s;}
.cat-btn:hover{border-color:var(--teal);color:var(--teal);}
.cat-btn.active{background:var(--teal);border-color:var(--teal);color:#000;}
.stories-list{flex:1;overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:8px;}
/* Bulk tab: side-by-side brand columns */
#tab-bulk{flex-direction:column;}
.bulk-cols{flex:1;display:flex;gap:14px;overflow-y:auto;padding:14px 18px;align-items:flex-start;}
.bulk-col{flex:1;min-width:0;background:var(--panel);border:1px solid var(--border);border-radius:10px;display:flex;flex-direction:column;max-height:100%;}
.bulk-col-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--border);}
.bulk-col-head img{height:26px;width:auto;max-width:80px;object-fit:contain;border-radius:5px;}
.bulk-fmts{display:flex;flex-wrap:wrap;gap:5px;padding:10px 14px;border-bottom:1px solid var(--border);}
.bulk-list{overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:7px;min-height:60px;}
#bulk-results{overflow-y:auto;}
.bulk-item{background:#0d1828;border:1px solid var(--border);border-radius:8px;padding:9px 11px;font-size:12px;display:flex;gap:8px;align-items:flex-start;}
.bulk-item .bt{color:#fff;line-height:1.35;}
.bulk-item .bs{font-size:10px;color:var(--muted);}
.story-card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 18px;cursor:pointer;transition:border-color .15s;}
.story-card:hover{border-color:var(--teal);}
.story-card.selected{border-color:var(--teal);background:#0d1e2e;}
.s-top{display:flex;align-items:center;gap:10px;margin-bottom:6px;}
.score-pill{padding:2px 10px;border-radius:20px;font-size:12px;font-weight:700;flex-shrink:0;}
.sh{background:rgba(0,200,150,.15);color:var(--green);}
.sm{background:rgba(0,180,200,.15);color:var(--teal);}
.sl{background:rgba(107,122,150,.12);color:var(--muted);}
.story-title{font-size:14px;font-weight:700;color:#fff;line-height:1.3;}
.story-reason{font-size:12px;color:var(--muted);line-height:1.4;margin-bottom:6px;}
.story-actions{display:flex;gap:6px;margin-top:8px;}
/* ═══ EDITOR TAB ═══════════════════════════════════════════════════════════ */
#tab-editor{flex-direction:row;}
.editor-left{width:400px;flex-shrink:0;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;}
.editor-right{flex:1;display:flex;flex-direction:column;overflow:hidden;}
.panel-header{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid var(--border);flex-shrink:0;}
.panel-header h3{font-size:13px;font-weight:700;color:#fff;flex:1;}
.plan-form{flex:1;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:12px;}
.field-group{display:flex;flex-direction:column;gap:4px;}
.field-group label{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;}
.field-group input,.field-group textarea,.field-group select{background:#0d1828;border:1px solid var(--border);border-radius:6px;color:#fff;padding:7px 10px;font-family:inherit;font-size:13px;line-height:1.4;resize:vertical;}
.field-group input:focus,.field-group textarea:focus,.field-group select:focus{outline:none;border-color:var(--teal);}
.slide-sec{background:rgba(0,180,200,.04);border:1px solid var(--border);border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:9px;}
.slide-sec-title{font-size:12px;font-weight:700;color:var(--teal);}
.img-row{display:flex;align-items:center;gap:6px;}
.img-thumb{width:48px;height:48px;object-fit:cover;border-radius:5px;border:1px solid var(--border);background:var(--panel);flex-shrink:0;}
.img-thumb.empty{display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:18px;}
.src-sel{background:#0d1828;border:1px solid var(--border);border-radius:6px;color:#fff;padding:5px 6px;font-size:12px;font-family:inherit;}
/* Preview pane */
.preview-pane{flex:1;display:flex;flex-direction:column;overflow:hidden;}
.preview-nav{display:flex;align-items:center;gap:8px;}
.slide-cnt{font-size:12px;color:var(--muted);min-width:52px;text-align:center;}
.preview-wrap{flex:1;overflow:hidden;display:flex;align-items:center;justify-content:center;padding:14px;background:#070b14;}
.preview-wrap img{max-height:100%;max-width:100%;border-radius:5px;object-fit:contain;box-shadow:0 8px 32px rgba(0,0,0,.6);}
.preview-placeholder{color:var(--muted);text-align:center;font-size:13px;line-height:1.6;}
/* Image from URL row */
.url-row{display:flex;gap:6px;align-items:center;}
.url-inp{flex:1;background:#0d1828;border:1px solid var(--border);border-radius:6px;color:#fff;padding:5px 8px;font-size:12px;font-family:inherit;}
.url-inp:focus{outline:none;border-color:var(--teal);}
/* ═══ CANVAS TAB ═══════════════════════════════════════════════════════════ */
#tab-canvas{flex-direction:row;}
.canvas-left{width:210px;flex-shrink:0;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;padding:14px 12px;gap:14px;}
.canvas-left h4{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px;}
.canvas-center{flex:1;display:flex;flex-direction:column;overflow:hidden;}
.canvas-toolbar{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap;}
.canvas-area{flex:1;display:flex;align-items:center;justify-content:center;background:#060a13;overflow:hidden;padding:20px;}
.canvas-wrap{position:relative;border:1px solid var(--border);box-shadow:0 8px 32px rgba(0,0,0,.6);}
.canvas-wrap .canvas-container{max-width:100%;}
.canvas-right{width:210px;flex-shrink:0;border-left:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;padding:14px 12px;gap:12px;}
.canvas-right h4{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px;}
.prop-row{display:flex;flex-direction:column;gap:4px;}
.prop-row label{font-size:11px;color:var(--muted);}
.prop-inp{background:#0d1828;border:1px solid var(--border);border-radius:5px;color:#fff;padding:5px 7px;font-size:12px;font-family:inherit;width:100%;}
.prop-inp:focus{outline:none;border-color:var(--teal);}
.color-row{display:flex;gap:5px;flex-wrap:wrap;}
.color-swatch{width:26px;height:26px;border-radius:5px;cursor:pointer;border:2px solid transparent;transition:all .15s;}
.color-swatch:hover,.color-swatch.active{border-color:#fff;}
.preset-btns{display:flex;flex-direction:column;gap:5px;}
/* ═══ TEMPLATES TAB ════════════════════════════════════════════════════════ */
#tab-templates{flex-direction:row;}
.tmpl-left{width:220px;flex-shrink:0;border-right:1px solid var(--border);display:flex;flex-direction:column;}
.tmpl-files{flex:1;overflow-y:auto;padding:10px;}
.tmpl-file-btn{display:block;width:100%;text-align:left;padding:8px 11px;border:none;border-radius:6px;background:transparent;color:var(--text);cursor:pointer;font-family:inherit;font-size:13px;font-weight:600;margin-bottom:3px;transition:background .15s;}
.tmpl-file-btn:hover{background:var(--border);}
.tmpl-file-btn.active{background:var(--teal);color:#000;}
.tmpl-right{flex:1;display:flex;flex-direction:column;overflow:hidden;}
.code-area{flex:1;overflow:hidden;display:flex;}
.code-area .CodeMirror{flex:1;height:100%;font-size:13px;font-family:'Cascadia Code','Consolas',monospace;}
.tmpl-preview-pane{width:300px;flex-shrink:0;border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;}
.tmpl-preview-header{padding:10px 14px;border-bottom:1px solid var(--border);font-size:13px;font-weight:700;color:#fff;}
.tmpl-preview-img-wrap{flex:1;overflow:hidden;display:flex;align-items:center;justify-content:center;padding:10px;background:#060a13;}
.tmpl-preview-img-wrap img{max-width:100%;max-height:100%;border-radius:4px;}
/* ═══ Shared ════════════════════════════════════════════════════════════════ */
.btn{display:inline-flex;align-items:center;gap:5px;padding:7px 14px;border:none;border-radius:7px;cursor:pointer;font-size:12px;font-family:inherit;font-weight:700;transition:all .15s;}
.btn:disabled{opacity:.42;cursor:not-allowed;}
.btn-primary{background:var(--teal);color:#000;}
.btn-primary:hover:not(:disabled){background:#00cbe0;}
.btn-green{background:var(--green);color:#000;}
.btn-green:hover:not(:disabled){background:#00e0aa;}
.btn-ghost{background:var(--border);color:var(--text);}
.btn-ghost:hover:not(:disabled){background:#2a3a50;}
.btn-danger{background:rgba(224,82,82,.15);color:var(--red);}
.btn-danger:hover:not(:disabled){background:rgba(224,82,82,.3);}
.btn-icon{padding:5px 9px;font-size:15px;}
.btn-sm{padding:4px 10px;font-size:11px;}
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:700;}
.badge-ok{background:rgba(0,200,150,.15);color:var(--green);}
.badge-err{background:rgba(224,82,82,.15);color:var(--red);}
.badge-info{background:rgba(0,180,200,.15);color:var(--teal);}
.timer{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;}
#toast{position:fixed;bottom:22px;right:22px;padding:10px 18px;border-radius:8px;font-size:12px;font-weight:600;z-index:9999;opacity:0;transition:opacity .2s;pointer-events:none;}
#toast.show{opacity:1;}
#toast.ok{background:var(--green);color:#000;}
#toast.err{background:var(--red);color:#fff;}
.spin{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.2);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;}
@keyframes spin{to{transform:rotate(360deg)}}
/* Settings form rows */
.set-row{display:flex;align-items:center;gap:14px;flex-wrap:wrap;}
.set-label{flex:1 1 180px;min-width:0;font-size:13px;color:#fff;display:flex;flex-direction:column;gap:2px;}
.set-hint{font-size:11px;color:var(--muted);font-weight:400;}
.set-control{flex:1 1 200px;min-width:0;}
::-webkit-scrollbar{width:5px;height:5px;}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px;}

@media (max-width: 900px){
  body{overflow:hidden;font-size:14px;}
  header{
    height:52px;
    padding:8px 12px;
    gap:10px;
    align-items:center;
  }
  .hamburger{display:flex;align-items:center;justify-content:center;width:40px;height:40px;flex:0 0 auto;}
  .logo{min-width:0;flex:1 1 auto;}
  .logo img{width:30px;height:30px;}
  .logo-text{font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .logo-text span{display:none;}

  /* Dim backdrop behind the open drawer; tap to close. */
  .nav-overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:40;}
  body:not(.nav-open) .nav-overlay{display:none;}
  body.nav-open .nav-overlay{display:block;}

  /* The drawer: off-screen by default, slides in when body.nav-open. */
  .nav-drawer{
    position:fixed;top:0;left:0;bottom:0;
    width:82%;max-width:320px;
    flex:none;
    flex-direction:column;align-items:stretch;justify-content:flex-start;
    gap:6px;padding:14px;
    background:var(--panel);border-right:1px solid var(--border);
    box-shadow:2px 0 18px rgba(0,0,0,.4);
    transform:translateX(-100%);transition:transform .25s ease;
    z-index:50;overflow-y:auto;-webkit-overflow-scrolling:touch;
  }
  body.nav-open .nav-drawer{transform:translateX(0);}

  .nav-drawer nav{flex-direction:column;width:100%;margin:0;gap:6px;overflow:visible;}
  .nav-drawer .nav-btn{
    width:100%;justify-content:flex-start;text-align:left;
    min-height:46px;padding:10px 14px;font-size:15px;border-radius:9px;
  }
  .nav-drawer .header-right{
    flex-direction:column;align-items:stretch;
    margin:14px 0 0;gap:9px;min-width:0;
    border-top:1px solid var(--border);padding-top:14px;
  }
  .nav-drawer .header-right label{display:block;font-size:11px;color:var(--muted);}
  .nav-drawer .timer{display:none;}
  .nav-drawer .model-sel,.nav-drawer .src-sel{width:100%;max-width:none;min-height:42px;font-size:14px;}
  .src-sel{min-height:34px;max-width:132px;font-size:12px;}
  .main{
    overflow:hidden;
    min-height:0;
  }
  .tab.active{
    overflow-y:auto;
    min-height:0;
    -webkit-overflow-scrolling:touch;
  }
  .stories-toolbar{
    position:sticky;
    top:0;
    z-index:8;
    padding:10px;
    gap:7px;
    background:var(--navy);
    align-items:stretch;
  }
  .stories-toolbar > .btn,
  .stories-toolbar > select,
  .stories-toolbar > input,
  .stories-toolbar > .src-sel{
    min-height:36px;
  }
  .tb-group > .src-sel,
  .tb-group > select,
  .tb-group > input{min-height:36px;}
  /* Bulk toolbar: stack into clean full-width rows instead of a wrapped jumble. */
  .bulk-toolbar{flex-direction:column;align-items:stretch;}
  .bulk-toolbar .tb-spacer{display:none;}
  .bulk-toolbar .tb-hint{display:none;}
  .bulk-toolbar .tb-group{display:flex;justify-content:space-between;}
  .bulk-toolbar .tb-group > .src-sel{flex:1;max-width:none;margin-left:8px;}
  .bulk-toolbar #btn-bulk-run{width:100%;}
  .cat-tabs{
    order:10;
    width:100%;
    padding-bottom:2px;
  }
  .cat-btn{
    min-height:32px;
    padding:6px 12px;
  }
  .stories-list{
    padding:10px;
    gap:10px;
  }
  .story-card{
    padding:12px;
    border-radius:8px;
  }
  .s-top{
    align-items:flex-start;
    flex-wrap:wrap;
  }
  .story-title{
    flex:1 1 220px;
    min-width:0;
    overflow-wrap:anywhere;
  }
  .story-actions{
    flex-wrap:wrap;
  }
  .btn{
    min-height:34px;
    justify-content:center;
    white-space:nowrap;
  }
  .btn-sm{min-height:32px;}

  #tab-bulk{overflow-y:auto;}
  .bulk-cols{
    flex:none;
    flex-direction:column;
    overflow:visible;
    padding:10px;
    gap:10px;
  }
  .bulk-col{
    width:100%;
    max-height:none;
  }
  .bulk-col-head{
    padding:10px;
    gap:7px;
  }
  .bulk-col-head select{
    flex:1 1 160px;
    min-width:0;
  }
  .bulk-list{
    max-height:none;
  }
  .bulk-item{
    padding:10px;
  }
  #bulk-results{
    overflow:visible;
    padding-bottom:12px;
  }

  #tab-editor,
  #tab-canvas,
  #tab-templates{
    flex-direction:column;
    overflow-y:auto;
  }
  .editor-left,
  .editor-right,
  .canvas-left,
  .canvas-center,
  .canvas-right,
  .tmpl-left,
  .tmpl-right,
  .tmpl-preview-pane{
    width:100%;
    flex:0 0 auto;
    border-left:none;
    border-right:none;
  }
  .editor-left{
    border-bottom:1px solid var(--border);
    max-height:none;
  }
  .editor-right{
    min-height:70vh;
  }
  .panel-header{
    padding:9px 10px;
    gap:7px;
    flex-wrap:wrap;
  }
  .panel-header h3{
    flex:1 1 140px;
  }
  .plan-form{
    padding:10px;
    gap:10px;
    overflow:visible;
  }
  .slide-sec{
    padding:10px;
  }
  .field-group input,
  .field-group textarea,
  .field-group select,
  .url-inp,
  .prop-inp{
    min-height:36px;
    font-size:14px;
  }
  .img-row,
  .url-row{
    flex-wrap:wrap;
  }
  .img-row .url-inp,
  .url-row .url-inp{
    flex-basis:100%;
  }
  .preview-wrap{
    min-height:calc(100vh - 210px);
    min-height:calc(100dvh - 210px);
    padding:10px;
  }
  .preview-wrap img{
    max-height:calc(100vh - 230px);
    max-height:calc(100dvh - 230px);
  }

  .canvas-left{
    order:2;
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(148px,1fr));
    gap:10px;
    padding:10px;
    border-bottom:1px solid var(--border);
  }
  .canvas-left .preset-btns,
  .canvas-left [style*="flex-direction:column"]{
    gap:6px;
  }
  .canvas-center{
    order:1;
    min-height:auto;
  }
  .canvas-toolbar{
    padding:9px 10px;
  }
  .canvas-area{
    min-height:calc(100vw * 1.25 + 24px);
    max-height:none;
    padding:10px;
    overflow:auto;
  }
  .canvas-wrap{
    width:min(100%,540px);
    aspect-ratio:540 / 675;
  }
  .canvas-wrap .canvas-container,
  .canvas-wrap canvas{
    width:100% !important;
    height:100% !important;
  }
  #overlay-canvas{
    width:100% !important;
    height:100% !important;
  }
  .canvas-right{
    order:3;
    display:grid;
    grid-template-columns:repeat(2,minmax(0,1fr));
    gap:10px;
    padding:10px;
    border-top:1px solid var(--border);
  }
  .canvas-right h4,
  .canvas-right hr,
  .canvas-right > button,
  .canvas-right .pos-grid,
  .canvas-right .prop-row:first-of-type{
    grid-column:1 / -1;
  }

  .tmpl-left{
    border-bottom:1px solid var(--border);
  }
  .tmpl-files{
    display:flex;
    gap:6px;
    overflow-x:auto;
    padding:8px 10px;
  }
  .tmpl-file-btn{
    width:auto;
    flex:0 0 auto;
    margin-bottom:0;
    white-space:nowrap;
  }
  .tmpl-right{
    min-height:65vh;
  }
  .code-area{
    min-height:65vh;
  }
  .tmpl-preview-pane{
    min-height:55vh;
    border-top:1px solid var(--border);
  }
  .tmpl-preview-img-wrap{
    min-height:48vh;
  }

  #modal-bg{
    align-items:flex-end !important;
    padding:10px;
  }
  #modal-bg > div{
    width:100% !important;
    max-width:100% !important;
    max-height:88vh !important;
    border-radius:10px !important;
  }
  #toast{
    left:12px;
    right:12px;
    bottom:12px;
    text-align:center;
  }
}

@media (max-width: 560px){
  header{
    padding:7px 8px;
  }
  .stories-toolbar > .btn{
    flex:1 1 auto;
  }
  #stories-status,
  #bulk-total{
    width:100%;
  }
  #bulk-fmt-chips{
    width:100%;
    overflow-x:auto;
    padding-bottom:2px;
  }
  .panel-header > .btn,
  .panel-header > select,
  .panel-header > .src-sel{
    flex:1 1 auto;
  }
  .preview-nav{
    flex:1 1 100%;
    justify-content:space-between;
  }
  .slide-cnt{
    flex:1;
  }
  .canvas-left{
    grid-template-columns:1fr;
  }
  .canvas-toolbar > .btn,
  .canvas-toolbar > span{
    flex:1 1 auto;
  }
  .canvas-area{
    min-height:calc(100vw * 1.25 + 20px);
  }
  .canvas-right{
    grid-template-columns:1fr;
  }
  .tmpl-right,
  .code-area{
    min-height:58vh;
  }
}
</style>
</head>
<body>
<header>
  <button class="hamburger" id="hamburger" onclick="toggleNav()" aria-label="Menu" aria-expanded="false">☰</button>
  <div class="logo">
    <img id="hdr-logo" src="/static/logo.png" alt="brand">
    <span class="logo-text" id="hdr-brand">K2<span> Digital Media</span></span>
  </div>
  <div class="nav-drawer" id="nav-drawer">
    <nav>
      <button class="nav-btn active" onclick="showTab('stories',this)">Stories</button>
      <button class="nav-btn"       onclick="showTab('bulk',this)">⚡ Bulk</button>
      <button class="nav-btn"       onclick="showTab('editor',this)">Editor</button>
      <button class="nav-btn"       onclick="showTab('canvas',this)">Canvas</button>
      <button class="nav-btn"       onclick="showTab('templates',this)">Templates</button>
    </nav>
    <div class="header-right">
      <span class="timer" id="hdr-timer"></span>
      <button class="model-sel" id="notif-btn" onclick="toggleNotify()" title="Get a desktop notification when a task finishes" style="cursor:pointer;">🔔 Off</button>
      <label style="font-size:11px;color:var(--muted);">Brand:</label>
      <select class="model-sel" id="brand-sel" onchange="switchBrand(this.value)" title="Active brand / IG page">
        <option>…</option>
      </select>
      <label style="font-size:11px;color:var(--muted);">Model:</label>
      <select class="model-sel" id="model-sel" onchange="setModel(this.value)">
        <option>Loading…</option>
      </select>
      <button class="model-sel" id="settings-btn" onclick="openSettings()" title="Defaults &amp; preferences" style="cursor:pointer;">⚙ Settings</button>
    </div>
  </div>
  <div class="nav-overlay" id="nav-overlay" onclick="toggleNav(false)"></div>
</header>

<div class="main">

<!-- ═══════════ STORIES ══════════════════════════════════════════════════ -->
<div id="tab-stories" class="tab active">
  <div class="stories-toolbar">
    <button class="btn btn-primary" onclick="fetchStories()" id="btn-fetch">Fetch &amp; Score</button>
    <button class="btn btn-ghost btn-sm" onclick="resetUsed()" id="btn-reset-used" title="Allow already-generated stories to be served again">↺ Reset seen</button>
    <button class="btn btn-danger btn-sm" id="btn-cancel" style="display:none;" onclick="cancelJob()">⨯ Cancel</button>
    <div class="cat-tabs" id="cat-tabs">
      <button class="cat-btn active" onclick="selectCat('',this)">All</button>
    </div>
    <label class="tb-group"><span style="font-size:11px;color:var(--muted);">fetch</span>
    <input type="number" id="fetch-limit" value="15" min="3" max="60" style="width:52px;background:#0d1828;border:1px solid var(--border);border-radius:5px;color:#fff;padding:4px 6px;font-size:12px;" title="stories to fetch"></label>
    <label class="tb-group"><span style="font-size:11px;color:var(--muted);">top</span>
    <input type="number" id="fetch-top" value="6" min="1" max="15" style="width:44px;background:#0d1828;border:1px solid var(--border);border-radius:5px;color:#fff;padding:4px 6px;font-size:12px;" title="top N"></label>
    <button class="btn btn-ghost btn-sm" onclick="saveStorySet()" title="Save this fetched + scored set">💾 Save set</button>
    <button class="btn btn-ghost btn-sm" onclick="openStoryLibrary()" title="Load a saved set (no re-fetch)">📂 Saved</button>
    <span id="stories-status"></span>
  </div>

  <!-- Batch action bar -->
  <div class="stories-toolbar" id="batch-bar" style="display:none;background:rgba(0,200,150,.05);">
    <label style="display:flex;align-items:center;gap:5px;font-size:12px;color:var(--text);font-weight:700;">
      <input type="checkbox" id="select-all" onchange="toggleAll(this)"> Select all
    </label>
    <span id="sel-count" style="font-size:12px;color:var(--muted);">0 selected</span>
    <div style="flex:1"></div>
    <span style="font-size:11px;color:var(--muted);">Apply formats to selected:</span>
    <div id="bulk-fmt-chips" style="display:flex;gap:4px;"></div>
    <select id="tone-sel" class="src-sel" title="Stance applied to generated copy">
      <option value="">Tone: Auto</option>
      <option value="positive, upbeat">Positive</option>
      <option value="negative, critical">Negative</option>
      <option value="neutral, factual">Neutral</option>
      <option value="hyped, exciting">Hype</option>
      <option value="analytical, measured">Analytical</option>
      <option value="skeptical, cautionary">Skeptical</option>
    </select>
    <select id="batch-src" class="src-sel"><option value="pexels">Pexels</option><option value="unsplash">Unsplash</option><option value="google">Google</option></select>
    <button class="btn btn-green" onclick="runBatch()" id="btn-batch">Generate All Selected</button>
    <button class="btn btn-danger btn-sm" id="btn-cancel-batch" style="display:none;" onclick="cancelJob()">⨯ Cancel</button>
  </div>

  <div class="stories-list" id="stories-list">
    <div style="color:var(--muted);text-align:center;padding:40px;font-size:13px;line-height:1.7;">
      Select a category and click <b>Fetch &amp; Score</b>.<br>
      Ollama will rank stories from your configured feeds.
    </div>
  </div>
</div>

<!-- ═══════════ BULK (BOTH BRANDS) ════════════════════════════════════════ -->
<div id="tab-bulk" class="tab">
  <div class="stories-toolbar bulk-toolbar" style="flex-wrap:wrap;">
    <b style="font-size:14px;color:#fff;">⚡ Bulk — all brands</b>
    <span class="tb-hint" style="font-size:11px;color:var(--muted);">Fetch top stories per brand, tick what you want, then generate everything in one run.</span>
    <div class="tb-spacer"></div>
    <label class="tb-group"><span style="font-size:11px;color:var(--muted);">Tone</span>
    <select id="bulk-tone" class="src-sel" title="Stance applied to all generated copy">
      <option value="">Auto</option>
      <option value="positive, upbeat">Positive</option>
      <option value="negative, critical">Negative</option>
      <option value="neutral, factual">Neutral</option>
      <option value="hyped, exciting">Hype</option>
      <option value="analytical, measured">Analytical</option>
      <option value="skeptical, cautionary">Skeptical</option>
    </select></label>
    <label class="tb-group"><span style="font-size:11px;color:var(--muted);">Images</span>
    <select id="bulk-src" class="src-sel"><option value="pexels">Pexels</option><option value="unsplash">Unsplash</option><option value="google">Google</option></select></label>
    <span id="bulk-total" style="font-size:12px;color:var(--muted);font-weight:700;">0 posts</span>
    <button class="btn btn-green" onclick="runBulk()" id="btn-bulk-run">⚡ Generate All</button>
    <button class="btn btn-danger btn-sm" id="btn-bulk-cancel" style="display:none;" onclick="cancelJob()">⨯ Cancel</button>
  </div>
  <div id="bulk-progress" style="display:none;margin:0 0 10px;"></div>
  <div id="bulk-brands" class="bulk-cols">
    <div style="color:var(--muted);text-align:center;padding:40px;font-size:13px;">Loading brands…</div>
  </div>
  <div id="bulk-results"></div>
</div>

<!-- ═══════════ EDITOR ═══════════════════════════════════════════════════ -->
<div id="tab-editor" class="tab">
  <div class="editor-left">
    <div class="panel-header">
      <h3>Plan Editor</h3>
      <button class="btn btn-ghost btn-sm" onclick="savePlan()" title="Save this plan to your library">💾 Save</button>
      <button class="btn btn-ghost btn-sm" onclick="openLibrary()" title="Load a saved plan">📂 Library</button>
      <select id="slide-count-sel" style="background:#0d1828;border:1px solid var(--border);border-radius:5px;color:#fff;padding:3px 6px;font-size:12px;font-family:inherit;">
        <option value="3">3 slides</option>
        <option value="4" selected>4 slides</option>
        <option value="5">5 slides</option>
        <option value="6">6 slides</option>
        <option value="7">7 slides</option>
        <option value="8">8 slides</option>
        <option value="9">9 slides</option>
        <option value="10">10 slides</option>
      </select>
    </div>
    <div class="plan-form" id="plan-form">
      <div style="color:var(--muted);text-align:center;padding:28px 10px;font-size:12px;line-height:1.7;">
        Pick a story → plan loads here.<br>
        <button class="btn btn-ghost btn-sm" style="margin-top:10px;" onclick="loadDummy()">Load Preview Plan</button>
      </div>
    </div>
  </div>

  <div class="editor-right">
    <div class="panel-header">
      <div class="preview-nav">
        <button class="btn btn-ghost btn-icon" onclick="prevSlide()">&#8249;</button>
        <span class="slide-cnt" id="slide-cnt">—/—</span>
        <button class="btn btn-ghost btn-icon" onclick="nextSlide()">&#8250;</button>
      </div>
      <button class="btn btn-primary btn-sm" onclick="previewCurrent()" id="btn-prev-slide">Preview</button>
      <div style="flex:1"></div>
      <!-- Image source -->
      <select id="img-source" class="src-sel">
        <option value="pexels">Pexels</option>
        <option value="unsplash">Unsplash</option>
        <option value="google">Google</option>
      </select>
      <button class="btn btn-ghost btn-sm" onclick="fetchImages()" id="btn-fetch-img">Fetch Images</button>
      <button class="btn btn-danger btn-sm" onclick="clearImageCache()" title="Delete all cached background images">🗑 Cache</button>
      <button class="btn btn-green" onclick="renderFull()" id="btn-render">Render Carousel</button>
    </div>
    <div class="preview-pane">
      <div class="preview-wrap" id="preview-wrap">
        <div class="preview-placeholder">Load a plan and click Preview</div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════ CANVAS ════════════════════════════════════════════════════ -->
<div id="tab-canvas" class="tab">
  <!-- Tools -->
  <div class="canvas-left">
    <div>
      <h4>From current plan</h4>
      <div class="preset-btns" style="margin-top:6px;">
        <button class="btn btn-primary btn-sm" style="justify-content:flex-start;" onclick="loadSlideToCanvas()">Load Editor Slide</button>
        <span style="font-size:10px;color:var(--muted);line-height:1.4;">Mirrors the slide open in the Editor (real text + image) so you can tweak it visually, then export.</span>
      </div>
    </div>
    <div>
      <h4>Blank presets</h4>
      <div class="preset-btns" style="margin-top:6px;">
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="applyPreset('title')">Title Card</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="applyPreset('content')">Content Slide</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="applyPreset('outro')">Outro / CTA</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="applyPreset('cover')">Brand Cover</button>
      </div>
    </div>
    <div>
      <h4>Add Elements</h4>
      <div style="display:flex;flex-direction:column;gap:5px;margin-top:6px;">
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addText('Headline',72,'bold')">+ Headline</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addText('Subheading',44,'bold')">+ Subheading</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addText('Body text goes here',32,'normal')">+ Body Text</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addText('@k2digitalmedia_',22,'bold')">+ Handle</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addRect()">+ Dark Panel</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addRule()">+ Teal Rule</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addLogo()">+ Logo Badge</button>
      </div>
    </div>
    <div>
      <h4>Background</h4>
      <div style="display:flex;flex-direction:column;gap:5px;margin-top:6px;">
        <button class="btn btn-ghost btn-sm" onclick="setBgNavy()">Navy (default)</button>
        <button class="btn btn-ghost btn-sm" onclick="setBgTeal()">Teal Gradient</button>
        <input type="file" id="bg-upload" accept="image/*" style="display:none" onchange="setBgFile(this)">
        <button class="btn btn-ghost btn-sm" onclick="document.getElementById('bg-upload').click()">Upload Image…</button>
        <div style="display:flex;gap:5px;margin-top:4px;">
          <input class="url-inp" id="canvas-img-url" placeholder="Image URL…" style="flex:1;font-size:11px;padding:4px 6px;">
          <button class="btn btn-ghost btn-sm" onclick="setBgUrl()">Set</button>
        </div>
        <div style="display:flex;gap:5px;margin-top:2px;">
          <input class="url-inp" id="canvas-pexels-q" placeholder="Pexels query…" style="flex:1;font-size:11px;padding:4px 6px;">
          <button class="btn btn-ghost btn-sm" onclick="setBgPexels()">Search</button>
        </div>
      </div>
    </div>
    <div>
      <h4>Canvas</h4>
      <div style="display:flex;flex-direction:column;gap:5px;margin-top:6px;">
        <button class="btn btn-danger btn-sm" onclick="clearCanvas()">Clear All</button>
      </div>
    </div>
  </div>

  <!-- Canvas -->
  <div class="canvas-center">
    <div class="canvas-toolbar">
      <button class="btn btn-primary" onclick="exportCanvas()" id="btn-export-canvas">Export PNG (1080×1350)</button>
      <span id="canvas-status" style="font-size:11px;color:var(--muted);margin-left:4px;">Design freely, then export a ready-to-post PNG</span>
      <div style="flex:1;"></div>
      <label style="font-size:11px;color:var(--muted);">Overlay:</label>
      <input type="range" id="overlay-opacity" min="0" max="90" value="65" style="width:80px;" oninput="updateOverlay(this.value)">
      <span id="overlay-val" style="font-size:11px;color:var(--muted);min-width:28px;">65%</span>
    </div>
    <div class="canvas-area">
      <div class="canvas-wrap" id="canvas-wrap">
        <canvas id="design-canvas"></canvas>
        <canvas id="overlay-canvas" style="position:absolute;top:0;left:0;pointer-events:none;"></canvas>
      </div>
    </div>
  </div>

  <!-- Properties -->
  <div class="canvas-right">
    <h4>Text Properties</h4>
    <div class="prop-row">
      <label>Content</label>
      <textarea class="prop-inp" id="prop-text" rows="3" oninput="applyProp()"></textarea>
    </div>
    <div class="prop-row">
      <label>Font size</label>
      <input class="prop-inp" type="number" id="prop-size" value="46" min="8" max="200" oninput="applyProp()">
    </div>
    <div style="display:flex;gap:6px;align-items:center;margin-top:4px;">
      <button class="btn btn-ghost btn-sm" id="prop-bold" onclick="toggleBold()"><b>B</b></button>
      <button class="btn btn-ghost btn-sm" id="prop-italic" onclick="toggleItalic()"><i>I</i></button>
      <button class="btn btn-ghost btn-sm" id="prop-upper" onclick="toggleUpper()">AA</button>
    </div>
    <div class="prop-row" style="margin-top:8px;">
      <label>Colour</label>
      <div class="color-row" id="color-swatches">
        <div class="color-swatch active" style="background:#fff;" onclick="setColor('#ffffff',this)" title="White"></div>
        <div class="color-swatch" style="background:#00B4C8;" onclick="setColor('#00B4C8',this)" title="Teal"></div>
        <div class="color-swatch" style="background:#00C896;" onclick="setColor('#00C896',this)" title="Green"></div>
        <div class="color-swatch" style="background:#0A0F1E;" onclick="setColor('#0A0F1E',this)" title="Navy"></div>
        <div class="color-swatch" style="background:#6b7a96;" onclick="setColor('#6b7a96',this)" title="Muted"></div>
        <input type="color" id="custom-color" value="#ffffff" style="width:26px;height:26px;border:none;border-radius:5px;cursor:pointer;padding:0;" onchange="setColor(this.value)">
      </div>
    </div>
    <hr style="border:none;border-top:1px solid var(--border);margin:8px 0;">
    <h4>Position &amp; Size</h4>
    <div class="pos-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:4px;">
      <div class="prop-row"><label>X</label><input class="prop-inp" type="number" id="prop-x" oninput="applyPos()"></div>
      <div class="prop-row"><label>Y</label><input class="prop-inp" type="number" id="prop-y" oninput="applyPos()"></div>
      <div class="prop-row"><label>W</label><input class="prop-inp" type="number" id="prop-w" oninput="applySize()"></div>
      <div class="prop-row"><label>H</label><input class="prop-inp" type="number" id="prop-h" oninput="applySize()"></div>
    </div>
    <hr style="border:none;border-top:1px solid var(--border);margin:8px 0;">
    <button class="btn btn-danger btn-sm" onclick="deleteSelected()" style="width:100%;">Delete Selected</button>
    <button class="btn btn-ghost btn-sm" style="width:100%;margin-top:5px;" onclick="bringFront()">Bring to Front</button>
    <button class="btn btn-ghost btn-sm" style="width:100%;margin-top:5px;" onclick="sendBack()">Send to Back</button>
  </div>
</div>

<!-- ═══════════ TEMPLATES ═════════════════════════════════════════════════ -->
<div id="tab-templates" class="tab">
  <div class="tmpl-left">
    <div style="padding:12px 10px 5px;font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;">Files</div>
    <div class="tmpl-files" id="tmpl-file-list"></div>
  </div>
  <div class="tmpl-right">
    <div class="panel-header">
      <h3 id="tmpl-filename">Select a file</h3>
      <button class="btn btn-ghost btn-sm" onclick="saveTemplate()">Save</button>
      <button class="btn btn-primary btn-sm" onclick="previewTemplate()">Preview</button>
      <span id="tmpl-status"></span>
    </div>
    <div class="code-area" id="code-area">
      <textarea id="code-editor"></textarea>
    </div>
  </div>
  <div class="tmpl-preview-pane">
    <div class="tmpl-preview-header">Preview</div>
    <div class="tmpl-preview-img-wrap" id="tmpl-preview-wrap">
      <div style="color:var(--muted);font-size:12px;text-align:center;">Save then click Preview</div>
    </div>
  </div>
</div>

</div><!-- .main -->

<!-- Library / picker modal -->
<div id="modal-bg" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9998;align-items:center;justify-content:center;" onclick="if(event.target===this)closeModal()">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;width:560px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;overflow:hidden;">
    <div style="display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid var(--border);">
      <h3 id="modal-title" style="font-size:15px;color:#fff;flex:1;">Library</h3>
      <button class="btn btn-ghost btn-sm" onclick="closeModal()">✕</button>
    </div>
    <div id="modal-body" style="padding:12px 14px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;"></div>
  </div>
</div>

<div id="toast"></div>

<script>
// ═══════════════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════════════
let S = {
  stories: [],
  plan: null,
  imagePaths: {},
  slideIdx: 0,
  totalSlides: 0,
  selectedCat: '',
  formats: {},            // {key: name} from /api/formats
  selected: {},           // {storyIdx: {checked:bool, formats:Set}}
  bulkFormats: new Set(['carousel']),
  busy: null,             // name of in-flight model job, or null
  batch: [],              // last batch results (for Edit buttons)
  brandInfo: null,        // active brand {name,short,handle,tagline,logo,accent,navy,...}
  notify: false,          // desktop notifications enabled
  cancelRequested: false, // user asked to cancel the running loop job
  bulk: {},               // {brandKey: {name, stories:[], sel:{}, formats:Set, count:int}}
  results: [],            // last batch/bulk results (for ✨ Suggest / re-render)
};

// ═══════════════════════════════════════════════════════════════════════════
// User settings / defaults — persisted in localStorage, applied on every load
// so the app boots into your preferred model / tone / image source etc.
// ═══════════════════════════════════════════════════════════════════════════
const SETTINGS_DEFAULTS = {
  notify: true,           // desktop notifications ON by default
  model: '',              // '' → keep whatever the server/first option is
  tone: '',               // '' → Auto
  imageSource: 'pexels',  // pexels | unsplash | google
  slides: 4,              // default carousel slide count
  fetchLimit: 15,         // stories to fetch per run
  fetchTop: 6,            // top N to keep after scoring
};
let SETTINGS = {...SETTINGS_DEFAULTS};

function loadSettings() {
  try { SETTINGS = {...SETTINGS_DEFAULTS, ...JSON.parse(localStorage.getItem('k2_settings') || '{}')}; }
  catch(e) { SETTINGS = {...SETTINGS_DEFAULTS}; }
  // Migrate the old standalone notify flag the first time (before any settings save).
  const legacy = localStorage.getItem('k2_notify');
  if (legacy !== null && localStorage.getItem('k2_settings') === null) SETTINGS.notify = legacy === '1';
  return SETTINGS;
}
function saveSettings() { localStorage.setItem('k2_settings', JSON.stringify(SETTINGS)); }

// Push saved defaults onto the live controls (only when the control exists / the
// value is a real option). Called after models/formats have loaded.
function applySettings() {
  const setVal = (id, v) => { const el = g(id); if (el && v != null && v !== '') el.value = v; };
  setVal('bulk-tone',       SETTINGS.tone);
  setVal('bulk-src',        SETTINGS.imageSource);
  setVal('slide-count-sel', String(SETTINGS.slides));
  setVal('fetch-limit',     String(SETTINGS.fetchLimit));
  setVal('fetch-top',       String(SETTINGS.fetchTop));
  const ms = g('model-sel');
  if (ms && SETTINGS.model && [...ms.options].some(o => o.value === SETTINGS.model)) {
    if (ms.value !== SETTINGS.model) { ms.value = SETTINGS.model; setModel(SETTINGS.model); }
  }
}

// Ask the server to stop the running fetch/batch after the current item.
async function cancelJob() {
  S.cancelRequested = true;
  try {
    const d = await api('/api/cancel','POST');
    toast(d.ok ? 'Cancelling — stopping after the current item…' : 'Nothing to cancel');
  } catch(e){ toast(e.message,'err'); }
}

// ═══════════════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════════════
window.addEventListener('DOMContentLoaded', async () => {
  loadSettings();
  initNotify();
  await loadBrands();
  await Promise.all([loadModels(), loadCategories(), loadFormats()]);
  applySettings();          // apply saved defaults now that controls/options exist
  await restoreSession();
  refreshResponsiveSurfaces();
});
window.addEventListener('resize', refreshResponsiveSurfaces);
window.addEventListener('orientationchange', () => setTimeout(refreshResponsiveSurfaces, 250));

// ── Desktop notifications ────────────────────────────────────────────────────
// Default-on: if the saved preference wants notifications and the browser hasn't
// decided yet, ask once on load so they "just work" without a manual toggle.
async function initNotify() {
  S.notify = false;
  if (SETTINGS.notify && ('Notification' in window)) {
    if (Notification.permission === 'granted') {
      S.notify = true;
    } else if (Notification.permission === 'default') {
      try { S.notify = (await Notification.requestPermission()) === 'granted'; } catch(e) {}
    }
  }
  updateNotifBtn();
}
function updateNotifBtn() {
  const b = g('notif-btn'); if(!b) return;
  b.textContent = S.notify ? '🔔 On' : '🔔 Off';
  b.style.color = S.notify ? 'var(--green)' : '';
}
async function toggleNotify() {
  if(!('Notification' in window)) { toast('This browser has no notifications','err'); return; }
  if(S.notify) { S.notify=false; SETTINGS.notify=false; saveSettings(); updateNotifBtn(); toast('Notifications off'); return; }
  let perm = Notification.permission;
  if(perm!=='granted') perm = await Notification.requestPermission();
  if(perm==='granted') {
    S.notify=true; SETTINGS.notify=true; saveSettings(); updateNotifBtn();
    notify('Notifications enabled', "You'll be pinged when each task finishes.");
  } else { toast('Notification permission denied — enable it in your browser site settings','err'); }
}
function notify(title, body) {
  try {
    if(S.notify && ('Notification' in window) && Notification.permission==='granted') {
      const icon = (S.brandInfo && S.brandInfo.logo) || '/static/logo.png';
      const n = new Notification(title, { body: body||'', icon, tag:'k2-task' });
      setTimeout(()=>{ try{ n.close(); }catch(e){} }, 6000);
    }
  } catch(e){}
}

// ── Brands ──────────────────────────────────────────────────────────────────
async function loadBrands() {
  const sel = document.getElementById('brand-sel');
  const data = await api('/api/brands').catch(() => ({ brands: {}, active: '' }));
  const entries = Object.entries(data.brands || {});
  if (!entries.length) { sel.innerHTML = '<option>—</option>'; return; }
  sel.innerHTML = entries.map(([k, name]) =>
    `<option value="${k}" ${k === data.active ? 'selected' : ''}>${esc(name)}</option>`).join('');
  applyBrandChrome(await api('/api/brand').catch(() => null));
}

function applyBrandChrome(b) {
  if (!b) return;
  S.brandInfo = b;
  const img = document.getElementById('hdr-logo');
  if (img) {
    img.src = b.logo + '?t=' + Date.now();
    if (b.shape === 'wide') {   // full wordmark — show it whole, don't crop to a circle
      img.style.cssText = 'height:30px;width:auto;max-width:120px;border-radius:0;object-fit:contain;';
    } else {
      img.style.cssText = 'width:34px;height:34px;border-radius:50%;object-fit:cover;';
    }
  }
  const txt = document.getElementById('hdr-brand');
  if (txt) txt.textContent = b.name || '';
  if (b.accent) document.documentElement.style.setProperty('--teal', b.accent);
}

async function switchBrand(key) {
  if (S.busy) { toast(`Wait — '${S.busy}' is still running`, 'err'); return; }
  try {
    const b = await api('/api/brand', 'PUT', { brand: key });
    applyBrandChrome(b);
    // brand switch clears server context; reset the UI too
    S.plan = null; S.stories = []; S.imagePaths = {}; S.batch = [];
    document.getElementById('stories-list').innerHTML =
      `<div style="color:var(--muted);text-align:center;padding:40px;font-size:13px;">
         Switched to <b>${esc(b.name)}</b>. Click <b>Fetch &amp; Score</b> to pull its feeds.</div>`;
    document.getElementById('batch-bar').style.display = 'none';
    document.getElementById('plan-form').innerHTML =
      `<div style="color:var(--muted);text-align:center;padding:28px 10px;font-size:12px;">
         Pick a story → plan loads here.</div>`;
    await Promise.all([loadCategories(true), loadFormats()]);
    toast(`Brand: ${b.name}`);
  } catch(e) { toast(e.message, 'err'); }
}

// Re-hydrate from server state so a refresh / back-navigation doesn't wipe your
// work (which previously forced you to regenerate, re-running the model).
async function restoreSession() {
  let s;
  try { s = await api('/api/session'); } catch(e) { return; }
  if (s.stories && s.stories.length) {
    S.stories = s.stories;
    renderStoriesList(s.stories);
  }
  if (s.plan) {
    loadPlan(s.plan);
    S.imagePaths = s.image_paths || {};
    Object.entries(S.imagePaths).forEach(([i,p]) => {
      if (p) setThumb(parseInt(i), '/image_cache/' + p.split(/[/\\]/).pop());
    });
    toast('Restored your last plan');
  }
  if (s.busy) {
    S.busy = s.busy;
    toast(`A '${s.busy}' job is still running on the server…`, 'err');
  }
}

// Warn before leaving while a model job is in flight.
window.addEventListener('beforeunload', (e) => {
  if (S.busy) { e.preventDefault(); e.returnValue = ''; }
});

async function loadFormats() {
  const data = await api('/api/formats').catch(() => ({ formats: {} }));
  S.formats = data.formats || {};
  S.formatRestrict = data.restrict || {};   // {fmt: [allowed brand keys]}
  // bulk format chips
  const wrap = document.getElementById('bulk-fmt-chips');
  if (wrap) {
    wrap.innerHTML = Object.entries(S.formats).map(([k, name]) =>
      `<button class="cat-btn ${S.bulkFormats.has(k) ? 'active' : ''}" data-bk="${k}" onclick="toggleBulkFmt('${k}',this)">${name}</button>`
    ).join('');
  }
}

function toggleBulkFmt(key, btn) {
  if (S.bulkFormats.has(key)) S.bulkFormats.delete(key); else S.bulkFormats.add(key);
  btn.classList.toggle('active');
}

async function loadModels() {
  const sel = document.getElementById('model-sel');
  const data = await api('/api/models').catch(() => ({ models: [] }));
  const models = data.models || [];
  if (!models.length) { sel.innerHTML = '<option>No models found</option>'; return; }
  sel.innerHTML = models.map(m => `<option value="${m}">${m}</option>`).join('');
}

async function setModel(m) {
  await api('/api/model', 'PUT', { model: m });
  toast(`Model: ${m}`);
}

async function loadCategories() {
  const data = await api('/api/categories').catch(() => ({ categories: {} }));
  const cats = data.categories || {};
  const tb = document.getElementById('cat-tabs');
  S.selectedCat = '';
  tb.innerHTML = '<button class="cat-btn active" onclick="selectCat(\'\',this)">All</button>'
    + '<button class="cat-btn" style="color:#ff8a3d;" onclick="selectCat(\'__trending__\',this)" title="Pull from Google Trends instead of RSS">🔥 Trending</button>';
  Object.entries(cats).forEach(([k, name]) => {
    const b = document.createElement('button');
    b.className = 'cat-btn';
    b.textContent = name;
    b.onclick = () => selectCat(k, b);
    tb.appendChild(b);
  });
}

function selectCat(key, btn) {
  S.selectedCat = key;
  // Scope to the category bar only — other .cat-btn (story format chips, bulk
  // format chips) keep their own active state, which is their Set's source of truth.
  document.querySelectorAll('#cat-tabs .cat-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

// ═══════════════════════════════════════════════════════════════════════════
// Mobile nav drawer (hamburger)
// ═══════════════════════════════════════════════════════════════════════════
function toggleNav(force) {
  const open = (force === undefined) ? !document.body.classList.contains('nav-open') : !!force;
  document.body.classList.toggle('nav-open', open);
  const h = document.getElementById('hamburger');
  if (h) h.setAttribute('aria-expanded', open ? 'true' : 'false');
}
// Close the drawer on Escape.
document.addEventListener('keydown', e => { if (e.key === 'Escape') toggleNav(false); });

// ═══════════════════════════════════════════════════════════════════════════
// Tabs
// ═══════════════════════════════════════════════════════════════════════════
function showTab(name, btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
  if (name === 'templates' && !window._cm) initCM();
  if (name === 'templates') loadTmplList();
  if (name === 'canvas' && !window._fc) initCanvas();
  if (name === 'bulk' && !window._bulkInit) { window._bulkInit = true; initBulk(); }
  toggleNav(false);   // collapse the mobile drawer after picking a tab
  requestAnimationFrame(refreshResponsiveSurfaces);
}

function refreshResponsiveSurfaces() {
  try { if (window._cm) window._cm.refresh(); } catch(e) {}
  try {
    if (fc) {
      fc.calcOffset();
      fc.requestRenderAll();
    }
  } catch(e) {}
}

// ═══════════════════════════════════════════════════════════════════════════
// Bulk — generate posts for every brand in one run
// ═══════════════════════════════════════════════════════════════════════════
async function initBulk() {
  let brands = {};
  try { brands = (await api('/api/brands')).brands || {}; } catch(e){}
  const host = g('bulk-brands');
  host.innerHTML = '';
  for (const [key, name] of Object.entries(brands)) {
    S.bulk[key] = { name, stories: [], sel: {}, formats: new Set(['carousel']), count: 25 };
    // per-brand categories (resolved without switching the active brand)
    let cats = {};
    try { cats = (await api('/api/categories?brand='+encodeURIComponent(key))).categories || {}; } catch(e){}
    const catOpts = ['<option value="">All RSS feeds</option>',
                     '<option value="__trending__">🔥 Google Trending</option>']
      .concat(Object.entries(cats).map(([k,n])=>`<option value="${k}">${esc(n)}</option>`)).join('');
    const restrict = S.formatRestrict || {};
    const fmtChips = Object.entries(S.formats)
      .filter(([fk]) => !restrict[fk] || restrict[fk].includes(key))   // hide brand-locked formats
      .map(([fk,fn]) =>
        `<button class="cat-btn bfmt" data-bk="${key}" data-fk="${fk}" onclick="bulkToggleFmt('${key}','${fk}',this)" style="padding:2px 9px;font-size:11px;">${esc(fn)}</button>`
      ).join('');
    const col = document.createElement('div');
    col.className = 'bulk-col';
    col.innerHTML = `
      <div class="bulk-col-head">
        <img src="/api/brand-logo?brand=${encodeURIComponent(key)}" onerror="this.style.display='none'" alt="">
        <b style="color:#fff;font-size:14px;">${esc(name)}</b>
        <div style="flex:1"></div>
        <select class="src-sel" id="bulk-cat-${key}">${catOpts}</select>
        <input type="number" id="bulk-count-${key}" value="25" min="1" max="60" style="width:52px;background:#0d1828;border:1px solid var(--border);border-radius:5px;color:#fff;padding:4px 6px;font-size:12px;" title="how many top stories">
        <button class="btn btn-primary btn-sm" onclick="bulkFetch('${key}')" id="bulk-fetch-${key}">Fetch top</button>
        <button class="btn btn-ghost btn-sm" onclick="bulkClear('${key}')" id="bulk-clear-${key}" title="Clear fetched stories" style="display:none;">✕ Clear</button>
        <span id="bulk-status-${key}" style="font-size:11px;color:var(--muted);"></span>
      </div>
      <div class="bulk-fmts" id="bulk-fmts-${key}">
        <span style="font-size:11px;color:var(--muted);align-self:center;">formats:</span>${fmtChips}
      </div>
      <div class="bulk-list" id="bulk-list-${key}">
        <div style="color:var(--muted);font-size:12px;text-align:center;padding:20px;">Pick a source and click <b>Fetch top</b>.</div>
      </div>`;
    host.appendChild(col);
    // reflect the default-on carousel chip
    col.querySelectorAll(`.bfmt[data-bk="${key}"]`).forEach(b => {
      if (S.bulk[key].formats.has(b.dataset.fk)) b.classList.add('active');
    });
  }
}

function bulkToggleFmt(key, fk, btn) {
  const st = S.bulk[key]; if (!st) return;
  if (st.formats.has(fk)) st.formats.delete(fk); else st.formats.add(fk);
  btn.classList.toggle('active');
  bulkUpdateTotal();
}

async function bulkFetch(key) {
  const st = S.bulk[key]; if (!st) return;
  const cat = g('bulk-cat-'+key).value;
  const count = parseInt(g('bulk-count-'+key).value || '25');
  st.count = count;
  const btn = g('bulk-fetch-'+key); const status = g('bulk-status-'+key);
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>';
  status.textContent = 'fetching…';
  const model = g('model-sel').value;
  try {
    // brand= makes the server score/trend for this brand (per-request brand, no UI switch)
    const data = await api(`/api/stories/fetch?limit=${count}&top=${count}&category=${encodeURIComponent(cat)}&model=${encodeURIComponent(model)}&brand=${encodeURIComponent(key)}`, 'POST');
    st.stories = data.stories || [];
    st.sel = {};
    st.stories.forEach((_,i)=> st.sel[i] = true);  // pre-select all
    renderBulkList(key);
    const clr = g('bulk-clear-'+key); if (clr) clr.style.display = st.stories.length ? 'inline-flex' : 'none';
    status.innerHTML = `<span class="badge badge-ok">${st.stories.length} ready</span>`;
  } catch(e) {
    status.innerHTML = `<span class="badge badge-err">${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false; btn.innerHTML = 'Fetch top';
  }
}

// ✕ Clear a column's fetched stories back to the empty state.
function bulkClear(key) {
  const st = S.bulk[key]; if (!st) return;
  st.stories = []; st.sel = {};
  const list = g('bulk-list-'+key);
  if (list) list.innerHTML = '<div style="color:var(--muted);font-size:12px;text-align:center;padding:20px;">Pick a source and click <b>Fetch top</b>.</div>';
  const status = g('bulk-status-'+key); if (status) status.textContent = '';
  const clr = g('bulk-clear-'+key); if (clr) clr.style.display = 'none';
  bulkUpdateTotal();
}

function renderBulkList(key) {
  const st = S.bulk[key]; const list = g('bulk-list-'+key);
  if (!st.stories.length) { list.innerHTML = '<div style="color:var(--muted);font-size:12px;text-align:center;padding:20px;">No stories.</div>'; bulkUpdateTotal(); return; }
  list.innerHTML = st.stories.map((s,i)=>`
    <label class="bulk-item">
      <input type="checkbox" ${st.sel[i]?'checked':''} onchange="bulkToggleStory('${key}',${i},this)" style="width:15px;height:15px;margin-top:2px;flex-shrink:0;">
      <div>
        <div class="bt">${esc(s.title)}</div>
        <div class="bs">${s.score!=null?('score '+s.score+' · '):''}${esc((s.reason||'').slice(0,90))}</div>
      </div>
    </label>`).join('');
  bulkUpdateTotal();
}

function bulkToggleStory(key, i, chk) {
  S.bulk[key].sel[i] = chk.checked;
  bulkUpdateTotal();
}

function bulkSelectedItems(key) {
  const st = S.bulk[key]; if (!st) return [];
  const fmts = [...st.formats];
  if (!fmts.length) return [];
  const total = parseInt(g('slide-count-sel')?.value || '4');
  const items = [];
  st.stories.forEach((s,i)=>{
    if (!st.sel[i]) return;
    items.push({ story:{title:s.title,summary:s.summary,url:s.url,published:s.published||''},
                 formats: fmts, total_slides: total });
  });
  return items;
}

function bulkUpdateTotal() {
  let posts = 0;
  Object.keys(S.bulk).forEach(k => {
    bulkSelectedItems(k).forEach(it => posts += it.formats.length);
  });
  const el = g('bulk-total'); if (el) el.textContent = posts + ' posts';
  return posts;
}

async function runBulk() {
  const groups = Object.keys(S.bulk)
    .map(k => ({ brand:k, items: bulkSelectedItems(k) }))
    .filter(g => g.items.length);
  if (!groups.length) { toast('Select at least one story','err'); return; }
  const totalPosts = bulkUpdateTotal();

  const btn = g('btn-bulk-run');
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span> Generating ${totalPosts}…`;
  S.busy = 'bulk'; S.cancelRequested = false;
  const cancelBtn = g('btn-bulk-cancel'); if (cancelBtn) cancelBtn.style.display='inline-flex';
  const prog = g('bulk-progress'); prog.style.display='block';
  const poll = setInterval(pollBulkProgress, 700);
  const t0 = Date.now();
  const tid = setInterval(()=>{ g('hdr-timer').textContent = ((Date.now()-t0)/1000).toFixed(1)+'s'; },200);
  try {
    const model = g('model-sel').value;
    const source = g('bulk-src').value;
    const tone = g('bulk-tone').value || '';
    const data = await api('/api/bulk/run','POST',{groups,model,source,tone});
    g('hdr-timer').textContent = data.elapsed+'s';
    showBulkResults(data);
    const ok = data.results.filter(r=>r.ok).length;
    const note = data.cancelled ? ' (cancelled)' : '';
    toast(`Bulk done${note}: ${ok}/${data.results.length} posts in ${data.elapsed}s`);
    notify(data.cancelled?'Bulk cancelled':'Bulk complete', `${ok}/${data.results.length} posts in ${data.elapsed}s`);
  } catch(e) {
    toast(e.message,'err'); notify('Bulk failed', e.message);
  } finally {
    S.busy = null; clearInterval(tid); clearInterval(poll);
    prog.style.display='none';
    if (cancelBtn) cancelBtn.style.display='none';
    btn.disabled = false; btn.innerHTML = '⚡ Generate All';
  }
}

async function pollBulkProgress() {
  try {
    const s = await api('/api/session');
    const p = s.progress;
    if (!p) return;
    const pct = p.total ? Math.round(p.done/p.total*100) : 0;
    g('bulk-progress').innerHTML = `
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 14px;">
        <div style="display:flex;justify-content:space-between;font-size:12px;color:#fff;margin-bottom:6px;">
          <span>${p.done} / ${p.total} posts ${p.brand?('· '+esc(p.brand)):''}</span>
          <span style="color:var(--muted);">${esc((p.current||'').slice(0,60))}</span>
        </div>
        <div style="height:7px;background:#0d1828;border-radius:4px;overflow:hidden;">
          <div style="height:100%;width:${pct}%;background:var(--green);transition:width .3s;"></div>
        </div>
      </div>`;
  } catch(e){}
}

function showBulkResults(data) {
  S.results = data.results || [];
  const host = g('bulk-results');
  // group results by brand
  const byBrand = {};
  S.results.forEach((r,ri)=>{ (byBrand[r.brand] = byBrand[r.brand] || []).push(ri); });
  const dirs = (data.batch_dirs||[]).join(' · ');
  let html = `<div style="display:flex;align-items:center;gap:10px;margin:4px 18px 10px;">
      <h3 style="font-size:15px;color:#fff;">Bulk results</h3>
      <span class="badge badge-ok">${S.results.filter(r=>r.ok).length} ok</span>
      ${S.results.some(r=>!r.ok)?`<span class="badge badge-err">${S.results.filter(r=>!r.ok).length} failed</span>`:''}
      <span style="font-size:11px;color:var(--muted);">${esc(dirs)}</span>
    </div>`;
  Object.entries(byBrand).forEach(([bk, idxs])=>{
    html += `<div style="margin:0 18px 6px;font-size:13px;font-weight:700;color:var(--teal);">${esc((S.bulk[bk]&&S.bulk[bk].name)||bk)}</div>
      <div style="display:flex;flex-direction:column;gap:8px;padding:0 18px 14px;">
      ${idxs.map(ri=>resultCard(ri)).join('')}</div>`;
  });
  host.innerHTML = html;
}

// Shared result-card renderer (used by single-brand batch + bulk). ri indexes S.results.
function resultCard(ri) {
  const r = S.results[ri];
  if (!r.ok) return `
    <div class="story-card" style="border-color:${r.skipped?'var(--border)':'var(--red)'};cursor:default;">
      <div class="s-top"><span class="score-pill badge ${r.skipped?'badge-info':'badge-err'}">${esc(r.format)}${r.skipped?' skipped':' failed'}</span><span class="story-title">${esc(r.title)}</span></div>
      <div class="story-reason" style="color:${r.skipped?'var(--muted)':'var(--red)'};">${esc(r.error||'')}</div>
    </div>`;
  const isCarousel = (r.format==='carousel' || r.format==='listicle');
  return `
    <div class="story-card" style="cursor:default;" id="rc-${ri}">
      <div class="s-top">
        <span class="score-pill badge badge-info">${esc(S.formats[r.format]||r.format)}</span>
        <span class="story-title">${esc(r.title)}</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0;" id="rc-imgs-${ri}">
        ${r.files.map(f=>`<a href="/outputs/${r.rel}/${f}" target="_blank"><img src="/outputs/${r.rel}/${f}" style="height:120px;border-radius:5px;border:1px solid var(--border);"></a>`).join('')}
      </div>
      <div class="story-reason" style="white-space:pre-wrap;">${esc(r.caption||'')}</div>
      <div class="story-actions" style="margin-top:8px;flex-wrap:wrap;">
        ${isCarousel ? `<button class="btn btn-primary btn-sm" onclick="editResultPlan(${ri})">✎ Edit in Editor</button>`
                     : `<button class="btn btn-green btn-sm" onclick="suggestForResult(${ri})">✨ Suggest images</button>`}
        <a href="/outputs/${r.rel}/" target="_blank" class="btn btn-ghost btn-sm">Open folder ↗</a>
      </div>
    </div>`;
}

// Load a result's carousel/listicle plan into the editor.
async function editResultPlan(ri) {
  const r = S.results[ri];
  if (!r || !r.plan) { toast('No editable plan for this result','err'); return; }
  // Switch brand FIRST and wait — switchBrand clears S.plan + server session,
  // so loading the plan before it finishes would get wiped by the race.
  if (r.brand && r.brand !== curBrand()) {
    const sel = g('brand-sel'); if (sel) sel.value = r.brand;
    await switchBrand(r.brand);
  }
  loadPlan(r.plan);
  S.imagePaths = {};
  await api('/api/plan','PUT',r.plan).catch(()=>{});
  showTab('editor', document.querySelectorAll('.nav-btn')[2]);
  toast('Loaded into editor — tweak then Render');
}

// ✨ Suggest images for a single-card result → swap + re-render just that post.
async function suggestForResult(ri) {
  const r = S.results[ri];
  const q = r.image_query || r.title || '';
  const source = g('bulk-src')?.value || g('batch-src')?.value || 'pexels';
  openModal(`✨ Suggest images · ${source} · "${esc(q)}"`);
  const body = g('modal-body');
  body.innerHTML = '<div style="color:var(--muted);font-size:12px;"><span class="spin"></span> Searching…</div>';
  try {
    const data = await api('/api/images/search','POST',{query:q, source, count:12});
    const results = data.results || [];
    if (!results.length) { body.innerHTML = '<div style="color:var(--muted);font-size:12px;">No results.</div>'; return; }
    body.innerHTML = `
      <div style="font-size:11px;color:var(--muted);margin-bottom:6px;">${results.length} images. Click one to swap it in and re-render this post.</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;">
        ${results.map(rr=>`
          <div style="cursor:pointer;border:1px solid var(--border);border-radius:6px;overflow:hidden;background:#0d1828;" onclick='pickForResult(${ri}, ${JSON.stringify(rr.url)}, ${JSON.stringify(source)})' title="${esc(rr.title||'')}">
            <img src="${esc(rr.thumb||rr.url)}" style="width:100%;height:120px;object-fit:cover;display:block;" loading="lazy" onerror="this.style.opacity=.3">
          </div>`).join('')}
      </div>`;
  } catch(e){ body.innerHTML = `<div style="color:var(--red);font-size:12px;">${esc(e.message)}</div>`; }
}

async function pickForResult(ri, url, source) {
  const r = S.results[ri];
  const body = g('modal-body');
  if (body) body.innerHTML = '<div style="color:var(--muted);font-size:12px;"><span class="spin"></span> Swapping & re-rendering…</div>';
  try {
    const dl = await api('/api/images/from-url','POST',{ url, name:`${r.slug||'post'}`, filter: source==='google' });
    const re = await api('/api/bulk/rerender','POST',{
      plan: r.plan, format: r.format, brand: r.brand, image_paths: { 0: dl.path },
    });
    r.rel = re.rel; r.files = re.files;
    const imgs = g('rc-imgs-'+ri);
    if (imgs) imgs.innerHTML = r.files.map(f=>`<a href="/outputs/${r.rel}/${f}?t=${Date.now()}" target="_blank"><img src="/outputs/${r.rel}/${f}?t=${Date.now()}" style="height:120px;border-radius:5px;border:1px solid var(--border);"></a>`).join('');
    closeModal();
    toast('Image swapped & re-rendered');
  } catch(e){ toast(e.message,'err'); closeModal(); }
}

// ═══════════════════════════════════════════════════════════════════════════
// Stories
// ═══════════════════════════════════════════════════════════════════════════
async function fetchStories() {
  const btn = document.getElementById('btn-fetch');
  const status = document.getElementById('stories-status');
  const model = document.getElementById('model-sel').value;
  const limit = document.getElementById('fetch-limit').value;
  const top   = document.getElementById('fetch-top').value;
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Fetching…';
  status.innerHTML = '<span class="badge badge-info">Scoring with Ollama…</span>';
  S.busy = 'fetch stories'; S.cancelRequested = false;
  const cancelBtn = g('btn-cancel'); if(cancelBtn) cancelBtn.style.display='inline-flex';
  const t0 = Date.now();
  const tid = setInterval(() => {
    document.getElementById('hdr-timer').textContent = ((Date.now()-t0)/1000).toFixed(1)+'s';
  }, 200);
  try {
    const data = await api(`/api/stories/fetch?limit=${limit}&top=${top}&category=${S.selectedCat}&model=${encodeURIComponent(model)}&brand=${encodeURIComponent(curBrand())}`, 'POST');
    S.stories = data.stories;
    renderStoriesList(data.stories);
    const cancelNote = S.cancelRequested ? ' · cancelled' : '';
    const skipNote = data.excluded ? ` · ${data.excluded} already used` : '';
    status.innerHTML = `<span class="badge badge-ok">${data.stories.length} ranked · ${data.elapsed}s${skipNote}${cancelNote}</span>`;
    document.getElementById('hdr-timer').textContent = data.elapsed+'s';
    notify(S.cancelRequested ? 'Fetch cancelled' : 'Fetch & Score complete',
           `${data.stories.length} ${curBrand()||''} stories ranked in ${data.elapsed}s`);
  } catch(e) {
    status.innerHTML = `<span class="badge badge-err">${e.message}</span>`;
    toast(e.message, 'err');
  } finally {
    S.busy = null;
    clearInterval(tid);
    if(cancelBtn) cancelBtn.style.display='none';
    btn.disabled = false; btn.innerHTML = 'Fetch &amp; Score';
  }
}

async function resetUsed() {
  try {
    const r = await api('/api/session/reset','POST',{});
    toast(r.cleared ? `Reset — ${r.cleared} story(ies) can appear again` : 'Nothing to reset');
  } catch(e){ toast(e.message,'err'); }
}

function renderStoriesList(stories) {
  const list = document.getElementById('stories-list');
  if (!stories.length) { list.innerHTML = '<div style="color:var(--muted);text-align:center;padding:40px;font-size:13px;">No stories returned.</div>'; return; }
  S.selected = {};
  document.getElementById('batch-bar').style.display = 'flex';
  list.innerHTML = stories.map((s,i) => {
    const cls = s.score>=70?'sh':s.score>=40?'sm':'sl';
    const fmtChips = Object.entries(S.formats).map(([k,name]) =>
      `<button class="cat-btn" data-si="${i}" data-fk="${k}" onclick="toggleStoryFmt(${i},'${k}',this)" style="padding:2px 9px;font-size:11px;">${name}</button>`
    ).join('');
    return `<div class="story-card" id="sc-${i}">
      <div class="s-top">
        <input type="checkbox" class="story-chk" onchange="toggleStory(${i},this)" style="width:16px;height:16px;flex-shrink:0;">
        <span class="score-pill badge ${cls}">${s.score}/100</span>
        <span class="story-title">${esc(s.title)}</span>
      </div>
      <div class="story-reason">${esc(s.reason)}</div>
      <div class="story-actions" style="flex-wrap:wrap;align-items:center;">
        <button class="btn btn-primary btn-sm" onclick="useStor(${i})">Edit as Carousel</button>
        <a href="${esc(s.url)}" target="_blank" class="btn btn-ghost btn-sm">Source ↗</a>
        <span style="font-size:11px;color:var(--muted);margin-left:4px;">formats:</span>
        ${fmtChips}
      </div>
    </div>`;
  }).join('');
  updateSelCount();
}

// ── Batch selection ──────────────────────────────────────────────────────────
function ensureSel(i) {
  if (!S.selected[i]) S.selected[i] = { checked:false, formats:new Set([...S.bulkFormats]) };
  return S.selected[i];
}
function toggleStory(i, chk) {
  const sel = ensureSel(i);
  sel.checked = chk.checked;
  document.getElementById('sc-'+i)?.classList.toggle('selected', chk.checked);
  // reflect this story's formats on its chips
  document.querySelectorAll(`[data-si="${i}"]`).forEach(b => {
    b.classList.toggle('active', sel.formats.has(b.dataset.fk));
  });
  updateSelCount();
}
function toggleStoryFmt(i, key, btn) {
  const sel = ensureSel(i);
  if (sel.formats.has(key)) sel.formats.delete(key); else sel.formats.add(key);
  btn.classList.toggle('active');
}
function toggleAll(chk) {
  document.querySelectorAll('.story-chk').forEach((c,i) => { c.checked = chk.checked; toggleStory(i,c); });
}
function updateSelCount() {
  const n = Object.values(S.selected).filter(s=>s.checked).length;
  document.getElementById('sel-count').textContent = `${n} selected`;
}

async function runBatch() {
  const items = [];
  const total = parseInt(document.getElementById('slide-count-sel')?.value || '4');
  Object.entries(S.selected).forEach(([i,sel]) => {
    if (!sel.checked) return;
    const s = S.stories[i];
    const formats = sel.formats.size ? [...sel.formats] : [...S.bulkFormats];
    items.push({ story:{title:s.title,summary:s.summary,url:s.url,published:s.published||''}, formats, total_slides: total });
  });
  if (!items.length) { toast('Select at least one story','err'); return; }

  const totalPosts = items.reduce((a,it)=>a+it.formats.length,0);
  const btn = document.getElementById('btn-batch');
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span> Generating ${totalPosts}…`;
  S.busy = 'batch'; S.cancelRequested = false;
  const cancelBtn = g('btn-cancel-batch'); if(cancelBtn) cancelBtn.style.display='inline-flex';
  const t0 = Date.now();
  const tid = setInterval(()=>{ document.getElementById('hdr-timer').textContent = ((Date.now()-t0)/1000).toFixed(1)+'s'; },200);
  try {
    const model = document.getElementById('model-sel').value;
    const source = document.getElementById('batch-src').value;
    const tone = document.getElementById('tone-sel')?.value || '';
    const data = await api('/api/batch/run','POST',{items,model,source,tone,brand:curBrand()});
    document.getElementById('hdr-timer').textContent = data.elapsed+'s';
    showBatchResults(data);
    const ok = data.results.filter(r=>r.ok).length;
    const note = data.cancelled ? ' (cancelled)' : '';
    toast(`Batch done${note}: ${ok}/${data.results.length} posts in ${data.elapsed}s`);
    notify(data.cancelled ? 'Batch cancelled' : 'Batch complete',
           `${ok}/${data.results.length} posts rendered in ${data.elapsed}s`);
  } catch(e) {
    toast(e.message,'err');
    notify('Batch failed', e.message);
  } finally {
    S.busy = null;
    clearInterval(tid);
    if(cancelBtn) cancelBtn.style.display='none';
    btn.disabled = false; btn.innerHTML = 'Generate All Selected';
  }
}

function showBatchResults(data) {
  S.batch = data.results || [];
  S.results = S.batch;   // shared store so ✨ Suggest / re-render work by index
  const list = document.getElementById('stories-list');
  list.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
      <h3 style="font-size:15px;color:#fff;">Batch results</h3>
      <span class="badge badge-ok">${data.results.filter(r=>r.ok).length} ok</span>
      ${data.results.some(r=>!r.ok)?`<span class="badge badge-err">${data.results.filter(r=>!r.ok).length} failed</span>`:''}
      <span style="font-size:11px;color:var(--muted);">${data.batch_dir}</span>
      <div style="flex:1"></div>
      <button class="btn btn-ghost btn-sm" onclick="renderStoriesList(S.stories)">← Back to stories</button>
    </div>
    ${data.results.map((r,ri) => r.ok ? `
      <div class="story-card">
        <div class="s-top">
          <span class="score-pill badge badge-info">${esc(S.formats[r.format]||r.format)}</span>
          <span class="story-title">${esc(r.title)}</span>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0;">
          ${r.files.map(f=>`<a href="/outputs/${r.rel}/${f}" target="_blank"><img src="/outputs/${r.rel}/${f}" style="height:120px;border-radius:5px;border:1px solid var(--border);"></a>`).join('')}
        </div>
        <div class="story-reason" style="white-space:pre-wrap;">${esc(r.caption)}</div>
        <div class="story-actions" style="margin-top:8px;flex-wrap:wrap;">
          ${(r.format==='carousel'||r.format==='listicle')
            ? `<button class="btn btn-primary btn-sm" onclick="editBatchPlan(${ri})">✎ Edit in Editor</button>`
            : `<button class="btn btn-green btn-sm" onclick="suggestForResult(${ri})">✨ Suggest images</button>`}
          <a href="/outputs/${r.rel}/" target="_blank" class="btn btn-ghost btn-sm">Open folder ↗</a>
        </div>
      </div>` : `
      <div class="story-card" style="border-color:var(--red);">
        <div class="s-top"><span class="score-pill badge badge-err">${esc(r.format)} failed</span><span class="story-title">${esc(r.title)}</span></div>
        <div class="story-reason" style="color:var(--red);">${esc(r.error||'')}</div>
        <div class="story-actions" style="margin-top:8px;">
          <button class="btn btn-ghost btn-sm" onclick="retryBatchItem(${ri})">↻ Retry this one</button>
        </div>
      </div>`).join('')}
  `;
}

// Load a finished batch result's plan into the Editor for tweaking + re-render.
function editBatchPlan(ri) {
  const r = S.batch[ri];
  if (!r || !r.plan) { toast('No editable plan for this result', 'err'); return; }
  loadPlan(r.plan);
  S.imagePaths = {};
  api('/api/plan', 'PUT', r.plan).catch(()=>{});   // sync server session for preview/render
  showTab('editor', document.querySelectorAll('.nav-btn')[2]);
  toast('Loaded into editor — tweak then Render');
}

// Re-run a single failed batch item (most failures are flaky model JSON).
async function retryBatchItem(ri) {
  const r = S.batch[ri];
  if (!r || !r.story) { toast('Cannot retry this item', 'err'); return; }
  if (S.busy) { toast(`Wait — '${S.busy}' is still running`, 'err'); return; }
  S.busy = 'batch';
  toast(`Retrying ${r.format}…`);
  try {
    const model  = document.getElementById('model-sel').value;
    const source = document.getElementById('batch-src').value;
    const total  = parseInt(document.getElementById('slide-count-sel')?.value || '4');
    const tone   = document.getElementById('tone-sel')?.value || '';
    const data = await api('/api/batch/run','POST',{
      items:[{story:r.story, formats:[r.format], total_slides:total, tone}], model, source, brand:curBrand(),
    });
    const nr = (data.results||[])[0];
    if (nr) { S.batch[ri] = nr; showBatchResults({results:S.batch, batch_dir:data.batch_dir}); }
    toast(nr && nr.ok ? `${r.format} succeeded` : `${r.format} failed again`, nr && nr.ok ? 'ok':'err');
  } catch(e) { toast(e.message,'err'); }
  finally { S.busy = null; }
}

async function useStor(i) {
  const s = S.stories[i];
  document.querySelectorAll('.story-card').forEach(c=>c.classList.remove('selected'));
  document.getElementById('sc-'+i)?.classList.add('selected');
  const model  = document.getElementById('model-sel').value;
  const total  = parseInt(document.getElementById('slide-count-sel').value);
  const tone   = document.getElementById('tone-sel')?.value || '';
  toast('Generating plan…');
  S.busy = 'generate plan';
  const t0 = Date.now();
  const tid = setInterval(() => {
    document.getElementById('hdr-timer').textContent = ((Date.now()-t0)/1000).toFixed(1)+'s';
  }, 200);
  try {
    const data = await api('/api/plan/generate', 'POST', {
      story: {title:s.title,summary:s.summary,url:s.url,published:s.published||''},
      total_slides: total,
      model,
      tone,
      brand: curBrand(),
    });
    document.getElementById('hdr-timer').textContent = data.elapsed+'s';
    loadPlan(data.plan);
    showTab('editor', document.querySelectorAll('.nav-btn')[2]);
    toast(`Plan ready (${data.elapsed}s)`);
    notify('Plan ready', `${(data.plan.title_card&&data.plan.title_card.headline)||s.title.slice(0,60)} · ${data.elapsed}s`);
  } catch(e) {
    toast(e.message,'err');
  } finally {
    S.busy = null;
    clearInterval(tid);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Editor / Plan form
// ═══════════════════════════════════════════════════════════════════════════
function loadPlan(plan) {
  S.plan = plan;
  S.imagePaths = {};
  S.slideIdx = 0;
  S.totalSlides = 1 + (plan.content_slides||[]).length + 1;
  renderForm(plan);
  updateCnt();
  document.getElementById('preview-wrap').innerHTML = '<div class="preview-placeholder">Click Preview to render slide</div>';
}

function renderForm(plan) {
  const slides = plan.content_slides || [];
  document.getElementById('plan-form').innerHTML = `
    <div class="slide-sec">
      <div class="slide-sec-title">Title Card</div>
      <div class="field-group"><label>Headline</label><input id="f-hl" value="${esc(plan.title_card?.headline||'')}" oninput="syncPlan()"></div>
      <div class="field-group"><label>Subhead</label><input id="f-sh" value="${esc(plan.title_card?.subhead||'')}" oninput="syncPlan()"></div>
    </div>
    ${slides.map((sl,i)=>`
    <div class="slide-sec">
      <div class="slide-sec-title">Content ${i+1}</div>
      <div class="field-group"><label>Heading (ALL CAPS)</label><input id="f-sh-${i}" value="${esc(sl.heading||'')}" oninput="syncPlan()"></div>
      <div class="field-group"><label>Body</label><textarea id="f-bd-${i}" rows="4" oninput="syncPlan()">${esc(sl.body||'')}</textarea></div>
      <div class="field-group"><label>Image Query</label>
        <div class="img-row">
          <div class="img-thumb empty" id="thumb-${i}">🖼</div>
          <input class="url-inp" id="f-iq-${i}" value="${esc(sl.image_query||'')}" placeholder="search images…" style="flex:1">
          <button class="btn btn-ghost btn-sm" onclick="searchImagesFor(${i})" title="Search and pick from a grid">🔍 Search</button>
          <button class="btn btn-ghost btn-sm" onclick="swapImage(${i})" title="Auto-use the top result">Swap</button>
        </div>
      </div>
      <div class="field-group"><label>Image from URL</label>
        <div class="url-row">
          <input class="url-inp" id="f-url-${i}" placeholder="https://…">
          <button class="btn btn-ghost btn-sm" onclick="imgFromUrl(${i})">Use</button>
        </div>
      </div>
    </div>`).join('')}
    <div class="slide-sec">
      <div class="slide-sec-title">Outro / CTA</div>
      <div class="field-group"><label>CTA Text</label><input id="f-cta" value="${esc(plan.outro_card?.cta||'')}" oninput="syncPlan()"></div>
      <div class="field-group"><label>Handle</label><input id="f-hdl" value="${esc(plan.outro_card?.handle||'')}" oninput="syncPlan()"></div>
    </div>

    <div class="slide-sec">
      <div class="slide-sec-title">Layout & Brand Furniture</div>
      <div class="field-group"><label>Bottom "brand badge" position</label>
        <select id="f-badge" onchange="syncLayout()">
          <option value="center">Center</option>
          <option value="left">Bottom-left</option>
          <option value="right">Bottom-right</option>
        </select>
      </div>
      <div class="field-group"><label>Background logo position</label>
        <select id="f-wmpos" onchange="syncLayout()">
          <option value="center">Center</option>
          <option value="top">Top</option>
          <option value="bottom">Bottom</option>
          <option value="left">Left</option>
          <option value="right">Right</option>
          <option value="top-left">Top-left</option>
          <option value="top-right">Top-right</option>
          <option value="bottom-left">Bottom-left</option>
          <option value="bottom-right">Bottom-right</option>
        </select>
      </div>
      <div class="field-group"><label>Background logo visibility: <span id="wmop-val">5</span>%</label>
        <input type="range" id="f-wmop" min="0" max="20" value="5" oninput="document.getElementById('wmop-val').textContent=this.value;syncLayout()">
      </div>
    </div>

    <div class="slide-sec">
      <div class="slide-sec-title">Caption & Hashtags</div>
      <div class="field-group"><label>Caption</label><textarea id="f-cap" rows="3" oninput="syncPlan()">${esc(plan.caption||'')}</textarea></div>
      <div class="field-group"><label>Hashtags (comma-separated)</label><input id="f-tags" value="${esc((plan.hashtags||[]).join(', '))}" oninput="syncPlan()"></div>
      <div class="field-group"><label>DM Keyword</label><input id="f-dm" value="${esc(plan.dm_keyword||'')}" oninput="syncPlan()"></div>
      <div class="field-group"><label>Tone / stance (optional)</label>
        <input id="f-tone" list="tone-presets" value="${esc(plan.tone||'')}" placeholder="e.g. positive, negative, hyped, skeptical…" oninput="syncPlan()">
        <datalist id="tone-presets">
          <option value="positive, upbeat"></option>
          <option value="negative, critical"></option>
          <option value="neutral, factual"></option>
          <option value="hyped, exciting"></option>
          <option value="analytical, measured"></option>
          <option value="skeptical, cautionary"></option>
        </datalist>
        <div style="font-size:11px;color:var(--muted);margin-top:3px;">Saved with the post. Applies on the next ✨ caption regen; for new copy, re-generate from Stories with this tone.</div>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="regenCaption()" id="btn-regen-cap" style="align-self:flex-start;">✨ Generate caption + hashtags</button>
    </div>
  `;
  // reflect saved layout into the controls
  const L = plan.layout || {};
  if (g('f-badge')) g('f-badge').value = L.badge_align || 'center';
  if (g('f-wmpos')) g('f-wmpos').value = L.wm_pos || 'center';
  if (g('f-wmop'))  { g('f-wmop').value = (L.wm_opacity ?? 5); g('wmop-val').textContent = (L.wm_opacity ?? 5); }
}

function syncLayout() {
  if (!S.plan) return;
  S.plan.layout = {
    badge_align: g('f-badge')?.value || 'center',
    wm_pos:      g('f-wmpos')?.value || 'center',
    wm_opacity:  parseInt(g('f-wmop')?.value ?? '5'),
  };
  api('/api/plan','PUT',S.plan).catch(()=>{});
  previewCurrent();   // live-reflect the layout change
}

async function regenCaption() {
  if (!S.plan) { toast('Load a plan first','err'); return; }
  syncPlan();
  const btn = g('btn-regen-cap');
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Generating…';
  try {
    const data = await api('/api/plan/caption','POST',{tone:g('f-tone')?.value||'',brand:curBrand()});
    S.plan.caption = data.caption; S.plan.hashtags = data.hashtags;
    if (g('f-cap'))  g('f-cap').value  = data.caption || '';
    if (g('f-tags')) g('f-tags').value = (data.hashtags||[]).join(', ');
    api('/api/plan','PUT',S.plan).catch(()=>{});
    toast('Caption + hashtags regenerated');
    notify('Caption ready', 'New caption + hashtags generated');
  } catch(e){ toast(e.message,'err'); }
  finally { btn.disabled=false; btn.innerHTML='✨ Generate caption + hashtags'; }
}

function syncPlan() {
  if (!S.plan) return;
  const slides = S.plan.content_slides || [];
  S.plan.title_card = {
    headline: g('f-hl')?.value||'',
    subhead:  g('f-sh')?.value||'',
  };
  slides.forEach((_,i) => {
    slides[i] = {
      ...slides[i],
      heading:     g(`f-sh-${i}`)?.value||'',
      body:        g(`f-bd-${i}`)?.value||'',
      image_query: g(`f-iq-${i}`)?.value||'',
    };
  });
  S.plan.outro_card = { cta: g('f-cta')?.value||'', handle: g('f-hdl')?.value||'' };
  S.plan.caption    = g('f-cap')?.value||'';
  S.plan.hashtags   = (g('f-tags')?.value||'').split(',').map(h=>h.trim().replace(/^#/,'')).filter(Boolean);
  S.plan.dm_keyword = g('f-dm')?.value||'';
  if (g('f-tone')) S.plan.tone = g('f-tone').value||'';
  api('/api/plan','PUT',S.plan).catch(()=>{});
}

async function loadDummy() {
  try {
    const data = await api('/api/plan/dummy', 'POST');
    loadPlan(data.plan);
    toast('Preview plan loaded');
  } catch(e) { toast(e.message, 'err'); }
}

function updateCnt() {
  document.getElementById('slide-cnt').textContent =
    S.totalSlides ? `${S.slideIdx+1}/${S.totalSlides}` : '—/—';
}
function prevSlide() { if(S.slideIdx>0){S.slideIdx--;updateCnt();previewCurrent();} }
function nextSlide() { if(S.slideIdx<S.totalSlides-1){S.slideIdx++;updateCnt();previewCurrent();} }

async function previewCurrent() {
  if (!S.plan) { toast('Load a plan first','err'); return; }
  syncPlan();
  const btn = g('btn-prev-slide');
  btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
  const wrap = g('preview-wrap');
  wrap.innerHTML='<div class="preview-placeholder"><span class="spin"></span> Rendering…</div>';
  try {
    const blob = await fetch(`/api/preview/${S.slideIdx}?brand=${encodeURIComponent(curBrand())}`).then(r=>{if(!r.ok)throw new Error('render failed');return r.blob();});
    const url  = URL.createObjectURL(blob);
    wrap.innerHTML = `<img src="${url}" alt="slide ${S.slideIdx}">`;
  } catch(e) {
    wrap.innerHTML=`<div class="preview-placeholder" style="color:var(--red);">${e.message}</div>`;
    toast(e.message,'err');
  } finally {
    btn.disabled=false; btn.innerHTML='Preview';
  }
}

async function fetchImages() {
  syncPlan();
  const source = g('img-source').value;
  const btn = g('btn-fetch-img');
  btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
  const t0 = Date.now();
  try {
    const data = await api('/api/images/fetch','POST',{source});
    S.imagePaths = data.image_paths;
    Object.entries(data.image_paths).forEach(([i,p])=>{
      if(p) setThumb(parseInt(i),'/image_cache/'+p.split(/[/\\]/).pop());
    });
    toast(`Images fetched (${data.elapsed}s)`);
    notify('Images fetched', `Background images ready in ${data.elapsed}s`);
  } catch(e) { toast(e.message,'err'); }
  finally { btn.disabled=false; btn.innerHTML='Fetch Images'; }
}

async function clearImageCache() {
  if (!confirm('Delete ALL cached background images? (Plans are kept; you can re-fetch images.)')) return;
  try {
    const d = await api('/api/images/cache/clear','POST');
    S.imagePaths = {};
    // blank out any thumbnails still shown in the form
    document.querySelectorAll('.img-thumb').forEach(el=>{
      const id = el.id; if(id){ el.outerHTML = `<div class="img-thumb empty" id="${id}">🖼</div>`; }
    });
    toast(`Cleared ${d.removed} cached images (${d.freed_mb} MB freed)`);
  } catch(e){ toast(e.message,'err'); }
}

async function swapImage(idx) {
  const q = g(`f-iq-${idx}`)?.value||'';
  if(!q){toast('Enter a search query','err');return;}
  const source = g('img-source').value;
  try {
    const data = await api(`/api/images/swap/${idx}`,'POST',{query:q,source});
    S.imagePaths[idx] = data.path;
    setThumb(idx,'/image_cache/'+data.filename);
    toast(`Slide ${idx+1} image updated`);
    showSlidePreview(idx+1);
  } catch(e){toast(e.message,'err');}
}

async function imgFromUrl(idx) {
  const url = g(`f-url-${idx}`)?.value||'';
  if(!url){toast('Enter an image URL','err');return;}
  try {
    const data = await api('/api/images/from-url','POST',{url,slide_idx:idx,name:`slide${idx+1}`});
    S.imagePaths[idx] = data.path;
    setThumb(idx,'/image_cache/'+data.filename);
    toast(`Slide ${idx+1} image set from URL`);
    showSlidePreview(idx+1);
  } catch(e){toast(e.message,'err');}
}

// Search a source (Pexels/Unsplash/Google) and let the user pick from a grid.
async function searchImagesFor(idx) {
  const q = g(`f-iq-${idx}`)?.value || '';
  if (!q) { toast('Enter a search query','err'); return; }
  const source = g('img-source')?.value || 'pexels';
  openModal(`Pick an image · ${source} · "${q}"`);
  const body = g('modal-body');
  body.innerHTML = '<div style="color:var(--muted);font-size:12px;"><span class="spin"></span> Searching…</div>';
  try {
    const data = await api('/api/images/search','POST',{query:q, source, count:12});
    const results = data.results || [];
    if (!results.length) { body.innerHTML = '<div style="color:var(--muted);font-size:12px;">No results.</div>'; return; }
    body.innerHTML = `
      <div style="font-size:11px;color:var(--muted);margin-bottom:6px;">
        ${results.length} results${source==='google'?' · web images get a slight filter baked in':''}. Click one to use it for slide ${idx+1}.</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;">
        ${results.map((r,ri)=>`
          <div style="cursor:pointer;border:1px solid var(--border);border-radius:6px;overflow:hidden;background:#0d1828;" onclick='pickImage(${idx}, ${JSON.stringify(r.url)}, ${JSON.stringify(source)})' title="${esc(r.title||'')}">
            <img src="${esc(r.thumb||r.url)}" style="width:100%;height:120px;object-fit:cover;display:block;" loading="lazy" onerror="this.style.opacity=.3">
          </div>`).join('')}
      </div>`;
  } catch(e){ body.innerHTML = `<div style="color:var(--red);font-size:12px;">${esc(e.message)}</div>`; }
}

async function pickImage(idx, url, source) {
  const body = g('modal-body');
  if (body) body.innerHTML = '<div style="color:var(--muted);font-size:12px;"><span class="spin"></span> Downloading…</div>';
  try {
    // Web (Google) picks get the bake-in filter; curated stock stays as-is.
    const data = await api('/api/images/from-url','POST',{
      url, slide_idx:idx, name:`slide${idx+1}`, filter: source==='google',
    });
    S.imagePaths[idx] = data.path;
    setThumb(idx,'/image_cache/'+data.filename);
    closeModal();
    toast(`Slide ${idx+1} image set`);
    showSlidePreview(idx+1);
  } catch(e){ toast(e.message,'err'); closeModal(); }
}

// Jump the preview pane to a content slide and re-render it so image changes show
// immediately (content slide N lives at overall index N).
function showSlidePreview(slideIdx) {
  if (slideIdx < 0 || slideIdx >= S.totalSlides) return;
  S.slideIdx = slideIdx;
  updateCnt();
  previewCurrent();
}

function setThumb(idx,url) {
  const el = g(`thumb-${idx}`);
  if(el) el.outerHTML=`<img class="img-thumb" id="thumb-${idx}" src="${url}?t=${Date.now()}" alt="">`;
}

async function renderFull() {
  syncPlan();
  const btn=g('btn-render');
  btn.disabled=true; btn.innerHTML='<span class="spin"></span> Rendering…';
  const t0=Date.now();
  const tid=setInterval(()=>{document.getElementById('hdr-timer').textContent=((Date.now()-t0)/1000).toFixed(1)+'s';},200);
  try {
    const data = await api('/api/render','POST',{brand:curBrand()});
    clearInterval(tid);
    document.getElementById('hdr-timer').textContent=data.elapsed+'s';
    toast(`✓ Rendered ${data.files.length} slides in ${data.elapsed}s`);
    notify('Carousel rendered', `${data.files.length} slides saved in ${data.elapsed}s`);
    g('preview-wrap').innerHTML=`
      <div style="text-align:center;padding:16px;line-height:1.8;">
        <div style="font-size:16px;font-weight:700;color:var(--green);margin-bottom:6px;">Carousel saved!</div>
        <div style="font-size:12px;color:var(--muted);">${data.output_dir}</div>
        <div style="margin-top:12px;display:flex;flex-wrap:wrap;gap:6px;justify-content:center;">
          ${data.files.map(f=>`<a href="/outputs/${data.rel}/${f}" target="_blank" style="color:var(--teal);font-size:11px;">${f}</a>`).join('')}
        </div>
      </div>`;
  } catch(e){
    clearInterval(tid);
    toast(e.message,'err');
    notify('Render failed', e.message);
  } finally {
    btn.disabled=false; btn.innerHTML='Render Carousel';
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Canvas (Fabric.js visual editor)
// ═══════════════════════════════════════════════════════════════════════════
const CW=540, CH=675;   // display size (50% of 1080x1350)
let fc=null, overlayRect=null;

const RMONO = "'Roboto Mono', monospace";

function initCanvas() {
  window._fc = true;
  fc = new fabric.Canvas('design-canvas', {
    width: CW, height: CH,
    backgroundColor: '#0A0F1E',
    preserveObjectStacking: true,
  });
  document.getElementById('overlay-canvas').width  = CW;
  document.getElementById('overlay-canvas').height = CH;
  drawOverlay(65);
  fc.on('selection:created', selectionChanged);
  fc.on('selection:updated', selectionChanged);
  fc.on('selection:cleared',  ()=>clearProps());
  fc.on('object:modified',    selectionChanged);
  // Make sure Roboto Mono is loaded before first paint so canvas == editor.
  const draw = () => applyPreset('title');
  if (document.fonts && document.fonts.load) {
    Promise.all([
      document.fonts.load("700 40px 'Roboto Mono'"),
      document.fonts.load("400 20px 'Roboto Mono'"),
    ]).then(draw).catch(draw);
  } else { draw(); }
}

// Active brand info (with safe fallback) for canvas drawing.
function _bi() {
  return S.brandInfo || {
    name:'K2 Digital Media', short:'K2', handle:'@k2digitalmedia_',
    tagline:'The complete online presence for local business',
    pitch:'One agency. Four services.', services:'Web · Video · Marketing · IT',
    location:'London, Ontario', website:'k2digitalmedia.ca', category:'WEB DEVELOPMENT',
    logo:'/static/logo.png', accent:'#00B4C8', accent2:'#00C896', navy:'#0A0F1E', text:'#FFFFFF',
  };
}
function _mkText(text,o){ return new fabric.Text(text,Object.assign({fontFamily:RMONO,fill:'#fff',selectable:true},o)); }
function _mkIText(text,o){ return new fabric.IText(text,Object.assign({fontFamily:RMONO,fill:'#fff',selectable:true,editable:true},o)); }

function addWatermark() {
  const bi=_bi();
  fabric.Image.fromURL(bi.logo, img=>{
    img.scaleToWidth(380);
    img.set({left:CW/2, top:CH*0.55, originX:'center', originY:'center', opacity:0.06, selectable:false, evented:false});
    fc.add(img); fc.sendToBack(img); fc.renderAll();
  });
}
function addFrame() {
  fc.add(new fabric.Rect({left:15, top:15, width:CW-30, height:CH-30, fill:'',
    stroke:'rgba(255,255,255,0.12)', strokeWidth:1, rx:8, ry:8, selectable:false, evented:false}));
}
function addLockup(x,y) {
  const bi=_bi();
  fc.add(_mkText(bi.name, {left:x+38, top:y+1, fontSize:14, fontWeight:'700'}));
  fc.add(_mkText(bi.tagline, {left:x+38, top:y+19, fontSize:8, fontWeight:'400', fontStyle:'italic', fill:'rgba(255,255,255,0.42)'}));
  fabric.Image.fromURL(bi.logo, img=>{ img.scaleToWidth(30); img.set({left:x, top:y, selectable:true}); fc.add(img); fc.renderAll(); });
}
function addBadge(cy) {
  const bi=_bi();
  fabric.Image.fromURL(bi.logo, img=>{
    img.scaleToWidth(23); img.set({left:0, top:0, originY:'center'});
    const name=_mkText(bi.name, {left:30, top:0, originY:'center', fontSize:12.5, fontWeight:'700'});
    const grp=new fabric.Group([img,name], {originX:'center', left:CW/2, top:cy, selectable:true});
    fc.add(grp); fc.renderAll();
  });
}

function applyPreset(type) {
  if(!fc) return;
  fc.clear();
  const bi=_bi(), plan=S.plan;
  fc.backgroundColor = bi.navy;

  if(type==='title') {
    const P=42;
    addWatermark();
    addLockup(P,P);
    fc.add(_mkIText(plan?.title_card?.headline||'Your headline goes here', {left:P, top:248, fontSize:35, fontWeight:'700', fill:'#fff', width:CW-2*P}));
    fc.add(new fabric.Rect({left:P, top:360, width:29, height:2, fill:bi.accent, selectable:true, strokeWidth:0}));
    fc.add(_mkIText(plan?.title_card?.subhead||'Your subhead goes here.', {left:P, top:374, fontSize:13, fontWeight:'400', fill:'rgba(255,255,255,0.62)', width:CW-2*P}));
    fc.add(_mkText(bi.handle, {left:CW/2, top:CH-46, originX:'center', fontSize:9, fontWeight:'400', fill:'rgba(255,255,255,0.45)'}));
    addFrame();
  } else if(type==='content') {
    const P=38;
    addWatermark();
    const meta=[bi.handle].filter(Boolean).join(' · ');
    fc.add(_mkText(meta?('· '+meta):'', {left:CW/2, top:P, originX:'center', fontSize:9, fontWeight:'400', fill:'rgba(255,255,255,0.45)'}));
    fc.add(_mkText('01', {left:P, top:248, fontSize:10.5, fontWeight:'700', fill:bi.accent, charSpacing:200}));
    fc.add(_mkIText('SLIDE HEADING', {left:P, top:270, fontSize:31, fontWeight:'700', fill:'#fff', width:CW-2*P}));
    fc.add(_mkIText('— Key point one\n— Key point two', {left:P, top:330, fontSize:16, fontWeight:'400', fill:'rgba(255,255,255,0.88)', width:CW-2*P}));
    addBadge(CH-44);
    addFrame();
  } else if(type==='outro') {
    const P=38;
    const grad=new fabric.Gradient({type:'linear', coords:{x1:0,y1:0,x2:CW,y2:CH},
      colorStops:[{offset:0,color:bi.accent},{offset:0.62,color:bi.navy}]});
    fc.setBackgroundColor(grad, fc.renderAll.bind(fc));
    addWatermark();
    fc.add(_mkText('· '+(bi.website||bi.handle||''), {left:CW/2, top:P, originX:'center', fontSize:9, fontWeight:'400', fill:'rgba(255,255,255,0.55)'}));
    fc.add(_mkIText(plan?.outro_card?.cta||'Follow for updates', {left:P, top:250, fontSize:29, fontWeight:'700', fill:'#fff', width:CW-2*P}));
    fc.add(_mkIText(plan?.outro_card?.handle||bi.handle, {left:P, top:330, fontSize:17, fontWeight:'500', fill:'rgba(255,255,255,0.9)', width:CW-2*P}));
    addBadge(CH-44);
    addFrame();
  } else if(type==='cover') {
    addWatermark();
    fc.add(new fabric.Rect({left:42, top:46, width:9, height:9, fill:bi.accent, selectable:true}));
    fc.add(_mkText((bi.category||'').toUpperCase(), {left:58, top:42, fontSize:13, fontWeight:'700', charSpacing:120}));
    fc.add(_mkText(bi.handle, {left:CW-42, top:44, originX:'right', fontSize:11, fontWeight:'500', fill:'rgba(255,255,255,0.6)'}));
    fc.add(_mkText(bi.short, {left:CW/2, top:CH*0.40, originX:'center', originY:'center', fontSize:120, fontWeight:'700'}));
    if((bi.name||'').indexOf(' ')>=0)
      fc.add(_mkText(bi.name.split(' ').slice(1).join(' ').toUpperCase(), {left:CW/2, top:CH*0.40+78, originX:'center', fontSize:28, fontWeight:'400', charSpacing:300, fill:'rgba(255,255,255,0.82)'}));
    fc.add(new fabric.Rect({left:CW/2-52, top:CH*0.40+118, width:46, height:2, fill:bi.accent, selectable:true}));
    fc.add(new fabric.Rect({left:CW/2+6,  top:CH*0.40+118, width:46, height:2, fill:bi.accent2||bi.accent, selectable:true}));
    fc.add(_mkText(bi.pitch||'', {left:CW/2, top:CH*0.40+138, originX:'center', fontSize:15, fontWeight:'500', fontStyle:'italic', fill:'rgba(255,255,255,0.85)'}));
    fc.add(_mkText(bi.services||'', {left:CW/2, top:CH*0.40+162, originX:'center', fontSize:12, fontWeight:'400', fill:'rgba(255,255,255,0.5)'}));
    const loc=[bi.location,bi.website].filter(Boolean).join(' · ');
    fc.add(_mkText(loc, {left:CW/2, top:CH-46, originX:'center', fontSize:10, fontWeight:'400', fill:'rgba(255,255,255,0.45)'}));
    addFrame();
  }
  fc.renderAll();
}

function drawOverlay(pct) {
  const oc = document.getElementById('overlay-canvas');
  const ctx = oc.getContext('2d');
  ctx.clearRect(0,0,CW,CH);
  const grad = ctx.createLinearGradient(0,0,0,CH);
  const op = pct/100;
  grad.addColorStop(0, `rgba(10,15,30,${(op*0.62).toFixed(2)})`);
  grad.addColorStop(1, `rgba(10,15,30,${Math.min(0.97,op).toFixed(2)})`);
  ctx.fillStyle = grad;
  ctx.fillRect(0,0,CW,CH);
}

function updateOverlay(v) {
  document.getElementById('overlay-val').textContent = v+'%';
  drawOverlay(parseInt(v));
}

// Mirror the slide currently open in the Editor (real text + background image)
function loadSlideToCanvas() {
  if(!fc) return;
  if(!S.plan){ toast('Generate/edit a plan first','err'); return; }
  syncPlan();
  const idx = S.slideIdx, n = (S.plan.content_slides||[]).length;
  const type = idx===0 ? 'title' : (idx<=n ? 'content' : 'outro');
  applyPreset(type);   // lays out the brand furniture
  // now overwrite the editable text objects with the real slide content
  const tc = S.plan.title_card||{}, oc = S.plan.outro_card||{};
  const texts = fc.getObjects().filter(o=>o.type==='i-text');
  if(type==='title'){
    if(texts[0]) texts[0].set('text', tc.headline||'');
    if(texts[1]) texts[1].set('text', tc.subhead||'');
  } else if(type==='content'){
    const sl = S.plan.content_slides[idx-1]||{};
    // preset content objects: [heading(itext), body(itext)] ; slide-num is static
    const statics = fc.getObjects().filter(o=>o.type==='text');
    // update the "01" number static
    const num = statics.find(o=>/^[0-9]{1,2}$/.test(o.text));
    if(num) num.set('text', String(idx).padStart(2,'0'));
    if(texts[0]) texts[0].set('text', (sl.heading||'').toUpperCase());
    if(texts[1]) texts[1].set('text', sl.body||'');
    // background image for this content slide
    const p = S.imagePaths[idx-1] || S.imagePaths[String(idx-1)];
    if(p) _setBgImage('/image_cache/'+p.split(/[/\\]/).pop());
  } else {
    if(texts[0]) texts[0].set('text', oc.cta||'');
    if(texts[1]) texts[1].set('text', oc.handle||'');
  }
  fc.renderAll();
  toast(`Loaded ${type} slide ${idx+1} into canvas`);
}

function addLogoImg(x,y,size) {
  fabric.Image.fromURL(_bi().logo, img=>{
    img.scaleToWidth(size);
    img.set({left:x,top:y,selectable:true});
    fc.add(img);fc.renderAll();
  });
}

function addStaticText(text,x,y,size,weight,color) {
  fc.add(new fabric.Text(text,{
    left:x,top:y,fontSize:size,fontFamily:RMONO,
    fill:color,fontWeight:weight,selectable:true
  }));
}

function addEditableText(text,x,y,size,weight,color,maxW,upperCase=false) {
  const t=new fabric.IText(text,{
    left:x,top:y,fontSize:size,fontFamily:RMONO,
    fill:color,fontWeight:weight,width:maxW||CW-80,
    selectable:true,editable:true,
  });
  fc.add(t);
  return t;
}

function addText(text,size,weight) {
  if(!fc) return;
  const t=new fabric.IText(text,{
    left:60,top:100,fontSize:size/2,fontFamily:RMONO,
    fill:'#ffffff',fontWeight:weight,selectable:true,editable:true,
  });
  fc.add(t);fc.setActiveObject(t);fc.renderAll();
}

function addRect() {
  if(!fc) return;
  const r=new fabric.Rect({left:30,top:200,width:400,height:120,fill:'rgba(10,15,30,0.82)',strokeWidth:0,selectable:true});
  fc.add(r);fc.setActiveObject(r);fc.renderAll();
}

function addRule() {
  if(!fc) return;
  const r=new fabric.Rect({left:38,top:300,width:58,height:3,fill:'#00B4C8',strokeWidth:0,selectable:true});
  fc.add(r);fc.setActiveObject(r);fc.renderAll();
}

function addLogo() { if(fc) addLogoImg(30,CH-60,50); }

function clearCanvas() { if(fc){fc.clear();fc.backgroundColor='#0A0F1E';fc.renderAll();} }

// Background setters
function setBgNavy()  { if(fc){fc.backgroundColor='#0A0F1E';fc.renderAll();} }
function setBgTeal()  {
  if(!fc) return;
  const grad=new fabric.Gradient({type:'linear',coords:{x1:0,y1:0,x2:CW,y2:0},colorStops:[{offset:0,color:'#00B4C8'},{offset:1,color:'#0A0F1E'}]});
  fc.setBackgroundColor(grad,fc.renderAll.bind(fc));
}
function setBgFile(inp) {
  if(!inp.files[0]||!fc) return;
  const url=URL.createObjectURL(inp.files[0]);
  _setBgImage(url);
}
async function setBgUrl() {
  const url=g('canvas-img-url')?.value||'';
  if(!url) return;
  try {
    const data=await api('/api/images/from-url','POST',{url,name:'canvas-bg'});
    _setBgImage('/image_cache/'+data.filename);
    toast('Background set');
  } catch(e){toast(e.message,'err');}
}
async function setBgPexels() {
  const q=g('canvas-pexels-q')?.value||'';
  if(!q) return;
  const src=g('img-source')?.value||'pexels';
  try {
    const data=await api(`/api/images/swap/999`,'POST',{query:q,source:src});
    _setBgImage('/image_cache/'+data.filename);
    toast('Background set');
  } catch(e){toast(e.message,'err');}
}
function _setBgImage(url) {
  if(!fc) return;
  fabric.Image.fromURL(url,img=>{
    img.scaleToWidth(CW);
    img.scaleToHeight(CH);
    fc.setBackgroundImage(img,fc.renderAll.bind(fc));
  },{crossOrigin:'anonymous'});
}

// Export
async function exportCanvas() {
  if(!fc){toast('Open the Canvas tab first','err');return;}
  const btn=g('btn-export-canvas');
  btn.disabled=true;btn.innerHTML='<span class="spin"></span> Exporting…';
  // Apply overlay to the canvas before export
  const overlayOpacity=parseInt(g('overlay-opacity')?.value||'65')/100;
  const oc=new fabric.Rect({left:0,top:0,width:CW,height:CH,fill:`rgba(10,15,30,${(overlayOpacity*0.62).toFixed(2)})`,selectable:false,evented:false,opacity:1});
  fc.add(oc); fc.sendToBack(oc); fc.renderAll();

  const dataUrl=fc.toDataURL({format:'png',multiplier:2,quality:1});
  fc.remove(oc);fc.renderAll();

  const link=document.createElement('a');
  link.href=dataUrl;
  link.download=`k2-slide-${Date.now()}.png`;
  link.click();
  btn.disabled=false;btn.innerHTML='Export PNG (1080×1350)';
  toast('PNG exported!');
}


// Property panel
function selectionChanged() {
  const obj=fc.getActiveObject();
  if(!obj) return;
  const isText=(obj.type==='i-text'||obj.type==='text');
  g('prop-text').value = isText?(obj.text||''):'';
  g('prop-size').value = isText?(obj.fontSize||46):46;
  g('prop-x').value=Math.round(obj.left);
  g('prop-y').value=Math.round(obj.top);
  g('prop-w').value=Math.round(obj.width*(obj.scaleX||1));
  g('prop-h').value=Math.round(obj.height*(obj.scaleY||1));
  const bold=isText&&obj.fontWeight==='bold';
  g('prop-bold').style.background=bold?'var(--teal)':'';
  g('prop-bold').style.color=bold?'#000':'';
}
function clearProps() { g('prop-text').value='';g('prop-size').value='46'; }
function applyProp() {
  const obj=fc.getActiveObject();
  if(!obj||(obj.type!=='i-text'&&obj.type!=='text')) return;
  obj.set({text:g('prop-text').value,fontSize:parseInt(g('prop-size').value)||46});
  fc.renderAll();
}
function applyPos() {
  const obj=fc.getActiveObject();
  if(!obj) return;
  const x=parseFloat(g('prop-x').value),y=parseFloat(g('prop-y').value);
  if(!isNaN(x)&&!isNaN(y)){obj.set({left:x,top:y});fc.renderAll();}
}
function applySize() {
  const obj=fc.getActiveObject();
  if(!obj) return;
  const w=parseFloat(g('prop-w').value),h=parseFloat(g('prop-h').value);
  if(!isNaN(w)&&!isNaN(h)){obj.set({scaleX:w/obj.width,scaleY:h/obj.height});fc.renderAll();}
}
function toggleBold() {
  const obj=fc.getActiveObject();
  if(!obj) return;
  const bold=obj.fontWeight==='bold';
  obj.set('fontWeight',bold?'normal':'bold');fc.renderAll();
  g('prop-bold').style.background=bold?'':'var(--teal)';
  g('prop-bold').style.color=bold?'':'#000';
}
function toggleItalic() {
  const obj=fc.getActiveObject();
  if(!obj) return;
  obj.set('fontStyle',obj.fontStyle==='italic'?'normal':'italic');fc.renderAll();
}
function toggleUpper() {
  const obj=fc.getActiveObject();
  if(!obj||(obj.type!=='i-text'&&obj.type!=='text')) return;
  const txt=obj.text;
  obj.set('text',txt===txt.toUpperCase()?txt.toLowerCase():txt.toUpperCase());
  fc.renderAll();
}
function setColor(hex,el) {
  if(el){document.querySelectorAll('.color-swatch').forEach(s=>s.classList.remove('active'));el.classList.add('active');}
  const obj=fc.getActiveObject();
  if(!obj) return;
  obj.set('fill',hex);fc.renderAll();
}
function deleteSelected() { const o=fc.getActiveObject();if(o){fc.remove(o);fc.renderAll();} }
function bringFront() { const o=fc.getActiveObject();if(o){fc.bringToFront(o);fc.renderAll();} }
function sendBack()   { const o=fc.getActiveObject();if(o){fc.sendToBack(o);fc.renderAll();} }

// ═══════════════════════════════════════════════════════════════════════════
// Templates tab
// ═══════════════════════════════════════════════════════════════════════════
let _cm=null, _curFile=null;
function initCM() {
  const ta=g('code-editor');
  _cm=CodeMirror.fromTextArea(ta,{lineNumbers:true,indentUnit:2,tabSize:2,lineWrapping:false,mode:'htmlmixed'});
  _cm.getWrapperElement().style.cssText='background:#0d1828;color:#c9d1e0;height:100%;';
  window._cm=_cm;
}
async function loadTmplList() {
  const data=await api('/api/templates');
  g('tmpl-file-list').innerHTML=data.files.map(f=>`
    <button class="tmpl-file-btn ${f===_curFile?'active':''}" onclick="openTmpl('${f}')">${f}</button>
  `).join('');
}
async function openTmpl(fn) {
  _curFile=fn;
  const data=await api(`/api/template/${fn}`);
  g('tmpl-filename').textContent=fn;
  if(!_cm) initCM();
  _cm.setOption('mode',fn.endsWith('.css')?'css':'htmlmixed');
  _cm.setValue(data.content);
  _cm.refresh();
  loadTmplList();
}
async function saveTemplate() {
  if(!_curFile||!_cm){toast('Open a file first','err');return;}
  const st=g('tmpl-status');
  st.innerHTML='<span class="spin"></span>';
  try {
    await api(`/api/template/${_curFile}`,'PUT',{content:_cm.getValue()});
    st.innerHTML='<span class="badge badge-ok">Saved</span>';
    toast(`${_curFile} saved`);
    setTimeout(()=>st.innerHTML='',2000);
  } catch(e){st.innerHTML=`<span class="badge badge-err">${e.message}</span>`;toast(e.message,'err');}
}
async function previewTemplate() {
  if(!_curFile){toast('Open a file first','err');return;}
  const btn=document.querySelector('#tab-templates .btn-primary');
  if(btn){btn.disabled=true;btn.innerHTML='<span class="spin"></span>';}
  const wrap=g('tmpl-preview-wrap');
  wrap.innerHTML='<div style="color:var(--muted);font-size:12px;"><span class="spin"></span> Rendering…</div>';
  try {
    const r=await fetch(`/api/template/preview/${_curFile}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    if(!r.ok) throw new Error(await r.text());
    const url=URL.createObjectURL(await r.blob());
    wrap.innerHTML=`<img src="${url}" style="max-width:100%;max-height:100%;border-radius:4px;">`;
  } catch(e){wrap.innerHTML=`<div style="color:var(--red);font-size:12px;padding:8px;">${e.message}</div>`;toast(e.message,'err');}
  finally{if(btn){btn.disabled=false;btn.innerHTML='Preview';}}
}

// ═══════════════════════════════════════════════════════════════════════════
// Library — save / load generated content
// ═══════════════════════════════════════════════════════════════════════════
function closeModal(){ g('modal-bg').style.display='none'; }
function openModal(title){ g('modal-title').textContent=title; g('modal-bg').style.display='flex'; }

// ═══════════════════════════════════════════════════════════════════════════
// Settings panel — edit the defaults that get applied on every load.
// ═══════════════════════════════════════════════════════════════════════════
function openSettings() {
  toggleNav(false);
  openModal('⚙ Settings & defaults');
  const body = g('modal-body');
  const opt = (v, l, cur) => `<option value="${esc(v)}" ${String(v)===String(cur)?'selected':''}>${esc(l)}</option>`;
  const tones = [['','Auto'],['positive, upbeat','Positive'],['negative, critical','Negative'],
                 ['neutral, factual','Neutral'],['hyped, exciting','Hype'],
                 ['analytical, measured','Analytical'],['skeptical, cautionary','Skeptical']];
  const ms = g('model-sel');
  const models = ms ? [...ms.options].map(o=>o.value)
      .filter(v=>v && v!=='Loading…' && v!=='No models found') : [];
  const modelOpts = ['<option value="">(server default)</option>']
      .concat(models.map(m=>opt(m,m,SETTINGS.model))).join('');
  const row = (label, hint, control) => `
    <div class="set-row">
      <div class="set-label">${label}${hint?`<span class="set-hint">${hint}</span>`:''}</div>
      <div class="set-control">${control}</div>
    </div>`;
  const sel = (id, optsHtml) => `<select id="${id}" class="src-sel" style="width:100%;max-width:none;min-height:38px;">${optsHtml}</select>`;
  const num = (id, val, min, max) => `<input type="number" id="${id}" value="${val}" min="${min}" max="${max}" style="width:100%;background:#0d1828;border:1px solid var(--border);border-radius:6px;color:#fff;padding:7px 8px;font-size:14px;">`;
  body.innerHTML = `
    ${row(`<label style="display:flex;align-items:center;gap:8px;cursor:pointer;">
        <input type="checkbox" id="set-notify" ${SETTINGS.notify?'checked':''} style="width:16px;height:16px;">
        Desktop notifications</label>`, 'On by default — pings you when a run finishes', '')}
    ${row('Default model', 'Used for scoring & copy', sel('set-model', modelOpts))}
    ${row('Default tone', 'Stance applied to generated copy', sel('set-tone', tones.map(([v,l])=>opt(v,l,SETTINGS.tone)).join('')))}
    ${row('Default image source', '', sel('set-source', [['pexels','Pexels'],['unsplash','Unsplash'],['google','Google']].map(([v,l])=>opt(v,l,SETTINGS.imageSource)).join('')))}
    ${row('Default carousel slides', '', sel('set-slides', [3,4,5,6].map(n=>opt(n,n+' slides',SETTINGS.slides)).join('')))}
    ${row('Stories to fetch', '', num('set-fetch', SETTINGS.fetchLimit, 3, 60))}
    ${row('Top N to keep', '', num('set-top', SETTINGS.fetchTop, 1, 15))}
    <div style="display:flex;justify-content:space-between;gap:8px;margin-top:6px;border-top:1px solid var(--border);padding-top:12px;">
      <button class="btn btn-ghost btn-sm" onclick="resetSettings()">↺ Reset to defaults</button>
      <button class="btn btn-green" onclick="saveSettingsForm()">Save settings</button>
    </div>`;
}

async function saveSettingsForm() {
  const wantNotify = g('set-notify').checked;
  SETTINGS.model       = g('set-model').value;
  SETTINGS.tone        = g('set-tone').value;
  SETTINGS.imageSource = g('set-source').value;
  SETTINGS.slides      = parseInt(g('set-slides').value) || 4;
  SETTINGS.fetchLimit  = parseInt(g('set-fetch').value) || 15;
  SETTINGS.fetchTop    = parseInt(g('set-top').value) || 6;
  // Reconcile the notification toggle with the actual browser permission.
  if (wantNotify && !S.notify && ('Notification' in window)) {
    let p = Notification.permission;
    if (p !== 'granted') p = await Notification.requestPermission();
    S.notify = p === 'granted';
    if (p !== 'granted') toast('Allow notifications in your browser to enable them','err');
  } else if (!wantNotify) {
    S.notify = false;
  }
  SETTINGS.notify = wantNotify;
  saveSettings();
  applySettings();
  updateNotifBtn();
  closeModal();
  toast('Settings saved');
}

function resetSettings() {
  SETTINGS = {...SETTINGS_DEFAULTS};
  saveSettings();
  applySettings();
  updateNotifBtn();
  openSettings();   // re-render the form with defaults
  toast('Reset to defaults');
}

async function savePlan() {
  if (!S.plan) { toast('No plan to save','err'); return; }
  syncPlan();
  const name = prompt('Save plan as:', S.plan.title_card?.headline || S.plan.slug || 'plan');
  if (name === null) return;
  try {
    await api('/api/library/plan','POST',{plan:S.plan, name});
    toast('Plan saved to library');
  } catch(e){ toast(e.message,'err'); }
}

async function openLibrary() {
  openModal('Saved plans');
  const body = g('modal-body');
  body.innerHTML = '<div style="color:var(--muted);font-size:12px;">Loading…</div>';
  try {
    const data = await api('/api/library/plans');
    if (!data.plans.length){ body.innerHTML='<div style="color:var(--muted);font-size:12px;">No saved plans yet.</div>'; return; }
    body.innerHTML = `<div style="display:flex;justify-content:flex-end;margin-bottom:8px;">
        <button class="btn btn-danger btn-sm" onclick="clearLibrary('plans')">🗑 Clear all plans</button>
      </div>` + data.plans.map(p=>`
      <div class="story-card" style="cursor:default;">
        <div class="s-top">
          <span class="score-pill badge badge-info">${esc(p.brand||'')}</span>
          <span class="story-title">${esc(p.name||p.id)}</span>
        </div>
        <div class="story-reason">${esc(p.format||'carousel')} · ${esc((p.when||'').replace('T',' '))}</div>
        <div class="story-actions">
          <button class="btn btn-primary btn-sm" onclick="loadSavedPlan('${esc(p.id)}')">Load</button>
          <button class="btn btn-danger btn-sm" onclick="delSavedPlan('${esc(p.id)}',this)">Delete</button>
        </div>
      </div>`).join('');
  } catch(e){ body.innerHTML=`<div style="color:var(--red);font-size:12px;">${esc(e.message)}</div>`; }
}

async function loadSavedPlan(id) {
  try {
    const data = await api('/api/library/plan/'+id);
    loadPlan(data.plan);
    closeModal();
    showTab('editor', document.querySelectorAll('.nav-btn')[2]);
    toast('Plan loaded');
  } catch(e){ toast(e.message,'err'); }
}
async function delSavedPlan(id, btn) {
  await api('/api/library/plan/'+id,'DELETE').catch(()=>{});
  btn.closest('.story-card')?.remove();
}

async function clearLibrary(kind) {
  const label = kind==='plans' ? 'saved plans' : 'saved story sets';
  if (!confirm(`Delete ALL ${label}? This cannot be undone.`)) return;
  try {
    const r = await api('/api/library/clear','POST',{kind});
    toast(`Cleared ${r.removed} item(s)`);
    kind==='plans' ? openLibrary() : openStoryLibrary();
  } catch(e){ toast(e.message,'err'); }
}

async function saveStorySet() {
  if (!S.stories.length){ toast('Fetch stories first','err'); return; }
  try { await api('/api/library/stories','POST',{}); toast('Story set saved'); }
  catch(e){ toast(e.message,'err'); }
}

async function openStoryLibrary() {
  openModal('Saved story sets');
  const body = g('modal-body');
  body.innerHTML = '<div style="color:var(--muted);font-size:12px;">Loading…</div>';
  try {
    const data = await api('/api/library/stories');
    if (!data.stories.length){ body.innerHTML='<div style="color:var(--muted);font-size:12px;">No saved sets yet.</div>'; return; }
    body.innerHTML = `<div style="display:flex;justify-content:flex-end;margin-bottom:8px;">
        <button class="btn btn-danger btn-sm" onclick="clearLibrary('stories')">🗑 Clear all sets</button>
      </div>` + data.stories.map(s=>`
      <div class="story-card" style="cursor:default;">
        <div class="s-top">
          <span class="score-pill badge badge-info">${esc(s.brand||'')}</span>
          <span class="story-title">${esc(s.name||s.id)}</span>
        </div>
        <div class="story-reason">${esc(s.count||'?')} stories · ${esc((s.when||'').replace('T',' '))}</div>
        <div class="story-actions">
          <button class="btn btn-primary btn-sm" onclick="loadSavedStories('${esc(s.id)}')">Load</button>
          <button class="btn btn-danger btn-sm" onclick="delSavedStories('${esc(s.id)}',this)">Delete</button>
        </div>
      </div>`).join('');
  } catch(e){ body.innerHTML=`<div style="color:var(--red);font-size:12px;">${esc(e.message)}</div>`; }
}
async function loadSavedStories(id) {
  try {
    const data = await api('/api/library/stories/'+id);
    S.stories = data.stories || [];
    renderStoriesList(S.stories);
    closeModal();
    toast(`Loaded ${S.stories.length} stories (no re-fetch)`);
  } catch(e){ toast(e.message,'err'); }
}
async function delSavedStories(id, btn) {
  await api('/api/library/stories/'+id,'DELETE').catch(()=>{});
  btn.closest('.story-card')?.remove();
}

// ═══════════════════════════════════════════════════════════════════════════
// Utilities
// ═══════════════════════════════════════════════════════════════════════════
function g(id){return document.getElementById(id);}
// The brand dropdown is the single source of truth for which brand every action uses.
function curBrand(){ return document.getElementById('brand-sel')?.value || ''; }
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

async function api(url, method='GET', body=null) {
  const opts={method,headers:{}};
  if(body!==null){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body);}
  const r=await fetch(url,opts);
  const ct=r.headers.get('content-type')||'';
  if(!ct.includes('application/json'))return r;
  const data=await r.json();
  if(!r.ok) throw new Error(data.detail||JSON.stringify(data));
  return data;
}

function toast(msg,type='ok'){
  const el=g('toast');el.textContent=msg;el.className='show '+type;
  clearTimeout(el._t);el._t=setTimeout(()=>el.classList.remove('show'),3200);
}
</script>
</body>
</html>
"""
