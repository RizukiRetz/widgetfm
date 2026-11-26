import re, requests, json, sys, time, random, threading, urllib.parse
from bs4 import BeautifulSoup
from config import *
from statsfmfetch import (
    get_user_stats       as _sf_get_user_stats,
    get_recent_stream    as _sf_get_recent_stream,
    get_track_play_count as _sf_get_track_play_count,
    get_top_items        as _sf_get_top_items,
    _norm                as _sf_norm,
)

# ── Optional: imgfixer (album art processor) ──────────────────────────────────────
# Loaded at startup only when IMGFIXER_ENABLED = True in config.py.
# Falls back gracefully if Pillow is not installed.
_imgfixer_available = False
if IMGFIXER_ENABLED:
    try:
        from imgfixer import fix_banner_url as _fix_banner_url
        _imgfixer_available = True
        print("[imgfixer] Loaded — album art processing enabled")
    except ImportError:
        print("[imgfixer] Pillow not installed — imgfixer disabled. Run: pip install Pillow")

# ── Optional: spotifyfetch (album art fallback) ───────────────────────────────
# Loaded only when SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET are set in .env.
# When Last.FM returns a dummy/placeholder image, spotifyfetch tries to find
# album art on Spotify using two strategies (see spotifyfetch.py for details).
_spotify_available      = False   # Client Credentials search (Strategy 2)
_spotify_user_available = False   # User OAuth currently-playing (Strategy 1)
if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    try:
        from spotifyfetch import (
            get_currently_playing_with_id as _get_spotify_currently_playing,
            get_spotify_album_art         as _get_spotify_art,
            get_recently_played_with_id   as _get_spotify_recent,
        )
        _spotify_available = True
        if SPOTIFY_REFRESH_TOKEN:
            _spotify_user_available = True
            print("[Spotify] currently-playing + recently-played + Search enabled")
        else:
            print("[Spotify] Search fallback enabled (run spotify_auth.py for exact match)")
    except ImportError:
        print("[Spotify] spotifyfetch.py not found — Spotify fallback disabled")

# ── Optional: lanyardfetch (priority album art source) ──────────────────────
# Loaded when USER_ID is set in .env (no extra credentials needed).
# Reads the user's 'Listening to Spotify' Discord presence via Lanyard API.
# Returns 640×640 Spotify CDN image — priority 1 in the art fallback chain.
# Rate limit: 1 000 req/hr (our 20-s poll = ~180 req/hr, well within limits).
_lanyard_available = False
if USER_ID:
    try:
        from lanyardfetch import get_lanyard_presence as _get_lanyard_art
        _lanyard_available = True
        print("[Lanyard] Presence enabled — song, artist, album, and art (priority 1)")
    except ImportError:
        print("[Lanyard] lanyardfetch.py not found — Lanyard disabled")

# ── Shared state: LS thread → TA thread ───────────────────────────────────────
# LS writes the currently playing artist; TA reads to avoid duplicating the
# bannermini image in the Top Artists panel.
_shared = {
    "artist_name":    None,   # name of the currently/last played artist
    "artist_pool":    [],     # image pool for the current artist
    "bannermini_url": None,   # active bannermini URL (excluded from TA pool picks)
}
_shared_lock = threading.Lock()

# ── Global image cache ─────────────────────────────────────────────────────────
# Pre-loaded from image_cache.json at startup and shared by both threads.
# Using a single global dict avoids race conditions where each thread would
# overwrite the other's freshly-written data.
_g_image_cache: dict = {}   # { artist_name: {"urls": [...], "fetched_at": float} }
_g_cache_lock  = threading.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# Image cache helpers
# ──────────────────────────────────────────────────────────────────────────────

def load_image_cache() -> dict:
    """Load the image URL cache from JSON file. Returns an empty dict on error."""
    try:
        import os
        if os.path.exists(IMAGE_CACHE_FILE):
            with open(IMAGE_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            print(f"[Cache] Loaded {len(data)} artists from {IMAGE_CACHE_FILE}")
            return data
    except Exception as e:
        print(f"[Cache] Failed to load: {e}")
    return {}


def save_image_cache(cache: dict) -> None:
    """Persist the image URL cache to JSON file."""
    try:
        with open(IMAGE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Cache] Failed to save: {e}")


def _is_blacklisted(url: str) -> bool:
    """Return True if the URL contains a blacklisted image hash."""
    return any(h in url for h in BLACKLISTED_HASHES)


def get_pool_cached(artist_name: str, mbid: str = "", label: str = "Cache") -> list:
    """Return the image pool for an artist, using the global cache when possible.

    Fetches from Last.FM + AudioDB only on a cache miss or when the TTL has expired.
    Thread-safe: used concurrently by both the LS thread (bannermini) and the
    TA thread (top artist images).
    """
    if not artist_name:
        return []

    with _g_cache_lock:
        cached = _g_image_cache.get(artist_name, {})
        age_s  = time.time() - cached.get("fetched_at", 0)
        if cached.get("urls") and (not IMAGE_CACHE_TTL_DAYS or age_s < IMAGE_CACHE_TTL_DAYS * 86400):
            # Retroactively filter blacklisted URLs and migrate all URLs to wsrv.nl proxy
            clean = []
            migrated = False
            for u in cached["urls"]:
                if _is_blacklisted(u):
                    migrated = True
                    continue
                if u.startswith("https://wsrv.nl/?url="):
                    clean.append(u)
                elif "lastfm.freetls.fastly.net" in u:
                    clean_u = u.replace("avatar300s", "770x0").replace("https://", "").replace("http://", "")
                    wsrv_url = (
                        f"https://wsrv.nl/?url={clean_u}"
                        f"&w=300&h=300&fit=cover&a=entropy&output=jpg&q=80"
                    )
                    clean.append(wsrv_url)
                    migrated = True
                else:
                    clean.append(u)

            if migrated:
                print(f"[{label}] Migrated cached pool for '{artist_name}' to wsrv.nl")
                _g_image_cache[artist_name]["urls"] = clean
                save_image_cache(dict(_g_image_cache))
            if DEBUG: print(f"[{label}] Pool '{artist_name}': {len(clean)} images (cache, {int(age_s // 3600)}h old)")
            return clean

    # Cache miss or expired — fetch from Last.FM + AudioDB
    pool = getArtistImagePool(mbid, artist_name=artist_name)

    with _g_cache_lock:
        _g_image_cache[artist_name] = {"urls": pool, "fetched_at": time.time()}
        save_image_cache(dict(_g_image_cache))

    return pool


# ──────────────────────────────────────────────────────────────────────────────
# Discord API
# ──────────────────────────────────────────────────────────────────────────────

def discordPatch(app_id: str, bot_token: str, payload: dict, label: str = "") -> int:
    """PATCH the Discord profile widget for the given application."""
    try:
        r = requests.patch(
            url=f"https://discord.com/api/v9/applications/{app_id}/users/{USER_ID}/identities/0/profile",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bot {bot_token}",
                "User-Agent": "DiscordBot (https://github.com/discord/discord-api-docs, 1.0.0)",
            },
            data=json.dumps(payload, separators=(",", ":")),
            timeout=15,
        )
    except requests.exceptions.RequestException as exc:
        print(f"[{label}] Discord PATCH network error: {exc}")
        return 0

    remaining   = r.headers.get("X-RateLimit-Remaining", "?")
    limit       = r.headers.get("X-RateLimit-Limit", "?")
    reset_after = r.headers.get("X-RateLimit-Reset-After", "?")
    try:
        reset_str = f"{float(reset_after):.1f}s"
    except (ValueError, TypeError):
        reset_str = "?s"

    if r.status_code == 429:
        retry_after = float(r.headers.get("Retry-After", r.headers.get("X-RateLimit-Reset-After", 60)))
        print(f"[{label}] ⚠️  Rate limited — waiting {retry_after:.1f}s...")
        time.sleep(retry_after + 0.5)
    else:
        try:
            rem_int = int(remaining)
        except (ValueError, TypeError):
            rem_int = None
        # Always show when ≤1 remaining (bucket nearly exhausted) or in DEBUG mode
        if DEBUG or rem_int is None or rem_int <= 1:
            print(f"[{label}] Discord → {r.status_code} | Rate: {remaining}/{limit} remaining, resets in {reset_str}")

    return r.status_code


