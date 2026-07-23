# 🚀 WidgetFM — Fly.io Deployment Guide

## Prerequisites

- A [Fly.io](https://fly.io) account (you can sign up with GitHub)
- Fly.io may ask for a credit card for verification — **you will not be charged** while within the free tier limits

---

## Step 1 — Install flyctl (Fly.io CLI)

Open **PowerShell** and run:

```powershell
winget install flyctl
```

Or download manually from: https://fly.io/docs/hands-on/install-flyctl/

Verify the installation:
```powershell
flyctl version
```

---

## Step 2 — Log in to Fly.io

```powershell
flyctl auth login
```

A browser window will open for login / sign-up.

---

## Step 3 — Initialize the App (run from the widgetfm folder)

```powershell
cd path\to\widgetfm
flyctl launch --no-deploy
```

Fly.io will prompt you for:
- **App name**: choose a unique name, e.g. `widgetfm-yourname`
- **Region**: select `sin` (Singapore) — closest to Southeast Asia
- **Would you like to set up a PostgreSQL database?**: **No**
- **Would you like to set up an Upstash Redis database?**: **No**

This generates a `fly.toml` file automatically.

---

## Step 4 — Set Secrets (API Keys & Tokens)

**Never put your `.env` on the server** — use Fly.io secrets instead.
Secrets are encrypted at rest and are never visible after being set.

```powershell
flyctl secrets set `
  LAST_FM_USERNAME="your_lastfm_username" `
  API_KEY="your_lastfm_api_key" `
  USER_ID="your_discord_user_id" `
  APPLICATION_ID="ls_application_id" `
  BOT_TOKEN="ls_bot_token" `
  TOPARTISTS_APPLICATION_ID="ta_application_id" `
  TOPARTISTS_BOT_TOKEN="ta_bot_token"
```

**Optional secrets** — add only if you use these features:

```powershell
# stats.fm — required for Top Artists widget and hoursstreamed/minutesstreamed slots
flyctl secrets set STATSFM_USERNAME="your_statsfm_username"

# Spotify — album art fallback (run spotify_auth.py locally first)
flyctl secrets set `
  SPOTIFY_CLIENT_ID="your_spotify_client_id" `
  SPOTIFY_CLIENT_SECRET="your_spotify_client_secret" `
  SPOTIFY_REFRESH_TOKEN="your_spotify_refresh_token"

# imgfixer — required only when IMGFIXER_ENABLED = True in config.py
flyctl secrets set DISCORD_IMAGE_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

See `.env.example` for a description of each variable.

> ⚠️ **Spotify auth must be done locally.** `spotify_auth.py` opens a browser and runs a local HTTP server — it cannot run on Fly.io. Obtain your refresh token locally first (`python spotify_auth.py`), then add it as a secret above.

---

## Step 4b — Adding Secrets to an Already-Deployed App

If you have already deployed and need to add new secrets (e.g. Spotify keys added later):

```powershell
# Set one or more secrets — Fly.io will automatically redeploy
flyctl secrets set SPOTIFY_CLIENT_ID="..." SPOTIFY_CLIENT_SECRET="..." SPOTIFY_REFRESH_TOKEN="..."
```

To set multiple secrets at once without triggering a redeploy per secret, chain them in a single command as shown above — Fly.io only redeploys once.

To check which secrets are currently set (values are hidden):
```powershell
flyctl secrets list
```

---

## Step 5 — Build the Image Cache Locally (optional but recommended)

Run the script on your local machine while listening to music.
`image_cache.json` will be populated with artist image URLs scraped from Last.FM.
This file is bundled into the Docker image at deploy time, so the server never needs to scrape Last.FM directly (datacenter IPs are often rate-limited by Last.FM).

---

## Step 6 — Deploy

```powershell
flyctl deploy
```

Fly.io will:
1. Build the Docker image from `Dockerfile`
2. Push it to the Fly.io registry
3. Deploy to the server in your chosen region
4. The script starts automatically

---

## Monitoring — Tail Live Logs

```powershell
flyctl logs
```

Normal startup output looks like:
```
=======================================================
   WidgetFM — Starting
   Listening Stats : ✓ fast=20s / slow=60s
   Top Artists     : ✓ rotate=ON | All Time → 6 Months → 30 Days | 25s/step
=======================================================
[Cache] Global cache ready: 12 artist(s) cached
[LS] Thread started
[LS] Status: Now Playing | Some Song — Some Artist
[LS] Discord → 204 | Rate: 2/3 remaining, resets in 20.0s
```

---

## Useful Commands

| Command | Description |
|---|---|
| `flyctl logs` | Tail live logs |
| `flyctl status` | Check app status (running / stopped) |
| `flyctl restart` | Restart the script |
| `flyctl secrets list` | List secret names (values are hidden) |
| `flyctl secrets set KEY=VALUE` | Update a single secret |
| `flyctl scale count 0` | Stop the server (e.g. before running locally) |
| `flyctl scale count 1` | Ensure exactly one instance is running |
| `flyctl deploy` | Re-deploy after code or cache changes |
| `flyctl open` | Open the Fly.io web dashboard |

---

## Running Locally vs. On the Server

**Do not run the script locally and on the server at the same time.**
Both instances would patch the same Discord widget simultaneously, causing conflicts and double the rate-limit consumption.

Workflow:
1. `flyctl scale count 0` — stop the server
2. Run locally for testing or cache-building
3. `flyctl deploy` — re-deploy (automatically starts the server with 1 instance)

---

## Notes

### Image cache

`image_cache.json` is committed to the repository and bundled into the Docker image.
Each artist's image URLs are cached for 30 days (configurable in `config.py`).
To refresh stale images, run the script locally for a while and then re-deploy.

> ⚠️ **Last.FM scraping from Fly.io:** Fly.io datacenter IPs may be rate-limited by Last.FM for image scraping. Since `image_cache.json` is bundled at deploy time, the server will not need to scrape for cached artists. For artists added after the last deploy whose cache has expired, the fallback is no image for that artist — the widget will show its default fallback image.

### Auto-restart

Fly.io automatically restarts the container if it crashes.
The script will not go down permanently.

### Fly.io free tier

- 3 shared-cpu-1x VMs (256 MB RAM) — **free forever**
- This script uses roughly ~30 MB RAM and near-zero CPU
- Well within free tier limits

---

## Troubleshooting

```powershell
# View detailed error output
flyctl logs --tail

# Rebuild without Docker layer cache
flyctl deploy --no-cache

# Destroy the app and start fresh
flyctl apps destroy widgetfm-yourname
flyctl launch --no-deploy
```
