# tests/page_scripts.py
# The page's scripts (workspace/ui), for the tests that read the page's own
# code. Since #414 the page is several ordered classic scripts, listed in
# index.html, instead of one app.js: source() joins them in that order, so a
# test that looked for a function or a phrase in app.js finds it in the same
# text. Guards that must see every script read every_script(), which takes the
# .js files from the folder itself rather than from index.html, and
# tests/test_page_scripts.py keeps the two, and the package allowlist, the same
# set.

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "workspace" / "ui"
SCRIPT_TAG = re.compile(r'<script src="([^"]+)"></script>')


def names():
    """The page's scripts, as index.html loads them, in order."""
    return SCRIPT_TAG.findall((UI / "index.html").read_text(encoding="utf-8"))


def paths():
    return [UI / name for name in names()]


def source():
    """Every page script's text, joined in load order."""
    return "\n".join(path.read_text(encoding="utf-8") for path in paths())


def every_script():
    """Every .js file under workspace/ui, sorted, whether index.html loads it or not."""
    return sorted(UI.rglob("*.js"))


def every_source():
    """The text of every_script(), joined."""
    return "\n".join(path.read_text(encoding="utf-8") for path in every_script())
