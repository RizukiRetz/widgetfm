import re, requests, json, time, random, threading, urllib.parse
from bs4 import BeautifulSoup
from config import *

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
            print(f"[{label}] Pool '{artist_name}': {len(clean)} images (cache, {int(age_s // 3600)}h old)")
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
    """Fetch total hours and minutes streamed from stats.fm (lifetime)."""
    r    = requests.get(
        f"https://api.stats.fm/api/v1/users/{STATSFM_USERNAME}/streams/stats?range=lifetime",
        headers=STATSFM_HEADERS, timeout=10,
    )
    data = r.json()
    if "items" not in data:
        print(f"[LS] Unexpected stats.fm response: {data}")
        raise KeyError(f"'items' key missing. Keys present: {list(data.keys())}")
    duration_ms     = data["items"]["durationMs"]
    hours_streamed  = duration_ms // 3_600_000
    minutes_streamed = duration_ms // 60_000
    print(f"[LS] stats.fm: {hours_streamed:,} hours | {minutes_streamed:,} minutes")
    return hours_streamed, minutes_streamed


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
                        print(f"[LS] Last.FM '{artist_name}': {total_pages} pages available, fetching up to {POOL_MAX_PAGES}")

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
                    print(f"[LS] Skipping blacklisted hash: {hash_name}")
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

            print(f"[LS] Last.FM page {page}: +{len(pool) - before} valid images ({len(pool)}/{POOL_MAX_IMAGES})")

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
                print(f"[LS] AudioDB for {artist.get('strArtist', mbid)}: +{len(pool) - before} images")
            else:
                print(f"[LS] AudioDB: no data for MBID {mbid}")
        except Exception as e:
            print(f"[LS] AudioDB error: {e}")

    print(f"[LS] Total pool for '{artist_name or mbid}': {len(pool)}/{POOL_MAX_IMAGES} images")
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
    banner_url   = None if LASTFM_DUMMY_HASH in raw_banner else raw_banner
    np_track     = track["name"]
    np_artist    = track["artist"]["#text"]
    mbid         = track["artist"].get("mbid", "")
    return now_playing, banner_url, np_track, np_artist, mbid


