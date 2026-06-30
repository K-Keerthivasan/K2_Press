# Publishing & Channels

How K2 Press publishes to Instagram and other social platforms, and how to connect
**multiple channels** — the core publishing workflow.

---

## How publishing works

K2 Press **renders** posts; it does not talk to Instagram directly. Publishing goes
through a self-hosted (or hosted) **[Postiz](https://postiz.com)** instance, which
holds the social-account connections and posts via each platform's API:

```
K2 Press  ──(Public API)──►  Postiz  ──(platform APIs)──►  Instagram / Facebook / LinkedIn / X / …
  renders + caption            holds the channel connections        publishes
```

K2 Press only needs **one secret**: the Postiz **Public API key** (`POSTIZ_API_KEY`).
Everything else — which platforms, which accounts — is configured **inside Postiz**.

Default mode is **draft**: approved posts land in your Postiz calendar as drafts, and
you publish/schedule them there. This is the review-and-approve gate.

---

## 1. Stand up Postiz

Run Postiz with Docker (self-host) or use Postiz Cloud. Full self-host steps:
<https://docs.postiz.com/installation/docker-compose>. Key points:

- Default local frontend: `http://localhost:4007`.
- Social provider credentials (Facebook/Instagram/LinkedIn/X app keys) go in the
  **Postiz container env**, not in K2 Press. See
  <https://docs.postiz.com/configuration/reference> for the exact env var per provider
  (e.g. `INSTAGRAM_APP_ID`/`INSTAGRAM_APP_SECRET`, `FACEBOOK_APP_ID`/`FACEBOOK_APP_SECRET`,
  `LINKEDIN_CLIENT_ID`/`LINKEDIN_CLIENT_SECRET`, …).

### Public HTTPS is required to connect accounts

Social OAuth (especially Instagram/Meta) **rejects `http://localhost` redirect URIs** —
they require HTTPS. So to *connect* an account you need Postiz reachable over a public
HTTPS URL. Options:

- **Cloudflare Tunnel** (named tunnel to a domain you own — recommended):
  ```bash
  cloudflared tunnel login
  cloudflared tunnel create my-postiz
  cloudflared tunnel route dns my-postiz postiz.yourdomain.com
  # config.yml ingress: postiz.yourdomain.com -> http://localhost:4007
  cloudflared tunnel run my-postiz
  ```
- **Quick tunnel** (zero setup, random URL): `cloudflared tunnel --url http://localhost:4007`
- **Tailscale Serve**, ngrok, or any reverse proxy that gives you HTTPS.

Then point Postiz at that host so the redirect URIs match:
```yaml
MAIN_URL: 'https://postiz.yourdomain.com'
FRONTEND_URL: 'https://postiz.yourdomain.com'
NEXT_PUBLIC_BACKEND_URL: 'https://postiz.yourdomain.com/api'
```
> **Error 1033 / 530** from the tunnel means the connector isn't running, or the DNS
> name points at a *different* tunnel than the one you're running. Make sure the
> `postiz` DNS record (a proxied CNAME) targets your running tunnel's
> `<UUID>.cfargotunnel.com`.

---

## 2. Get the Public API key into K2 Press

1. In Postiz → **Settings → Public API** → generate/copy the key.
2. Put it in K2 Press's `.env`:
   ```env
   POSTIZ_API_KEY=your_public_api_key
   ```
3. Set the base URL in `config.yaml` under `postiz.base_url`. It is your Postiz
   `NEXT_PUBLIC_BACKEND_URL` + `/public/v1`:
   - Local Docker (frontend on :4007): `http://localhost:4007/api/public/v1`
   - Hosted: `https://api.postiz.com/public/v1`
4. Verify K2 Press can reach Postiz:
   ```bash
   python postiz.py --list-channels
   ```
   This lists every channel (Postiz calls them *integrations*) with its **id**, name,
   and platform. An **empty list** with HTTP 200 means the key belongs to a different
   Postiz **organization** than the one with your channels — copy the key from the same
   account that shows the channels.

---

## 3. Connect Instagram

Instagram must be a **Business or Creator** account.

- **Recommended — Facebook-login route:** in Postiz add **Instagram (via Facebook Page)**.
  Your IG account must be linked to a Facebook Page. This is the most reliable route on
  self-hosted Postiz.
- **Instagram Standalone route:** newer "Instagram business login". Register the redirect
  URI `https://<your-postiz-host>/integrations/social/instagram-standalone` in your Meta
  app, and add your IG accounts as **Instagram testers** (accept the invite in the IG app)
  while the Meta app is in Development mode. *Note: the standalone route has had known
  OAuth issues on self-hosted Postiz — if it fails, use the Facebook-login route.*

Reels/9:16 video posts: on the Standalone integration a reel is sent as a `post` (a 9:16
video is published as a Reel by Instagram). K2 Press handles this automatically.

---

## 4. Add MULTIPLE channels (the main event)

You can connect as many channels as you like — more Instagram accounts, **and** other
platforms — then map each to a brand in K2 Press.

### 4a. Connect each channel in Postiz

In Postiz → **Add Channel**, connect whatever you need, once per account:

- Instagram account A, Instagram account B, … (one connection each)
- Facebook Page, LinkedIn, X/Twitter, TikTok, YouTube, Threads, Mastodon, etc.
  (each needs its provider app keys set in the Postiz container env first)

Each connection becomes one **integration** in Postiz with a unique **id**.

### 4b. Get the integration ids

```bash
python postiz.py --list-channels
```
Example output:
```
  cmqy696200001ok9ikctq0bzm  brand_a_ig       [instagram-standalone]
  cmqy69vee0003ok9ido3xvdrs  Brand B Studio   [instagram-standalone]
  cmrf1a2b30007ok9i...        Brand A LinkedIn [linkedin]
```

### 4c. Map channels to brands in `config.yaml`

K2 Press is multi-brand. Under `postiz.channels`, map **each brand key** (from your
`brands:` block) to its integration **id** (or name — both are matched at runtime):

```yaml
postiz:
  base_url: "http://localhost:4007/api/public/v1"
  api_key_env: "POSTIZ_API_KEY"
  channels:
    brand_a: "cmqy696200001ok9ikctq0bzm"   # brand_a → Instagram account A
    brand_b: "cmqy69vee0003ok9ido3xvdrs"   # brand_b → Brand B Studio
```

When you approve a post, K2 Press routes it to the channel mapped to **that post's
brand** (the Brand selector in the header). So a `brand_b` post publishes to Brand B's
Instagram, a `brand_a` post to Brand A's. Switching the active brand switches the target
account automatically.

> **Going forward, to add a new channel/brand:** (1) connect the account in Postiz,
> (2) `python postiz.py --list-channels` to get its id, (3) add `brand_key: "<id>"`
> under `postiz.channels`, and add the matching brand under `brands:` if it's new.
> No code changes — it's all config.

### 4d. (Optional) one brand → multiple platforms

The current mapping is one integration per brand (its Instagram). To fan a brand out to
several platforms (e.g. IG **and** LinkedIn) at once, that's a small extension to the
publisher — see [ARCHITECTURE.md](ARCHITECTURE.md) (`_postiz_publish` in `app.py`). For
now, connect the extra platform in Postiz and publish to it from the Postiz calendar, or
add a second brand key pointing at that integration.

---

## 5. Publish from K2 Press

**From the app (Review tab):** Agent/autopilot/manual posts land in **Review**. Click
**✓ Approve & publish** → the post is uploaded to Postiz as a **draft** on the brand's
channel. Then open Postiz to schedule or publish it live.

- When `PUBLIC_BASE_URL` is set in `.env`, Postiz fetches the media over HTTP
  (`/upload-from-url`) — needed when Postiz is remote/Dockerized; set it to a public URL
  Postiz can reach (e.g. your tunnel/Tailscale URL). Otherwise K2 uploads the local files.

**From the CLI (`postiz.py`):**
```bash
python postiz.py --type carousel --asset s1.png --asset s2.png --caption cap.txt --channel brand_a
python postiz.py --type post  --asset card.png --caption "Hello 👋" --channel brand_b
python postiz.py --type reel  --asset reel.mp4 --caption cap.txt --dry-run
```
- `--mode draft | schedule | now` (default `draft`). `now` is hard-guarded behind
  `postiz.publish.allow_now: true`; `schedule` requires `--date <ISO8601>`.
- `--dry-run` builds and prints the payload without posting.

---

## 6. Rate limits

Postiz's Public API allows **30 requests/hour** (requests, not posts). K2 Press accounts
for this up front (a 5-slide carousel = 6 requests: 5 uploads + 1 create; a reel = 2).
Tune the ceiling under `postiz.rate_limit` in `config.yaml`.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `--list-channels` is empty (HTTP 200) | API key is from a different Postiz **org** than the one with your channels. Copy the key from the same account. |
| Approve says "POSTIZ_API_KEY not set" | Key missing in `.env`, or the app wasn't restarted after adding it. |
| Post goes to the wrong account | `postiz.channels.<brand>` points at the wrong integration id, or the post's brand differs from what you expect. Re-check with `--list-channels`. |
| Can't connect Instagram | Needs HTTPS (not localhost) for OAuth, IG must be Business/Creator, and (standalone) the account added as a tester. Try the Facebook-Page route. |
| "Media fetch failed" in Postiz | Postiz can't reach your media. Set `PUBLIC_BASE_URL` to a host Postiz can fetch from. |
| Tunnel shows Error 1033/530 | `cloudflared` not running, or DNS points at a different tunnel. |
