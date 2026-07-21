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

# ── Artist image (bannermini) ──────────────────────────────────────────────────
# True  = fetch artist photos from Last.FM gallery + AudioDB
# False = always use the fallback asset set in the Discord Application
ARTIST_IMAGE_ENABLED = True

# URL returned when no image is found in the pool.
# None = let Discord display the default fallback asset you set in the Application.
AUDIODB_FALLBACK_URL = None

# Last.FM placeholder image hash — images containing this hash have no real artwork.
LASTFM_DUMMY_HASH = "2a96cbd8b46e442fc41c2b86b821562f"

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
