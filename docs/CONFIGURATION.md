# Configuration reference (`config.yaml`)

Everything brand- and behaviour-specific lives in `config.yaml`. Start from
`config.example.yaml` (a generic single-brand template). `config.yaml` is gitignored.

## Top level

```yaml
app:
  name: "My Studio"          # browser tab title
active_brand: demo           # which brand key is selected on boot
```

## `postiz:` — publishing

See [PUBLISHING_AND_CHANNELS.md](PUBLISHING_AND_CHANNELS.md) for the full guide.

```yaml
postiz:
  base_url: "http://localhost:4007/api/public/v1"   # = NEXT_PUBLIC_BACKEND_URL + /public/v1
  api_key_env: "POSTIZ_API_KEY"        # env var name holding the Public API key
  channels:                            # brand key -> Postiz integration id (or name)
    demo: "your-integration-id-or-name"
  publish:
    default_mode: "draft"              # draft | schedule | now
    allow_now: false                   # must be true to allow mode "now"
  rate_limit:
    max_requests_per_hour: 30
    safety_margin: 2                   # effective ceiling = max - margin
  reel:
    require_9x16: true
    min_seconds: 5
    max_seconds: 90
```

## `video_reels:` — YouTube → 9:16

```yaml
video_reels:
  default_clip_seconds: 4.0      # per-card clip length
  scene_threshold: 0.4           # ffmpeg scene-change sensitivity (scene_cut method)
  allowlist: []                  # cleared YouTube channel_ids / RSS urls / video urls
```

## `brands:` — one block per brand

The brand **key** (e.g. `demo`, `k2`, `jkr`) is what you reference in `postiz.channels`
and the Brand dropdown.

```yaml
brands:
  demo:
    name:      "Your Brand"       # display name (and sidebar logo text)
    short:     "YB"
    author:    "Your Name"        # byline
    handle:    "@yourbrand"       # shown on cards/reels
    instagram: "yourbrand"
    tagline:   "Your tagline"
    pitch:     "What you do, in one line."
    services:  "Service 1 · Service 2 · Service 3"
    location:  "Your City"
    website:   "yourbrand.com"
    email:     "hello@yourbrand.com"
    category:  "YOUR NICHE"
    logo_path: "static/logo.png"  # your logo file
    logo_shape: "wide"            # optional: 'wide' = full wordmark (don't crop to circle)
    image_forward: true           # optional: image-led layouts (image+text split)
    hashtags:  ["yourbrand", "marketing"]
    templates:                    # optional: override shared templates per brand
      title:   custom_title.html
      content: custom_content.html
      outro:   custom_outro.html
    theme:
      navy:    "#0A0F1E"          # background base
      navy2:   "#0C1426"
      accent:  "#00B4C8"          # primary accent
      accent2: "#00C896"          # secondary accent
      text:    "#FFFFFF"
    video:                        # 9:16 Reels styling (see config.example.yaml for all keys)
      enabled: true
      color_grade: { overlay: "#0A0F1E", opacity: 0.15 }
      captions:   { font_size: 54, text_color: "#FFFFFF", box_opacity: 0.55, position: 0.74 }
      hook_card:  { enabled: true, duration: 2.0 }
      end_card:   { enabled: true, duration: 3.0, cta: "Link in bio" }
    profile: >
      Describe your niche + audience + what should score high. Drives 0-100 story scoring.
    personality: >
      Describe your brand voice. The AI rewrites copy in this voice on a final pass.
    feeds:
      categories:
        tech:
          name:    "Tech"
          enabled: true
          urls:
            - "https://techcrunch.com/feed"
        youtube:
          name:    "YouTube"
          enabled: true
          urls:
            # channel feed: https://www.youtube.com/feeds/videos.xml?channel_id=<ID>
            - "https://www.youtube.com/feeds/videos.xml?channel_id=UC2Xd-TjJByJyK2w1zNwY0zQ"
```

## `llm:` — local model

```yaml
llm:
  base_url: "hermes://cli"       # active backend on boot (env K2_LLM_* overrides)
  model:    "hermes:gpt-5.5"
  format:   "json"
  backends:                      # selectable in the header Engine dropdown
    hermes: { base_url: "hermes://cli",                 model: "hermes:gpt-5.5" }
    ollama: { base_url: "http://localhost:11434/v1",    model: "qwen3:8b" }
```

## `output:` / `images:` / `slides:`

```yaml
output:  { width: 1080, height: 1350, directory: "outputs" }

images:                          # filter baked into web/Google images on download
  filter: { enabled: true, saturation: 0.85, brightness: 0.95, contrast: 1.05 }

slides:
  min_content_cards: 1           # min total = 3 (title + 1 + outro)
  max_content_cards: 8           # max total = 10
  default_total:     4
  short_story: { max_summary_words: 220, content_cards: 2 }
  long_story:  { min_summary_words: 221, content_cards: 4 }
```

## `formats:` — post types

Each format has a size and template; `type: multi` = carousel-style, `single` = one card.
Add `brands: ["key1","key2"]` to restrict a format to specific brands.

```yaml
formats:
  carousel:  { name: "Carousel",       width: 1080, height: 1350, type: multi }
  square:    { name: "Square Post",    width: 1080, height: 1080, type: single, template: square.html }
  story:     { name: "Story",          width: 1080, height: 1920, type: single, template: story.html }
  # … quote / comparison / breaking / listicle / linkedin / x / cover
  linkedin:
    name: "LinkedIn"
    width: 1200
    height: 1200
    type: single
    template: linkedin.html
    # brands: ["demo"]           # uncomment to restrict
```

Templates live in `templates/` (Jinja2 HTML/CSS); brand colours come from the `theme`
block via CSS variables. Edit templates live in the **Templates** tab.
