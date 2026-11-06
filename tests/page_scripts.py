# tests/page_scripts.py
# The page's scripts (workspace/ui), for the tests that read the page's own
# code. Since #414 the page is several scripts instead of one app.js, and
# since #488 they are ES modules: index.html loads main.js, which imports the
# others. names() lists them in the order main.js's PAGE_SCRIPTS numbers them
# (a page error's file goes by that number), and source() joins them in that
# order, so a test that looked for a function or a phrase in app.js finds it in
# the same text. Guards that must see every script read every_script(), which
# takes the .js files from the folder itself, and tests/test_page_scripts.py
# keeps that, PAGE_SCRIPTS, the modules main.js's imports reach, and the
# package allowlist the same set.
#
# index.html is read with the standard library's HTML parser, not a pattern,
# so a script tag is found however it is written (<SCRIPT>, </script >, an
# attribute on its own line), as the browser would find it (CodeQL
# py/bad-tag-filter; #414 step 3a review).
#
# Tests that run the page's code use run(): tests/page_vm.js loads the real
# modules, from main.js through their imports, into one Node.js context and
# runs the test's JavaScript after them, so it calls the functions the page
# ships rather than copies cut out of the source (#414 step 3b, #488).
# function_text() and listener_texts() give a loaded function's own text as
# Node.js holds it, for the checks on how a function is written.

import functools
import json
import re
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


def tag_sources(html=None):
    """The scripts index.html loads, in order: each script element's src (None
    for one without), so a test sees every element."""
    if html is None:
        html = (UI / "index.html").read_text(encoding="utf-8")
    return [dict(found["attrs"]).get("src") for found in script_tags(html)[0]]


def names(main=None):
    """The page's modules, in the order main.js's PAGE_SCRIPTS lists them: a
    page error's file is sent as its place in that list (main.js, scriptLine)."""
    if main is None:
        main = (UI / "main.js").read_text(encoding="utf-8")
    listed = re.search(r"^const PAGE_SCRIPTS = \[([^\]]*)\];", main, re.M)
    if listed is None:
        raise AssertionError("PAGE_SCRIPTS not found in main.js")
    return re.findall(r'"([^"]+)"', listed.group(1))


def paths():
    return [UI / name for name in names()]


def source():
    """Every page module's text, joined in PAGE_SCRIPTS's order."""
    return "\n".join(path.read_text(encoding="utf-8") for path in paths())


def every_script():
    """Every .js file under workspace/ui, sorted, whether index.html loads it or not."""
    return sorted(UI.rglob("*.js"))


def every_source():
    """The text of every_script(), joined."""
    return "\n".join(path.read_text(encoding="utf-8") for path in every_script())


def run(code, timeout=60, before=None, main=None):
    """Run code (JavaScript) in Node.js after the page's modules, and return
    what it printed, read as JSON. The modules are the real ones, loaded from
    main.js through their imports (tests/page_vm.js says what code may
    replace). before, if given, runs first, before the page loads; main may
    name another folder's main.js (synthetic modules, for the harness's own
    tests). The test is skipped without Node.js; anything Node.js reports as an
    error fails it."""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("Node.js not installed")
    command = [node, "--experimental-vm-modules", str(PAGE_VM)]
    if before is not None:
        command += ["--before", before]
    command.append(str(main or UI / "main.js"))
    done = subprocess.run(command, input=code,
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                          check=False, cwd=ROOT)
    if done.returncode != 0:
        raise AssertionError(f"Node.js exited with {done.returncode}:\n{done.stderr}")
    return json.loads(done.stdout)


@functools.cache
def _loaded_texts():
    """Every function the page's modules hold at top level (function name()
    {...}, or a const that holds one), by name, and every listener the page
    added to the document and the window as it loaded, by event type in the
    order added: each as written, since Node.js keeps a function's own text."""
    return run(r"""
const functions = {}, native = /\{\s*\[native code\]\s*\}$/;
for (const name of Object.values(loaded.modules).flat()) {
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
        raise AssertionError(f"{name} is not a function declared by the page modules")
    return _lines(texts[name])


def listener_texts(target, event):
    """The text of each listener for event that the page added to target
    ("document" or "window") as it loaded, in the order added."""
    return [_lines(text) for text in _loaded_texts()[target].get(event, [])]
