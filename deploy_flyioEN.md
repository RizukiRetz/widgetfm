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
fly version
```

---

## Step 2 — Log in to Fly.io

```powershell
fly auth login
```

A browser window will open for login / sign-up.

---

## Step 3 — Initialize the App (run from the widgetfm folder)

```powershell
cd path\to\widgetfm
fly launch --no-deploy
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
fly secrets set `
  LAST_FM_USERNAME="your_lastfm_username" `
  API_KEY="your_lastfm_api_key" `
  USER_ID="your_discord_user_id" `
  STATSFM_USERNAME="your_statsfm_username" `
  APPLICATION_ID="ls_application_id" `
  BOT_TOKEN="ls_bot_token" `
  TOPARTISTS_APPLICATION_ID="ta_application_id" `
  TOPARTISTS_BOT_TOKEN="ta_bot_token"
```

See `.env.example` for a description of each variable.

---

## Step 5 — Build the Image Cache Locally (optional but recommended)

Run the script on your local machine while listening to music.  
`image_cache.json` will be populated with artist image URLs scraped from Last.FM.  
This file is bundled into the Docker image at deploy time, so the server never needs to scrape Last.FM directly (datacenter IPs are often rate-limited by Last.FM).

---

## Step 6 — Deploy

```powershell
fly deploy
```

Fly.io will:
1. Build the Docker image from `Dockerfile`
2. Push it to the Fly.io registry
3. Deploy to the server in your chosen region
4. The script starts automatically

---

## Monitoring — Tail Live Logs

```powershell
fly logs
```

Normal startup output looks like:
```
[TA] Thread started
[LS] Thread started
[Cache] Loaded 12 artists from image_cache.json
[Cache] Global cache ready: 12 artist(s) cached
[LS] Status: Now Playing | Some Song — Some Artist
[LS] Discord → 204 | Rate: 2/3 remaining, resets in 20.0s
```

---

## Useful Commands

| Command | Description |
|---|---|
| `fly logs` | Tail live logs |
| `fly status` | Check app status (running / stopped) |
| `fly restart` | Restart the script |
| `fly secrets list` | List secret names (values are hidden) |
| `fly secrets set KEY=VALUE` | Update a single secret |
| `fly scale count 0` | Stop the server (e.g. before running locally) |
| `fly scale count 1` | Ensure exactly one instance is running |
| `fly deploy` | Re-deploy after code or cache changes |
| `fly dashboard` | Open the Fly.io web dashboard |

---

## Running Locally vs. On the Server

**Do not run the script locally and on the server at the same time.**  
Both instances would patch the same Discord widget simultaneously, causing conflicts and double the rate-limit consumption.

Workflow:
1. `fly scale count 0` — stop the server
2. Run locally for testing or cache-building
3. `fly deploy` — re-deploy (automatically starts the server with 1 instance)

---

## Notes

### Image cache

`image_cache.json` is committed to the repository and bundled into the Docker image.  
Each artist's image URLs are cached for 30 days (configurable in `config.py`).  
To refresh stale images, run the script locally for a while and then re-deploy.

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
fly logs --tail

# Rebuild without Docker layer cache
fly deploy --no-cache

# Destroy the app and start fresh
fly apps destroy widgetfm-yourname
fly launch --no-deploy
```
