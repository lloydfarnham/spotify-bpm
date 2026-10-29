"""Fill in BPMs that ReccoBeats doesn't have, a few tracks per run, and
cross-check existing BPMs against preview analysis.

For each liked song without a BPM (run check_bpm.py first), newest first:
  1. Beatport: search by artist + title and accept results whose ISRC
     matches Spotify's. If none does, accept an exact title + artist match
     (stored separately as "beatport_title"). Beatport sometimes lists the
     same recording more than once with different BPMs; the preview estimate
     then decides which listing to trust.
  2. Otherwise download the 30-second Deezer preview (found by ISRC) and
     estimate the tempo with DeepRhythm (stored as "estimate").

With --verify, tracks that already have a BPM get a preview estimate too.
If it disagrees with a Beatport BPM, all Beatport listings are re-checked and
the one the estimate supports is used. Remaining confident disagreements are
flagged as disputed in the report and the web app.

Usage:
    python fill_gaps.py                 # next 30 tracks
    python fill_gaps.py --limit 100
    python fill_gaps.py --no-estimate   # Beatport only
    python fill_gaps.py --retry         # re-check tracks that found nothing before
    python fill_gaps.py --verify --limit 3000   # cross-check existing BPMs
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter

import requests

import bpmdb

SONGS_FILE = "liked_songs.json"
BEATPORT_SEARCH = "https://www.beatport.com/search/tracks"
BEATPORT_DELAY = 3  # seconds between Beatport requests
DEEZER_TRACK = "https://api.deezer.com/2.0/track/isrc:{}"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
PLAIN_MIXES = {"", "original mix", "original", "extended mix", "extended", "radio edit", "clean", "explicit"}
VERIFIABLE = ("reccobeats", "beatport", "beatport_title")


class Blocked(Exception):
    pass


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def base_title(name):
    """'Song (feat. X) - Y Remix' -> 'Song'."""
    return re.sub(r"\s*[\(\[].*?[\)\]]|\s+-\s+.*$", "", name).strip() or name


def find_tracks(obj):
    """Beatport search results are nested in the page's Next.js data."""
    if isinstance(obj, dict):
        if "bpm" in obj and "isrc" in obj:
            yield obj
        for v in obj.values():
            yield from find_tracks(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_tracks(v)


def beatport_search(session, query):
    resp = session.get(BEATPORT_SEARCH, params={"q": query}, timeout=30)
    if resp.status_code in (403, 429):
        raise Blocked(resp.status_code)
    resp.raise_for_status()
    m = NEXT_DATA.search(resp.text)
    return list(find_tracks(json.loads(m.group(1)))) if m else []


def mix_ok(spotify_name, mix_name):
    """Only title-match a remix to the same remix, and an original to an original."""
    mix = norm(mix_name)
    if mix in PLAIN_MIXES:
        return not re.search(r"remix|rmx|edit|dub|vip|bootleg|rework", spotify_name, re.I)
    return mix in norm(spotify_name)


def beatport_candidates(session, track):
    """Return (source, [(bpm, data), ...]) for every matching Beatport listing.

    source is "beatport" for ISRC matches, "beatport_title" if only the
    title + artist matched, or None with an empty list.
    """
    query = f"{track['artists'][0]} {base_title(track['name'])}"
    results = [r for r in beatport_search(session, query) if r.get("bpm")]

    def data(r):
        genre = r.get("genre")
        genre = genre[0] if isinstance(genre, list) and genre else genre
        return {"beatport_id": r.get("track_id"), "mix": r.get("mix_name"),
                "genre": (genre or {}).get("genre_name"), "isrc": r.get("isrc"), "query": query}

    if track.get("isrc"):
        isrc_matches = [(float(r["bpm"]), data(r)) for r in results if r.get("isrc") == track["isrc"]]
        if isrc_matches:
            return "beatport", isrc_matches

    title = norm(base_title(track["name"]))
    artists = {norm(a) for a in track["artists"]}
    title_matches = []
    for r in results:
        bp_artists = {norm(a.get("artist_name") or a.get("name")) for a in r.get("artists") or []}
        if (norm(r.get("track_name") or r.get("name")) == title and artists & bp_artists
                and mix_ok(track["name"], r.get("mix_name"))):
            title_matches.append((float(r["bpm"]), data(r)))
    return ("beatport_title", title_matches) if title_matches else (None, [])


def choose(candidates, estimate_bpm):
    """Pick one listing when several match.

    Prefer the listing whose BPM the preview estimate supports (counting
    half/double time); otherwise the BPM most listings agree on.
    """
    bpms = [bpm for bpm, _ in candidates]
    resolved_by = None
    if len(set(bpms)) > 1:
        supported = [c for c in candidates if estimate_bpm and bpmdb.same_tempo(estimate_bpm, c[0])]
        if supported:
            pick, resolved_by = supported[0], "estimate"
        else:
            common = Counter(bpms).most_common(1)[0][0]
            pick, resolved_by = next(c for c in candidates if c[0] == common), "majority"
    else:
        pick = candidates[0]
    bpm, data = pick
    data = dict(data)
    if resolved_by:
        data["candidates"] = sorted(set(bpms))
        data["resolved_by"] = resolved_by
    return bpm, data


def save_beatport(db, sid, source, bpm, data):
    bpmdb.save(db, sid, "beatport", bpm if source == "beatport" else None, data)
    if source == "beatport_title":
        bpmdb.save(db, sid, "beatport_title", bpm, data)
    else:
        bpmdb.delete(db, sid, "beatport_title")


def deezer_preview(session, isrc):
    for attempt in range(5):
        j = session.get(DEEZER_TRACK.format(isrc), timeout=20).json()
        if j.get("error", {}).get("code") == 4:  # quota exceeded
            time.sleep(5 * (attempt + 1))
            continue
        return None if "error" in j else j
    return None


def estimate(session, predictor, track):
    """Return (bpm, data) from the Deezer preview, or (None, data)."""
    if not track.get("isrc"):
        return None, {"reason": "no ISRC"}
    try:
        dz = deezer_preview(session, track["isrc"])
        if not dz or not dz.get("preview"):
            return None, {"reason": "no Deezer preview"}
        audio = session.get(dz["preview"], timeout=30).content
    except (requests.RequestException, ValueError) as e:
        return None, {"reason": f"network error: {e}"[:200]}
    fd, path = tempfile.mkstemp(suffix=".mp3")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        bpm, confidence = predictor.predict(path, include_confidence=True)
    except Exception as e:  # e.g. a truncated or silent preview DeepRhythm can't read
        return None, {"reason": f"analysis failed: {type(e).__name__}", "deezer_id": dz.get("id")}
    finally:
        os.remove(path)
    return round(float(bpm), 1), {"confidence": round(float(confidence), 3), "deezer_id": dz.get("id")}


def load_predictor():
    try:
        from deeprhythm import DeepRhythmPredictor
    except ImportError:
        sys.exit("DeepRhythm isn't installed. Run: pip install -r requirements-analysis.txt\n"
                 "(or use --no-estimate to skip preview analysis)")
    return DeepRhythmPredictor()


def fill(db, tracks, lookups, session, predictor, args):
    def pending(t):
        e = lookups.get(t["spotify_id"], {})
        if "reccobeats" not in e or bpmdb.best(e)[0] is not None:
            return False
        if args.retry:
            return True
        return "beatport" not in e or (predictor and "estimate" not in e)

    todo = sorted((t for t in tracks if pending(t)), key=lambda t: t.get("added_at") or "", reverse=True)
    if not todo:
        print("Nothing to fill in.")
        return
    batch = todo[:args.limit]
    print(f"{len(todo)} tracks without a BPM; processing {len(batch)} (newest first).")
    counts = Counter()

    for n, t in enumerate(batch, 1):
        sid, e = t["spotify_id"], lookups.get(t["spotify_id"], {})
        label = f"{', '.join(t['artists'])} - {t['name']}"[:60]
        est_bpm = None

        if args.retry or "beatport" not in e:
            source, cands = beatport_candidates(session, t)
            time.sleep(BEATPORT_DELAY)
            if source:
                if predictor and len({c[0] for c in cands}) > 1:
                    est_bpm, est_data = estimate(session, predictor, t)
                    bpmdb.save(db, sid, "estimate", est_bpm, est_data)
                bpm, data = choose(cands, est_bpm)
                save_beatport(db, sid, source, bpm, data)
                db.commit()
                counts[source] += 1
                note = f" (listings {data['candidates']}, by {data['resolved_by']})" if "candidates" in data else ""
                print(f"  [{n}/{len(batch)}] {bpm:6.1f}  {source:<15} {label}{note}")
                continue
            bpmdb.save(db, sid, "beatport", None)
            db.commit()

        if predictor and (args.retry or "estimate" not in e):
            bpm, data = estimate(session, predictor, t)
            bpmdb.save(db, sid, "estimate", bpm, data)
            db.commit()
            if bpm is not None:
                counts["estimate"] += 1
                print(f"  [{n}/{len(batch)}] {bpm:6.1f}  estimate ({data['confidence']:.2f})  {label}")
                continue

        counts["none"] += 1
        print(f"  [{n}/{len(batch)}]    ---  not found       {label}")

    print(f"\nThis run: Beatport (ISRC) {counts['beatport']}, Beatport (title) {counts['beatport_title']}, "
          f"estimated {counts['estimate']}, nothing {counts['none']}.")
    left = len(todo) - len(batch)
    if left > 0:
        print(f"{left} tracks still to process; run again to continue.")


def verify(db, tracks, lookups, session, predictor, args):
    def needs_verify(t):
        e = lookups.get(t["spotify_id"], {})
        return bpmdb.best(e)[1] in VERIFIABLE and "estimate" not in e

    todo = sorted((t for t in tracks if needs_verify(t)), key=lambda t: t.get("added_at") or "", reverse=True)
    if not todo:
        print("All BPMs already cross-checked.")
        return
    batch = todo[:args.limit]
    print(f"{len(todo)} BPMs not yet cross-checked; checking {len(batch)} (newest first).")
    counts = Counter()

    for n, t in enumerate(batch, 1):
        if n % 25 == 0:
            print(f"  ... {n}/{len(batch)} checked", flush=True)
        sid = t["spotify_id"]
        e = lookups.get(sid, {})
        bpm, source = bpmdb.best(e)
        label = f"{', '.join(t['artists'])} - {t['name']}"[:55]
        est_bpm, est_data = estimate(session, predictor, t)
        bpmdb.save(db, sid, "estimate", est_bpm, est_data)
        db.commit()
        if est_bpm is None:
            counts["no preview"] += 1
            continue
        if bpmdb.same_tempo(est_bpm, bpm):
            counts["agree"] += 1
            continue

        if source in ("beatport", "beatport_title"):
            new_source, cands = beatport_candidates(session, t)
            time.sleep(BEATPORT_DELAY)
            if new_source:
                new_bpm, data = choose(cands, est_bpm)
                if new_bpm != bpm and bpmdb.same_tempo(est_bpm, new_bpm):
                    data["replaced"] = bpm
                    save_beatport(db, sid, new_source, new_bpm, data)
                    db.commit()
                    counts["fixed"] += 1
                    print(f"  [{n}/{len(batch)}] FIXED     {bpm:6.1f} -> {new_bpm:6.1f} "
                          f"(listings {sorted({c[0] for c in cands})}, estimate {est_bpm})  {label}")
                    continue

        if est_data["confidence"] >= bpmdb.DISPUTE_CONFIDENCE:
            counts["disputed"] += 1
            print(f"  [{n}/{len(batch)}] DISPUTED  {source} {bpm:6.1f} vs estimate {est_bpm:6.1f} "
                  f"({est_data['confidence']:.2f})  {label}")
        else:
            counts["unsure"] += 1

    print(f"\nCross-check: {counts['agree']} agree, {counts['fixed']} fixed from another Beatport listing, "
          f"{counts['disputed']} disputed, {counts['unsure']} low-confidence disagreements (ignored), "
          f"{counts['no preview']} without a preview.")
    left = len(todo) - len(batch)
    if left > 0:
        print(f"{left} BPMs still to cross-check; run again to continue.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=30, help="max tracks to process this run (default 30)")
    parser.add_argument("--no-estimate", action="store_true", help="Beatport only; skip preview analysis")
    parser.add_argument("--retry", action="store_true", help="re-check tracks that found nothing before")
    parser.add_argument("--verify", action="store_true", help="cross-check existing BPMs against previews")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if not os.path.exists(SONGS_FILE):
        sys.exit(f"{SONGS_FILE} not found. Run fetch_liked_songs.py first.")
    with open(SONGS_FILE, encoding="utf-8") as f:
        tracks = [t for t in json.load(f) if t["spotify_id"]]

    db = bpmdb.connect()
    lookups = bpmdb.load(db)
    unchecked = sum(1 for t in tracks if "reccobeats" not in lookups.get(t["spotify_id"], {}))
    if unchecked:
        print(f"Note: {unchecked} tracks haven't been checked on ReccoBeats yet; run check_bpm.py first.")

    if args.verify and args.no_estimate:
        sys.exit("--verify needs preview analysis; drop --no-estimate.")
    predictor = None if args.no_estimate else load_predictor()
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    try:
        if args.verify:
            verify(db, tracks, lookups, session, predictor, args)
        else:
            fill(db, tracks, lookups, session, predictor, args)
    except Blocked as err:
        print(f"Beatport refused the request (HTTP {err}). Stopping; progress is saved, try again later.")
    except KeyboardInterrupt:
        print("\nInterrupted; progress saved.")
    finally:
        db.commit()
    print("Run `python check_bpm.py --report-only` for the updated coverage report.")


if __name__ == "__main__":
    main()
