"""Fetch all Spotify Liked Songs (read-only) and cache them to liked_songs.json.

Uses the Authorization Code flow with PKCE, so only a client ID is needed.
Scope is user-library-read; nothing in the account is modified.

Usage:
    python fetch_liked_songs.py            # uses cache if present
    python fetch_liked_songs.py --refresh  # ignore cache and re-fetch
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import sys
import time
import urllib.parse
import webbrowser

import requests
from dotenv import load_dotenv

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "user-library-read"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
CACHE_FILE = "liked_songs.json"


class CallbackServer(http.server.HTTPServer):
    # On Windows, SO_REUSEADDR lets a second process bind the same port and
    # silently steal the callback, so claim the port exclusively instead.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def get_token(client_id):
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            # A callback from an older run (e.g. a leftover tab being reloaded)
            # carries a different state; ignore it and keep waiting.
            if qs.get("state", [None])[0] != state:
                msg = ("This login link is from an earlier run and was ignored. "
                       "Close this tab and use the newest one.")
            else:
                result["code"] = qs.get("code", [None])[0]
                result["error"] = qs.get("error", [None])[0]
                msg = ("Authorised. You can close this tab and return to the terminal."
                       if result["code"] else f"Authorisation failed: {result['error']}")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<p>{msg}</p>".encode())

        def log_message(self, *args):
            pass

    try:
        server = CallbackServer(("127.0.0.1", 8888), Handler)
    except OSError:
        sys.exit("Port 8888 is already in use. Another copy of this script may still be "
                 "running; close it (or its terminal) and try again.")
    server.timeout = 1

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("Opening browser for Spotify login. If it doesn't open, visit:\n" + url)
    webbrowser.open(url)

    deadline = time.time() + 300
    try:
        while not result and time.time() < deadline:
            server.handle_request()
    except KeyboardInterrupt:
        sys.exit("Cancelled.")
    finally:
        server.server_close()

    if result.get("error") or not result.get("code"):
        sys.exit(f"Authorisation failed: {result.get('error') or 'timed out waiting for login'}")

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        sys.exit(f"Token exchange failed ({resp.status_code}): {resp.text}")
    return resp.json()["access_token"]


def get_json(session, url, params=None):
    for attempt in range(8):
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            print(f"  Spotify rate limit hit, waiting {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code != 200:
            sys.exit(f"Spotify API error {resp.status_code} for {resp.url}: {resp.text}")
        return resp.json()
    sys.exit(f"Giving up on {url} after repeated failures.")


def simplify(item):
    track = item.get("track") or {}
    album = track.get("album") or {}
    return {
        "spotify_id": track.get("id"),
        "name": track.get("name"),
        "artists": [a.get("name") for a in track.get("artists") or []],
        "album": album.get("name"),
        "release_date": album.get("release_date"),
        "added_at": item.get("added_at"),
        "isrc": (track.get("external_ids") or {}).get("isrc"),
        "is_local": track.get("is_local", False),
    }


def fetch_all(token):
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    tracks = []
    url, params = f"{API}/me/tracks", {"limit": 50, "offset": 0}
    while url:
        page = get_json(session, url, params)
        tracks.extend(simplify(item) for item in page.get("items", []))
        print(f"  fetched {len(tracks)} / {page.get('total', '?')}")
        url, params = page.get("next"), None
        time.sleep(0.1)
    return tracks


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="ignore liked_songs.json and re-fetch")
    args = parser.parse_args()

    if os.path.exists(CACHE_FILE) and not args.refresh:
        with open(CACHE_FILE, encoding="utf-8") as f:
            tracks = json.load(f)
        print(f"{CACHE_FILE} already has {len(tracks)} tracks. Use --refresh to re-fetch.")
        return

    load_dotenv()
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    if not client_id:
        sys.exit("SPOTIFY_CLIENT_ID is not set. Copy .env.example to .env and fill it in.")

    token = get_token(client_id)
    print("Fetching Liked Songs...")
    tracks = fetch_all(token)

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(tracks, f, ensure_ascii=False, indent=1)
    with_isrc = sum(1 for t in tracks if t["isrc"])
    print(f"Saved {len(tracks)} tracks to {CACHE_FILE} ({with_isrc} with ISRC).")


if __name__ == "__main__":
    main()
