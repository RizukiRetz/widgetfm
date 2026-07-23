"""spotify_auth.py — One-time Spotify OAuth authorization.

Run this script once to let the bot access your Spotify player state:
    python spotify_auth.py

What it does:
  1. Opens your browser to Spotify's authorization page
  2. Starts a local server to catch the redirect from Spotify
  3. Exchanges the auth code for an access_token + refresh_token
  4. Saves SPOTIFY_REFRESH_TOKEN to your .env file automatically

Prerequisites:
  • SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET already in .env
  • Add the Redirect URI below to your Spotify App settings:
      developer.spotify.com/dashboard → Your App → Settings → Redirect URIs
"""

import os, sys, urllib.parse, base64, webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv, set_key
import requests

REDIRECT_URI = "http://localhost:8888/callback"
SCOPES       = "user-read-currently-playing user-read-playback-state"
PORT         = 8888

load_dotenv()
CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

if not CLIENT_ID or not CLIENT_SECRET:
    print("❌ Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env first.")
    sys.exit(1)

_received_code: dict = {"value": None}


class _CallbackHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler — catches the Spotify OAuth callback."""

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            _received_code["value"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="font-family:sans-serif;text-align:center;padding:60px;
                                   background:#121212;color:#fff;">
                <h2 style="color:#1db954;">&#10003; Authorized!</h2>
                <p>You can close this tab and return to the terminal.</p>
                </body></html>
            """)
        else:
            error = params.get("error", ["unknown"])[0]
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f"Authorization failed: {error}".encode())

    def log_message(self, *_):
        pass  # suppress server access logs


def main():
    print("=" * 60)
    print("  Spotify OAuth Setup — WidgetFM")
    print("=" * 60)
    print(f"\n⚠  Before continuing, add this Redirect URI to your Spotify App:")
    print(f"\n     {REDIRECT_URI}")
    print(f"\n  Steps:")
    print(f"    1. developer.spotify.com/dashboard → Your App → Settings")
    print(f"    2. Redirect URIs → Add URI → paste the URL above → Save\n")
    input("Press Enter once the Redirect URI is saved in Spotify...")

    auth_params = urllib.parse.urlencode({
        "client_id":     CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  REDIRECT_URI,
        "scope":         SCOPES,
    })
    auth_url = f"https://accounts.spotify.com/authorize?{auth_params}"

    print("\nOpening browser for authorization...")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", PORT), _CallbackHandler)
    print(f"Waiting for Spotify to redirect to localhost:{PORT}...")
    server.handle_request()   # handles exactly one request (the callback)

    code = _received_code["value"]
    if not code:
        print("❌ No authorization code received. Did you allow the request?")
        sys.exit(1)

    # Exchange authorization code → access_token + refresh_token
    credentials = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data={
            "grant_type":   "authorization_code",
            "code":         code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=10,
    )

    if r.status_code != 200:
        print(f"❌ Token exchange failed ({r.status_code}): {r.text}")
        sys.exit(1)

    data          = r.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        print("❌ Spotify did not return a refresh_token:", data)
        sys.exit(1)

    # Persist to .env
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    set_key(env_path, "SPOTIFY_REFRESH_TOKEN", refresh_token)

    print("\n✓  SPOTIFY_REFRESH_TOKEN saved to .env")
    print("✓  The bot will now use the Spotify queue API for 'Now Playing' tracks.")
    print("\nRestart the bot (upstats.py) to apply.\n")


if __name__ == "__main__":
    main()
