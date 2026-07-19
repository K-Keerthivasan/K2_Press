# Master Build Prompt — "PostPilot" (working name)

> Copy everything below the line into a fresh Claude Code session in an **empty new repo**.
> Work phase by phase — don't paste all phases at once if the tool struggles; each phase is self-contained.
> Note: this doc describes the commercial rewrite. Don't commit it to the open-source K2_PostTool repo.

---

## The Prompt

You are building **a commercial, multi-tenant SaaS web app for AI-powered social media content creation**, from scratch, in TypeScript. It is a ground-up rewrite of an existing Python/Flask tool that already works — the feature set below is proven, so build to this spec rather than re-deriving the product.

### Product summary

A web app where a user connects one or more **brands** (Instagram-first, other platforms later) and generates ready-to-publish content:

- **Auto mode:** pull stories from RSS feeds / Google Trends per brand niche, pick the best ones, generate a content plan (hook / title / caption / hashtags / CTA) per story.
- **Manual mode:** user types a topic or pastes text; same plan-generation pipeline runs on a synthetic story.
- **Post formats:** standard post, carousel, quote card, comparison, breaking-news, LinkedIn-style, listicle.
- **Image pipeline:** suggest images from Pexels / Unsplash / web search, show a candidate grid, user picks, then render the branded post image (headline text baked onto image, brand colors/logo/fonts) server-side.
- **Video/Reels (phase 2):** given a YouTube URL (rights-gated), download with yt-dlp, cut clips with ffmpeg, render a branded 9:16 reel or image carousel from an editable manifest.
- **Review queue → publish:** every generated post lands in a review queue; on approve, it is pushed to the user's connected **Postiz** instance (or later, directly to Meta Graph API) as a scheduled draft.
- **Content library:** plans and story sets persist per brand; dedup so the same story isn't reused.

### The one non-negotiable architectural requirement: dual AI runtime

The app must run in two modes, selected by environment config, with **zero code changes between them**:

1. **`local` mode (development / self-host):** all LLM calls go to a local OpenAI-compatible endpoint (LM Studio / Ollama serving a Hermes model) at `LOCAL_LLM_BASE_URL`. No cloud AI keys needed. This is how the developer tests everything for free.
2. **`cloud` mode (production SaaS):** LLM calls go to real provider APIs — **Anthropic** (primary) and **OpenAI** (secondary), selectable per task tier.

Implement this as a single **provider factory module** (`src/lib/ai/provider.ts`) using the **Vercel AI SDK** (`ai` package):

- `@ai-sdk/anthropic` for Claude — use `claude-opus-4-8` for the "quality" tier (plan generation, captions) and `claude-haiku-4-5` for the "cheap" tier (classification, hashtag cleanup, scoring stories). These are current model IDs — do not substitute older ones from memory.
- `@ai-sdk/openai` for OpenAI models.
- `createOpenAICompatible` (from `@ai-sdk/openai-compatible`) pointed at `LOCAL_LLM_BASE_URL` for local mode.

Env contract:

```
AI_MODE=local | cloud
LOCAL_LLM_BASE_URL=http://localhost:1234/v1   # LM Studio / Ollama
LOCAL_LLM_MODEL=hermes-3-llama-3.1-8b
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
AI_QUALITY_MODEL=claude-opus-4-8              # cloud only
AI_CHEAP_MODEL=claude-haiku-4-5               # cloud only
```

Every feature calls `getModel("quality" | "cheap")` — nothing imports a provider SDK directly. All generation uses the AI SDK's `generateObject`/`streamObject` with Zod schemas so plan output is always valid JSON regardless of provider. Local models are weaker at JSON, so include one retry-with-repair pass on schema validation failure.

### Stack (use exactly this)

