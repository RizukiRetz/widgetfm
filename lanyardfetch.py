"""lanyardfetch.py — Lanyard presence data fetcher.

Lanyard (github.com/phineas/lanyard) reads the user's 'Listening to Spotify'
Discord rich presence and exposes it as a free public REST API — no auth needed.

Response contains:
  spotify.album_art_url — 640×640 Spotify CDN image (i.scdn.co)
  spotify.album         — album title, always from Spotify (never mislabeled)
  spotify.song          — track name as on Spotify
  spotify.artist        — artist(s) joined with '; ' for collaborations

Rate limit: 1 000 requests/hour (≈ one per 3.6 s).
With a 20-second poll interval, this script uses ≈180 req/hr — well within limits.

Fallthrough (returns all None) when:
  • User not in the Lanyard Discord server (discord.gg/UrXF2cfJ7F)
    OR not hosting their own Lanyard instance → {"error": "user_not_monitored"}
  • User is offline/invisible on Discord → `discord_status == "offline"` → no data.
  • User is not listening to Spotify → `listening_to_spotify == false` → no data.
  • Discord/Spotify rich-presence outage → presence vanishes, API returns null.
  • Any network / timeout error.
"""

import requests

LANYARD_API = "https://api.lanyard.rest/v1/users"


def get_lanyard_album_art(
    user_id: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (art_url, album, song, artist) from the user's Lanyard Spotify presence.

    All values come directly from Spotify Rich Presence via Discord:
      - art_url : 640×640 album art (Spotify CDN)
      - album   : album name — always correct, never mislabeled as "Single"
      - song    : track name as shown on Spotify
      - artist  : artist string; Lanyard joins collaborators with '; '
                  → returned with '; ' replaced by ', ' for display

    Returns (None, None, None, None) when the user is not actively playing
    Spotify via Discord (offline, not monitored, Private Session, etc.).
    """
    if not user_id:
        return None, None, None, None

    try:
        r = requests.get(f"{LANYARD_API}/{user_id}", timeout=6)

        if r.status_code != 200:
            return None, None, None, None

        data = r.json()

        # Lanyard error (user_not_monitored, etc.)
        if not data.get("success"):
            return None, None, None, None

        payload = data.get("data", {})

        # Offline / invisible → Discord hides all presence data
        if payload.get("discord_status") == "offline":
            return None, None, None, None

        # Not listening to Spotify (nothing playing, paused, Private Session)
        if not payload.get("listening_to_spotify"):
            return None, None, None, None

        sp = payload.get("spotify")
        if not sp:
            return None, None, None, None

        art_url = sp.get("album_art_url") or None
        album   = sp.get("album",  "").strip() or None
        song    = sp.get("song",   "").strip() or None

        # Lanyard joins collaboration artists with "; " — replace with ", " for display.
        # e.g. "Wet Leg; horsegiirL" → "Wet Leg, horsegiirL"
        raw_artist = sp.get("artist", "").strip()
        artist = ", ".join(p.strip() for p in raw_artist.split(";")) if raw_artist else None

        return art_url, album, song, artist

    except Exception as exc:
        print(f"[Lanyard] Error: {exc}")
        return None, None, None, None
