"""statsfmfetch.py — stats.fm API v2 data fetcher for WidgetFM.

Replaces all Last.FM REST API (ws.audioscrobbler.com) dependencies with the
public stats.fm v2 API (https://api-v2.stats.fm/api/v1), modeled after dotme's
stats.fm integration. No API key required — only STATSFM_USERNAME.

Endpoints used:
  - /users/{u}/streams/stats?range=lifetime       → total streams, tracks, artists, albums, duration
  - /users/{u}/streams/current                    → currently playing track (if any)
  - /users/{u}/streams/recent?limit=1             → most recently played track
  - /users/{u}/streams/tracks/{trackId}/stats     → per-user play count for a track
  - /users/{u}/top/{artists|tracks|albums}?...    → top artists / tracks / albums by range
  - /search?query={q}&type=tracks                 → track ID resolution (for Lanyard/Spotify overrides)
"""

import re
import time
import urllib.parse
import requests

DEFAULT_API_BASE = "https://api-v2.stats.fm/api/v1"
DEFAULT_HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
}

# In-memory cache for per-track play counts: track_id → (timestamp, count)
_track_stats_cache: dict[int, tuple[float, int]] = {}
_TRACK_STATS_TTL = 180.0   # 3 minutes (short enough to reflect new plays quickly)

# In-memory mapping: spotify_track_id → statsfm_track_id (populated automatically
# from streams/current, streams/recent, and top/tracks so placeholder titles like
# "Track 2" or "Unknown" artist entries still resolve play counts accurately).
_spotify_to_statsfm_id: dict[str, int] = {}
_recent50_cache: tuple[float, list]    = (0.0, [])
_RECENT50_TTL                          = 45.0
_top_tracks_cache: dict[str, tuple[float, list]] = {}
_TOP_TRACKS_TTL                        = 300.0


def _remember_track(track: dict) -> None:
    """Record spotify_track_id → statsfm_track_id mappings from any stats.fm track object."""
    tid = track.get("id")
    if not isinstance(tid, int):
        return
    sp_ids = (track.get("externalIds") or {}).get("spotify") or []
    if isinstance(sp_ids, list):
        for sid in sp_ids:
            if sid:
                _spotify_to_statsfm_id[sid] = tid