- **Next.js 15 (App Router) + TypeScript**, Tailwind CSS + shadcn/ui, dark-mode default.
- **Postgres + Drizzle ORM** (Neon in prod, Docker Postgres locally). No SQLite.
- **Better Auth** for authentication (email + Google OAuth).
- **Stripe** for subscriptions (free tier: 1 brand / 20 posts-month; pro: unlimited brands, video).
- **Inngest** for background jobs (feed polling, image rendering, publish pushes, video jobs).
- **Cloudflare R2** (S3 API) for rendered images/videos.
- **Image rendering:** headless Chromium (Playwright) rendering HTML/CSS templates to PNG — port the existing template concept (hook/title/CTA layers over background image) rather than canvas drawing.
- **Video worker:** a separate small Node service in `apps/worker` with ffmpeg + yt-dlp, deployed as a container (Fly.io/Railway); the Next.js app never runs ffmpeg itself. Monorepo: pnpm workspaces + turborepo (`apps/web`, `apps/worker`, `packages/db`, `packages/ai`).
- **Deploy target:** Vercel for `apps/web`, container host for `apps/worker`.

### Multi-tenancy model

- `users` → `organizations` → `brands`. A brand holds: name, handle, niche, tone-of-voice prompt, colors, logo, fonts, RSS feed list, Postiz credentials (encrypted at rest), posting schedule.
- Everything (stories, plans, posts, media, jobs) is scoped by `brand_id` and row-level checked by `org_id` in every query. No global state, no config files — the old app's `config.yaml` becomes DB rows editable in a Brand Settings UI.

### Build phases (do them in order; each ends with the app running and a short verification note)

**Phase 1 — Skeleton & auth.** Monorepo scaffold, Next.js app, Drizzle schema (users/orgs/brands), Better Auth, brand CRUD UI with theming fields, env validation (zod-validated `env.ts`), seed script that creates a demo brand.

**Phase 2 — AI core.** `packages/ai`: provider factory, `local`/`cloud` switch, Zod schemas for the content plan (hook, title, caption, hashtags, cta, format, image_query), `generatePlan(story, brand, format)` with tone-of-voice injection, retry-with-repair. CLI test script that runs one generation in each mode.

**Phase 3 — Stories & plans.** RSS ingestion (Inngest cron per brand), story dedup (hash + already-used table), trending topics (Google News RSS fallback), Manual mode (topic → synthetic story → same pipeline), plan generation UI: pick stories → generate → editable plan cards with per-post tone override and format picker.

**Phase 4 — Images.** Pexels/Unsplash search server routes, candidate grid picker UI, Playwright HTML-template renderer (hook/title/cta template variants ported as React → static HTML), upload to R2, attach to plan.

**Phase 5 — Review & publish.** Review queue (approve / reject / edit), Postiz adapter (create scheduled draft via Postiz API with brand credentials), publish log with retry, status webhooks/polling back into the queue.

**Phase 6 — Billing & limits.** Stripe checkout + customer portal, plan limits enforced middleware-side (posts/month, brand count), usage metering table (record model, tokens in/out per AI call — read token usage from AI SDK responses — so cloud costs per org are visible in an admin page).

**Phase 7 — Video/Reels.** `apps/worker`: yt-dlp download (only when user confirms rights), manifest-based clip cutting + branded overlay via ffmpeg, 9:16 reel + carousel export, job status streamed to the web UI, publish via Postiz.

### Guardrails

- No feature flags, no speculative abstractions beyond the provider factory. Do the simplest thing that satisfies the spec.
- Secrets never reach the client; brand credentials encrypted (libsodium sealed box or AES-GCM with a `SECRET_ENCRYPTION_KEY`).
- Every external call (LLM, Pexels, Postiz, yt-dlp) goes through one module with timeout + typed error, so providers stay swappable.
- Rate-limit generation endpoints per org.
- Write a `README.md` with local-mode quickstart: `docker compose up db`, `pnpm dev`, LM Studio pointed at `LOCAL_LLM_BASE_URL` — a new dev must get to a generated post with zero cloud keys.
- After each phase, run the app and verify the phase's happy path end-to-end before moving on; report what you verified.

Start with Phase 1 now. Ask me only for decisions that genuinely change the product (naming, pricing numbers); pick sensible defaults for everything else and note them.