# ──────────────────────────────────────────────────────────────────────────────
# Listening Stats — data fetchers (stats.fm API v2)
# ──────────────────────────────────────────────────────────────────────────────

def getUserStats() -> dict:
    """Fetch lifetime listening stats from stats.fm (count, tracks, artists, albums, hours, minutes)."""
    stats = _sf_get_user_stats(
        STATSFM_USERNAME,
        api_base=STATSFM_API_BASE,
        headers=STATSFM_HEADERS,
    )
    if DEBUG:
        print(
            f"[LS] stats.fm lifetime: {stats['scrobbles']:,} streams | "
            f"{stats['totaltracks']:,} tracks | {stats['totalalbums']:,} albums | "
            f"{stats['totalartists']:,} artists | {stats['hoursstreamed']:,}h"
        )
    return stats


def getTopStat(type_: str, period: str, user_info: dict | None = None) -> tuple[str, str]:
    """Return (value_str, label_str) for a configurable Listening Stats slot.

    All slots are powered by stats.fm API v2 (no Last.FM API key needed):
      Static (no extra API call — reads from already-fetched user_info dict):
        scrobbles, totalalbums, totalartists, totaltracks,
        hoursstreamed, minutesstreamed
      stats.fm top* (one API call per unique type+period):
        topartist, toptrack, topalbum

    Returns ('\u2014', label) gracefully on error or missing config.
    """
    period_label = LS_PERIOD_LABELS.get(period, period)
    base_label   = LS_TYPE_LABELS.get(type_, type_)

    # ── Static lifetime types: read directly from getUserStats() dict ────────
    if type_ in (
        "scrobbles", "totalalbums", "totalartists", "totaltracks",
        "hoursstreamed", "minutesstreamed",
    ):
        count = (user_info or {}).get(type_)
        return (f"{count:,}" if count is not None else "—"), base_label

    # ── stats.fm top* types ──────────────────────────────────────────────────
    kind_map = {
        "topartist": ("artists", "artist"),
        "toptrack":  ("tracks",  "track"),
        "topalbum":  ("albums",  "album"),
    }
    entry = kind_map.get(type_)
    if not entry:
        return "—", base_label

    kind, item_key = entry
    label = f"{base_label} ({period_label})"
    try:
        items = _sf_get_top_items(
            STATSFM_USERNAME,
            kind=kind,
            period=period,
            limit=1,
            api_base=STATSFM_API_BASE,
            headers=STATSFM_HEADERS,
        )
        if not items or item_key not in items[0]:
            return "—", label
        name = items[0][item_key]["name"]
        if DEBUG: print(f"[LS] getTopStat {type_}/{period}: {name}")
        return name, label
    except Exception as exc:
        print(f"[LS] getTopStat error ({type_}/{period}): {exc}")
        return "—", label


