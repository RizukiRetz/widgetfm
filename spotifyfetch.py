"""spotifyfetch.py — Spotify album art fetcher.

Two fetch strategies:
  1. Queue API  (get_queue_album_art)  — requires user OAuth (refresh_token).
                                         Fetches currently_playing from
                                         /v1/me/player/queue and matches the
                                         track name exactly. No encoding issues.
                                         Only works when something is playing.

  2. Search API (get_spotify_album_art) — requires only Client Credentials.
                                          Searches Spotify by track + artist name.
                                          Used as fallback for 'Last Played' or
                                          when queue returns no match.

Both strategies return the largest available album art URL (by height × width).
All tokens are cached in-memory and refreshed automatically before expiry.
"""

import re
import base64
import time
import requests

# ── Client Credentials token cache (for search) ───────────────────────────────
_cc_cache: dict = {"access_token": None, "expires_at": 0.0}

# ── User OAuth token cache (for queue API) ────────────────────────────────────
_user_cache: dict = {"access_token": None, "expires_at": 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# Token helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_cc_token(client_id: str, client_secret: str) -> str | None:
    """Return a valid Client Credentials access token (auto-refreshes)."""
    now = time.time()
    if _cc_cache["access_token"] and now < _cc_cache["expires_at"] - 60:
        return _cc_cache["access_token"]

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        r = requests.post(
            "https://accounts.spotify.com/api/token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
            timeout=10,
        )
    except Exception as exc:
        print(f"[Spotify] CC token request failed: {exc}")
        return None

    if r.status_code != 200:
        print(f"[Spotify] CC token error {r.status_code}: {r.text[:200]}")
        return None

    data = r.json()
    _cc_cache["access_token"] = data["access_token"]
    _cc_cache["expires_at"]   = now + data.get("expires_in", 3600)
    return _cc_cache["access_token"]


def _get_user_token(refresh_token: str, client_id: str, client_secret: str) -> str | None:
    """Return a valid user OAuth access token, refreshing via refresh_token."""
    now = time.time()
    if _user_cache["access_token"] and now < _user_cache["expires_at"] - 60:
        return _user_cache["access_token"]

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        r = requests.post(
            "https://accounts.spotify.com/api/token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
            data={
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=10,
        )
    except Exception as exc:
        print(f"[Spotify] User token refresh failed: {exc}")
        return None

    if r.status_code != 200:
        print(f"[Spotify] User token error {r.status_code}: {r.text[:200]}")
        return None

    data = r.json()
    _user_cache["access_token"] = data["access_token"]
    _user_cache["expires_at"]   = now + data.get("expires_in", 3600)
    return _user_cache["access_token"]


# ─────────────────────────────────────────────────────────────────────────────
# Title normalization (for queue API match)
# ─────────────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Strip all non-alphanumeric characters and lowercase for loose matching.

    'u + me = <3'  →  'ume3'
    'U + Me = <3'  →  'ume3'   → match ✓
    Handles Unicode apostrophes, special chars, and case differences.
    """
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ─────────────────────────────────────────────────────────────────────────────
# Image picker
# ─────────────────────────────────────────────────────────────────────────────

def _best_image(images: list) -> str | None:
    """Return the URL of the largest image (by height × width)."""
    if not images:
        return None
    best = max(images, key=lambda img: img.get("height", 0) * img.get("width", 0))
    return best.get("url")


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 1 — Queue API (user OAuth, Now Playing only)
# ─────────────────────────────────────────────────────────────────────────────

def get_queue_album_art(np_track: str, np_artist: str,
                         client_id: str, client_secret: str,
                         refresh_token: str) -> str | None:
    """Fetch album art from Spotify's /v1/me/player/currently-playing endpoint.

    Simpler and more reliable than /v1/me/player/queue:
      - 204 = nothing is playing → return None cleanly
      - 200 = track object with album.images directly

    Matches currently_playing.name against np_track (normalized, so special
    characters like '+', '=', '<3' are not a problem).

    Returns None when:
      - Nothing is currently playing on Spotify
      - The playing track doesn't match np_track (prevents wrong art)
      - Any network / token error
    """
    if not refresh_token or not client_id or not client_secret:
        return None

    token = _get_user_token(refresh_token, client_id, client_secret)
    if not token:
        return None

    def _fetch(tok: str):
        return requests.get(
            "https://api.spotify.com/v1/me/player/currently-playing",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=8,
        )

    try:
        r = _fetch(token)

        if r.status_code == 401:
            # Token expired edge-case — clear cache and retry once
            _user_cache["access_token"] = None
            _user_cache["expires_at"]   = 0.0
            token = _get_user_token(refresh_token, client_id, client_secret)
            if not token:
                return None
            r = _fetch(token)

        if r.status_code == 204:
            return None   # nothing is playing

        if r.status_code != 200:
            print(f"[Spotify] currently-playing HTTP {r.status_code}")
            return None

        data = r.json()
        item = data.get("item")
        if not item:
            return None   # ad or episode, not a track

        # Normalize both sides to handle special characters safely
        spotify_name = item.get("name", "")
        if _norm(spotify_name) != _norm(np_track):
            return None   # different track on Spotify vs Last.FM — skip

        images = item.get("album", {}).get("images", [])
        return _best_image(images)

    except Exception as exc:
        print(f"[Spotify] currently-playing error: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 2 — Search API (Client Credentials, any status)
# ─────────────────────────────────────────────────────────────────────────────

def _do_search(token: str, query: str) -> dict | None:
    try:
        r = requests.get(
            "https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "type": "track", "limit": 1},
            timeout=8,
        )
        return r.json() if r.status_code == 200 else None
    except Exception as exc:
        print(f"[Spotify] Search error: {exc}")
        return None


def get_spotify_album_art(track: str, artist: str,
                           client_id: str, client_secret: str) -> str | None:
    """Search Spotify for a track and return the largest album art URL.

    Uses Client Credentials flow (no user login required).
    Serves as fallback when queue API is unavailable or returns no match.
    Returns None when track not found or any error occurs.
    """
    if not client_id or not client_secret:
        return None

    token = _get_cc_token(client_id, client_secret)
    if not token:
        return None

    query = f"track:{track} artist:{artist}"
    data  = _do_search(token, query)

    # Retry once on token edge-case
    if data is None:
        _cc_cache["access_token"] = None
        _cc_cache["expires_at"]   = 0.0
        token = _get_cc_token(client_id, client_secret)
        if token:
            data = _do_search(token, query)

    if not data:
        return None

    items = data.get("tracks", {}).get("items", [])
    if not items:
        print(f"[Spotify] Search: no results for '{track}' — '{artist}'")
        return None

    images = items[0].get("album", {}).get("images", [])
    return _best_image(images)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 3 — Recently Played API (user OAuth, Last Played only)
# ─────────────────────────────────────────────────────────────────────────────

def get_recently_played(
    client_id: str, client_secret: str, refresh_token: str
) -> tuple[str | None, str | None, str | None, str | None]:
    """Fetch the most recently played track from /v1/me/player/recently-played.

    Requires user OAuth (refresh_token).  Used as the primary data source for
    'Last Played' status — provides song, artist, album, and album art directly
    from Spotify (always correct, no Last.FM indexing delay).

    Response path:
      items[0].track.name            → song
      items[0].track.artists[].name  → artists (joined with ', ')
      items[0].track.album.name      → album
      items[0].track.album.images[]  → art (largest picked by _best_image)

    Returns (song, artist, album, art_url).
    All values are None on any error or if the history is empty.
    """
    if not refresh_token or not client_id or not client_secret:
        return None, None, None, None

    token = _get_user_token(refresh_token, client_id, client_secret)
    if not token:
        return None, None, None, None

    def _fetch(tok: str):
        return requests.get(
            "https://api.spotify.com/v1/me/player/recently-played",
            headers={"Authorization": f"Bearer {tok}"},
            params={"limit": 1},
            timeout=8,
        )

    try:
        r = _fetch(token)

        if r.status_code == 401:
            _user_cache["access_token"] = None
            _user_cache["expires_at"]   = 0.0
            token = _get_user_token(refresh_token, client_id, client_secret)
            if not token:
                return None, None, None, None
            r = _fetch(token)

        if r.status_code != 200:
            print(f"[Spotify] recently-played HTTP {r.status_code}")
            return None, None, None, None

        data  = r.json()
        items = data.get("items", [])
        if not items:
            return None, None, None, None

        track = items[0].get("track") or {}
        if not track:
            return None, None, None, None

        song   = track.get("name", "").strip() or None
        artist = ", ".join(
            a["name"] for a in track.get("artists", []) if a.get("name")
        ) or None
        album  = track.get("album", {}).get("name", "").strip() or None
        art    = _best_image(track.get("album", {}).get("images", []))

        return song, artist, album, art

    except Exception as exc:
        print(f"[Spotify] recently-played error: {exc}")
        return None, None, None, None

