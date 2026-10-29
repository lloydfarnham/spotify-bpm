"""Export liked songs + best BPMs from bpm.db to docs/data/tracks.json for the web app.

Usage:
    python export_data.py
"""

import json
import os
import sys
import time

import bpmdb

SONGS_FILE = "liked_songs.json"
OUT_FILE = os.path.join("docs", "data", "tracks.json")


def bpm_fields(entries):
    """The BPM part of a track's row: chosen BPM, its source, and any dispute."""
    bpm, source = bpmdb.best(entries)
    fields = {"bpm": round(bpm, 1) if bpm is not None else None, "source": source}
    if source == "estimate":
        fields["conf"] = (entries["estimate"][1] or {}).get("confidence")
    dispute = bpmdb.disputed(entries)
    if dispute:
        fields["alt"], fields["altConf"] = dispute
    return fields


def main():
    if not os.path.exists(SONGS_FILE):
        sys.exit(f"{SONGS_FILE} not found. Run fetch_liked_songs.py first.")
    with open(SONGS_FILE, encoding="utf-8") as f:
        tracks = [t for t in json.load(f) if t["spotify_id"]]
    lookups = bpmdb.load(bpmdb.connect())

    rows = []
    for t in tracks:
        e = lookups.get(t["spotify_id"], {})
        rb = e.get("reccobeats", (None, None))[1] or {}
        row = {
            "id": t["spotify_id"],
            "name": t["name"],
            "artists": t["artists"],
            "album": t.get("album"),
            "released": t.get("release_date"),
            "added": t.get("added_at"),
            **bpm_fields(e),
        }
        if rb.get("energy") is not None:
            row["energy"] = rb["energy"]
            row["dance"] = rb.get("danceability")
        rows.append(row)

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tracks": rows},
                  f, ensure_ascii=False, separators=(",", ":"))
    with_bpm = sum(1 for r in rows if r["bpm"] is not None)
    disputed = sum(1 for r in rows if "alt" in r)
    print(f"Wrote {len(rows)} tracks ({with_bpm} with BPM, {disputed} disputed) to {OUT_FILE} "
          f"({os.path.getsize(OUT_FILE) // 1024} KB).")


if __name__ == "__main__":
    main()
