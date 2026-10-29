# Spotify Liked Songs: ReccoBeats BPM coverage

A small proof of concept that measures how many of your Spotify Liked Songs
have BPM (tempo) data on [ReccoBeats](https://reccobeats.com). It only reads
from Spotify. The only scope it requests is `user-library-read`, so it can't
change your account.

## 1. Create the Spotify app

1. Go to <https://developer.spotify.com/dashboard> and log in.
2. Click **Create app**.
   - Name/description: anything, e.g. "BPM coverage check".
   - **Redirect URI:** `http://127.0.0.1:8888/callback` (exactly this, then click **Add**).
     Spotify no longer accepts `localhost`.
   - **APIs used:** tick **Web API**.
3. Save, open the app's **Settings**, and copy the **Client ID**. You don't
   need a client secret, because the script uses PKCE.

The app stays in Development Mode, which is fine. As the app owner you're
already allowed to use it.

## 2. Configure

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Edit `.env` and paste in your client ID:

```
SPOTIFY_CLIENT_ID=abc123...
```

## 3. Run

```bash
python fetch_liked_songs.py
```

This opens a browser window for you to approve read access, then fetches every
Liked Song (50 per page) into `liked_songs.json`. Later runs reuse that file.
Pass `--refresh` to fetch again.

```bash
python check_bpm.py
```

This looks up each track on ReccoBeats in batches of 40, about two requests per
second, backing off on HTTP 429. Results, including "not found", go into
`reccobeats_cache.json`, which is saved every 10 batches. If the run is
interrupted, just run it again and it carries on where it stopped. A
10,000-song library takes about 5 minutes the first time.

The script then prints:

- totals, and the number and % of tracks with and without a tempo
- coverage by the year you added each song, and by release decade
- a 20-track random sample of found tracks with BPM and a Spotify link to check by ear

It also writes `results.csv`, with one row per track: name, artists,
release_year, added_at, spotify_id, tempo, energy, danceability, and status
(`found` or `not_found`).

Useful flags:

| Flag | Effect |
|---|---|
| `--report-only` | No API calls, just report from the cache |
| `--retry-missing` | Re-query tracks cached as not found (in case ReccoBeats has added them since) |
| `--seed N` | Use the same random sample each run |

## How ReccoBeats results are matched to Spotify tracks

`GET https://api.reccobeats.com/v1/audio-features?ids=<id1>,<id2>,...` accepts
up to 40 Spotify track IDs. Each result has a ReccoBeats UUID as its `id`, and
the Spotify URL in `href`. Tracks it doesn't know about are simply left out of
the response.

The script maps each result back using the Spotify ID in `href`. If a result's
`href` doesn't match any requested ID, it falls back to matching by ISRC within
that batch. At the end of a query run it prints a mapping check showing how
many ISRCs agree between Spotify and ReccoBeats, how many differ, and how many
results couldn't be matched at all.

Spotify may not return ISRCs (`external_ids`) for Development Mode apps. If so,
the ISRC check will show zeros and only the `href` match is used.

## Credits

BPM data provided by [GetSongBPM](https://getsongbpm.com). Audio features from
[ReccoBeats](https://reccobeats.com). The project page in `docs/` is published
with GitHub Pages and carries the GetSongBPM backlink required for its API key.