def _norm(s: str) -> str:
    """Strip non-alphanumeric chars and lowercase for loose track/artist matching."""
    if not s:
        return ""
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def build_range_query(period: str) -> str:
    """Convert a config period string into a stats.fm v2 range query string.

    Supports both legacy Last.FM period names and stats.fm range names:
      - "overall" / "lifetime"        → range=lifetime
      - "6month"  / "180d" / "months" → range=months
      - "1month"  / "30d"  / "weeks"  → range=weeks
      - "7day"                        → rolling 7-day after/before window
      - "3month"                      → rolling 90-day after/before window
      - "12month" / "365d" / "year"   → rolling 365-day after/before window
    """
    if period in ("1month", "30d", "weeks"):
        return "range=weeks"
    if period in ("6month", "180d", "months"):
        return "range=months"
    if period in ("overall", "lifetime"):
        return "range=lifetime"

    days_map = {
        "7day":    7,
        "3month":  90,
        "12month": 365,
        "365d":    365,
        "year":    365,
        "years":   365,
    }
    days = days_map.get(period)
    if days:
        # Round to the hour for cache stability (matching dotme)
        now_ms   = (int(time.time()) // 3600) * 3600 * 1000
        after_ms = now_ms - days * 86_400_000
        return f"after={after_ms}&before={now_ms}"

    return "range=lifetime"


def get_user_stats(
    username: str,
    api_base: str = DEFAULT_API_BASE,
    headers: dict | None = None,
) -> dict:
    """Fetch lifetime listening stats from stats.fm in a single request.

    Returns dict with keys:
      scrobbles, totaltracks, totalartists, totalalbums,
      hoursstreamed, minutesstreamed
    """
    if not username:
        raise ValueError("STATSFM_USERNAME is required")

    user_enc = urllib.parse.quote(username, safe="")
    url = f"{api_base}/users/{user_enc}/streams/stats?range=lifetime"
    r = requests.get(url, headers=headers or DEFAULT_HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json()

    items = data.get("items")
    if not isinstance(items, dict):
        raise KeyError(f"stats.fm streams/stats missing 'items'. Keys: {list(data.keys())}")

    card        = items.get("cardinality") or {}
    duration_ms = int(items.get("durationMs") or 0)

    return {
        "scrobbles":       int(items.get("count") or 0),
        "totaltracks":     int(card.get("tracks") or 0),
        "totalartists":    int(card.get("artists") or 0),
        "totalalbums":     int(card.get("albums") or 0),
        "hoursstreamed":   duration_ms // 3_600_000,
        "minutesstreamed": duration_ms // 60_000,
    }


def _extract_track_fields(track: dict) -> dict:
    """Normalize a stats.fm track object into a flat dict for upstats.py."""
    _remember_track(track)
    artists = track.get("artists") or []
    albums  = track.get("albums")  or []

    primary_artist = (artists[0].get("name") or "").strip() if artists else ""
    artist_display = ", ".join(
        a["name"].strip() for a in artists if a.get("name")
    ) or primary_artist

    album_name   = (albums[0].get("name") or "").strip() if albums else ""
    album_image  = (albums[0].get("image") or "").strip() if albums else ""
    artist_image = (artists[0].get("image") or "").strip() if artists else ""

    ext_ids     = track.get("externalIds") or {}
    raw_sp_ids  = ext_ids.get("spotify") or []
    spotify_ids = [s for s in raw_sp_ids if s] if isinstance(raw_sp_ids, list) else []
    spotify_id  = spotify_ids[0] if spotify_ids else None

    return {
        "track":          (track.get("name") or "").strip(),
        "artist":         artist_display,
        "primary_artist": primary_artist,
        "album":          album_name,
        "banner_url":     album_image or artist_image or None,
        "artist_image":   artist_image or None,
        "track_id":       track.get("id"),
        "spotify_id":     spotify_id,
        "spotify_ids":    spotify_ids,
    }


def get_current_stream(
    username: str,
    api_base: str = DEFAULT_API_BASE,
    headers: dict | None = None,
) -> tuple[bool, dict | None]:
    """Fetch the user's currently playing stream from stats.fm.

    Returns (is_playing, track_info_dict) or (False, None) if nothing is playing.
    """
    if not username:
        return False, None

    user_enc = urllib.parse.quote(username, safe="")
    url = f"{api_base}/users/{user_enc}/streams/current"
    try:
        r = requests.get(url, headers=headers or DEFAULT_HEADERS, timeout=8)
        if r.status_code != 200:
            return False, None
        data = r.json()
        item = data.get("item")
        if not item or not item.get("track"):
            return False, None
        is_playing = bool(item.get("isPlaying", True))
        # Stale guard: if stats.fm reports progressMs >= durationMs, treat as finished
        progress_ms = item.get("progressMs")
        duration_ms = item.get("durationMs") or (item["track"].get("durationMs"))
        if (
            isinstance(progress_ms, (int, float))
            and isinstance(duration_ms, (int, float))
            and duration_ms > 0
            and progress_ms >= duration_ms
        ):
            is_playing = False
        return is_playing, _extract_track_fields(item["track"])
    except Exception:
        return False, None


def get_recent_stream(
    username: str,
    api_base: str = DEFAULT_API_BASE,
    headers: dict | None = None,
) -> tuple[str, dict]:
    """Fetch the currently playing or most recently played track from stats.fm.

    Checks /streams/current first; if nothing is actively playing, falls back
    to /streams/recent?limit=1.

    Returns (status_str, track_info_dict) where status_str is
    "Now Playing" or "Last Played".
    """
    if not username:
        raise ValueError("STATSFM_USERNAME is required")

    hdrs = headers or DEFAULT_HEADERS

    # 1. Check active stream
    is_playing, current_info = get_current_stream(username, api_base=api_base, headers=hdrs)
    if current_info and current_info.get("track"):
        return ("Now Playing" if is_playing else "Last Played"), current_info

    # 2. Fall back to most recent completed stream
    user_enc = urllib.parse.quote(username, safe="")
    url = f"{api_base}/users/{user_enc}/streams/recent?limit=1"
    r = requests.get(url, headers=hdrs, timeout=10)
    r.raise_for_status()
    data = r.json()
    items = data.get("items") or []
    if not items or not items[0].get("track"):
        raise ValueError(f"stats.fm streams/recent returned no tracks for user '{username}'")

    return "Last Played", _extract_track_fields(items[0]["track"])


def search_track_id(
    title: str,
    artist: str = "",
    username: str = "",
    spotify_track_id: str | None = None,
    api_base: str = DEFAULT_API_BASE,
    headers: dict | None = None,
) -> int | None:
    """Resolve a stats.fm numeric track ID by title + artist (ported from dotme).

    Used when Lanyard or Spotify detects a track before stats.fm /streams/current
    updates, so we can still fetch the user's exact play count from stats.fm.
    """
    if not title:
        return None

    hdrs = headers or DEFAULT_HEADERS
    clean_title = re.sub(r"\s*\(.*?\)\s*", " ", title)
    clean_title = re.sub(r"\s+-\s+.*$", "", clean_title).strip() or title.strip()

    try:
        r = requests.get(
            f"{api_base}/search",
            params={"query": clean_title, "type": "tracks"},
            headers=hdrs,
            timeout=8,
        )
        if r.status_code != 200:
            return None
        tracks = (r.json().get("items") or {}).get("tracks") or []
        if not tracks:
            return None

        # 1. Exact Spotify ID match if available
        if spotify_track_id:
            for t in tracks:
                sp_ids = (t.get("externalIds") or {}).get("spotify") or []
                if spotify_track_id in sp_ids:
                    return t.get("id")

        target_title_norm = _norm(title)
        clean_title_norm  = _norm(clean_title)
        target_artist_norm = _norm(artist.split(",")[0]) if artist else ""

        candidates = []
        for t in tracks:
            t_norm = _norm(t.get("name", ""))
            title_ok = (
                t_norm == target_title_norm
                or t_norm == clean_title_norm
                or (clean_title_norm and clean_title_norm in t_norm)
                or (t_norm and t_norm in clean_title_norm)
            )
            if not title_ok:
                continue

            if not target_artist_norm:
                candidates.append(t)
                continue

            art_norms = [_norm(a.get("name", "")) for a in (t.get("artists") or [])]
            if any(target_artist_norm in a or a in target_artist_norm for a in art_norms if a):
                candidates.append(t)

        if not candidates:
            return None

        # If multiple candidates exist and username is provided, prefer one the user has streamed
        if len(candidates) > 1 and username:
            user_enc = urllib.parse.quote(username, safe="")
            for c in candidates[:3]:
                cid = c.get("id")
                if not cid:
                    continue
                try:
                    sr = requests.get(
                        f"{api_base}/users/{user_enc}/streams/tracks/{cid}/stats",
                        headers=hdrs,
                        timeout=4,
                    )
                    if sr.status_code == 200:
                        cnt = int((sr.json().get("items") or {}).get("count") or 0)
                        if cnt > 0:
                            _track_stats_cache[cid] = (time.time(), cnt)
                            return cid
                except Exception:
                    pass

        return candidates[0].get("id")
    except Exception:
        return None


def resolve_track_id_from_user_history(
    username: str,
    spotify_track_id: str | None,
    api_base: str = DEFAULT_API_BASE,
    headers: dict | None = None,
) -> int | None:
    """Resolve a stats.fm numeric track ID using the user's own streams & top tracks.

    Matches strictly by Spotify Track ID (externalIds.spotify), which succeeds even
    when stats.fm still shows a placeholder title ('Track 2'), an 'Unknown' artist,
    or a common title buried outside global /search top-5 results.
    """
    global _recent50_cache

    if not username or not spotify_track_id:
        return None

    # 1. Instant hit from in-memory map
    cached_id = _spotify_to_statsfm_id.get(spotify_track_id)
    if cached_id:
        return cached_id

    hdrs     = headers or DEFAULT_HEADERS
    user_enc = urllib.parse.quote(username, safe="")
    now      = time.time()

    # 2. Scan /users/{u}/streams/recent?limit=50 (cached 45s)
    ts_r, items_r = _recent50_cache
    if not items_r or (now - ts_r) >= _RECENT50_TTL:
        try:
            r = requests.get(
                f"{api_base}/users/{user_enc}/streams/recent",
                params={"limit": 50},
                headers=hdrs,
                timeout=10,
            )
            if r.status_code == 200:
                items_r = r.json().get("items") or []
                _recent50_cache = (now, items_r)
                for item in items_r:
                    t = item.get("track")
                    if isinstance(t, dict):
                        _remember_track(t)
        except Exception:
            pass

    cached_id = _spotify_to_statsfm_id.get(spotify_track_id)
    if cached_id:
        return cached_id

    # 3. Scan /users/{u}/top/tracks for weeks & months (cached 5m each)
    for rng in ("weeks", "months"):
        ts_t, items_t = _top_tracks_cache.get(rng, (0.0, []))
        if not items_t or (now - ts_t) >= _TOP_TRACKS_TTL:
            try:
                r = requests.get(
                    f"{api_base}/users/{user_enc}/top/tracks",
                    params={"range": rng},
                    headers=hdrs,
                    timeout=10,
                )
                if r.status_code == 200:
                    items_t = r.json().get("items") or []
                    _top_tracks_cache[rng] = (now, items_t)
                    for item in items_t:
                        t = item.get("track")
                        if isinstance(t, dict):
                            _remember_track(t)
            except Exception:
                pass

        cached_id = _spotify_to_statsfm_id.get(spotify_track_id)
        if cached_id:
            return cached_id

    return None


def get_track_play_count(
    username: str,
    track_id: int | None = None,
    title: str = "",
    artist: str = "",
    spotify_track_id: str | None = None,
    api_base: str = DEFAULT_API_BASE,
    headers: dict | None = None,
    force_refresh: bool = False,
) -> int:
    """Return the user's lifetime play count for a track from stats.fm.

    Resolution order when track_id is not provided:
      1. User's own recent streams & top tracks matched by Spotify Track ID
         (handles placeholder titles like 'Track 2', 'Unknown' artists, and
         common-title collisions that miss global /search).
      2. Global stats.fm track search (search_track_id).
    Returns 0 if not found or on any network error.
    """
    if not username:
        return 0

    hdrs = headers or DEFAULT_HEADERS

    if not track_id and spotify_track_id:
        track_id = resolve_track_id_from_user_history(
            username,
            spotify_track_id,
            api_base=api_base,
            headers=hdrs,
        )

    if not track_id and title:
        track_id = search_track_id(
            title,
            artist=artist,
            username=username,
            spotify_track_id=spotify_track_id,
            api_base=api_base,
            headers=hdrs,
        )

    if not track_id:
        return 0

    if not force_refresh:
        cached = _track_stats_cache.get(track_id)
        if cached and (time.time() - cached[0]) < _TRACK_STATS_TTL:
            return cached[1]

    user_enc = urllib.parse.quote(username, safe="")
    url = f"{api_base}/users/{user_enc}/streams/tracks/{track_id}/stats"
    try:
        r = requests.get(url, headers=hdrs, timeout=8)
        if r.status_code != 200:
            cached = _track_stats_cache.get(track_id)
            return cached[1] if cached else 0
        count = int((r.json().get("items") or {}).get("count") or 0)
        _track_stats_cache[track_id] = (time.time(), count)
        return count
    except Exception:
        cached = _track_stats_cache.get(track_id)
        return cached[1] if cached else 0


def get_top_items(
    username: str,
    kind: str = "artists",
    period: str = "lifetime",
    limit: int = 5,
    api_base: str = DEFAULT_API_BASE,
    headers: dict | None = None,
) -> list:
    """Fetch top artists, tracks, or albums from stats.fm v2 API.

    kind   : "artists" | "tracks" | "albums"
    period : "lifetime" | "months" | "weeks" | "overall" | "7day" | "1month" | "3month" | "6month" | "12month"
    """
    if not username:
        raise ValueError("STATSFM_USERNAME is required")

    user_enc    = urllib.parse.quote(username, safe="")
    range_query = build_range_query(period)
    url = f"{api_base}/users/{user_enc}/top/{kind}?{range_query}"
    r = requests.get(url, headers=headers or DEFAULT_HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json()
    if "items" not in data:
        raise KeyError(f"stats.fm top/{kind} missing 'items'. Keys: {list(data.keys())}")
    return (data["items"] or [])[:limit]