# ──────────────────────────────────────────────────────────────────────────────
# Top Artists — data fetchers
# ──────────────────────────────────────────────────────────────────────────────

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
    cached_pool: list = []
    banner_mini_url = None

    print("[LS] Thread started")

    while True:
        now = time.time()
        try:
            # --- Slow data: refresh every LS_SLOW_INTERVAL seconds ---
            if cached_slow is None or (now - last_slow_fetch) >= LS_SLOW_INTERVAL:
                scrobbles, total_artists, total_albums, total_tracks = getUserInfo()
                hours_streamed, minutes_streamed = getStreamStats()
                cached_slow     = (scrobbles, total_artists, total_albums, total_tracks,
                                   hours_streamed, minutes_streamed)
                last_slow_fetch = now
                print("[LS] Slow data refreshed")
            else:
                scrobbles, total_artists, total_albums, total_tracks, hours_streamed, minutes_streamed = cached_slow

            # --- Fast data: every LS_FAST_INTERVAL seconds ---
            now_playing, banner_url, np_track, np_artist, mbid = getRecentScrobble()
            print(f"[LS] Status: {now_playing} | {np_track} — {np_artist}")

            # --- Artist image (bannermini) ---
            if ARTIST_IMAGE_ENABLED:
                # Detect artist change by MBID when available, or by name for artists without one
                artist_changed = (mbid != prev_mbid) or (not mbid and np_artist != prev_npartist)
                if artist_changed:
                    cached_pool     = get_pool_cached(np_artist, mbid=mbid, label="LS")
                    prev_mbid       = mbid
                    prev_npartist   = np_artist
                    banner_mini_url = random.choice(cached_pool) if cached_pool else AUDIODB_FALLBACK_URL
                    print(f"[LS] New artist bannermini: {np_artist} → {banner_mini_url}")
                elif np_track != prev_nptrack:
                    if cached_pool:
                        banner_mini_url = random.choice(cached_pool)
                        print(f"[LS] New track, updating bannermini (pool={len(cached_pool)}): {banner_mini_url}")
                prev_nptrack = np_track
            else:
                banner_mini_url = AUDIODB_FALLBACK_URL

            # Expose current artist info to the TA thread
            with _shared_lock:
                _shared["artist_name"]    = np_artist
                _shared["artist_pool"]    = list(cached_pool)
                _shared["bannermini_url"] = banner_mini_url

            # --- Push to Discord only when something has changed ---
            current_payload = (
                now_playing, banner_url, np_track, np_artist, mbid, banner_mini_url,
                scrobbles, total_artists, total_albums, total_tracks,
                hours_streamed, minutes_streamed,
            )

            if current_payload != prev_payload:
                dynamic = []
                if banner_url:
                    dynamic.append({"type": 3, "name": "bannerwidgettop", "value": {"url": banner_url}})
                dynamic += [
                    {"type": 1, "name": "nowplaying",     "value": now_playing},
                    {"type": 1, "name": "nptrack",         "value": np_track},
                    {"type": 1, "name": "npartist",        "value": np_artist},
                    {"type": 1, "name": "scrobbles",       "value": f"{scrobbles:,}"},
                    {"type": 1, "name": "hoursstreamed",   "value": f"{hours_streamed:,}"},
                    {"type": 1, "name": "minutesstreamed", "value": f"{minutes_streamed:,}"},
                    {"type": 1, "name": "totalalbums",     "value": f"{total_albums:,}"},
                    {"type": 1, "name": "totalartists",    "value": f"{total_artists:,}"},
                    {"type": 1, "name": "totaltracks",     "value": f"{total_tracks:,}"},
                    {"type": 1, "name": "totaltrackmini",  "value": f"{total_tracks:,} Total Songs "},
                ]
                if banner_mini_url:
                    dynamic.append({"type": 3, "name": "bannermini", "value": {"url": banner_mini_url}})

                discordPatch(LS_APPLICATION_ID, LS_BOT_TOKEN, {"data": {"dynamic": dynamic}}, "LS")
                prev_payload = current_payload
            else:
                print("[LS] No changes, skip")

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
                    print("[TA] No changes, skip")
                time.sleep(STATIC_INTERVAL)

        except Exception as e:
            print(f"[TA] ERROR: {e}")
            time.sleep(ROTATION_INTERVAL if TOPARTISTS_ROTATE else STATIC_INTERVAL)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ta_mode = (
        f"rotate=ON | {' → '.join(ROTATION_LABELS[r] for r in ROTATION_ORDER)} "
        f"| {ROTATION_INTERVAL}s/step | start={ROTATION_LABELS.get(TOPARTISTS_RANGE, TOPARTISTS_RANGE)}"
        if TOPARTISTS_ROTATE else
        f"rotate=OFF | range={ROTATION_LABELS.get(TOPARTISTS_RANGE, TOPARTISTS_RANGE)} | refresh={STATIC_INTERVAL}s"
    )
    print("=" * 55)
    print("   Discord Widget Stats — Starting")
    print(f"   Listening Stats : fast={LS_FAST_INTERVAL}s / slow={LS_SLOW_INTERVAL}s")
    print(f"   Top Artists     : {ta_mode}")
    print("=" * 55)

    # Pre-load the image cache so both threads can serve from it immediately
    _g_image_cache.update(load_image_cache())
    print(f"[Cache] Global cache ready: {len(_g_image_cache)} artist(s) cached")

    threads = [
        threading.Thread(target=run_listening_stats, name="ListeningStats", daemon=True),
        threading.Thread(target=run_top_artists,     name="TopArtists",     daemon=True),
    ]

    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[EXIT] Stopped by user (Ctrl+C).")
