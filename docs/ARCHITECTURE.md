# Architecture

A map of the modules and how a post flows through them. Useful for contributors and for
extending the app.

## Modules

| File | Responsibility |
|---|---|
| `app.py` | FastAPI control panel: all API endpoints, the entire web UI (one HTML/JS string), session state, the review queue, and orchestration for manual/auto/bulk generation. |
| `feeds.py` | RSS/Atom ingestion (category-aware), `Story` dataclass. |
| `filter.py` | LLM relevance scoring (0–100) of stories against a brand's `profile`. |
| `plan.py` | LLM post planning. `plan_post` dispatches by format → `plan_story` (carousel), `plan_single`, `plan_special`, `plan_listicle`. `_ensure_caption` guarantees a caption; `regen_caption` rewrites just caption+hashtags. |
| `images.py` | Image fetching: Pexels / Unsplash / Google / direct URL, plus the Pillow "bake-in" filter for web images. |
| `render.py` | Jinja2 → Playwright (headless Chromium) → PNG. `generate_carousel`, single-card render, `render_slide_to_bytes` (live preview), `render_overlay_to_bytes` (transparent reel overlays). |
| `llm.py` | Thin OpenAI-compatible client + a Hermes CLI shim. Backend switch + model list. |
| `brands.py` | Brand resolution, active-brand state, per-brand template/feed selection. |
| `postiz.py` | Postiz publishing **engine + CLI**: `PostizClient`, asset validation, channel resolution, rate limiter, `publish()`. |
| `video.py` | `extract_clip`/`download_full` (yt-dlp) + ffmpeg compositing helpers (`composite`, color grade, ASS captions, probe). |
| `video_reels.py` | YouTube trailer → editable manifest → 3 branded 9:16 cards → carousel/reel → Postiz draft. |
| `scripts.py` | Platform-specific voiceover scripts. `trends.py` — trending topics. `db.py` — optional MySQL review-queue store. |
| `config.yaml` / `templates/` / `static/` | Config (gitignored), Jinja2 templates, logo/CSS. |

## Data flow

**Auto (RSS):**
```
feeds.fetch → filter.rank (LLM score) → plan.plan_post (LLM) → render.generate_carousel
   → review queue → (approve) → postiz.publish (draft)
```

**Manual:**
```
idea + notes + slide count  →  /api/plan/generate with a synthetic Story
   →  plan.plan_post  →  Editor  →  render  →  review  →  postiz
```
(`/api/manual/suggest` proposes alternative angles before committing.)

**Video Reels:**
```
RSS/video_id → video_reels.emit_manifest (LLM copy + clip pick) → [edit manifest]
   → download_full (max-res) → render_overlay_to_bytes (transparent PNG per card)
   → ffmpeg composite per card → carousel/reel → postiz (draft)
```

## Key state

- **Session** (`_session` in `app.py`): current `plan`, `image_paths` ({slide_idx: path}),
  fetched `stories`, and `used_urls`. In-memory, single uvicorn worker.
- **Persistent dedup** (`library/used_urls.json`): story URLs already turned into posts,
  so a restart doesn't re-serve them. Cleared by "Reset seen".
- **Review queue** (`library/review_queue.json` or MySQL): generated posts awaiting
  approval; approval calls `_sync_publish` → `_postiz_publish`.
- **Library** (`library/plans/<brand>/*.json`): saved plans, including their fetched
  `image_paths`, so a reloaded plan is render-ready.

## Extension points

| To add… | Do this |
|---|---|
| A new **brand** | Config only — add a block under `brands:` and a `postiz.channels` mapping. |
| A new **channel/platform** | Connect it in Postiz, then map `brand_key: "<integration id>"` in `postiz.channels`. |
| A new **post format** | Add to `formats:` in config + a Jinja2 template in `templates/`. |
| A new **LLM backend** | Add under `llm.backends`; `llm.py` speaks OpenAI-compatible + Hermes CLI. |
| **Multi-platform fan-out** (one brand → IG + LinkedIn + …) | Extend `_postiz_publish` in `app.py` to resolve and post to several integrations per brand. |
| A new **image source** | Add a fetcher in `images.py` and a `source` option. |

## Conventions

- **Brand identity is config-driven** — no brand strings hardcoded in code; defaults are
  neutral/empty. Keep it that way.
- **Rendering is HTML/CSS → Playwright**, not Pillow (except the legacy reel cards). New
  visual work should be a Jinja2 template.
- **Publishing is draft-by-default** behind the review gate. Respect `allow_now`.
- The UI is a single HTML/JS string in `app.py` served by `/`; edits there need a server
  restart (or `--reload`) to take effect.
