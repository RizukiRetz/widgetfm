import re, requests, json, sys, time, random, threading, urllib.parse
from bs4 import BeautifulSoup
from config import *

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
            get_queue_album_art  as _get_spotify_queue_art,
            get_spotify_album_art as _get_spotify_art,
        )
        _spotify_available = True
        if SPOTIFY_REFRESH_TOKEN:
            _spotify_user_available = True
            print("[Spotify] currently-playing + Search enabled")
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
        from lanyardfetch import get_lanyard_album_art as _get_lanyard_art
        _lanyard_available = True
        print("[Lanyard] Album art source enabled (priority 1)")
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
        if cached.get("urls") and age_s < IMAGE_CACHE_TTL_DAYS * 86400:
            # Retroactively filter any blacklisted URLs that slipped in earlier
            clean = [u for u in cached["urls"] if not _is_blacklisted(u)]
            if len(clean) < len(cached["urls"]):
                removed = len(cached["urls"]) - len(clean)
                print(f"[{label}] Removed {removed} blacklisted URL(s) from cached pool for '{artist_name}'")
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
    r = requests.patch(
        url=f"https://discord.com/api/v9/applications/{app_id}/users/{USER_ID}/identities/0/profile",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bot {bot_token}",
            "User-Agent": "DiscordBot (https://github.com/discord/discord-api-docs, 1.0.0)",
        },
        data=json.dumps(payload, separators=(",", ":")),
    )

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
# Listening Stats — data fetchers
# ──────────────────────────────────────────────────────────────────────────────

def getUserInfo() -> tuple:
    """Fetch total scrobbles, artists, albums and tracks from Last.FM."""
    r    = requests.get(f"http://ws.audioscrobbler.com/2.0/?method=user.getinfo&user={LAST_FM_USERNAME}&api_key={API_KEY}&format=json")
    data = r.json()
    return (
        int(data["user"]["playcount"]),
        int(data["user"]["artist_count"]),
        int(data["user"]["album_count"]),
        int(data["user"]["track_count"]),
    )


def getStreamStats() -> tuple:
    """Fetch total hours and minutes streamed from stats.fm (lifetime).
    Returns (None, None) when STATSFM_USERNAME is not configured.
    """
    if not STATSFM_USERNAME:
        return None, None
    r    = requests.get(
        f"https://api.stats.fm/api/v1/users/{STATSFM_USERNAME}/streams/stats?range=lifetime",
        headers=STATSFM_HEADERS, timeout=10,
    )
    data = r.json()
    if "items" not in data:
        print(f"[LS] Unexpected stats.fm response: {data}")
        raise KeyError(f"'items' key missing. Keys present: {list(data.keys())}")
    duration_ms      = data["items"]["durationMs"]
    hours_streamed   = duration_ms // 3_600_000
    minutes_streamed = duration_ms // 60_000
    if DEBUG: print(f"[LS] stats.fm: {hours_streamed:,} hours | {minutes_streamed:,} minutes")
    return hours_streamed, minutes_streamed

