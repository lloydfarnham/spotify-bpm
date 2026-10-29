"""SQLite store shared by check_bpm.py and fill_gaps.py.

Every lookup is one row per (track, source). A NULL bpm means the source was
checked and had nothing, so it isn't queried again.
"""

import json
import sqlite3
import time

DB_FILE = "bpm.db"

# Highest priority first: a manual override beats everything, and a
# preview-based estimate is only used when nothing better exists.
SOURCES = ("override", "reccobeats", "beatport", "beatport_title", "estimate")


def connect(path=DB_FILE):
    db = sqlite3.connect(path)
    db.execute("""
        CREATE TABLE IF NOT EXISTS lookups (
            spotify_id TEXT NOT NULL,
            source     TEXT NOT NULL,
            bpm        REAL,
            data       TEXT,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (spotify_id, source)
        )""")
    return db


def save(db, spotify_id, source, bpm, data=None):
    db.execute(
        "INSERT OR REPLACE INTO lookups VALUES (?, ?, ?, ?, ?)",
        (spotify_id, source, bpm, json.dumps(data) if data else None,
         time.strftime("%Y-%m-%dT%H:%M:%S")),
    )


def delete(db, spotify_id, source):
    db.execute("DELETE FROM lookups WHERE spotify_id = ? AND source = ?", (spotify_id, source))


def load(db):
    """Return {spotify_id: {source: (bpm, data)}}."""
    out = {}
    for sid, source, bpm, data, _ in db.execute("SELECT * FROM lookups"):
        out.setdefault(sid, {})[source] = (bpm, json.loads(data) if data else None)
    return out


def best(entries):
    """Return (bpm, source) from the highest-priority source that has one."""
    for source in SOURCES:
        bpm = entries.get(source, (None, None))[0]
        if bpm is not None:
            return bpm, source
    return None, None


# Estimates never replace a database BPM, but a confident one that disagrees gets flagged.
DISPUTE_CONFIDENCE = 0.8


def same_tempo(a, b, tolerance=0.04):
    """True if a and b are the same tempo, allowing for half/double-time counting."""
    return any(abs(a * f - b) <= tolerance * b for f in (1, 2, 0.5))


def disputed(entries):
    """Return (estimate_bpm, confidence) if a confident estimate contradicts the chosen BPM."""
    bpm, source = best(entries)
    est_bpm, est = entries.get("estimate", (None, None))
    if bpm is None or est_bpm is None or source in ("estimate", "override"):
        return None
    confidence = (est or {}).get("confidence") or 0
    if confidence >= DISPUTE_CONFIDENCE and not same_tempo(est_bpm, bpm):
        return est_bpm, confidence
    return None
