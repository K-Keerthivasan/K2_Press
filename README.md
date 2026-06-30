# K2 Press — Instagram & Social Media Content Studio

A self-hosted, local-AI **content studio for Instagram and social media**: create
branded posts (manually from your own idea, or automatically from RSS/trending news
and YouTube trailers), edit every pixel, and **publish to Instagram and other
platforms through [Postiz](https://postiz.com)**. Multi-brand, multi-channel, and
fully config-driven — copy `config.example.yaml` to `config.yaml` and add your own
brands. Bring your own logo, colours, feeds, and channels.

## What it does

- **Create posts two ways** — ✍️ **Manual** (your idea + notes + images → AI builds the
  carousel and suggests angles) or 📡 **Auto** (fetch + AI-score RSS/trending stories).
- **Many formats** — carousel, square, story, X/Twitter, quote, comparison, breaking,
  listicle, LinkedIn — plus **9:16 Reels & video carousels** from YouTube trailers.
- **Full editor** — edit every line, fetch/upload/paste/URL images per slide, live preview,
  template editor, and a Fabric.js canvas for hand layout.
- **Publish anywhere Postiz supports** — Instagram (Business/Creator), and any other
  channel you connect in Postiz (Facebook, LinkedIn, X, TikTok, YouTube, Threads…).
  A review-and-approve gate sends each post to Postiz as a **draft** by default.
- **Local & private** — runs on your machine with a local LLM (Hermes or Ollama). Your
  brands, keys, and channels stay in gitignored config; only the generic template ships.

## Documentation

| Doc | What's in it |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Install, `.env`, host vs Docker, Hermes/Ollama, auto-start |
| [docs/PUBLISHING_AND_CHANNELS.md](docs/PUBLISHING_AND_CHANNELS.md) | **Connect Postiz + add multiple Instagram/social channels** (the publishing guide) |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | `config.yaml` reference — brands, themes, feeds, formats, Postiz |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the modules fit together (for contributors) |

## Tech stack

| Layer | Tool |
|---|---|
| Feed ingestion | feedparser |
| LLM | Hermes CLI by default, or any OpenAI-compatible local server |
| Images | Pexels + Unsplash APIs, or any image URL |
| Templating | Jinja2 |
| Rendering | Playwright / headless Chromium |
| Control panel | FastAPI + vanilla JS + Fabric.js (canvas) |
| Config | PyYAML |

---

## Quick start

Double-click **`run.bat`** — it activates the venv, checks the local model setup, launches the server, and
opens your browser at `http://localhost:8000`.

### First-time setup

On Windows, run **`setup.bat`** for the full local setup. It creates the virtual environment,
installs dependencies, installs Playwright Chromium, creates a placeholder `.env` if needed,
and can build the Docker image when Docker Compose is available.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
hermes status             # confirm Hermes is logged in and ready
```

Add API keys to `.env`:

```env
PEXELS_API_KEY=your_pexels_key        # free at pexels.com/api
UNSPLASH_API_KEY=your_unsplash_key    # optional, free at unsplash.com/developers
```

### Configure your brand(s)

Nothing in the app is hard-coded to a specific brand — all identity (name, handle,
logo, theme, feeds, Postiz channel) is config-driven. Copy the example config and
edit it:

```powershell
copy config.example.yaml config.yaml      # (the app also auto-copies it on first run)
```

Then open `config.yaml` and set your `app.name`, your brand block(s) under `brands:`
(name, handle, logo, theme colours, scoring `profile`/`personality`, and feed URLs),
and your Postiz `channels` (run `python postiz.py --list-channels` to get the ids).
Drop your logo at the `logo_path` you set (e.g. `static/logo.png`). `config.yaml` is
**gitignored**, so your real details and keys never get committed — only the generic
`config.example.yaml` template is tracked.

### Docker

```powershell
docker compose up --build
```

Open `http://localhost:8000`. The compose setup maps `outputs/`, `image_cache/`, and
`library/` back to the project folder. For Ollama-based Docker runs, point
`llm.base_url` at `http://host.docker.internal:11434/v1`.

### Docker + Tailscale

Add a Tailscale pre-auth key to a local `.env.tailscale` file:

```env
TS_AUTHKEY=<paste-your-tailscale-pre-auth-key-here>
```

Then run the app with the Tailscale sidecar:

```powershell
docker compose --profile tailscale up -d --build
```

The sidecar uses Tailscale Serve to proxy the app privately over HTTPS inside your
tailnet. After it starts, get the URL with:

```powershell
docker exec k2-posttool-tailscale tailscale serve status
```

On your phone or another device logged into the same tailnet, open the shown
`https://k2-posttool.<your-tailnet>.ts.net` URL. The local Docker URL still works at
`http://localhost:8000`.

---

## Using the app

1. **Stories** — pick a feed category (Web Dev / Marketing / Tech), set how many to fetch and
   keep, then **Fetch & Score**. Your selected local AI model ranks each story 0–100 (timed).
   - **Single:** click **Edit as Carousel** to open one story in the Editor.
   - **Bulk:** tick 5–10 stories, choose the **formats** each should produce (Carousel / Square
     / Story / X), then **Generate All Selected**. The AI writes a plan + caption for every
     post, fetches images, and renders the whole batch to `outputs/batch_<timestamp>/`. Results
     (thumbnails + captions) show inline.
2. **Editor** — review/edit every line of a carousel plan. Choose **3–10 slides**. Fetch images
   (Pexels or Unsplash), swap any slide's image by query or **image URL**, preview each slide,
   then **Render Carousel**.
3. **Canvas** — mirrors the HTML, doesn't replace it. **Load Editor Slide** pulls the real
   text + background of the slide you're editing into a Fabric.js canvas so you can nudge type
   and layout by hand, then **Export PNG**. The HTML render stays the source of truth.
4. **Templates** — edit any template (`title/content/outro/cover/square/story/xpost.html`) or
   `brand.css` with live preview. Saves are backed up as `.bak`. `cover.html` is the standalone
   brand-splash card (the big K2 wordmark); it reads brand copy from `config.yaml` (`brand:`).

## Formats

| Format | Size | Notes |
|---|---|---|
| Carousel | 1080×1350 | Multi-slide: title + 1–8 content + outro |
| Square Post | 1080×1080 | Single feed card |
| Story | 1080×1920 | Vertical, story-safe margins, tap CTA |
| X / Twitter | 1600×900 | Landscape card + AI-written tweet text |

You pick the model (top-right) and feed category per run; everything is timed.

---

## CLI (engine without the UI)

```powershell
python feeds.py  --limit 5                 # pull + print stories
python filter.py --top 5                   # score with the configured LLM
python plan.py   --total-slides 6          # generate a JSON plan
python images.py "city skyline" --source unsplash
python render.py --no-images               # render a carousel
python postiz.py --list-channels           # list Postiz integrations
python video_reels.py --rss "<feed>" --emit-manifest m.json   # YouTube → 9:16 manifest
```

---

## Publishing to Instagram (Postiz)

Finished assets are pushed to a self-hosted **Postiz** instance, which posts to
Instagram via the Meta Graph API. K2 renders nothing here — it hands Postiz the
finished carousel / single post / reel / story plus a caption. Default mode is
**draft**: you review the queue in the Postiz calendar, then publish.

**From the app:** in the **Review** tab, **Approve** pushes the post to Postiz as
a draft (falls back to the `N8N_WEBHOOK_URL` webhook if `POSTIZ_API_KEY` is unset,
so the queue still works standalone). When `PUBLIC_BASE_URL` is set, Postiz fetches
the bytes over HTTP (`/upload-from-url`); otherwise local files are uploaded.

**From the CLI** (`postiz.py`, the engine without the UI):

```powershell
python postiz.py --type carousel --asset s1.png --asset s2.png --caption cap.txt
python postiz.py --type post  --asset card.png --caption "Hello 👋"
python postiz.py --type reel  --asset reel.mp4 --caption cap.txt --dry-run
python postiz.py --type story --asset card.png --mode draft
```

`--mode` is `draft | schedule | now` (default from config). `now` is hard-guarded
behind `postiz.publish.allow_now`; `schedule` requires `--date` (ISO8601). `--dry-run`
builds and prints the payload without POSTing. The 30-requests/hour Postiz ceiling is
accounted for up front (a 5-slide carousel = 6 requests; a reel = 2).

Config lives in `config.yaml` under `postiz:` (instance URL, per-brand `channels`,
modes, rate limit, reel specs). The API key is read from `POSTIZ_API_KEY` in `.env`
(Postiz → Settings → Public API). The IG account must be Business/Creator. On the
**Instagram Standalone** integration a reel is sent as a `post` (a 9:16 video is
published as a Reel by Instagram itself) — the publisher handles this automatically.

---

## YouTube Reels & Video-Carousel (`video_reels` mode)

Turn a YouTube trailer into branded **9:16** content (hook → title → CTA cards) and
ship it as a **video carousel** or a single stitched **reel**, gated through Postiz
as a draft. The **manifest** (JSON) is the edit surface: generate it, hand-edit the
clips/copy/highlights, then render. Engine + CLI: `video_reels.py`. Rights-gated —
yt-dlp downloads only when a source is cleared.

```powershell
# 1. Generate a manifest (RSS poll → LLM copy → clip pick), then stop to edit:
python video_reels.py --rss "<channel_feed_url>" --mode carousel --emit-manifest m.json
#    ...edit m.json: swap clip.start, rewrite text, toggle highlight, set output_mode,
#    and set source.rights_cleared: true (you assert rights) ...
# 2. Render + push to Postiz as a draft:
python video_reels.py --from-manifest m.json --send
```

- `--mode carousel|reel`, `--clip-method even_intervals|scene_cut|manual`, `--cards 3`,
  `--video-id <id>` (instead of `--rss`), `--dry-run` (build manifest, no write/post).
- Clip methods always pick **different** moments per card. `scene_cut` uses ffmpeg
  scene detection and needs the source downloaded (so it requires clearance up front).
- Rights: a source is cleared if `source.rights_cleared: true` in the manifest, or its
  channel id / RSS url / video url is in `config.yaml` `video_reels.allowlist`.
- Templates: `templates/vr_hook.html` · `vr_title.html` · `vr_cta.html` (K2 navy/teal/
  green, logo + handle, keyword highlights) — rendered to transparent PNGs and
  composited over the clip by ffmpeg. `config.yaml` → `video_reels:` for clip length,
  scene threshold, and the allowlist.

---

## Brand

Defined once in `static/brand.css`:

- `--navy #0A0F1E` · `--teal #00B4C8` · `--green #00C896` · Calibri
- Logo: `static/logo.png` (circular K2 mark, used in corner + faded watermark)
- Handle: `@k2digitalmedia_` · Byline: Keerthivasan
- Background images are auto-muted + navy-tinted so copy always wins. Tune the
  `.bg-image` filter and `.slide::before` wash in `brand.css`.

Edit `content_profile.md` to change what scores high. Edit `config.yaml` for feeds, slide-count
rules, output size, and the LLM `base_url`. The default backend is Hermes:

```yaml
llm:
  base_url: "hermes://cli"
  model:    "hermes:gpt-5.5"
```

You can still override the model endpoint at runtime:

```powershell
$env:K2_LLM_BASE_URL="hermes://cli"               # Hermes CLI backend
$env:K2_LLM_MODEL="hermes:gpt-5.5"

# Or use LM Studio / llama.cpp / Ollama OpenAI-compatible endpoint:
$env:K2_LLM_BASE_URL="http://localhost:1234/v1"
$env:K2_LLM_MODEL="local-model-name"
$env:K2_LLM_API_KEY="not-needed-for-local"        # optional
```

**Switching engine at runtime:** the header has an **Engine** dropdown — flip between **Hermes**
and **Ollama** live (no restart). Post generation runs on whichever engine is selected, and the
Model dropdown refreshes to that engine's models. The selectable backends (and their host/model)
are defined in `config.yaml` under `llm.backends`:

```yaml
llm:
  backends:
    hermes:
      base_url: "hermes://cli"
      model:    "hermes:gpt-5.5"
    ollama:
      base_url: "http://localhost:11434/v1"
      model:    "qwen3:8b"
```

The Agent tab uses native tool calls when the selected backend supports them. Hermes CLI and plain
chat models use a JSON command protocol so the same fetch and generate tasks still work.

### Managing post data in MySQL (optional)

By default the generated-post / review queue is a JSON file (`library/review_queue.json`). To manage
post data in **MySQL** instead, set the `K2_MYSQL_*` vars in `.env` (at minimum `K2_MYSQL_DB`):

```
K2_MYSQL_HOST=localhost
K2_MYSQL_PORT=3306
K2_MYSQL_USER=root
K2_MYSQL_PASSWORD=...
K2_MYSQL_DB=k2_posts
```

The app auto-creates a `posts` table on first use, with real `DATETIME` columns for the **created**
and **approved** dates (so posts can be queried/sorted by date). If `K2_MYSQL_DB` is blank or MySQL
is unreachable, it transparently falls back to the JSON queue.

Brand personality lives in `config.yaml` under each brand's `personality` field. Post generation
now performs a final personality edit pass that rewrites copy fields while preserving facts,
formats, image queries, hashtags, handles, and output schema.

---

## Project structure

```
config.yaml          feeds (by category), slide rules, brand, LLM endpoint
content_profile.md   niche / tone / audience — drives scoring + planning
.env                 PEXELS_API_KEY, UNSPLASH_API_KEY (never committed)
run.bat              one-click launcher
static/
  brand.css          brand variables + slide base styles + image wash
  logo.png           K2 Digital Media logo
templates/           title.html · content.html · outro.html · cover.html
feeds.py             RSS/Atom ingestion (category-aware)
filter.py            local AI relevance scoring
plan.py              local AI post planning (strict JSON, 3–10 slides)
images.py            Pexels / Unsplash / URL fetching + cache
render.py            Jinja2 → HTML → Playwright → PNG
llm.py               thin OpenAI-compatible client (model list + switch)
postiz.py            Postiz publisher — engine + k2publish CLI (Instagram drafts)
app.py               FastAPI control panel, canvas, template editor
image_cache/         downloaded images
outputs/             rendered carousels
```
