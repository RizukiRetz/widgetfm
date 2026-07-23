"""lanyardfetch.py — Lanyard album art fetcher.

Lanyard (github.com/phineas/lanyard) reads the user's 'Listening to Spotify'
Discord rich presence and exposes it as a free public REST API — no auth needed.

Response contains `spotify.album_art_url` which is a 640×640 Spotify CDN image
(i.scdn.co), identical in quality to what /v1/me/player/currently-playing returns.

Rate limit: 1 000 requests/hour (≈ one per 3.6 s).
With a 20-second poll interval, this script uses ≈180 req/hr — well within limits.

Limitations (and when we fall through to Spotify or Last.FM):
  • User must be in the Lanyard Discord server (discord.gg/lanyard)
    OR host their own Lanyard instance.  Otherwise: {"error": "user_not_monitored"}
  • User is offline/invisible on Discord → `discord_status == "offline"` → no data.
  • User is not listening to Spotify → `listening_to_spotify == false` → no data.
  • Discord rich-presence outage → presence vanishes, API returns null.
  • The currently playing Spotify track differs from the Last.FM track being
    displayed (e.g. 'Last Played' shown while something else plays on Spotify).
"""

import re
import requests

LANYARD_API = "https://api.lanyard.rest/v1/users"


def _norm(s: str) -> str:
    """Strip non-alphanumeric chars and lowercase for loose track-name comparison.

    Examples:
      'u + me = <3'  → 'ume3'
      'She's Got Other Friends'  → 'shesgototherfriendsers'  (apostrophe stripped)
    """
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def get_lanyard_album_art(user_id: str, np_track: str, np_artist: str) -> tuple[str | None, str | None]:
    """Return (album_art_url, album_name) from Lanyard for np_track/np_artist.

    Both values come from Spotify Rich Presence via Discord and are available
    immediately — even for newly released tracks before Last.FM has indexed them.

    Performs a normalized track-name match so special characters in song titles
    ('+', '=', '<3', etc.) never cause a mismatch between Last.FM and Lanyard data.

    Multi-artist tracks: Lanyard joins artists with '; ' — we don't match on artist,
    only on song name, to avoid false negatives from separator differences.

    Returns (None, None) when:
      - `user_id` is empty
      - User is not monitored by Lanyard
      - User is offline / invisible on Discord
      - `listening_to_spotify` is false (nothing playing or Private Session)
      - Currently playing track does not match np_track (prevents wrong art)
      - Any network / timeout error
    """
    if not user_id:
        return None, None

    try:
        r = requests.get(f"{LANYARD_API}/{user_id}", timeout=6)

        if r.status_code != 200:
            return None

        data = r.json()

        # Lanyard error (user_not_monitored, etc.)
        if not data.get("success"):
            err = data.get("error", {}).get("code", "unknown")
            if err == "user_not_monitored":
                # This won't fix itself — log once-ish via caller, not every poll
                pass
            return None, None

        payload = data.get("data", {})

        # Offline / invisible → Discord hides all presence data
        if payload.get("discord_status") == "offline":
            return None, None

        # Not listening to Spotify (nothing playing, paused, Private Session)
        if not payload.get("listening_to_spotify"):
            return None, None

        sp = payload.get("spotify")
        if not sp:
            return None, None

        # Normalized track name match — handles special characters safely
        spotify_song = sp.get("song", "")
        if _norm(spotify_song) != _norm(np_track):
            return None, None   # different track on Spotify vs Last.FM display

        art_url    = sp.get("album_art_url") or None
        album_name = sp.get("album", "").strip() or None
        return art_url, album_name

    except Exception as exc:
        print(f"[Lanyard] Error: {exc}")
        return None, None