# Valid Last.FM image hash: 32 hex chars (MD5).
# User-uploaded images that aren't from the gallery have descriptive filenames → skip them.
_VALID_LFM_HASH = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def getLastFMImagePool(artist_name: str) -> list:
    """Scrape the Last.FM /+images gallery for an artist and return a list of image URLs.

    - Source: artist name from Last.FM API (no disambiguation ambiguity)
    - Filter: only accepts 32-char MD5 hex hashes; skips named user uploads and blacklisted hashes
    - Multi-page: fetches additional pages up to POOL_MAX_IMAGES or POOL_MAX_PAGES
    - URLs: converted from avatar170s thumbnails to 770x0 full-res and stored as-is.
      Last.FM artist gallery images are already square/portrait — no crop needed.
      Direct URLs are used because wsrv.nl cannot reliably access Last.FM's Fastly CDN.
    """
    if not artist_name:
        return []

    encoded  = urllib.parse.quote_plus(artist_name)
    base_url = f"https://www.last.fm/music/{encoded}/+images"
    headers  = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer":         "https://www.last.fm/",
        "Sec-Fetch-Dest":  "document",
        "Sec-Fetch-Mode":  "navigate",
        "Sec-Fetch-Site":  "same-origin",
        "Sec-Fetch-User":  "?1",
        "Cache-Control":   "max-age=0",
        "Connection":      "keep-alive",
    }

    seen        = set()
    pool        = []
    total_pages = 1   # updated from pagination on the first page

    try:
        session = requests.Session()
        session.headers.update(headers)

        for page in range(1, POOL_MAX_PAGES + 1):
            if len(pool) >= POOL_MAX_IMAGES or page > total_pages:
                break

            url = base_url if page == 1 else f"{base_url}?page={page}"

            # Retry up to 3 times on 5xx errors (Last.FM occasionally returns 502)
            for attempt in range(3):
                r = session.get(url, timeout=8)
                if r.status_code < 500:
                    break
                if attempt < 2:
                    print(f"[LS] Last.FM {r.status_code} on page {page}, retry {attempt + 1}/3...")
                    time.sleep(2)

            # 406 = Last.FM rejected the scrape (anti-bot), skip gracefully
            if r.status_code == 406:
                if DEBUG: print(f"[LS] Last.FM 406 for '{artist_name}' page {page}, skipping")
                break

            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")


            # Detect total page count from pagination (only needed on the first page)
            if page == 1:
                page_links = soup.select("a.pagination-page")
                if page_links:
                    nums = [int(a.text) for a in page_links if a.text.strip().isdigit()]
                    if nums:
                        total_pages = max(nums)
                        if DEBUG: print(f"[LS] Last.FM '{artist_name}': {total_pages} pages available, fetching up to {POOL_MAX_PAGES}")

            before = len(pool)
            for img in soup.find_all("img"):
                src = img.get("src", "")
                if "avatar170s" not in src:
                    continue
                try:
                    img_hash = src.split("avatar170s/")[1].split("?")[0]
                except IndexError:
                    continue

                # Skip user uploads with descriptive filenames (not valid MD5 hashes)
                hash_name = img_hash.rsplit(".", 1)[0]
                if not _VALID_LFM_HASH.match(hash_name):
                    continue

                # Skip blacklisted hashes (images of the wrong person)
                if hash_name in BLACKLISTED_HASHES:
                    if DEBUG: print(f"[LS] Skipping blacklisted hash: {hash_name}")
                    continue

                if img_hash in seen:
                    continue
                seen.add(img_hash)

                full_url = src.replace("avatar170s", "770x0")
                wsrv_url = (
                    f"https://wsrv.nl/?url={full_url.replace('https://', '')}"
                    f"&w=300&h=300&fit=cover&a=entropy&output=jpg&q=80"
                )
                pool.append(wsrv_url)

                if len(pool) >= POOL_MAX_IMAGES:
                    break

            if DEBUG: print(f"[LS] Last.FM page {page}: +{len(pool) - before} valid images ({len(pool)}/{POOL_MAX_IMAGES})")

            if page < min(total_pages, POOL_MAX_PAGES) and len(pool) < POOL_MAX_IMAGES:
                time.sleep(0.5)   # polite delay between pages

        return pool
    except Exception as e:
        print(f"[LS] Last.FM scrape error for '{artist_name}': {e}")
        return pool if pool else []


def getArtistImagePool(mbid: str, artist_name: str = "") -> list:
    """Build an image pool for an artist (up to POOL_MAX_IMAGES, no duplicates).

    Priority:
    1. Last.FM photo gallery (primary) — scraped HTML, best quality & variety
    2. AudioDB (secondary)            — REST API, used when Last.FM pool is incomplete
    """
    pool = []
    seen = set()

    def add(urls):
        for u in urls:
            if u and u not in seen and len(pool) < POOL_MAX_IMAGES:
                seen.add(u)
                pool.append(u)

    # 1. Last.FM (primary)
    if artist_name:
        add(getLastFMImagePool(artist_name))

    # 2. AudioDB (secondary — only if pool is not yet full)
    if len(pool) < POOL_MAX_IMAGES and mbid:
        try:
            r       = requests.get(f"https://www.theaudiodb.com/api/v1/json/123/artist-mb.php?i={mbid}", timeout=5)
            data    = r.json()
            artists = data.get("artists")
            if artists:
                artist  = artists[0]
                thumb   = artist.get("strArtistThumb")
                fanarts = [
                    artist.get("strArtistFanart"),
                    artist.get("strArtistFanart2"),
                    artist.get("strArtistFanart3"),
                    artist.get("strArtistFanart4"),
                ]
                audiodb = []
                if thumb and thumb.strip():
                    audiodb.append(thumb)  # thumb — already portrait, no crop needed
                for u in fanarts:
                    if u and u.strip():
                        # AudioDB fanarts are landscape (1920×1080) — crop to square via wsrv.nl.
                        audiodb.append(
                            f"https://wsrv.nl/?url={u.replace('https://', '')}"
                            f"&w=300&h=300&fit=cover&a=entropy&output=jpg&q=80"
                        )
                before = len(pool)
                add(audiodb)
                if DEBUG: print(f"[LS] AudioDB for {artist.get('strArtist', mbid)}: +{len(pool) - before} images")
            else:
                if DEBUG: print(f"[LS] AudioDB: no data for MBID {mbid}")
        except Exception as e:
            print(f"[LS] AudioDB error: {e}")

    if DEBUG: print(f"[LS] Total pool for '{artist_name or mbid}': {len(pool)}/{POOL_MAX_IMAGES} images")
    return pool


def _warm_url(url: str) -> None:
    """Fire-and-forget: stream-open a URL to warm proxy/CDN caches.

    For wsrv.nl URLs: triggers processing + caching so Discord's widget renderer
    gets a fast (cached) response instead of a slow cold-cache delay.
    For direct CDN URLs: confirms accessibility and warms any CDN edge caches.
    No body is downloaded — just the response headers.
    """
    try:
        with requests.get(url, stream=True, timeout=8) as _:
            pass  # headers received; cache warmed
    except Exception:
        pass


def getRecentStream() -> tuple:
    """Fetch the currently playing or most recent track from stats.fm v2 API.

    Returns:
      (now_playing, banner_url, np_track, np_artist, primary_artist,
       np_album, statsfm_track_id, spotify_track_id, spotify_ids, artist_image)
    """
    now_playing, info = _sf_get_recent_stream(
        STATSFM_USERNAME,
        api_base=STATSFM_API_BASE,
        headers=STATSFM_HEADERS,
    )
    return (
        now_playing,
        info["banner_url"],
        info["track"],
        info["artist"],
        info["primary_artist"],
        info["album"],
        info["track_id"],
        info["spotify_id"],
        info["spotify_ids"],
        info["artist_image"],
    )



# ──────────────────────────────────────────────────────────────────────────────
# npcount helpers (track play-count + album subtitle)
# ──────────────────────────────────────────────────────────────────────────────

# Strips [Explicit] and (Explicit) tags from album titles.
_EXPLICIT_RE = re.compile(r'\s*[\[(]Explicit[\])]\s*', re.IGNORECASE)

