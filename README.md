# K2 Digital Media — Carousel Generator

Turn RSS/news feeds into branded Instagram carousel posts — automatically planned,
image-fetched, and rendered to 1080×1350 PNGs using HTML/CSS templates and a headless browser.
Includes a live template editor and a drag-and-drop canvas design tool.

## Tech stack

| Layer | Tool |
|---|---|
| Feed ingestion | feedparser |
| LLM (local) | Ollama — `qwen3:8b` / `gemma` (switchable in the UI) |
| Images | Pexels + Unsplash APIs, or any image URL |
| Templating | Jinja2 |
| Rendering | Playwright / headless Chromium |
| Control panel | FastAPI + vanilla JS + Fabric.js (canvas) |
| Config | PyYAML |

---

## Quick start

Double-click **`run.bat`** — it activates the venv, checks Ollama, launches the server, and
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
ollama pull qwen3:8b      # or your preferred model
```

Add API keys to `.env`:

```env
PEXELS_API_KEY=your_pexels_key        # free at pexels.com/api
UNSPLASH_API_KEY=your_unsplash_key    # optional, free at unsplash.com/developers
```

### Docker

```powershell
docker compose up --build
```

Open `http://localhost:8000`. The compose setup maps `outputs/`, `image_cache/`, and
`library/` back to the project folder, and points containerized Ollama requests to
`http://host.docker.internal:11434/v1`.

### Docker + Tailscale

Add a Tailscale pre-auth key to a local `.env.tailscale` file:

```env
TS_AUTHKEY=tskey-auth-your-key
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
   keep, then **Fetch & Score**. Ollama ranks each story 0–100 (timed).
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
python filter.py --top 5                   # score with Ollama
python plan.py   --total-slides 6          # generate a JSON plan
python images.py "city skyline" --source unsplash
python render.py --no-images               # render a carousel
```

---

## Brand

Defined once in `static/brand.css`:

- `--navy #0A0F1E` · `--teal #00B4C8` · `--green #00C896` · Calibri
- Logo: `static/logo.png` (circular K2 mark, used in corner + faded watermark)
- Handle: `@k2digitalmedia_` · Byline: Keerthivasan
- Background images are auto-muted + navy-tinted so copy always wins. Tune the
  `.bg-image` filter and `.slide::before` wash in `brand.css`.

Edit `content_profile.md` to change what scores high. Edit `config.yaml` for feeds, slide-count
rules, output size, and the LLM `base_url` (point it at a cloud model to switch off local).

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
filter.py            Ollama relevance scoring
plan.py              Ollama post planning (strict JSON, 3–10 slides)
images.py            Pexels / Unsplash / URL fetching + cache
render.py            Jinja2 → HTML → Playwright → PNG
llm.py               thin OpenAI-compatible client (model list + switch)
app.py               FastAPI control panel, canvas, template editor
image_cache/         downloaded images
outputs/             rendered carousels
```