def getTopStat(type_: str, period: str, user_info: dict | None = None) -> tuple[str, str]:
    """Return (value_str, label_str) for a configurable Listening Stats slot.

    Handles all configurable types:
      Static (no API call — reads from user_info dict):
        scrobbles, totalalbums, totalartists, totaltracks
      Last.FM top* (one API call per unique type+period):
        topartist, toptrack, topalbum
      stats.fm (optional — requires STATSFM_USERNAME):
        hoursstreamed, minutesstreamed

    Returns ('\u2014', label) gracefully on error or missing config.
    """
    period_label = LS_PERIOD_LABELS.get(period, period)
    base_label   = LS_TYPE_LABELS.get(type_, type_)

    # ── Static types: read from already-fetched getUserInfo() data ─────────────
    if type_ in ("scrobbles", "totalalbums", "totalartists", "totaltracks"):
        count = (user_info or {}).get(type_, 0)
        return f"{count:,}", base_label

    # ── stats.fm types (optional) ───────────────────────────────────────────
    if type_ in ("hoursstreamed", "minutesstreamed"):
        if not STATSFM_USERNAME:
            return "—", base_label
        h, m = getStreamStats()
        val = h if type_ == "hoursstreamed" else m
        return (f"{val:,}" if val is not None else "—"), base_label

    # ── Last.FM top* types ───────────────────────────────────────────────
    method_map = {
        "topartist": "user.gettopartists",
        "toptrack":  "user.gettoptracks",
        "topalbum":  "user.gettopalbums",
    }
    method = method_map.get(type_)
    if not method:
        return "—", base_label

    label = f"{base_label} ({period_label})"
    try:
        r    = requests.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method":  method,
                "user":    LAST_FM_USERNAME,
                "api_key": API_KEY,
                "format":  "json",
                "limit":   1,
                "period":  period,
            },
            timeout=10,
        )
        data = r.json()
        if type_ == "topartist":
            name = data["topartists"]["artist"][0]["name"]
        elif type_ == "toptrack":
            name = data["toptracks"]["track"][0]["name"]
        else:  # topalbum
            name = data["topalbums"]["album"][0]["name"]
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
    - URLs: converted from avatar170s thumbnails to 770x0 full-res, then proxied
      through wsrv.nl for square crop + JPEG compression (300×300, q=80)
    """
    if not artist_name:
        return []

    encoded  = urllib.parse.quote_plus(artist_name)
    base_url = f"https://www.last.fm/music/{encoded}/+images"
    headers  = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    seen        = set()
    pool        = []
    total_pages = 1   # updated from pagination on the first page

    try:
        for page in range(1, POOL_MAX_PAGES + 1):
            if len(pool) >= POOL_MAX_IMAGES or page > total_pages:
                break

            url = base_url if page == 1 else f"{base_url}?page={page}"

            # Retry up to 3 times on 5xx errors (Last.FM occasionally returns 502)
            for attempt in range(3):
                r = requests.get(url, headers=headers, timeout=8)
                if r.status_code < 500:
                    break
                if attempt < 2:
                    print(f"[LS] Last.FM {r.status_code} on page {page}, retry {attempt + 1}/3...")
                    time.sleep(2)
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
                    audiodb.append(thumb)
                for u in fanarts:
                    if u and u.strip():
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


def getRecentScrobble() -> tuple:
    """Fetch the most recent (or currently playing) track from Last.FM."""
    r    = requests.get(
        f"http://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks"
        f"&user={LAST_FM_USERNAME}&limit=1&api_key={API_KEY}&format=json"
    )
    data         = r.json()
    track        = data["recenttracks"]["track"][0]
    is_playing   = track.get("@attr", {}).get("nowplaying") == "true"
    now_playing  = "Now Playing" if is_playing else "Last Played"
    raw_banner   = track["image"][3]["#text"]

    # Return the raw Last.FM URL — the caller (run_listening_stats) will decide
    # whether to proxy it through wsrv.nl (imgfixer OFF) or feed it directly to
    # imgfixer for local processing + Discord CDN re-hosting (imgfixer ON).
    if not raw_banner:
        banner_url = None
        if DEBUG: print("[LS] bannerwidgettop: Last.FM returned no image URL")
    elif LASTFM_DUMMY_HASH in raw_banner:
        banner_url = None
        if DEBUG: print("[LS] bannerwidgettop: Last.FM placeholder image — trying fallback sources")
    else:
        banner_url = raw_banner  # raw Last.FM CDN URL

    np_track      = track["name"]
    np_artist     = track["artist"]["#text"]
    np_album      = track.get("album", {}).get("#text", "")  # scrobble metadata album (fallback)
    np_track_mbid = track.get("mbid", "")                    # track MBID — more reliable than name for API lookup
    mbid          = track["artist"].get("mbid", "")
    return now_playing, banner_url, np_track, np_artist, mbid, np_album, np_track_mbid



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


def getTrackInfo(artist: str, track: str, track_mbid: str = "") -> tuple:
    """Fetch per-user play count and album title for the current track.

    Tries up to 3 lookup strategies so tracks with special characters
    (e.g. '<3', '&', '>') still resolve correctly:

      1. MBID lookup — bypasses all name/encoding issues (best, when available)
      2. Exact name lookup — works for the vast majority of tracks
      3. html.escape(name) — Last.FM's server sometimes accepts '&lt;' where '<'
         fails, covering edge cases like 'u + me = <3'

    Returns (userplaycount: int, album_title: str | None).
    Returns (0, None) when all strategies fail or on network error.
    """
    import html as _html

    try:
        base = {
            "method":  "track.getInfo",
            "user":    LAST_FM_USERNAME,
            "api_key": API_KEY,
            "format":  "json",
        }

        # Build list of params dicts to try in order
        attempts: list[dict] = []

        if track_mbid:
            attempts.append({**base, "mbid": track_mbid})

        attempts.append({**base, "artist": artist, "track": track})

        # Strategy 3: pre-encode '+' for Last.FM's double-URL-decode quirk.
        # Last.FM server decodes query params TWICE. For tracks with '+' in the
        # name (e.g. "u + me = <3"), standard %2B gets double-decoded to '+',
        # but if we send %252B (double-encoded '+'), it double-decodes correctly:
        #   %252B → first decode → %2B → second decode → + ✓
        # Also keep '=' unencoded (safe) — Last.FM handles literal '=' in values.
        if "+" in track:
            from urllib.parse import quote_plus as _qp
            _pre    = track.replace("+", "%2B")
            _track  = _qp(_pre, safe="=")
            _artist = _qp(artist)
            _user   = _qp(LAST_FM_USERNAME)
            _key    = _qp(API_KEY)
            attempts.append(
                f"http://ws.audioscrobbler.com/2.0/"
                f"?method=track.getInfo&user={_user}&api_key={_key}"
                f"&format=json&artist={_artist}&track={_track}"
            )

        data: dict | None = None
        for i, attempt in enumerate(attempts, start=1):
            if isinstance(attempt, str):
                r = requests.get(attempt, timeout=8)          # raw pre-built URL
            else:
                r = requests.get("http://ws.audioscrobbler.com/2.0/",
                                 params=attempt, timeout=8)   # params dict
            if DEBUG: print(f"[LS] track.getInfo attempt {i}/{len(attempts)}: {r.url}")
            data = r.json()
            if "error" not in data:
                break   # success — stop trying

        if not data or "error" in data:
            msg = (data.get("message", f"error {data.get('error', '?')}") if data
                   else "no response")
            print(f"[LS] track.getInfo: all {len(attempts)} lookups failed ({msg})"
                  " — treating as 1st scrobble")
            return 0, None

        track_data  = data["track"]
        count       = int(track_data.get("userplaycount", 0))

        album_title = None
        album       = track_data.get("album")
        if album:
            raw_title = album.get("title", "").strip()
            if raw_title:
                album_title = _EXPLICIT_RE.sub("", raw_title).strip()

        return count, album_title

    except Exception as exc:
        print(f"[LS] track.getInfo error: {exc}")
        return 0, None



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
    if count == 0:
        return "1st Play"

    play_word = "Play" if count == 1 else "Plays"
    count_str = f"{_format_count(count)} {play_word}"

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
    """Fetch the top N artists for the configured stats.fm user."""
    r    = requests.get(
        f"https://api.stats.fm/api/v1/users/{STATSFM_USERNAME}/top/artists?range={range_}",
        headers=STATSFM_HEADERS, timeout=10,
    )
    data = r.json()
    if "items" not in data:
        print(f"[TA] Unexpected response: {data}")
        raise KeyError(f"'items' key missing. Keys present: {list(data.keys())}")
    return data["items"][:limit]


# ──────────────────────────────────────────────────────────────────────────────
# Thread: Listening Stats
# ──────────────────────────────────────────────────────────────────────────────

def run_listening_stats() -> None:
    """Continuously poll Last.FM and stats.fm, then PATCH the Listening Stats widget."""
    cached_slow     = None
    last_slow_fetch = 0
    prev_payload    = None
    prev_mbid       = None
    prev_npartist   = None   # fallback artist-change detection when MBID is empty (new/indie artists)
    prev_nptrack    = None
    cached_pool:     list  = []
    banner_mini_url         = None
    prev_npcount_key:  tuple = ()    # (track_name, artist_name) — re-fetch on change
    cached_npcount:    str   = ""    # last formatted npcount string

    # ─ Art priority chain state ────────────────────────────────────────────
    # All vars reset whenever the track changes (prev_art_key mismatch).
    # Lanyard retries every poll for 'Now Playing' (user IS listening),
    # but only once per track for 'Last Played' (track name won't match).
    # Spotify tries once per track regardless of status.
    prev_art_key:       tuple    = ()    # (np_track, np_artist)
    lanyard_fetched:    bool     = False # True = tried Lanyard for this track
    cached_lanyard_url: str | None = None
    cached_lanyard_album: str | None = None  # album name from Lanyard (Spotify Rich Presence)
    spotify_fetched:    bool     = False # True = tried Spotify for this track
    cached_spotify_url: str | None = None

    print("[LS] Thread started")

    while True:
        now = time.time()
        try:
            # --- Slow data: refresh every LS_SLOW_INTERVAL seconds ---
            if cached_slow is None or (now - last_slow_fetch) >= LS_SLOW_INTERVAL:
                scrobbles, total_artists, total_albums, total_tracks = getUserInfo()
                ui = {
                    "scrobbles":    scrobbles,
                    "totalalbums":  total_albums,
                    "totalartists": total_artists,
                    "totaltracks":  total_tracks,
                }
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

            # --- Fast data: every LS_FAST_INTERVAL seconds ---
            now_playing, banner_url, np_track, np_artist, mbid, np_album, np_track_mbid = getRecentScrobble()
            print(f"[LS] Status: {now_playing} | {np_track} — {np_artist}")

            # ─── bannerwidgettop: Lanyard → Spotify → Last.FM priority chain ───────────
            # Save Last.FM art as the final fallback (may be None for dummy images).
            # All art caches reset when the track changes.
            lastfm_banner_url = banner_url

            art_key = (np_track, np_artist)
            if art_key != prev_art_key:
                cached_lanyard_url   = None
                cached_lanyard_album = None
                lanyard_fetched      = False
                cached_spotify_url   = None
                spotify_fetched      = False
                prev_art_key         = art_key
                if DEBUG: print(f"[LS] Art cache cleared: {np_track}")

            # Priority 1 — Lanyard: Discord presence, 640×640, no auth, free.
            # Retry every poll for 'Now Playing' (user is actively listening).
            # Try once for 'Last Played' (track won't match Spotify presence).
            if cached_lanyard_url is None and _lanyard_available:
                if not lanyard_fetched or now_playing == "Now Playing":
                    _art, _lanyard_album = _get_lanyard_art(USER_ID, np_track, np_artist)
                    lanyard_fetched = True
                    if _art:
                        cached_lanyard_url   = _art
                        cached_lanyard_album = _lanyard_album
                        print("[LS] bannerwidgettop: Lanyard")
            banner_url = cached_lanyard_url

            # Priority 2 — Spotify: try once per track (needs API call / auth).
            # Skipped entirely if Lanyard already succeeded.
            if banner_url is None and not spotify_fetched and (_spotify_available or _spotify_user_available):
                _art = None
                # Strategy 1: currently-playing API (exact match, no encoding issues)
                if now_playing == "Now Playing" and _spotify_user_available:
                    _art = _get_spotify_queue_art(
                        np_track, np_artist,
                        SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN,
                    )
                    if _art:
                        print("[LS] bannerwidgettop: Spotify currently-playing")
                # Strategy 2: search (Last Played or queue no match)
                if _art is None and _spotify_available:
                    _art = _get_spotify_art(
                        np_track, np_artist,
                        SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
                    )
                    if _art:
                        _why = "Last Played" if now_playing != "Now Playing" else "queue no match"
                        print(f"[LS] bannerwidgettop: Spotify search ({_why})")
                cached_spotify_url = _art
                spotify_fetched    = True
                if not _art:
                    if DEBUG: print(f"[LS] bannerwidgettop: Spotify found no art for '{np_track}'")
            if banner_url is None:
                banner_url = cached_spotify_url

            # Priority 3 — Last.FM: final fallback (may be None = dummy / no art)
            if banner_url is None and lastfm_banner_url:
                banner_url = lastfm_banner_url
                if DEBUG: print("[LS] bannerwidgettop: Last.FM")
            # ──────────────────────────────────────────────────────────────

            # --- bannerwidgettop: resolve final URL ---
            # imgfixer ON  → download raw Last.FM URL, process with Pillow,
            #                 re-host on Discord CDN via webhook.
            # imgfixer OFF → proxy raw Last.FM URL through wsrv.nl so Discord's
            #                 widget renderer can access it (some Fastly CDN
            #                 routes are inaccessible from Discord's servers).
            fixed_banner_url = banner_url
            if banner_url:
                if IMGFIXER_ENABLED and _imgfixer_available and DISCORD_IMAGE_WEBHOOK_URL:
                    fixed_banner_url = _fix_banner_url(
                        banner_url,
                        DISCORD_IMAGE_WEBHOOK_URL,
                        reupload_interval=IMGFIXER_REUPLOAD_INTERVAL,
                    )
                else:
                    clean_url        = banner_url.replace("https://", "")
                    fixed_banner_url = f"https://wsrv.nl/?url={clean_url}&output=jpg&q=85"

            # --- Artist image (bannermini) ---
            if ARTIST_IMAGE_ENABLED:
                # Detect artist change by MBID when available, or by name for artists without one
                artist_changed = (mbid != prev_mbid) or (not mbid and np_artist != prev_npartist)
                if artist_changed:
                    cached_pool     = get_pool_cached(np_artist, mbid=mbid, label="LS")
                    prev_mbid       = mbid
                    prev_npartist   = np_artist
                    banner_mini_url = random.choice(cached_pool) if cached_pool else AUDIODB_FALLBACK_URL
                    if DEBUG:
                        print(f"[LS] New artist: {np_artist} → {banner_mini_url}")
                    else:
                        print(f"[LS] New artist: {np_artist}")
                elif np_track != prev_nptrack:
                    if cached_pool:
                        # Exclude the currently shown image so it always refreshes on track change.
                        # Discord silently ignores a PATCH when the bannermini URL is identical
                        # to the one already displayed — making it look like the image is stuck.
                        alt_pool = [u for u in cached_pool if u != banner_mini_url]
                        banner_mini_url = random.choice(alt_pool if alt_pool else cached_pool)
                        if DEBUG: print(f"[LS] New track, updating bannermini (pool={len(cached_pool)}): {banner_mini_url}")
                prev_nptrack = np_track
            else:
                banner_mini_url = AUDIODB_FALLBACK_URL

            # --- npcount: per-track play count + album title ---
            # Only calls track.getInfo when the playing track changes; cached
            # for all subsequent polls of the same track.
            npcount_key = (np_track, np_artist)
            if npcount_key != prev_npcount_key:
                count, album_title  = getTrackInfo(np_artist, np_track, track_mbid=np_track_mbid)
                scrobble_album      = _EXPLICIT_RE.sub("", np_album).strip() if np_album else ""
                if DEBUG: print(f"[LS] npcount raw → count={count} | getInfo album='{album_title}' | scrobble album='{scrobble_album}'")

                # Prefer scrobble metadata album in two cases:
                # 1. track.getInfo returned no album (not linked in Last.FM's DB)
                # 2. track.getInfo returned album == track name (registered as a
                #    single on Last.FM), but scrobble has the real album title
                if not album_title and scrobble_album:
                    album_title = scrobble_album
                elif (album_title
                      and album_title.lower() == np_track.lower()
                      and scrobble_album
                      and scrobble_album.lower() != np_track.lower()):
                    album_title = scrobble_album

                # 3rd fallback: Lanyard carries spotify.album from Discord Rich Presence.
                # Available immediately — even for newly released tracks Last.FM hasn't indexed.
                if not album_title and cached_lanyard_album:
                    album_title = cached_lanyard_album
                    if DEBUG: print(f"[LS] npcount: album from Lanyard → '{album_title}'")

                cached_npcount      = format_npcount(count, album_title, np_track)
                prev_npcount_key    = npcount_key
                print(f"[LS] npcount: {cached_npcount}")


            # Expose current artist info to the TA thread
            with _shared_lock:
                _shared["artist_name"]    = np_artist
                _shared["artist_pool"]    = list(cached_pool)
                _shared["bannermini_url"] = banner_mini_url

            # --- Push to Discord only when something has changed ---
            current_payload = (
                now_playing, fixed_banner_url, np_track, np_artist, mbid, banner_mini_url,
                stat1_val, stat2_val, stat3_val, stat4_val, stat5_val, stat6_val,
                stat1_label, stat2_label, stat3_label, stat4_label, stat5_label, stat6_label,
                lsmini, cached_npcount,
            )

            if current_payload != prev_payload:
                dynamic = []
                if fixed_banner_url:
                    if IMGFIXER_ENABLED and _imgfixer_available:
                        print("[LS] bannerwidgettop: imgfixer → Discord CDN")
                    else:
                        print("[LS] bannerwidgettop: wsrv.nl proxy → OK")
                else:
                    print("[LS] bannerwidgettop: absent (no album art) → Discord will show fallback")
                if fixed_banner_url:
                    dynamic.append({"type": 3, "name": "bannerwidgettop", "value": {"url": fixed_banner_url}})
                dynamic += [
                    {"type": 1, "name": "nowplaying",  "value": now_playing},
                    {"type": 1, "name": "nptrack",     "value": np_track},
                    {"type": 1, "name": "npartist",    "value": np_artist},
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

                discordPatch(LS_APPLICATION_ID, LS_BOT_TOKEN, {"data": {"dynamic": dynamic}}, "LS")
                prev_payload = current_payload
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
                    discordPatch(TA_APPLICATION_ID, TA_BOT_TOKEN, {"data": {"dynamic": dynamic}}, "TA")
                    prev_static_payload = fp
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

    if effective_ls and not (LS_APPLICATION_ID and LS_BOT_TOKEN):
        print("[LS] WARNING: APPLICATION_ID or BOT_TOKEN not set → Listening Stats widget disabled")
        effective_ls = False

    if not effective_ls and not effective_ta:
        print("[ERROR] Both widgets are disabled. Set at least one to True in config.py.")
        sys.exit(1)

    # ── Startup banner ───────────────────────────────────────────────────────────
    ls_status = (
        f"✓ fast={LS_FAST_INTERVAL}s / slow={LS_SLOW_INTERVAL}s"
        if effective_ls else
        "✗ disabled"                      if not ENABLE_LISTENING_STATS else
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

    threads = []
    if effective_ls:
        threads.append(threading.Thread(target=run_listening_stats, name="ListeningStats", daemon=True))
    if effective_ta:
        threads.append(threading.Thread(target=run_top_artists,     name="TopArtists",     daemon=True))

    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[EXIT] Stopped by user (Ctrl+C).")