# Strips release-type suffixes that Last.FM appends to album names for singles.
# e.g. "I'm Your Man - Single" → "I'm Your Man", "Heat Waves - EP" → "Heat Waves"
_SINGLE_SUFFIX_RE = re.compile(
    r'\s*[-\u2013\u2014]\s*'
    r'(?:Single|EP|Live|Acoustic|Remix(?:es)?|'
    r'Deluxe(?:\s+Edition)?|(?:Anniversary\s+)?Edition|Version)\s*$',
    re.IGNORECASE,
)


def _normalize_title(s: str) -> str:
    """Normalize Unicode apostrophes/quotes for comparison.

    Spotify scrobbles use curly right-single-quote \u2019 (’) while Last.FM’s
    database uses a straight apostrophe \u0027 ('). Without normalization,
    'I\u2019m Your Man' != 'I\u0027m Your Man' even though they look identical.
    """
    return (
        s.replace("\u2019", "'")
         .replace("\u2018", "'")
         .replace("\u201c", '"')
         .replace("\u201d", '"')
         .lower()
    )


def getTrackPlayCount(
    artist: str,
    track: str,
    track_id: int | None = None,
    spotify_track_id: str | None = None,
) -> int:
    """Fetch per-user lifetime play count for a track via stats.fm v2 API.

    Uses the numeric stats.fm track ID directly when available, or resolves it
    via stats.fm track search (for tracks detected early by Lanyard/Spotify).
    Returns 0 on lookup failure (rendered as '1st Play').
    """
    try:
        return _sf_get_track_play_count(
            STATSFM_USERNAME,
            track_id=track_id,
            title=track,
            artist=artist,
            spotify_track_id=spotify_track_id,
            api_base=STATSFM_API_BASE,
            headers=STATSFM_HEADERS,
        )
    except Exception as exc:
        print(f"[LS] stats.fm track stats error: {exc}")
        return 0



