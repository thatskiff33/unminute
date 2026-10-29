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
#
# Tests that run the page's code use run(): tests/page_vm.js loads the real
# scripts, in index.html's order, into one Node.js context and runs the test's
# JavaScript after them, so it calls the functions the page ships rather than
# copies cut out of the source (#414 step 3b). function_text() and
# listener_texts() give a loaded function's own text as Node.js holds it, for
# the checks on how a function is written.

import functools
import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "workspace" / "ui"
PAGE_VM = Path(__file__).resolve().parent / "page_vm.js"


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


def run(code, timeout=60):
    """Run code (JavaScript) in Node.js after the page's scripts, and return
    what it printed, read as JSON. The scripts are the real ones, loaded in
    index.html's order (tests/page_vm.js says what code may replace). The test
    is skipped without Node.js; anything Node.js reports as an error fails it."""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("Node.js not installed")
    done = subprocess.run([node, str(PAGE_VM), *map(str, paths())], input=code,
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                          check=False, cwd=ROOT)
    if done.returncode != 0:
        raise AssertionError(f"Node.js exited with {done.returncode}:\n{done.stderr}")
    return json.loads(done.stdout)


@functools.cache
def _loaded_texts():
    """Every function the page's scripts declare (function name() {...}), by
    name, and every listener the page added to the document and the window as
    it loaded, by event type in the order added: each as written, since Node.js
    keeps a function's own text."""
    return run(r"""
const functions = {}, native = /\{\s*\[native code\]\s*\}$/;
for (const name of Object.getOwnPropertyNames(globalThis)) {
  const value = globalThis[name];
  if (typeof value === "function" && !native.test(String(value))) functions[name] = String(value);
}
const texts = (target) => Object.fromEntries(Object.entries(target.listeners)
  .map(([type, fns]) => [type, fns.map(String)]));
console.log(JSON.stringify({ functions, document: texts(loaded.document),
                             window: texts(loaded.window) }));
""")


def _lines(text):
    # A Windows checkout may end the scripts' lines in a carriage return and a
    # line feed, which Node.js keeps; source() reads them as a line feed.
    return text.replace("\r\n", "\n")


def function_text(name):
    """The text of the page function declared as name, as the page ships it."""
    texts = _loaded_texts()["functions"]
    if name not in texts:
        raise AssertionError(f"{name} is not a function declared by the page scripts")
    return _lines(texts[name])


def listener_texts(target, event):
    """The text of each listener for event that the page added to target
    ("document" or "window") as it loaded, in the order added."""
    return [_lines(text) for text in _loaded_texts()[target].get(event, [])]
