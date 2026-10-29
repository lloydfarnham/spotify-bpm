"""Stamp docs/index.html with a content hash of app.js and app.css.

Browsers (and GitHub Pages' 10-minute cache) can keep serving an old app.js
after an update. Linking "app.js?v=<hash>" makes every change a new URL, so
a normal reload picks it up. Run after editing the app; export_data.py also
runs it.

Usage:
    python stamp_assets.py
"""

import hashlib
import os
import re

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
ASSETS = ("app.js", "app.css")


def stamp():
    index = os.path.join(DOCS, "index.html")
    with open(index, encoding="utf-8") as f:
        html = f.read()
    new = html
    for name in ASSETS:
        with open(os.path.join(DOCS, name), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:10]
        new = re.sub(rf'(["\']){re.escape(name)}(\?v=[0-9a-f]+)?(["\'])', rf"\g<1>{name}?v={digest}\g<3>", new)
    if new != html:
        with open(index, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        return True
    return False


if __name__ == "__main__":
    print("Updated asset versions in docs/index.html." if stamp() else "Asset versions already current.")
