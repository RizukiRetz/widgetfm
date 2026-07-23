# WidgetFM

Real-time Discord profile widget that displays your Last.FM listening stats and top artists — with album art sourced from Lanyard, Spotify, and Last.FM.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-GPL--3.0-green)

---

## Showcase

![Listening Stats Mini Profile](showcase/minigif.gif)

![Now Playing Widget](showcase/npgif.gif)

![Top Artists Widget](showcase/retztawidgetshowcase.gif)

---

## Features

- **Listening Stats widget** — Now Playing / Last Played track, scrobble count, play count subtitle, and six fully configurable stat slots (scrobbles, top artists/tracks/albums, hours/minutes listened, etc.)
- **Top Artists widget** — Your top 5 artists across three time ranges (All Time / 6 Months / 30 Days), rotating every 25 seconds
- **Three-source album art priority chain** — Lanyard (Discord Rich Presence, 640×640) → Spotify API → Last.FM (automatic fallback per track)
- **Dynamic artist images** — Scraped from Last.FM's photo gallery, with AudioDB as a fallback; served through [wsrv.nl](https://wsrv.nl) for consistent sizing
- **Smart image caching** — Locally built cache is bundled into the Docker image so the server never needs to scrape Last.FM directly
- **Configurable stat slots** — All six stat display slots are configurable from `config.py` without touching any other code
- **24/7 deployment** — Ships with a `Dockerfile` and a guide for [Fly.io](https://fly.io) free-tier hosting

---

## Prerequisites

- Python 3.11 or later (download from [python.org](https://www.python.org/downloads/))
- Git (download from [git-scm.com](https://git-scm.com/downloads))
- A [Last.FM](https://www.last.fm) account with API access
- Two Discord Applications (one per widget)
- *(Optional)* A [Spotify](https://developer.spotify.com/dashboard) app — for album art fallback when Last.FM has no artwork
- *(Optional)* A [stats.fm](https://stats.fm) account — required only for the Top Artists widget and `hoursstreamed` / `minutesstreamed` stat slots
- *(Optional)* A [Fly.io](https://fly.io) account for 24/7 hosting

---

## References

This project patches Discord's widget API. If you are setting up the widget config for the first time, these guides explain the full process:

- 📖 [How to Make Discord Widgets — Chloe Cinders](https://chloecinders.com/blog/discord-widgets)
- 🎥 [Video Tutorial — YouTube](https://youtu.be/gYv7D83u7yQ)

---

## Get Started

Open a terminal (PowerShell on Windows, Terminal on macOS/Linux) and run:

```bash
# 1. Clone the repository
git clone https://github.com/RizukiRetz/widgetfm.git

# 2. Enter the project folder
cd widgetfm

# 3. Install dependencies
#    Run this inside the widgetfm folder — it installs all required Python packages
pip install -r requirements.txt

# 4. Create your config file
#    Copy the template and fill in your credentials (see Configuration section below)
cp .env.example .env
```

> On Windows, use `copy .env.example .env` instead of `cp`.

After filling in `.env`, run the script:

```bash
python upstats.py
```

On Windows you can also double-click `run.bat`.

---

## Discord Application Setup

You need **two separate Discord Applications** — one for each widget.

### Step 1 — Enable the Widget Editor

The widget editor in the Discord Developer Portal is behind an experiment flag. You need to unlock it manually once.

1. Go to the [Discord Developer Portal](https://discord.com/developers/home) in your **browser**.
2. Press `Ctrl + Shift + I` (or `Cmd + Option + I` on macOS) to open Developer Tools.
3. Go to the **Console** tab and paste the following code, then press Enter:

```js
let _mods = webpackChunkdiscord_developers.push([[Symbol()],{},r=>r.c]);
webpackChunkdiscord_developers.pop();
let findByProps = (...props) => {
  for (let m of Object.values(_mods)) {
    try {
      if (!m.exports || m.exports === window) continue;
      if (props.every((x) => m.exports?.[x])) return m.exports;
      for (let ex in m.exports) {
        if (props.every((x) => m.exports?.[ex]?.[x]) && m.exports[ex][Symbol.toStringTag] !== 'IntlMessagesProxy') return m.exports[ex];
      }
    } catch {}
  }
}
findByProps("getAll").getAll().find(e=>e.getName() === "ApexExperimentStore").createOverride("2026-03-widget-config-editor", 1)
```

> ⚠️ You need to run this code again every time you refresh or return to the Developer Portal page.

### Step 2 — Create the Applications

1. In the Developer Portal, go to **Applications** → **New Application**.
2. Name it (e.g. `Listening Stats`) → **Create**.
3. Go to **Bot** → **Add Bot** → confirm.
4. Copy the **Application ID** (from *General Information*) and **Bot Token** (from *Bot*).
5. Repeat for the second application (e.g. `Top Artists`).

### Step 3 — Configure the Widget Fields

In each Discord Application, go to **Games → Widget → Create Widget**.

Then go to the **Content** tab and add each field below. Set **Value Type** to **User Data** and the **Data Field** (key name) to the exact name listed in the table.

> ⚠️ **Field names are case-sensitive and must match exactly.**

#### Listening Stats widget (Application #1)

| Field name | Type | Notes |
|---|---|---|
| `bannerwidgettop` | **Image** | Album artwork — updates with every track. Set a fallback image. |
| `nowplaying` | Text | `"Now Playing"` or `"Last Played"` |
| `nptrack` | Text | Track name |
| `npartist` | Text | Artist name |
| `npcount` | Text | Play count + album subtitle (e.g. `3 Plays • Music, Fashion, Film`) |
| `lsstat1` | Text | Stat slot 1 value (configurable in `config.py`) |
| `lsstat2` | Text | Stat slot 2 value |
| `lsstat3` | Text | Stat slot 3 value |
| `lsstat4` | Text | Stat slot 4 value |
| `lsstat5` | Text | Stat slot 5 value |
| `lsstat6` | Text | Stat slot 6 value |
| `lslabel1` | Text | Stat slot 1 label (auto-generated — set **Value Type** to **User Data**) |
| `lslabel2` | Text | Stat slot 2 label |
| `lslabel3` | Text | Stat slot 3 label |
| `lslabel4` | Text | Stat slot 4 label |
| `lslabel5` | Text | Stat slot 5 label |
| `lslabel6` | Text | Stat slot 6 label |
| `lsmini` | Text | Mini profile stat — combined value + label string (e.g. `28,745 Total Songs`) |
| `bannermini` | **Image** | Artist photo — updates per artist or track. Set a fallback image. |

> ⚠️ **Set a default fallback** for `bannerwidgettop` and `bannermini`.
> This image shows before the first update or when no artwork is available.

> ⚠️ **Subtitle 3 on Widget Top** — if your widget layout uses a Subtitle 3 field, set its **Value Type** to **Custom String** and enter a static text (e.g. your Last.FM profile URL like `last.fm/user/YourUsername`). Leaving it empty or as User Data with no fallback will cause a **skeleton loading animation** that never resolves.

#### Top Artists widget (Application #2)

| Field name | Type | Description |
|---|---|---|
| `1artistimg` | **Image** | #1 artist photo |
| `2artistimg` | **Image** | #2 artist photo |
| `3artistimg` | **Image** | #3 artist photo |
| `4artistimg` | **Image** | #4 artist photo |
| `5artistimg` | **Image** | #5 artist photo |
| `1artisttitle` | Text | e.g. `#1 Holly Humberstone (All Time)` |
| `2artisttitle` | Text | e.g. `#2 Olivia Rodrigo` |
| `3artisttitle` | Text | e.g. `#3 Gracie Abrams` |
| `4artisttitle` | Text | e.g. `#4 Clairo` |
| `5artisttitle` | Text | e.g. `#5 Phoebe Bridgers` |
| `1minutesplayed` | Text | e.g. `1,234 Minutes Listened` |
| `2minutesplayed` | Text | e.g. `987 Minutes Listened` |
| `3minutesplayed` | Text | e.g. `654 Minutes Listened` |
| `4minutesplayed` | Text | e.g. `321 Minutes Listened` |
| `5minutesplayed` | Text | e.g. `100 Minutes Listened` |
| `1genre` | Text | Top artist's genre(s) (rank #1 only) |

> ⚠️ **Set a default fallback image** for all five `{n}artistimg` fields.
> These show before the first update or when no pool image is available for an artist.

### Step 4 — Apply the Application Identity

Follow the [Chloe Cinders guide](https://chloecinders.com/blog/discord-widgets) under **"Applying an Application Identity"** using the [Widget Identity Creator](https://github.com/chloecinders/widget-identity-creator/releases) tool. This step links the widget config to your Discord profile.

### Step 5 — Add the Widget to Your Profile

Follow the guide under **"Adding the Widget to your Profile"** to pin both widgets to your Discord profile using the browser console snippet.

---

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

| Variable | Required | Where to find it |
|---|---|---|
| `LAST_FM_USERNAME` | ✅ | Your Last.FM username |
| `API_KEY` | ✅ | [last.fm/api/account/create](https://www.last.fm/api/account/create) |
| `USER_ID` | ✅ | Discord → Settings → Advanced → Developer Mode → right-click your profile |
| `APPLICATION_ID` | ✅ | Discord Developer Portal → Application #1 → General Information |
| `BOT_TOKEN` | ✅ | Discord Developer Portal → Application #1 → Bot → Reset Token |
| `TOPARTISTS_APPLICATION_ID` | ✅ | Discord Developer Portal → Application #2 → General Information |
| `TOPARTISTS_BOT_TOKEN` | ✅ | Discord Developer Portal → Application #2 → Bot → Reset Token |
| `STATSFM_USERNAME` | ⚪ Optional | Required for Top Artists widget and `hoursstreamed`/`minutesstreamed` stat slots |
| `DISCORD_IMAGE_WEBHOOK_URL` | ⚪ Optional | Required when `IMGFIXER_ENABLED = True` in `config.py` |
| `SPOTIFY_CLIENT_ID` | ⚪ Optional | [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) — for album art fallback |
| `SPOTIFY_CLIENT_SECRET` | ⚪ Optional | Same app as above |
| `SPOTIFY_REFRESH_TOKEN` | ⚪ Optional | Run `python spotify_auth.py` once after setting the two above |

Additional settings (intervals, stat slot types, rotation, image pool size, blacklisted image hashes) can be tuned in [`config.py`](config.py).

---

## Spotify Setup (Optional)

Spotify is used as a fallback album art source when Last.FM has no artwork for a track.

1. Create a Spotify app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Add `http://127.0.0.1:8888/callback` as a Redirect URI in the app settings
3. Copy `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` to `.env`
4. Run `python spotify_auth.py` once — it opens a browser and saves your refresh token automatically

---

## Fly.io Deployment (24/7 free hosting)

See [`deploy_flyioEN.md`](deploy_flyioEN.md) for the full step-by-step guide.

---

## Image cache

`image_cache.json` stores image URLs scraped from Last.FM, keyed by artist name, with a 30-day TTL. It is committed to Git and bundled into Docker images on deploy. To refresh stale images, run the script locally for a while and then re-deploy.

### Blacklisting wrong images

If a wrong photo appears in a widget, copy the 32-char hash from the `wsrv.nl` URL shown in the script's log output and add it to `BLACKLISTED_HASHES` in [`config.py`](config.py):

```python
BLACKLISTED_HASHES: set[str] = {
    "309a64bc97a0c73ac25968e6b4f0aa69",  # wrong photo uploaded to Artist X's Last.FM page
}
```

The blacklist is applied both during scraping and retroactively when loading from cache.

---

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
