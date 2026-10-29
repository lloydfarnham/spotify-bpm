# Spotify Liked Songs: BPM lookup

A small proof of concept that finds the BPM (tempo) of your Spotify Liked Songs.
It only reads from Spotify. The only scope it requests is `user-library-read`,
so it can't change your account.

BPMs come from several sources, tried in this order:

| Source | How | Accuracy |
|---|---|---|
| `override` | A value you set by hand | Yours |
| `reccobeats` | [ReccoBeats](https://reccobeats.com) database, looked up by Spotify ID | Good, but few releases since early 2025 |
| `beatport` | [Beatport](https://www.beatport.com) search, accepted only if the ISRC matches | Label-supplied; best for electronic music |
| `beatport_title` | Beatport, exact title + artist (+ same mix) when no ISRC matched | Usually right; check if it looks off |
| `estimate` | 30-second Deezer preview analysed with [DeepRhythm](https://github.com/bleugreen/deeprhythm) | ~70% exact, ~90% right apart from half/double time |

Every value is stored in `bpm.db` (SQLite) with its source, so you can always
tell a database value from an estimate.

## 1. Create the Spotify app

1. Go to <https://developer.spotify.com/dashboard> and log in.
2. Click **Create app**.
   - Name/description: anything, e.g. "BPM coverage check".
   - **Redirect URIs:** add both of these, exactly (click **Add** after each):
     - `http://127.0.0.1:8888/callback` for the scripts. Spotify no longer accepts `localhost`.
     - `https://lloydfarnham.github.io/spotify-bpm/` for the web app.
       (Optional: also `http://127.0.0.1:8000/` to log in when testing it locally.)
   - **APIs used:** tick **Web API**.
3. Save, open the app's **Settings**, and copy the **Client ID**. You don't
   need a client secret, because the script uses PKCE.

The app stays in Development Mode, which is fine. As the app owner you're
already allowed to use it.

## 2. Configure

```bash
pip install -r requirements.txt
```

For preview-based estimates, also install DeepRhythm. This pulls in PyTorch,
about 500 MB:

```bash
pip install -r requirements-analysis.txt
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
Pass `--refresh` to pick up newly liked songs.

```bash
python check_bpm.py
```

This looks up new tracks on ReccoBeats in batches of 40, about two requests per
second, backing off on HTTP 429. It then prints a coverage report: totals, BPMs
by source, coverage by year added and release decade, and a 20-track random
sample to check by ear. It also writes `results.csv`.

```bash
python fill_gaps.py
```

This tries Beatport, then the preview estimate, for up to 30 tracks without a
BPM, newest first. It waits 3 seconds between Beatport requests. Run it again
to do the next 30, then re-run `python check_bpm.py --report-only` to see the
updated coverage.

Useful flags:

| Command | Effect |
|---|---|
| `check_bpm.py --report-only` | No API calls, just report |
| `check_bpm.py --retry-missing` | Re-query tracks ReccoBeats didn't have |
| `check_bpm.py --set SPOTIFY_ID 128` | Set a BPM by hand (beats every other source) |
| `check_bpm.py --unset SPOTIFY_ID` | Remove that override |
| `fill_gaps.py --limit 100` | Process more tracks this run |
| `fill_gaps.py --no-estimate` | Beatport only, no preview analysis |
| `fill_gaps.py --retry` | Re-check tracks that found nothing before |
| `fill_gaps.py --verify` | Cross-check existing BPMs against preview estimates; fixes duplicate Beatport listings, flags disputes |
| `check_bpm.py --seed N` | Use the same random sample each run |

`results.csv` has one row per track: name, artists, release_year, added_at,
spotify_id, bpm, bpm_source, the value from each source (reccobeats_tempo,
beatport_bpm, estimated_bpm, estimate_confidence), energy, danceability, and
status (`found` or `not_found`).

## 4. Web app

`docs/` is a static web app, published by GitHub Pages at
<https://lloydfarnham.github.io/spotify-bpm/>. It works on phones too. It shows every liked song with
its BPM and source, and lets you search, filter by BPM range (optionally
counting half/double time), and sort by BPM, date added, title, artist or
release year.

Press **Connect Spotify** to log in. You can then:

- **Play from any row.** It starts that song and queues the next 49 in the
  current sort order, so sorting by BPM gives a tempo-ordered queue.
- **Pick where it plays** from the device list: your phone or computer's
  Spotify app, a speaker, or (desktop browsers only) the page itself.
  If the list is empty, open Spotify on the device you want first.
- **See new likes.** Songs liked since the last export appear marked NEW with
  their BPM pending.
- **Add to playlists.** The **+** on each row adds that song to one of your
  playlists (or a new one). **Add shown to playlist** adds everything the
  current filter shows, in the current order, e.g. all songs from 120 to 130 BPM.
  The first time, Spotify asks you to approve playlist editing.

BPMs under 90 are shown doubled (marked ×2, e.g. 72 → 144), so half-time
listings sort alongside full-tempo ones. The database keeps the original value.

Playback needs Spotify Premium. The data file is public: anyone with the URL
can see your liked songs and their BPMs.

## 5. Keeping it up to date

New BPMs can only be found on this computer, because the Beatport and preview
steps run here. The easiest way is from the web app:

```bash
python serve.py
```

Open <http://127.0.0.1:8000/> and press **Update BPMs**. It runs the whole
update here with a progress bar: re-fetch liked songs (optional; a Spotify tab
may open for approval), ReccoBeats, fill gaps, cross-check, and export. Tick
**Publish to GitHub Pages when done** to push the new data too. The list
reloads when it finishes. The button only appears on this computer; it's hidden
on GitHub Pages and other devices.

Or from the terminal:

```bash
python update.py --publish
```

This re-fetches your liked songs (browser login), looks up new ones on
ReccoBeats, fills gaps via Beatport and preview estimates, exports
`docs/data/tracks.json`, and commits and pushes it. GitHub Pages then updates
the site within a minute or two. Leave off `--publish` to update locally only.

## Things to know about the numbers

- **Half and double time.** Sources disagree on whether a track is, say, 70 or
  140 BPM. Beatport often lists dubstep and drum & bass at half tempo (e.g. 87
  rather than 174). Most estimate "errors" are this kind of disagreement rather
  than wrong tempos.
- **Estimates are estimates.** DeepRhythm hears only 30 seconds and reports a
  confidence (0–1) in `estimate_confidence`. Fix any you check by ear with
  `--set`.
- **Beatport is mostly electronic music**, so hip-hop, jazz, indie, etc. usually
  fall through to the estimate.
- **Beatport's terms of use** weren't checked (the page refuses automated
  access). Keep runs small and occasional, as the defaults do.

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

If you used an earlier version that kept `reccobeats_cache.json`, it's imported
into `bpm.db` automatically on the first run, and can then be deleted.

## Credits

BPM data provided by [Beatport](https://www.beatport.com) and
[ReccoBeats](https://reccobeats.com).
Previews from [Deezer](https://www.deezer.com). The project page in `docs/` is
published with GitHub Pages.
