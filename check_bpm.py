"""Look up ReccoBeats audio features for every track in liked_songs.json and
report how many have a BPM (tempo) value.

Writes reccobeats_cache.json (including "not found" entries, stored as null)
and results.csv. Re-runs only query tracks missing from the cache.

Usage:
    python check_bpm.py                # query missing tracks, then report
    python check_bpm.py --report-only  # skip the API, report from cache
    python check_bpm.py --retry-missing  # re-query tracks cached as not found
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from collections import defaultdict

import requests

API = "https://api.reccobeats.com/v1/audio-features"
BATCH_SIZE = 40  # API returns 400 "size must be between 1 and 40" above this
DELAY = 0.5  # seconds between requests
SONGS_FILE = "liked_songs.json"
CACHE_FILE = "reccobeats_cache.json"
CSV_FILE = "results.csv"
FEATURE_KEYS = ("tempo", "energy", "danceability", "valence", "key", "mode", "loudness", "isrc", "id")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_cache(cache):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)


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
    sys.exit("Giving up after repeated ReccoBeats failures. Progress so far is cached; re-run to resume.")


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


def query_missing(tracks, cache, retry_missing):
    isrc_by_id = {t["spotify_id"]: t.get("isrc") for t in tracks if t["spotify_id"]}
    todo = list(dict.fromkeys(
        sid for sid in isrc_by_id
        if sid not in cache or (retry_missing and cache[sid] is None)
    ))
    if not todo:
        print("All tracks already cached; nothing to query.")
        return
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    print(f"Querying ReccoBeats for {len(todo)} tracks in {len(batches)} batches...")

    stats = defaultdict(int)
    session = requests.Session()
    session.headers["Accept"] = "application/json"
    for n, batch in enumerate(batches, 1):
        mapped = map_results(batch, fetch_batch(session, batch), isrc_by_id, stats)
        for sid in batch:
            r = mapped.get(sid)
            cache[sid] = {k: r.get(k) for k in FEATURE_KEYS} if r else None
        if n % 10 == 0 or n == len(batches):
            save_cache(cache)
            found = sum(1 for sid in batch if cache[sid])
            print(f"  batch {n}/{len(batches)} done (last batch: {found}/{len(batch)} found)")
        time.sleep(DELAY)

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
    for t, feat in rows:
        g = groups[key_fn(t)]
        g[0] += 1
        g[1] += 1 if feat and feat.get("tempo") is not None else 0
    print(f"\nCoverage by {title}:")
    print(f"  {'':<10}{'tracks':>8}{'found':>8}{'coverage':>10}")
    for k in sorted(groups, key=lambda k: (k is None, k)):
        total, found = groups[k]
        print(f"  {label_fn(k):<10}{total:>8}{found:>8}{pct(found, total):>10}")


def report(tracks, cache, seed):
    rows = [(t, cache.get(t["spotify_id"])) for t in tracks if t["spotify_id"]]
    skipped = len(tracks) - len(rows)
    total = len(rows)
    found = [(t, f) for t, f in rows if f and f.get("tempo") is not None]
    unqueried = sum(1 for t, _ in rows if t["spotify_id"] not in cache)

    print("\n=== Summary ===")
    print(f"Total liked songs:   {len(tracks)}")
    if skipped:
        print(f"  (skipped {skipped} local/unavailable tracks with no Spotify ID)")
    print(f"With tempo:          {len(found)} ({pct(len(found), total).strip()})")
    print(f"Missing tempo:       {total - len(found)} ({pct(total - len(found), total).strip()})")
    if unqueried:
        print(f"  ({unqueried} of those not queried yet; run without --report-only)")

    print_breakdown("year added", rows, lambda t: (t.get("added_at") or "")[:4] or None,
                    lambda k: k or "unknown")

    def decade(t):
        y = release_year(t.get("release_date"))
        return y // 10 * 10 if y else None
    print_breakdown("release decade", rows, decade, lambda k: f"{k}s" if k else "unknown")

    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["name", "artists", "release_year", "added_at", "spotify_id",
                    "tempo", "energy", "danceability", "status"])
        for t, feat in rows:
            ok = bool(feat and feat.get("tempo") is not None)
            w.writerow([
                t["name"], "; ".join(t["artists"]), release_year(t.get("release_date")) or "",
                t.get("added_at") or "", t["spotify_id"],
                feat["tempo"] if ok else "",
                feat.get("energy", "") if ok else "",
                feat.get("danceability", "") if ok else "",
                "found" if ok else "not_found",
            ])
    print(f"\nWrote {len(rows)} rows to {CSV_FILE}")

    sample = random.Random(seed).sample(found, min(20, len(found)))
    print(f"\n=== Random sample of {len(sample)} found tracks (seed {seed}) ===")
    for t, feat in sample:
        artists = ", ".join(t["artists"])
        print(f"  {feat['tempo']:6.1f} BPM  {artists} - {t['name']}")
        print(f"              https://open.spotify.com/track/{t['spotify_id']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report-only", action="store_true", help="don't call ReccoBeats, just report from cache")
    parser.add_argument("--retry-missing", action="store_true", help="re-query tracks cached as not found")
    parser.add_argument("--seed", type=int, default=None, help="random seed for the sample (default: random)")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    tracks = load_json(SONGS_FILE, None)
    if tracks is None:
        sys.exit(f"{SONGS_FILE} not found. Run fetch_liked_songs.py first.")
    cache = load_json(CACHE_FILE, {})

    if not args.report_only:
        try:
            query_missing(tracks, cache, args.retry_missing)
        except KeyboardInterrupt:
            save_cache(cache)
            print("\nInterrupted; progress saved. Re-run to resume.")
            return

    seed = args.seed if args.seed is not None else random.randrange(10_000)
    report(tracks, cache, seed)


if __name__ == "__main__":
    main()
