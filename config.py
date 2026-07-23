import os
from dotenv import load_dotenv

load_dotenv()

# ── Credentials (loaded from .env) ─────────────────────────────────────────────
LAST_FM_USERNAME  = os.getenv("LAST_FM_USERNAME")
API_KEY           = os.getenv("API_KEY")
USER_ID           = os.getenv("USER_ID")           # your Discord user ID
STATSFM_USERNAME  = os.getenv("STATSFM_USERNAME")

# ── Widget: Listening Stats ────────────────────────────────────────────────────
LS_APPLICATION_ID = os.getenv("APPLICATION_ID")
LS_BOT_TOKEN      = os.getenv("BOT_TOKEN")

# ── Widget: Top Artists ────────────────────────────────────────────────────────
TA_APPLICATION_ID = os.getenv("TOPARTISTS_APPLICATION_ID")
TA_BOT_TOKEN      = os.getenv("TOPARTISTS_BOT_TOKEN")

# ── imgfixer: webhook URL for hosting processed album art ──────────────────────
# Required when IMGFIXER_ENABLED = True.
# Create a webhook in a private Discord channel:
#   Channel Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL
DISCORD_IMAGE_WEBHOOK_URL = os.getenv("DISCORD_IMAGE_WEBHOOK_URL", "")

# ── Spotify (optional — album art fallback) ────────────────────────────────────
# When Last.FM returns a placeholder/dummy image for a track, the bot will
# search Spotify and use its album cover instead (processed by imgfixer if enabled).
# Leave empty to disable — Discord will show its default fallback asset.
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID",     "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
# Optional: enables queue API fallback (exact track match, no encoding issues).
# Get this by running: python spotify_auth.py
SPOTIFY_REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN", "")

# ── imgfixer — album art fixer for bannerwidgettop ────────────────────────────
# True  = process each album art image with the D.W.I.F. algorithm (removes the
#         thin top strip + rounds the top-right corner) then host it on Discord
#         CDN via the webhook URL below. Requires Pillow and DISCORD_IMAGE_WEBHOOK_URL.
# False = use the raw Last.FM CDN URL as-is (default, no extra dependencies).
IMGFIXER_ENABLED = True

# How long (seconds) before a cached Discord CDN URL is considered stale and
# the image is re-uploaded. Discord signed CDN URLs expire at ~24 h;
# 20 h (72 000 s) gives a safety margin so the banner never goes blank.
IMGFIXER_REUPLOAD_INTERVAL = 72_000   # 20 hours

# ── Artist image (bannermini) ──────────────────────────────────────────────────
# True  = fetch artist photos from Last.FM gallery + AudioDB
# False = always use the fallback asset set in the Discord Application
ARTIST_IMAGE_ENABLED = True

# URL returned when no image is found in the pool.
# None = let Discord display the default fallback asset you set in the Application.
AUDIODB_FALLBACK_URL = None

# ── Debug mode ────────────────────────────────────────────────────────────────
# True  = verbose logs: full URLs, raw API responses, pool hits, "No changes, skip"
# False = clean logs: status, errors, and key events only (recommended for daily use)
DEBUG = False


# Last.FM placeholder image hash — images containing this hash have no real artwork.
LASTFM_DUMMY_HASH = "2a96cbd8b46e442fc41c2b86b821562f"

# ── npcount: play-count subtitle ───────────────────────────────────────────────
# True  = two-line format, album below the count (requires Discord to render \n):
#           21 Scrobbles
#           Daughter From Hell
#         ⚠ Discord strips \n in subtitle fields — this shows as one line without
#           separator. Keep False unless Discord adds newline support.
# False = single-line with bullet separator and dynamic truncation:
#           21 Scrobbles • Daughter from H...
NPCOUNT_SPLIT_LINES = False

# Maximum TOTAL characters of the entire npcount string (count + separator + album).
# Only used when NPCOUNT_SPLIT_LINES = False.
# This is the approximate 1-line limit of the Discord widget subtitle field.
# The album portion gets whatever space remains after the count prefix.
# Recalculated after changing "Scrobbles" → "Plays" (saves 4 chars):
#   "7 Plays • It Took Me Falling in Lo..." (36 chars) → 1 line ✓
#   "168 Plays • Live From Mother Earth..." (36 chars) → 1 line ✓
#   (old value was 32 with "Scrobbles"; +4 for the shorter word)
NPCOUNT_TOTAL_MAX_LEN = 36

# ── Intervals (seconds) ────────────────────────────────────────────────────────
LS_FAST_INTERVAL = 20   # how often to poll Last.FM for recent tracks
LS_SLOW_INTERVAL = 60   # how often to refresh user info & stats.fm stream stats

# ── Image pool ────────────────────────────────────────────────────────────────
# Last.FM /+images shows up to 40 images per page.
POOL_MAX_IMAGES = 40   # maximum images to keep in a single artist's pool
POOL_MAX_PAGES  = 2    # maximum Last.FM gallery pages to scrape per artist
                       # raise this for very popular artists (some have 14+ pages)

# ── Top Artists ───────────────────────────────────────────────────────────────
# Range values: "lifetime" = all-time | "months" = 6 months | "weeks" = 30 days
# TOPARTISTS_RANGE = starting range when rotation is ON, or fixed range when OFF.
TOPARTISTS_RANGE  = "lifetime"

# Rotation mode:
# True  = auto-rotate All Time → 6 Months → 30 Days, every ROTATION_INTERVAL seconds
# False = show a fixed range (TOPARTISTS_RANGE), refresh every STATIC_INTERVAL seconds
TOPARTISTS_ROTATE  = True
ROTATION_INTERVAL  = 25    # seconds per rotation step (keep >= 20; Discord rate limit: 3 req/~60s)
STATIC_INTERVAL    = 120   # seconds between refreshes when rotation is OFF

ROTATION_ORDER  = ["lifetime", "months", "weeks"]
ROTATION_LABELS = {"lifetime": "All Time", "months": "6 Months", "weeks": "30 Days"}

# ── Image cache ───────────────────────────────────────────────────────────────
IMAGE_CACHE_FILE     = "image_cache.json"
IMAGE_CACHE_TTL_DAYS = 30   # days before re-scraping Last.FM for an artist's images
                             # re-deploy after this period to bundle a fresh cache

# ── Blacklisted image hashes ──────────────────────────────────────────────────
# Add 32-char hex hashes for Last.FM images that show the wrong person.
# Find the hash in the log: "[LS] Last.FM page 1: ..." → copy from the wsrv.nl URL
# Example URL: https://wsrv.nl/?url=lastfm.freetls.fastly.net/i/u/770x0/<hash>&...
BLACKLISTED_HASHES: set[str] = {
    # "309a64bc97a0c73ac25968e6b4f0aa69",  # wrong photo on Kyra Fields page
}

# ── HTTP headers ──────────────────────────────────────────────────────────────
STATSFM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