def _format_count(n: int) -> str:
    """Abbreviate a play count: 1200 → '1.2k', 19000 → '19k', 1_200_000 → '1.2M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{round(n / 1_000)}k"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def format_npcount(count: int, album_title, track_name: str) -> str:
    """Build the npcount subtitle string.

    NPCOUNT_SPLIT_LINES = True  → two-line format using \\n:
      '21 Scrobbles\\nDaughter From Hell'

    NPCOUNT_SPLIT_LINES = False → single-line with bullet + dynamic truncation:
      '21 Scrobbles • Daughter from H...'

    Special cases (both modes):
      count=0  → '1st Scrobble'
      count=1  → '1 Scrobble'
      no album / album == track name (single) → just the count string
    """
    count_str = "1st Play" if count == 0 else f"{_format_count(count)} {'Play' if count == 1 else 'Plays'}"

    # No album to show (absent or it's a single — album title == track title).
    # 1. Strip release-type suffixes: "I'm Your Man - Single" → "I'm Your Man"
    # 2. Use Unicode-normalized comparison: Spotify curly apostrophe \u2019 (’)
    #    must match Last.FM straight apostrophe \u0027 (') to avoid false mismatches.
    if album_title:
        album_title = _SINGLE_SUFFIX_RE.sub("", album_title).strip()
    if not album_title or _normalize_title(album_title) == _normalize_title(track_name):
        return count_str

    if NPCOUNT_SPLIT_LINES:
        # Two-line mode: Discord renders \n as a visual line break in subtitle fields.
        # If Discord strips newlines, the two parts will appear on one line instead —
        # not ideal, but not broken. Truncate very long album names at ~30 chars.
        MAX_ALBUM_SPLIT = 30
        album_display = (
            album_title[:MAX_ALBUM_SPLIT - 3] + "..."
            if len(album_title) > MAX_ALBUM_SPLIT
            else album_title
        )
        return f"{count_str}\n{album_display}"

    # Single-line mode: dynamic allocation so total ≤ NPCOUNT_TOTAL_MAX_LEN chars.
    separator = " • "
    budget    = NPCOUNT_TOTAL_MAX_LEN - len(count_str) - len(separator)
    if budget < 4:
        return count_str

    album_display = (
        album_title[:budget - 3] + "..."  # -3 accounts for the "..." suffix
        if len(album_title) > budget
        else album_title
    )
    return f"{count_str}{separator}{album_display}"






def getTopArtists(range_: str = "lifetime", limit: int = 5) -> list:
    """Fetch the top N artists for the configured stats.fm user (API v2)."""
    return _sf_get_top_items(
        STATSFM_USERNAME,
        kind="artists",
        period=range_,
        limit=limit,
        api_base=STATSFM_API_BASE,
        headers=STATSFM_HEADERS,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Thread: Listening Stats
# ──────────────────────────────────────────────────────────────────────────────

def run_listening_stats() -> None:
    """Continuously poll stats.fm (+ Lanyard/Spotify), then PATCH the Listening Stats widget."""
    cached_slow     = None
    last_slow_fetch = 0
    prev_payload    = None
    prev_npartist   = None
    prev_nptrack    = None
    cached_pool:     list  = []
    banner_mini_url         = None
    prev_npcount_key:  tuple = ()    # (display_track, display_artist) — re-fetch on change
    cached_npcount:    str   = ""    # last formatted npcount string
    cached_npcount_val: int  = -1    # last numeric play count (-1 = unfetched)

    # ─ Art / live-playback priority chain state ─────────────────────────────
    prev_art_key:         tuple      = ()
    prev_live_url:        str | None = None
    _spotify_lp_fetched:  bool       = False # True = tried Spotify recently-played for this track
    cached_lp_url:        str | None = None  # matched Spotify recently-played art
    cached_lp_album:      str | None = None
    cached_lp_song:       str | None = None
    cached_lp_artist:     str | None = None
    cached_lp_sp_id:      str | None = None
    prev_search_key:      tuple      = ()    # (display_track, display_artist) for Spotify search
    search_fetched:       bool       = False # True = tried Spotify search for current display track
    cached_spotify_url:   str | None = None

    print("[LS] Thread started")

    while True:
        now = time.time()
        try:
            # --- Slow data: refresh every LS_SLOW_INTERVAL seconds ---
            if cached_slow is None or (now - last_slow_fetch) >= LS_SLOW_INTERVAL:
                ui = getUserStats()
                # Fetch all slots — deduplicate API calls for identical type+period.
                _slot_cfgs = [
                    (LS_STAT1_TYPE, LS_STAT1_PERIOD),
                    (LS_STAT2_TYPE, LS_STAT2_PERIOD),
                    (LS_STAT3_TYPE, LS_STAT3_PERIOD),
                    (LS_STAT4_TYPE, LS_STAT4_PERIOD),
                    (LS_STAT5_TYPE, LS_STAT5_PERIOD),
                    (LS_STAT6_TYPE, LS_STAT6_PERIOD),
                    (LS_MINI_TYPE,  LS_MINI_PERIOD),
                ]
                _sc: dict = {}
                for _t, _p in _slot_cfgs:
                    if (_t, _p) not in _sc:
                        _sc[(_t, _p)] = getTopStat(_t, _p, ui)

                stat1_val, stat1_label = _sc[(LS_STAT1_TYPE, LS_STAT1_PERIOD)]
                stat2_val, stat2_label = _sc[(LS_STAT2_TYPE, LS_STAT2_PERIOD)]
                stat3_val, stat3_label = _sc[(LS_STAT3_TYPE, LS_STAT3_PERIOD)]
                stat4_val, stat4_label = _sc[(LS_STAT4_TYPE, LS_STAT4_PERIOD)]
                stat5_val, stat5_label = _sc[(LS_STAT5_TYPE, LS_STAT5_PERIOD)]
                stat6_val, stat6_label = _sc[(LS_STAT6_TYPE, LS_STAT6_PERIOD)]
                mini_val,  mini_label  = _sc[(LS_MINI_TYPE,  LS_MINI_PERIOD)]

                # lsmini: self-contained string (value + label combined, no separate label field)
                if LS_MINI_TYPE in ("topartist", "toptrack", "topalbum"):
                    lsmini = f"{mini_label}: {mini_val}"  # e.g. "Top Artist (30-Day): Holly Humberstone"
                else:
                    lsmini = f"{mini_val} {mini_label}"   # e.g. "28,745 Total Songs"

                cached_slow = (
                    stat1_val, stat1_label, stat2_val, stat2_label,
                    stat3_val, stat3_label, stat4_val, stat4_label,
                    stat5_val, stat5_label, stat6_val, stat6_label,
                    lsmini,
                )
                last_slow_fetch = now
                if DEBUG: print("[LS] Slow data refreshed")
            else:
                (stat1_val, stat1_label, stat2_val, stat2_label,
                 stat3_val, stat3_label, stat4_val, stat4_label,
                 stat5_val, stat5_label, stat6_val, stat6_label,
                 lsmini) = cached_slow

            # --- Fast data: every LS_FAST_INTERVAL seconds (stats.fm v2 API) ---
            (now_playing, statsfm_banner_url, np_track, np_artist,
             primary_artist, np_album, sf_track_id, sp_track_id,
             sf_spotify_ids, sf_artist_img) = getRecentStream()

            # ─── Now Playing verification + Art priority chain ──────────────────────────────
            # Priority 1: Lanyard  — real-time Discord presence, no auth, free (checked every poll).
            # Priority 2: Spotify  — currently-playing API (verifies active playback).
            # Priority 3: Spotify  — recently-played (Last Played, matched by Spotify ID or title).
            # Priority 4: Spotify  — search (art fallback, Client Credentials only).
            # Priority 5: stats.fm — final art fallback for bannerwidgettop ONLY (never for bannermini).
            art_key = (sf_track_id or np_track, np_artist)
            if art_key != prev_art_key:
                _spotify_lp_fetched = False
                cached_lp_url       = None
                cached_lp_album     = None
                cached_lp_song      = None
                cached_lp_artist    = None
                cached_lp_sp_id     = None
                prev_art_key        = art_key
                if DEBUG: print(f"[LS] Art cache cleared: {np_track}")

            live_url:    str | None = None
            live_album:  str | None = None
            live_song:   str | None = None
            live_artist: str | None = None
            live_sp_id:  str | None = None

            # ── Priority 1 — Lanyard (real-time Discord presence) ────────────────────
            if _lanyard_available:
                _art, _lanyard_album, _lanyard_song, _lanyard_artist, _lanyard_sp_id = _get_lanyard_art(USER_ID)
                if _lanyard_song:
                    # Lanyard confirms: user IS actively streaming Spotify via Discord.
                    now_playing = "Now Playing"
                    live_url    = _art
                    live_album  = _lanyard_album
                    live_song   = _lanyard_song
                    live_artist = _lanyard_artist
                    live_sp_id  = _lanyard_sp_id
                    if _art and _art != prev_live_url:
                        print("[LS] bannerwidgettop: Lanyard")
                elif now_playing == "Now Playing":
                    # stats.fm says Now Playing but Lanyard says not playing.
                    # Verify with Spotify currently-playing before accepting the override.
                    if _spotify_user_available:
                        _is_playing, _sp_song, _sp_artist, _sp_album, _sp_art, _sp_id = \
                            _get_spotify_currently_playing(
                                SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
                            )
                        if _is_playing and _sp_song:
                            now_playing = "Now Playing"
                            live_url    = _sp_art
                            live_album  = _sp_album
                            live_song   = _sp_song
                            live_artist = _sp_artist
                            live_sp_id  = _sp_id
                            if _sp_art and _sp_art != prev_live_url:
                                print(f"[LS] Now Playing: Spotify → {_sp_song}")
                        else:
                            now_playing = "Last Played"
                            print("[LS] Now Playing → Last Played (Lanyard + Spotify inactive)")
            elif now_playing == "Now Playing" and _spotify_user_available:
                _is_playing, _sp_song, _sp_artist, _sp_album, _sp_art, _sp_id = \
                    _get_spotify_currently_playing(
                        SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
                    )
                if _is_playing and _sp_song:
                    now_playing = "Now Playing"
                    live_url    = _sp_art
                    live_album  = _sp_album
                    live_song   = _sp_song
                    live_artist = _sp_artist
                    live_sp_id  = _sp_id
                    if _sp_art and _sp_art != prev_live_url:
                        print(f"[LS] Now Playing: Spotify → {_sp_song}")
                else:
                    now_playing = "Last Played"
                    print("[LS] Now Playing → Last Played (Spotify inactive)")

            prev_live_url = live_url

            # ── Priority 3 — Spotify recently-played (Last Played direct cover art) ──
            # Queried once per Last Played track and accepted when Spotify Track ID
            # matches stats.fm (fixes placeholder titles like 'Track 2' / 'Unknown' artist)
            # OR when the normalized track title matches.
            if (now_playing == "Last Played"
                    and live_song is None
                    and _spotify_user_available
                    and not _spotify_lp_fetched):
                _sp_song, _sp_artist, _sp_album, _sp_art, _sp_id = _get_spotify_recent(
                    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
                )
                _spotify_lp_fetched = True
                _lp_matches = bool(
                    (_sp_id and _sp_id in sf_spotify_ids)
                    or (_sp_song and _sf_norm(_sp_song) == _sf_norm(np_track))
                )
                if _sp_song and _lp_matches:
                    cached_lp_url    = _sp_art
                    cached_lp_album  = _sp_album
                    cached_lp_song   = _sp_song
                    cached_lp_artist = _sp_artist
                    cached_lp_sp_id  = _sp_id
                    print(f"[LS] Last Played: Spotify recently-played → {_sp_song} — {_sp_artist}")
                elif _sp_song and DEBUG:
                    print(f"[LS] Spotify recently-played mismatch ('{_sp_song}' != '{np_track}'), skipped")

            # Active override: live playback (Lanyard/Spotify) takes precedence over cached LP
            cached_lanyard_url    = live_url    if live_song else cached_lp_url
            cached_lanyard_album  = live_album  if live_song else cached_lp_album
            cached_lanyard_song   = live_song   if live_song else cached_lp_song
            cached_lanyard_artist = live_artist if live_song else cached_lp_artist
            effective_sp_id       = live_sp_id  if live_song else (cached_lp_sp_id or sp_track_id)

            # Resolve effective track/artist for consistent display, bannermini, and npcount
            display_track            = cached_lanyard_song   or np_track
            display_artist           = cached_lanyard_artist or np_artist
            effective_primary_artist = (
                cached_lanyard_artist.split(",")[0].strip()
                if cached_lanyard_artist else primary_artist
            )
            same_as_sf = bool(
                (effective_sp_id and effective_sp_id in sf_spotify_ids)
                or (_sf_norm(display_track) == _sf_norm(np_track))
            )
            print(f"[LS] Status: {now_playing} | {display_track} — {display_artist}")

            # ── Priority 4 — Spotify search (direct Spotify cover art fallback) ──────
            search_key = (display_track, display_artist)
            if search_key != prev_search_key:
                search_fetched     = False
                cached_spotify_url = None
                prev_search_key    = search_key

            if cached_lanyard_url is None and not search_fetched and _spotify_available:
                _art = _get_spotify_art(
                    display_track,
                    display_artist,
                    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
                )
                cached_spotify_url = _art
                search_fetched     = True
                if _art:
                    print("[LS] bannerwidgettop: Spotify search")

            # Direct cover art strictly from Lanyard or Spotify (NEVER stats.fm)
            direct_cover_art = cached_lanyard_url or cached_spotify_url

            # Resolve bannerwidgettop URL: Lanyard/Spotify direct → stats.fm last-resort fallback
            banner_url = direct_cover_art
            if banner_url is None and statsfm_banner_url and same_as_sf:
                banner_url = statsfm_banner_url
                if DEBUG: print("[LS] bannerwidgettop: stats.fm (fallback)")
            # ──────────────────────────────────────────────────────────────────────────

            # --- bannerwidgettop: resolve final URL ---
            fixed_banner_url = banner_url
            if banner_url:
                if IMGFIXER_ENABLED and _imgfixer_available and DISCORD_IMAGE_WEBHOOK_URL:
                    fixed_banner_url = _fix_banner_url(
                        banner_url,
                        DISCORD_IMAGE_WEBHOOK_URL,
                        reupload_interval=IMGFIXER_REUPLOAD_INTERVAL,
                    )
                elif any(h in banner_url for h in ("lastfm", "last.fm")):
                    clean_url        = banner_url.replace("https://", "")
                    fixed_banner_url = f"https://wsrv.nl/?url={clean_url}&output=jpg&q=85"

            # --- Artist image (bannermini) ---
            # When the artist has no image in cache/gallery, fall back to the song's
            # cover art from Lanyard or Spotify directly (never stats.fm).
            if ARTIST_IMAGE_ENABLED:
                artist_changed = (effective_primary_artist != prev_npartist)
                mini_fallback  = direct_cover_art or AUDIODB_FALLBACK_URL
                if artist_changed:
                    cached_pool   = get_pool_cached(effective_primary_artist, label="LS")
                    prev_npartist = effective_primary_artist
                    if cached_pool:
                        banner_mini_url = random.choice(cached_pool)
                    else:
                        banner_mini_url = mini_fallback
                        if direct_cover_art:
                            print("[LS] bannermini: no artist image → using Lanyard/Spotify cover art")
                    print(f"[LS] New artist: {effective_primary_artist}")
                    print(f"[LS] bannermini: {banner_mini_url}")
                    if banner_mini_url:
                        threading.Thread(
                            target=_warm_url, args=(banner_mini_url,), daemon=True
                        ).start()
                elif display_track != prev_nptrack:
                    if cached_pool:
                        alt_pool = [u for u in cached_pool if u != banner_mini_url]
                        banner_mini_url = random.choice(alt_pool if alt_pool else cached_pool)
                    else:
                        banner_mini_url = mini_fallback
                        if direct_cover_art:
                            print("[LS] bannermini: no artist image → updated to Lanyard/Spotify cover art")
                    print(f"[LS] bannermini: {banner_mini_url}")
                    if banner_mini_url:
                        threading.Thread(
                            target=_warm_url, args=(banner_mini_url,), daemon=True
                        ).start()
                elif not cached_pool and mini_fallback != banner_mini_url:
                    banner_mini_url = mini_fallback
                    print(f"[LS] bannermini: cover art synced → {banner_mini_url}")
                    if banner_mini_url:
                        threading.Thread(
                            target=_warm_url, args=(banner_mini_url,), daemon=True
                        ).start()
                prev_nptrack = display_track
            else:
                banner_mini_url = AUDIODB_FALLBACK_URL

            # --- npcount: per-track play count + album title (stats.fm v2 API) ---
            npcount_key = (display_track, display_artist, effective_sp_id)
            should_refresh_npcount = (
                npcount_key != prev_npcount_key
                or (cached_npcount_val == 0 and same_as_sf and sf_track_id)
            )
            if should_refresh_npcount:
                count = getTrackPlayCount(
                    effective_primary_artist,
                    display_track,
                    track_id=(sf_track_id if same_as_sf else None),
                    spotify_track_id=effective_sp_id,
                )
                scrobble_album = (
                    _EXPLICIT_RE.sub("", np_album).strip()
                    if (np_album and same_as_sf) else ""
                )
                if DEBUG: print(
                    f"[LS] npcount raw → count={count}"
                    f" | sp_id={effective_sp_id}"
                    f" | same_as_sf={same_as_sf}"
                    f" | lanyard='{cached_lanyard_album}'"
                    f" | statsfm='{scrobble_album}'"
                )

                if cached_lanyard_album:
                    album_title = cached_lanyard_album
                else:
                    album_title = scrobble_album

                cached_npcount     = format_npcount(count, album_title, display_track)
                cached_npcount_val = count
                prev_npcount_key   = npcount_key
                print(f"[LS] npcount: {cached_npcount}")

            # Expose current artist info to the TA thread
            with _shared_lock:
                _shared["artist_name"]    = effective_primary_artist
                _shared["artist_pool"]    = list(cached_pool)
                _shared["bannermini_url"] = banner_mini_url

            # --- Push to Discord only when something has changed ---
            current_payload = (
                now_playing, fixed_banner_url, display_track, display_artist, banner_mini_url,
                stat1_val, stat2_val, stat3_val, stat4_val, stat5_val, stat6_val,
                stat1_label, stat2_label, stat3_label, stat4_label, stat5_label, stat6_label,
                lsmini, cached_npcount,
            )

            if current_payload != prev_payload:
                dynamic = []
                if fixed_banner_url:
                    if IMGFIXER_ENABLED and _imgfixer_available:
                        print("[LS] bannerwidgettop: imgfixer → Discord CDN")
                    elif "wsrv.nl" in fixed_banner_url:
                        print("[LS] bannerwidgettop: wsrv.nl proxy → OK")
                    else:
                        print("[LS] bannerwidgettop: direct URL → OK")
                else:
                    print("[LS] bannerwidgettop: absent (no album art) → Discord will show fallback")
                if fixed_banner_url:
                    dynamic.append({"type": 3, "name": "bannerwidgettop", "value": {"url": fixed_banner_url}})
                dynamic += [
                    {"type": 1, "name": "nowplaying",  "value": now_playing},
                    {"type": 1, "name": "nptrack",     "value": display_track},
                    {"type": 1, "name": "npartist",    "value": display_artist},
                    {"type": 1, "name": "npcount",     "value": cached_npcount},
                    # Stat values (configurable via config.py LS_STAT*_TYPE / LS_STAT*_PERIOD)
                    {"type": 1, "name": "lsstat1",     "value": stat1_val},
                    {"type": 1, "name": "lsstat2",     "value": stat2_val},
                    {"type": 1, "name": "lsstat3",     "value": stat3_val},
                    {"type": 1, "name": "lsstat4",     "value": stat4_val},
                    {"type": 1, "name": "lsstat5",     "value": stat5_val},
                    {"type": 1, "name": "lsstat6",     "value": stat6_val},
                    # Stat labels (auto-generated from type + period)
                    {"type": 1, "name": "lslabel1",    "value": stat1_label},
                    {"type": 1, "name": "lslabel2",    "value": stat2_label},
                    {"type": 1, "name": "lslabel3",    "value": stat3_label},
                    {"type": 1, "name": "lslabel4",    "value": stat4_label},
                    {"type": 1, "name": "lslabel5",    "value": stat5_label},
                    {"type": 1, "name": "lslabel6",    "value": stat6_label},
                    # Mini profile stat (combined value + label string)
                    {"type": 1, "name": "lsmini",      "value": lsmini},
                ]
                if banner_mini_url:
                    dynamic.append({"type": 3, "name": "bannermini", "value": {"url": banner_mini_url}})

                status = discordPatch(LS_APPLICATION_ID, LS_BOT_TOKEN, {"data": {"dynamic": dynamic}}, "LS")
                if 200 <= status < 300:
                    prev_payload = current_payload
                else:
                    print(f"[LS] PATCH failed (status={status}) — will retry on next poll")
            else:
                if DEBUG: print("[LS] No changes, skip")

        except Exception as e:
            print(f"[LS] ERROR: {e}")

        time.sleep(LS_FAST_INTERVAL)


# ──────────────────────────────────────────────────────────────────────────────
# Thread: Top Artists
# ──────────────────────────────────────────────────────────────────────────────

def run_top_artists() -> None:
    """Continuously fetch top artists from stats.fm and PATCH the Top Artists widget.

    When rotation is ON, cycles through All Time → 6 Months → 30 Days every
    ROTATION_INTERVAL seconds, picking a fresh random image from each artist's
    pool on every step.
    """
    print("[TA] Thread started")

    try:
        rotation_idx = ROTATION_ORDER.index(TOPARTISTS_RANGE)
    except ValueError:
        rotation_idx = 0

    prev_static_payload = None
    prev_top_names: list = []
    artist_pools: dict   = {}   # name → list[url]  (in-memory shortcut into the global cache)
    artist_chosen_imgs: dict = {}   # name → chosen URL for this rotation step

    while True:
        current_range = ROTATION_ORDER[rotation_idx] if TOPARTISTS_ROTATE else TOPARTISTS_RANGE
        range_label   = ROTATION_LABELS.get(current_range, current_range)

        try:
            items     = getTopArtists(range_=current_range, limit=5)
            top_names = [item["artist"]["name"] for item in items]

            # Load image pools for any newly-seen artists (uses global cache first)
            if top_names != prev_top_names:
                new_artists = [n for n in top_names if n not in artist_pools]
                if new_artists:
                    print(f"[TA] {len(new_artists)} new artist(s) detected, loading pools...")
                    for name in new_artists:
                        pool = get_pool_cached(name, label="TA")
                        artist_pools[name] = pool
                        if new_artists.index(name) < len(new_artists) - 1:
                            time.sleep(0.5)
                prev_top_names = top_names

            # Pick a fresh image for each artist on every rotation step.
            # Avoid using the same image as the current bannermini.
            with _shared_lock:
                shared_mini = _shared["bannermini_url"]

            for name in top_names:
                pool     = artist_pools.get(name, [])
                alt_pool = [u for u in pool if u != shared_mini]
                chosen   = random.choice(alt_pool) if alt_pool else (pool[0] if pool else None)
                if chosen:
                    artist_chosen_imgs[name] = chosen
                else:
                    artist_chosen_imgs.pop(name, None)   # let Discord use the stats.fm fallback

            dynamic = []
            for i, item in enumerate(items, start=1):
                artist      = item["artist"]
                minutes     = item["playedMs"] // 60_000
                artist_name = artist["name"]

                genres    = artist.get("genres", [])
                genre_str = ", ".join(g.title() for g in genres[:3])

                # Use Last.FM pool image when available, fall back to stats.fm (Spotify CDN)
                img_url = artist_chosen_imgs.get(artist_name) or artist.get("image", "")

                # Pre-warm the wsrv.nl TA image so Discord gets a fast cached response
                if img_url:
                    threading.Thread(target=_warm_url, args=(img_url,), daemon=True).start()


                title = f"#{i} {artist_name} ({range_label})" if i == 1 else f"#{i} {artist_name}"

                dynamic.append({"type": 3, "name": f"{i}artistimg",     "value": {"url": img_url}})
                dynamic.append({"type": 1, "name": f"{i}artisttitle",   "value": title})
                dynamic.append({"type": 1, "name": f"{i}minutesplayed", "value": f"{minutes:,} Minutes Listened"})

                if i == 1 and genre_str:
                    dynamic.append({"type": 1, "name": "1genre", "value": genre_str})

            # Subtitle 3: configurable profile link or custom text from config.py
            _ta_sub3 = TA_SUBTITLE3 or (f"stats.fm/{STATSFM_USERNAME}" if STATSFM_USERNAME else "")
            if _ta_sub3:
                dynamic.append({"type": 1, "name": "tasubtitle3", "value": _ta_sub3})

            if TOPARTISTS_ROTATE:
                print(f"[TA] Rotation → {range_label}")
                discordPatch(TA_APPLICATION_ID, TA_BOT_TOKEN, {"data": {"dynamic": dynamic}}, "TA")
                rotation_idx = (rotation_idx + 1) % len(ROTATION_ORDER)
                time.sleep(ROTATION_INTERVAL)
            else:
                fp = tuple((item["artist"]["name"], item["playedMs"]) for item in items)
                if fp != prev_static_payload:
                    print(f"[TA] Data changed, pushing | range={current_range}")
                    status = discordPatch(TA_APPLICATION_ID, TA_BOT_TOKEN, {"data": {"dynamic": dynamic}}, "TA")
                    if 200 <= status < 300:
                        prev_static_payload = fp
                    else:
                        print(f"[TA] PATCH failed (status={status}) — will retry on next poll")
                else:
                    if DEBUG: print("[TA] No changes, skip")
                time.sleep(STATIC_INTERVAL)

        except Exception as e:
            print(f"[TA] ERROR: {e}")
            time.sleep(ROTATION_INTERVAL if TOPARTISTS_ROTATE else STATIC_INTERVAL)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Ensure Unicode characters (→, —, etc.) display correctly on Windows.
    # Without this, cp1252 terminals raise UnicodeEncodeError on non-ASCII glyphs.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ta_mode = (
        f"rotate=ON | {' → '.join(ROTATION_LABELS[r] for r in ROTATION_ORDER)} "
        f"| {ROTATION_INTERVAL}s/step | start={ROTATION_LABELS.get(TOPARTISTS_RANGE, TOPARTISTS_RANGE)}"
        if TOPARTISTS_ROTATE else
        f"rotate=OFF | range={ROTATION_LABELS.get(TOPARTISTS_RANGE, TOPARTISTS_RANGE)} | refresh={STATIC_INTERVAL}s"
    )

    # ── Widget toggle validation ───────────────────────────────────────────────
    effective_ls = ENABLE_LISTENING_STATS
    effective_ta = ENABLE_TOP_ARTISTS

    if effective_ta and not STATSFM_USERNAME:
        print("[TA] WARNING: STATSFM_USERNAME not set → Top Artists widget disabled")
        effective_ta = False

    if effective_ta and not (TA_APPLICATION_ID and TA_BOT_TOKEN):
        print("[TA] WARNING: TOPARTISTS_APPLICATION_ID or TOPARTISTS_BOT_TOKEN not set → Top Artists widget disabled")
        effective_ta = False

    if effective_ls and not STATSFM_USERNAME:
        print("[LS] WARNING: STATSFM_USERNAME not set → Listening Stats widget disabled")
        effective_ls = False

    if effective_ls and not (LS_APPLICATION_ID and LS_BOT_TOKEN):
        print("[LS] WARNING: APPLICATION_ID or BOT_TOKEN not set → Listening Stats widget disabled")
        effective_ls = False

    if not effective_ls and not effective_ta:
        print("[ERROR] Both widgets are disabled. Set at least one to True in config.py.")
        sys.exit(1)

    # ── Startup banner ───────────────────────────────────────────────────────────
    ls_status = (
        f"✓ fast={LS_FAST_INTERVAL}s / slow={LS_SLOW_INTERVAL}s (stats.fm)"
        if effective_ls else
        "✗ disabled"                       if not ENABLE_LISTENING_STATS else
        "✗ STATSFM_USERNAME not set"       if not STATSFM_USERNAME else
        "✗ APPLICATION credentials missing"
    )
    ta_status = (
        f"✓ {ta_mode}"     if effective_ta else
        "✗ disabled"      if not ENABLE_TOP_ARTISTS else
        "✗ STATSFM_USERNAME not set"       if not STATSFM_USERNAME else
        "✗ TOPARTISTS credentials missing"
    )

    print("=" * 55)
    print("   WidgetFM — Starting")
    print(f"   Listening Stats : {ls_status}")
    print(f"   Top Artists     : {ta_status}")
    print("=" * 55)

    # Pre-load the image cache so both threads can serve from it immediately
    _g_image_cache.update(load_image_cache())
    print(f"[Cache] Global cache ready: {len(_g_image_cache)} artist(s) cached")

    active_workers = {}
    if effective_ls:
        active_workers["ListeningStats"] = run_listening_stats
    if effective_ta:
        active_workers["TopArtists"] = run_top_artists

    running_threads = {}
    for name, target_fn in active_workers.items():
        t = threading.Thread(target=target_fn, name=name, daemon=True)
        t.start()
        running_threads[name] = t

    try:
        while True:
            time.sleep(5)
            for name, target_fn in active_workers.items():
                t = running_threads.get(name)
                if t is None or not t.is_alive():
                    print(f"[WATCHDOG] ⚠ Thread '{name}' stopped unexpectedly! Auto-restarting...")
                    new_t = threading.Thread(target=target_fn, name=name, daemon=True)
                    new_t.start()
                    running_threads[name] = new_t
    except KeyboardInterrupt:
        print("\n[EXIT] Stopped by user (Ctrl+C).")
