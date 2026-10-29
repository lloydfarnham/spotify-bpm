"""Look up ReccoBeats audio features for every track in liked_songs.json and
report BPM coverage across all sources in bpm.db.

ReccoBeats results (including "not found") are stored in bpm.db, so re-runs
only query new tracks. Run fill_gaps.py to fill in what ReccoBeats lacks.

Usage:
    python check_bpm.py                  # query new tracks, then report
    python check_bpm.py --report-only    # skip the API, just report
    python check_bpm.py --retry-missing  # re-query tracks ReccoBeats didn't have
    python check_bpm.py --set SPOTIFY_ID 128   # manual BPM override
    python check_bpm.py --unset SPOTIFY_ID     # remove an override
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict

import requests

import bpmdb

API = "https://api.reccobeats.com/v1/audio-features"
BATCH_SIZE = 40  # API returns 400 "size must be between 1 and 40" above this
DELAY = 0.5  # seconds between requests
SONGS_FILE = "liked_songs.json"
OLD_CACHE_FILE = "reccobeats_cache.json"
CSV_FILE = "results.csv"
FEATURE_KEYS = ("tempo", "energy", "danceability", "valence", "key", "mode", "loudness", "isrc", "id")


def import_old_cache(db):
    """One-off migration of the JSON cache used before bpm.db existed."""
    if not os.path.exists(OLD_CACHE_FILE):
        return
    if db.execute("SELECT 1 FROM lookups WHERE source = 'reccobeats' LIMIT 1").fetchone():
        return
    with open(OLD_CACHE_FILE, encoding="utf-8") as f:
        cache = json.load(f)
    for sid, feat in cache.items():
        bpmdb.save(db, sid, "reccobeats", feat.get("tempo") if feat else None, feat)
    db.commit()
    print(f"Imported {len(cache)} ReccoBeats results from {OLD_CACHE_FILE} into {bpmdb.DB_FILE}.")


def spotify_id_from_href(href):
    if href and "open.spotify.com/track/" in href:
        return href.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return None


def fetch_batch(session, ids):
    for attempt in range(8):
        try:
            resp = session.get(API, params={"ids": ",".join(ids)}, timeout=30)
        except requests.RequestException as e:
            wait = 2 ** attempt
            print(f"  network error ({e}), retrying in {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 0)) or min(2 ** (attempt + 1), 120)
            print(f"  429 rate limited, waiting {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            wait = 2 ** (attempt + 1)
            print(f"  server error {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            sys.exit(f"ReccoBeats error {resp.status_code}: {resp.text[:500]}")
        return resp.json().get("content", [])
    sys.exit("Giving up after repeated ReccoBeats failures. Progress so far is saved; re-run to resume.")


def map_results(batch, results, isrc_by_id, stats):
    """Map ReccoBeats results back to the requested Spotify IDs.

    Primary key is the Spotify ID in `href`. If a result's href isn't one we
    asked for (e.g. Spotify relinked the track), fall back to a unique ISRC
    match among the still-unmatched IDs in this batch.
    """
    wanted = set(batch)
    mapped = {}
    leftovers = []
    for r in results:
        sid = spotify_id_from_href(r.get("href"))
        if sid in wanted and sid not in mapped:
            mapped[sid] = r
        else:
            leftovers.append(r)
    for r in leftovers:
        candidates = [s for s in batch if s not in mapped and r.get("isrc") and isrc_by_id.get(s) == r["isrc"]]
        if len(candidates) == 1:
            mapped[candidates[0]] = r
            stats["isrc_fallback"] += 1
        else:
            stats["unmapped"] += 1
    for sid, r in mapped.items():
        sp_isrc = isrc_by_id.get(sid)
        if sp_isrc and r.get("isrc"):
            stats["isrc_match" if sp_isrc == r["isrc"] else "isrc_mismatch"] += 1
    return mapped


def query_missing(db, tracks, lookups, retry_missing):
    isrc_by_id = {t["spotify_id"]: t.get("isrc") for t in tracks if t["spotify_id"]}

    def needs_query(sid):
        entry = lookups.get(sid, {}).get("reccobeats")
        return entry is None or (retry_missing and entry[0] is None)

    todo = [sid for sid in isrc_by_id if needs_query(sid)]
    if not todo:
        print("All tracks already checked on ReccoBeats; nothing to query.")
        return
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    print(f"Querying ReccoBeats for {len(todo)} tracks in {len(batches)} batches...")

    stats = defaultdict(int)
    session = requests.Session()
    session.headers["Accept"] = "application/json"
    try:
        for n, batch in enumerate(batches, 1):
            mapped = map_results(batch, fetch_batch(session, batch), isrc_by_id, stats)
            for sid in batch:
                r = mapped.get(sid)
                feat = {k: r.get(k) for k in FEATURE_KEYS} if r else None
                bpmdb.save(db, sid, "reccobeats", feat["tempo"] if feat else None, feat)
            if n % 10 == 0 or n == len(batches):
                db.commit()
                found = sum(1 for sid in batch if sid in mapped)
                print(f"  batch {n}/{len(batches)} done (last batch: {found}/{len(batch)} found)")
            time.sleep(DELAY)
    finally:
        db.commit()

    print("\nID mapping check:")
    print(f"  ISRC agrees with Spotify:    {stats['isrc_match']}")
    print(f"  ISRC differs from Spotify:   {stats['isrc_mismatch']}")
    print(f"  mapped via ISRC fallback:    {stats['isrc_fallback']}")
    print(f"  results that couldn't map:   {stats['unmapped']}")


def release_year(date):
    if date and date[:4].isdigit() and int(date[:4]) > 0:
        return int(date[:4])
    return None


def pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "   n/a"


def print_breakdown(title, rows, key_fn, label_fn=str):
    groups = defaultdict(lambda: [0, 0])
    for t, entries in rows:
        g = groups[key_fn(t)]
        g[0] += 1
        g[1] += 1 if bpmdb.best(entries)[0] is not None else 0
    print(f"\nCoverage by {title}:")
    print(f"  {'':<10}{'tracks':>8}{'found':>8}{'coverage':>10}")
    for k in sorted(groups, key=lambda k: (k is None, k)):
        total, found = groups[k]
        print(f"  {label_fn(k):<10}{total:>8}{found:>8}{pct(found, total):>10}")


def report(tracks, lookups, seed):
    rows = [(t, lookups.get(t["spotify_id"], {})) for t in tracks if t["spotify_id"]]
    skipped = len(tracks) - len(rows)
    total = len(rows)
    sources = Counter(bpmdb.best(e)[1] for _, e in rows)
    found = total - sources[None]
    unqueried = sum(1 for _, e in rows if "reccobeats" not in e)
    unfilled = sum(1 for _, e in rows
                   if bpmdb.best(e)[0] is None and "reccobeats" in e and "beatport" not in e)

    print("\n=== Summary ===")
    print(f"Total liked songs:   {len(tracks)}")
    if skipped:
        print(f"  (skipped {skipped} local/unavailable tracks with no Spotify ID)")
    print(f"With BPM:            {found} ({pct(found, total).strip()})")
    print(f"Missing BPM:         {total - found} ({pct(total - found, total).strip()})")
    if unqueried:
        print(f"  ({unqueried} not checked on ReccoBeats yet; run without --report-only)")
    if unfilled:
        print(f"  ({unfilled} not run through fill_gaps.py yet)")

    print("\nBPM by source:")
    for source in bpmdb.SOURCES:
        print(f"  {source:<16}{sources[source]:>6}  {pct(sources[source], total)}")
    verified = sum(1 for _, e in rows if bpmdb.best(e)[1] in ("reccobeats", "beatport", "beatport_title")
                   and e.get("estimate", (None,))[0] is not None)
    disputed = sum(1 for _, e in rows if bpmdb.disputed(e))
    print(f"\nCross-checked against previews: {verified}; disputed (confident estimate disagrees): {disputed}")

    print_breakdown("year added", rows, lambda t: (t.get("added_at") or "")[:4] or None,
                    lambda k: k or "unknown")

    def decade(t):
        y = release_year(t.get("release_date"))
        return y // 10 * 10 if y else None
    print_breakdown("release decade", rows, decade, lambda k: f"{k}s" if k else "unknown")

    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["name", "artists", "release_year", "added_at", "spotify_id",
                    "bpm", "bpm_source", "reccobeats_tempo", "beatport_bpm", "estimated_bpm",
                    "estimate_confidence", "disputed", "energy", "danceability", "status"])
        for t, e in rows:
            bpm, source = bpmdb.best(e)
            rb_bpm, rb = e.get("reccobeats", (None, None))
            bp_bpm = e.get("beatport", (None,))[0] or e.get("beatport_title", (None,))[0]
            est_bpm, est = e.get("estimate", (None, None))
            w.writerow([
                t["name"], "; ".join(t["artists"]), release_year(t.get("release_date")) or "",
                t.get("added_at") or "", t["spotify_id"],
                bpm if bpm is not None else "", source or "",
                rb_bpm if rb_bpm is not None else "",
                bp_bpm if bp_bpm is not None else "",
                est_bpm if est_bpm is not None else "",
                (est or {}).get("confidence", "") if est_bpm is not None else "",
                "yes" if bpmdb.disputed(e) else "",
                (rb or {}).get("energy", ""), (rb or {}).get("danceability", ""),
                "found" if bpm is not None else "not_found",
            ])
    print(f"\nWrote {len(rows)} rows to {CSV_FILE}")

    with_bpm = [(t, bpmdb.best(e)) for t, e in rows if bpmdb.best(e)[0] is not None]
    sample = random.Random(seed).sample(with_bpm, min(20, len(with_bpm)))
    print(f"\n=== Random sample of {len(sample)} tracks with a BPM (seed {seed}) ===")
    for t, (bpm, source) in sample:
        artists = ", ".join(t["artists"])
        print(f"  {bpm:6.1f} BPM  [{source}]  {artists} - {t['name']}")
        print(f"              https://open.spotify.com/track/{t['spotify_id']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report-only", action="store_true", help="don't call ReccoBeats, just report")
    parser.add_argument("--retry-missing", action="store_true", help="re-query tracks ReccoBeats didn't have")
    parser.add_argument("--seed", type=int, default=None, help="random seed for the sample (default: random)")
    parser.add_argument("--set", nargs=2, metavar=("SPOTIFY_ID", "BPM"), help="set a manual BPM override")
    parser.add_argument("--unset", metavar="SPOTIFY_ID", help="remove a manual BPM override")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    db = bpmdb.connect()
    if args.set:
        sid, bpm = args.set
        bpmdb.save(db, sid, "override", float(bpm))
        db.commit()
        print(f"Set {sid} to {float(bpm)} BPM.")
        return
    if args.unset:
        bpmdb.delete(db, args.unset, "override")
        db.commit()
        print(f"Removed override for {args.unset}.")
        return

    if not os.path.exists(SONGS_FILE):
        sys.exit(f"{SONGS_FILE} not found. Run fetch_liked_songs.py first.")
    with open(SONGS_FILE, encoding="utf-8") as f:
        tracks = json.load(f)
    import_old_cache(db)

    if not args.report_only:
        try:
            query_missing(db, tracks, bpmdb.load(db), args.retry_missing)
        except KeyboardInterrupt:
            print("\nInterrupted; progress saved. Re-run to resume.")
            return

    seed = args.seed if args.seed is not None else random.randrange(10_000)
    report(tracks, bpmdb.load(db), seed)


if __name__ == "__main__":
    main()
