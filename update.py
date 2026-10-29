"""Refresh everything and (optionally) publish the web app data.

Runs, in order:
  1. fetch_liked_songs.py --refresh  (opens a browser for Spotify login)
  2. check_bpm.py                    (ReccoBeats for new songs + report)
  3. fill_gaps.py                    (Beatport, then preview estimates)
  4. export_data.py                  (writes docs/data/tracks.json)
  5. with --publish: commit and push docs/data/tracks.json to GitHub Pages

Usage:
    python update.py --publish
    python update.py --no-fetch     # skip the Spotify login, reuse liked_songs.json
"""

import argparse
import subprocess
import sys

DATA_FILE = "docs/data/tracks.json"


def run(*cmd):
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    if subprocess.run(cmd).returncode != 0:
        sys.exit(f"Stopped: `{' '.join(cmd)}` failed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-fetch", action="store_true", help="don't re-fetch liked songs from Spotify")
    parser.add_argument("--limit", default="200", help="max tracks for fill_gaps.py (default 200)")
    parser.add_argument("--publish", action="store_true", help="commit and push the exported data")
    args = parser.parse_args()

    py = sys.executable
    if not args.no_fetch:
        run(py, "fetch_liked_songs.py", "--refresh")
    run(py, "check_bpm.py", "--seed", "0")
    run(py, "fill_gaps.py", "--limit", args.limit)
    run(py, "export_data.py")

    if args.publish:
        run("git", "add", DATA_FILE)
        if subprocess.run(["git", "diff", "--cached", "--quiet", "--", DATA_FILE]).returncode == 0:
            print("\nData unchanged; nothing to publish.")
            return
        run("git", "commit", "-m", "Update BPM data", "--", DATA_FILE)
        run("git", "push")
        print("\nPublished. GitHub Pages usually updates within a minute or two.")


if __name__ == "__main__":
    main()
