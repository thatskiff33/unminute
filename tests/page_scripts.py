# tests/page_scripts.py
# The page's scripts (workspace/ui), for the tests that read the page's own
# code. Since #414 the page is several ordered classic scripts, listed in
# index.html, instead of one app.js: source() joins them in that order, so a
# test that looked for a function or a phrase in app.js finds it in the same
# text. Guards that must see every script read every_script(), which takes the
# .js files from the folder itself rather than from index.html, and
# tests/test_page_scripts.py keeps the two, and the package allowlist, the same
# set.
#
# index.html is read with the standard library's HTML parser, not a pattern,
# so a script tag is found however it is written (<SCRIPT>, </script >, an
# attribute on its own line), as the browser would find it (CodeQL
# py/bad-tag-filter; #414 step 3a review).

from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "workspace" / "ui"


class _ScriptTags(HTMLParser):
    """Each <script> element of a page: its start tag as written, its
    attributes and any text inside it; and how many </script> end tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found, self.ends, self._open = [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._open = {"tag": self.get_starttag_text(), "attrs": attrs, "text": ""}
            self.found.append(self._open)

    def handle_startendtag(self, tag, attrs):       # <script src="x.js" />
        if tag == "script":
            self.found.append({"tag": self.get_starttag_text(), "attrs": attrs, "text": ""})

    def handle_data(self, data):
        if self._open is not None:
            self._open["text"] += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.ends += 1
            self._open = None


def script_tags(html):
    """(the page's script elements, the number of </script> end tags)."""
    parser = _ScriptTags()
    parser.feed(html)
    parser.close()
    return parser.found, parser.ends


def names(html=None):
    """The page's scripts, as index.html loads them, in order: each script
    element's src (None for one without), so a test sees every element."""
    if html is None:
        html = (UI / "index.html").read_text(encoding="utf-8")
    return [dict(found["attrs"]).get("src") for found in script_tags(html)[0]]


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
