"""Serve the web app on this computer, with an "Update BPMs" button.

    python serve.py           # then open http://127.0.0.1:8000/

It's the same app as GitHub Pages, plus a small local API the page uses to run
the update scripts here and show their progress. The API only accepts requests
from this machine's own page.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import bpmdb
import export_data

ROOT = os.path.dirname(os.path.abspath(__file__))
SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
EDIT_LOCK = threading.Lock()
DOCS = os.path.join(ROOT, "docs")
DATA_FILE = "docs/data/tracks.json"
# Progress lines the scripts print: "[12/530]", "batch 10/74", "fetched 50 / 2954", "... 300/2334"
PROGRESS = re.compile(r"^\s*(?:\[|batch |fetched |\.\.\. )(\d+)\s?/\s?(\d+)")
NOISE = re.compile(r"id3|warning|conv2d|return F\.", re.I)


class Job:
    """Runs the update steps one after another in a background thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.steps = []
        self.cmds = []
        self.log = deque(maxlen=300)
        self.running = False
        self.error = None
        self.started = None
        self.finished = None
        self.proc = None
        self.cancelled = False

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running, "error": self.error,
                "started": self.started, "finished": self.finished,
                "steps": [dict(s) for s in self.steps], "log": list(self.log)[-60:],
            }

    def start(self, refresh, publish):
        py = sys.executable
        steps = []
        if refresh:
            steps.append(("Fetching liked songs (approve in the browser if asked)",
                          [py, "-u", "fetch_liked_songs.py", "--refresh"]))
        steps += [
            ("Checking ReccoBeats", [py, "-u", "check_bpm.py", "--seed", "0"]),
            ("Finding missing BPMs", [py, "-u", "fill_gaps.py", "--limit", "5000"]),
            ("Cross-checking BPMs", [py, "-u", "fill_gaps.py", "--verify", "--limit", "5000"]),
            ("Exporting data", [py, "-u", "export_data.py"]),
        ]
        if publish:
            steps.append(("Publishing to GitHub Pages", None))
        with self.lock:
            if self.running:
                return False
            self.steps = [{"name": n, "status": "pending", "done": 0, "total": 0} for n, _ in steps]
            self.cmds = [c for _, c in steps]
            self.log.clear()
            self.running, self.error, self.cancelled = True, None, False
            self.started, self.finished = time.time(), None
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def cancel(self):
        with self.lock:
            self.cancelled = True
            if self.proc:
                self.proc.terminate()

    def _update(self, i, **fields):
        with self.lock:
            self.steps[i].update(fields)

    def _run(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        try:
            for i, cmd in enumerate(self.cmds):
                self._update(i, status="running")
                ok = self._publish(env) if cmd is None else self._exec(i, cmd, env)
                if ok:
                    self._update(i, status="done")
                    continue
                self._update(i, status="cancelled" if self.cancelled else "failed")
                for j in range(i + 1, len(self.cmds)):
                    self._update(j, status="skipped")
                with self.lock:
                    self.error = "cancelled" if self.cancelled else f"{self.steps[i]['name']} failed (see log)"
                break
        finally:
            with self.lock:
                self.running, self.proc, self.finished = False, None, time.time()

    def _exec(self, i, cmd, env):
        if self.cancelled:
            return False
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        with self.lock:
            self.proc = proc
        for line in proc.stdout:
            line = line.rstrip()
            if not line or NOISE.search(line):
                continue
            with self.lock:
                self.log.append(line)
            m = PROGRESS.search(line)
            if m and i is not None:
                self._update(i, done=int(m.group(1)), total=int(m.group(2)))
        return proc.wait() == 0 and not self.cancelled

    def _publish(self, env):
        if not self._exec(None, ["git", "add", DATA_FILE], env):
            return False
        unchanged = subprocess.run(["git", "diff", "--cached", "--quiet", "--", DATA_FILE], cwd=ROOT).returncode == 0
        if unchanged:
            with self.lock:
                self.log.append("Data unchanged; nothing to publish.")
            return True
        return (self._exec(None, ["git", "commit", "-m", "Update BPM data", "--", DATA_FILE], env)
                and self._exec(None, ["git", "push"], env))


JOB = Job()


def set_override(spotify_id, bpm):
    """Save (or with bpm=None remove) a manual BPM, re-export, and return the track's BPM fields."""
    with EDIT_LOCK:
        db = bpmdb.connect()
        try:
            if bpm is None:
                bpmdb.delete(db, spotify_id, "override")
            else:
                bpmdb.save(db, spotify_id, "override", bpm)
            db.commit()
            rows = db.execute("SELECT source, bpm, data FROM lookups WHERE spotify_id = ?", (spotify_id,))
            entries = {s: (b, json.loads(d) if d else None) for s, b, d in rows}
        finally:
            db.close()
        try:
            export_data.main()  # keep docs/data/tracks.json in step, so a reload shows the change
        except SystemExit:
            pass  # no liked_songs.json yet; the override is still saved
    return export_data.bpm_fields(entries)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DOCS, **kwargs)

    def _from_this_machine(self):
        # Host check blocks DNS-rebinding; Origin check blocks other sites' pages.
        host = self.headers.get("Host", "").rsplit(":", 1)[0]
        origin = self.headers.get("Origin")
        same_origin = origin is None or origin == f"http://{self.headers.get('Host')}"
        return host in ("127.0.0.1", "localhost") and same_origin

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/status":
            if not self._from_this_machine():
                return self._json(403, {"error": "forbidden"})
            return self._json(200, JOB.snapshot())
        return super().do_GET()

    def do_POST(self):
        # The custom header forces a CORS preflight, which this server never approves,
        # so other websites can't trigger these.
        if not self._from_this_machine() or self.headers.get("X-BPM-App") != "1":
            return self._json(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad JSON"})
        path = self.path.split("?")[0]
        if path == "/api/update":
            started = JOB.start(bool(body.get("refresh", True)), bool(body.get("publish", False)))
            return self._json(200 if started else 409, JOB.snapshot())
        if path == "/api/cancel":
            JOB.cancel()
            return self._json(200, JOB.snapshot())
        if path == "/api/bpm":
            sid, bpm = body.get("id"), body.get("bpm")
            if not isinstance(sid, str) or not SPOTIFY_ID.match(sid):
                return self._json(400, {"error": "bad track id"})
            if bpm is not None:
                try:
                    bpm = round(float(bpm), 1)
                except (TypeError, ValueError):
                    return self._json(400, {"error": "BPM must be a number"})
                if not 20 <= bpm <= 300:
                    return self._json(400, {"error": "BPM must be between 20 and 300"})
            return self._json(200, set_override(sid, bpm))
        return self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        if args and "/api/status" in str(args[0]):
            return
        super().log_message(fmt, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    os.chdir(ROOT)  # the scripts and bpm.db use paths relative to the project folder
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Serving on http://127.0.0.1:{args.port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        JOB.cancel()


if __name__ == "__main__":
    main()
