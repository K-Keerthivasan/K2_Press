# K2 Digital Media — Carousel Generator

Turn RSS/news feeds into branded Instagram carousel posts — automatically planned,
image-fetched, and rendered to 1080×1350 PNGs using HTML/CSS templates and a headless browser.
Includes a live template editor and a drag-and-drop canvas design tool.

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
app.py               FastAPI control panel, canvas, template editor
image_cache/         downloaded images
outputs/             rendered carousels
```
