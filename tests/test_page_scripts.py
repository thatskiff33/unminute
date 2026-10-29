# tests/test_page_scripts.py
# The page's ordered classic scripts (#414 step 3, the owner's decision of
# 30 Oct 2026, Option A). app.js was split along its section markers into
# several plain <script src> files that share one global scope, as app.js's
# code did. Three rules keep that safe:
#   - the scripts index.html loads, the .js files in workspace/ui and the
#     package allowlist's entries are the same set, so no file escapes the
#     guards that read every page script (I3, T1) or ships unlisted;
#   - no two scripts declare the same top-level name: a repeated const, let or
#     class stops the later script loading at all, and a repeated function
#     silently replaces the earlier one;
#   - only main.js, the last, runs anything as the page loads. The others only
#     declare, so no script uses a name from a later one before it exists.
# The rules read the scripts with a small tokeniser (strings, template
# literals, regular expressions and comments skipped; brackets counted), not a
# full parser: the page uses plain modern JavaScript, and the tokeniser's own
# tests below show it finds what the rules forbid.

import json
import re
import shutil
import subprocess
import unittest

import page_scripts

ROOT = page_scripts.ROOT
UI = page_scripts.UI
MAIN = "main.js"
DECLARES = ("function", "async", "class", "const", "let", "var")
DIRECTIVE = ('"use strict"', "'use strict'")
REGEX_AFTER = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void",
               "throw", "yield", "await", "instanceof"}
# A token after which an expression carries on onto the next line.
CONTINUES = set("=,([{+-*/%?:<>!&|^~.") | {"=>", "&&", "||", "??", "==", "===", "!=", "!==",
                                           "+=", "-=", "<=", ">=", "?.", "..."}
PUNCTUATION = sorted(["===", "!==", "...", "=>", "&&", "||", "??", "==", "!=", "+=", "-=", "*=",
                      "/=", "<=", ">=", "?.", "++", "--", "**", "<<", ">>", "|=", "&="],
                     key=len, reverse=True)
NAME = re.compile(r"[A-Za-z_$][\w$]*")
NUMBER = re.compile(r"\d[\w.]*|\.\d\w*")
CLOSE = {"(": ")", "[": "]", "{": "}"}


def tokens(src):
    """(kind, text, depth, starts_line) for each token of a script; depth is
    the number of brackets open around it. Comments and the text of strings,
    template literals and regular expressions are skipped."""
    out, stack = [], []               # stack: "(", "[", "{", or "${" in a template literal
    i, n, first, prev = 0, len(src), True, None

    def emit(kind, text):
        nonlocal first, prev
        out.append((kind, text, len(stack), first))
        first, prev = False, (kind, text)

    def template(i):
        """From inside a template literal to just past its end or a "${"."""
        while i < n:
            if src[i] == "\\":
                i += 2
            elif src[i] == "`":
                return i + 1, False
            elif src.startswith("${", i):
                return i + 2, True
            else:
                i += 1
        raise ValueError("a template literal is not closed")

    def line():
        return src.count("\n", 0, i) + 1

    while i < n:
        c = src[i]
        if c == "\n":
            first, i = True, i + 1
        elif c in " \t\r":
            i += 1
        elif src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end < 0 else end
        elif src.startswith("/*", i):
            i = src.index("*/", i) + 2
        elif c in "'\"":
            j = i + 1
            while src[j] != c:
                if src[j] == "\n":
                    raise ValueError(f"a string runs past line {line()}")
                j += 2 if src[j] == "\\" else 1
            emit("str", src[i:j + 1])
            i = j + 1
        elif c == "`":
            emit("tpl", "`")
            i, opened = template(i + 1)
            if opened:
                stack.append("${")
        elif c == "/" and (prev is None or (prev[0] == "punct" and prev[1] not in (")", "]"))
                           or (prev[0] == "name" and prev[1] in REGEX_AFTER)):
            j, in_class = i + 1, False
            while src[j] != "/" or in_class:
                if src[j] == "\n":
                    raise ValueError(f"a regular expression runs past line {line()}")
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                j += 1
            flags = NAME.match(src, j + 1)
            end = flags.end() if flags else j + 1
            emit("re", src[i:end])
            i = end
        elif NAME.match(src, i):
            word = NAME.match(src, i)
            emit("name", word.group())
            i = word.end()
        elif NUMBER.match(src, i):
            number = NUMBER.match(src, i)
            emit("num", number.group())
            i = number.end()
        elif c in "([{":
            emit("punct", c)
            stack.append(c)
            i += 1
        elif c in ")]}":
            if not stack:
                raise ValueError(f"an unopened {c} at line {line()}")
            top = stack.pop()
            if top == "${" and c == "}":
                i, opened = template(i + 1)
                if opened:
                    stack.append("${")
                continue
            if top == "${" or CLOSE[top] != c:
                raise ValueError(f"{top} closed by {c} at line {line()}")
            emit("punct", c)
            i += 1
        else:
            mark = next((p for p in PUNCTUATION if src.startswith(p, i)), c)
            emit("punct", mark)
            i += len(mark)
    if stack:
        raise ValueError("not closed: " + "".join(stack))
    return out


def statements(src):
    """The script's top-level statements, each a list of tokens. A statement
    ends at a top-level ";", at the "}" that closes a function or class, or
    where a new line starts one after a statement left without its ";"."""
    found, current, last = [], [], None
    for token in tokens(src):
        kind, text, depth, starts_line = token
        if depth == 0:
            if (current and starts_line and last[1] not in CONTINUES
                    and not (kind == "punct" and text in CONTINUES)
                    and current[0][1] not in ("function", "async", "class")):
                found.append(current)
                current = []
            current.append(token)
            last = token
            if kind == "punct" and (text == ";" or (text == "}" and current[0][1] in
                                                    ("function", "async", "class"))):
                found.append(current)
                current, last = [], None
        else:
            current.append(token)
    if current:
        found.append(current)
    return found


def declared(statement):
    """The top-level names a declaration statement declares."""
    words = [t[1] for t in statement]
    if words[0] == "async":
        words = words[1:]
    if words[0] in ("function", "class"):
        return [words[2] if words[1] == "*" else words[1]]
    names, expect = [], True                  # const a = 1, b = 2;  let x, y;
    for kind, text, depth, _ in statement[1:]:
        if depth != 0:
            continue
        if expect:
            if kind != "name":
                raise ValueError(f"a destructured declaration: {' '.join(words[:6])}")
            names.append(text)
            expect = False
        elif kind == "punct" and text == ",":
            expect = True
    return names


def problems_at_load(src):
    """The top-level statements of a script that are not declarations (after
    the opening "use strict"), as their first few tokens."""
    found = []
    for k, statement in enumerate(statements(src)):
        head = statement[0][1]
        if k == 0 and head in DIRECTIVE:
            continue
        if head not in DECLARES or (head == "async" and statement[1][1] != "function"):
            found.append(" ".join(t[1] for t in statement[:4]))
    return found


def allowlisted_scripts():
    allowlist = json.loads((ROOT / "packaging" / "app-allowlist.json").read_text(encoding="utf-8"))
    return {p[len("workspace/ui/"):] for p in allowlist["app"]
            if p.startswith("workspace/ui/") and p.endswith(".js")}


class PageScriptSetTests(unittest.TestCase):
    """index.html, the folder and the package allowlist name the same scripts."""

    def test_index_html_loads_every_script_in_the_folder_and_the_allowlist(self):
        loaded = page_scripts.names()
        in_folder = {p.relative_to(UI).as_posix() for p in page_scripts.every_script()}
        self.assertEqual(len(loaded), len(set(loaded)), "index.html loads a script twice")
        self.assertGreaterEqual(len(loaded), 10, "the script pattern no longer matches index.html")
        self.assertEqual(set(loaded), in_folder,
                         "index.html and the .js files in workspace/ui differ")
        self.assertEqual(set(loaded), allowlisted_scripts(),
                         "index.html and packaging/app-allowlist.json name different scripts")

    def test_every_script_tag_is_a_plain_ordered_one_and_main_js_is_last(self):
        # No inline script (the CSP allows none), and no async, defer or module
        # script: each runs in turn, in the order listed.
        html = (UI / "index.html").read_text(encoding="utf-8")
        tags = re.findall(r"<script\b[^>]*>", html)
        self.assertEqual(len(tags), html.count("</script>"))
        self.assertEqual(tags, [f'<script src="{name}">' for name in page_scripts.names()])
        self.assertEqual(MAIN, page_scripts.names()[-1])

    def test_main_js_numbers_the_scripts_in_index_html_order(self):
        # PAGE_SCRIPTS numbers a page error's file (docs/DEVELOPER_NOTES.md,
        # "Page scripts"), so it must follow index.html's order.
        main = (UI / MAIN).read_text(encoding="utf-8")
        listed = re.search(r"^const PAGE_SCRIPTS = \[([^\]]*)\];", main, re.M)
        self.assertIsNotNone(listed, "PAGE_SCRIPTS not found in main.js")
        self.assertEqual(re.findall(r'"([^"]+)"', listed.group(1)), page_scripts.names())


class PageScriptRuleTests(unittest.TestCase):
    """Only main.js runs anything at load, and no top-level name is declared twice."""

    @classmethod
    def setUpClass(cls):
        cls.sources = {p.name: p.read_text(encoding="utf-8") for p in page_scripts.paths()}

    def test_each_script_is_strict(self):
        for name, src in self.sources.items():
            with self.subTest(script=name):
                self.assertIn(statements(src)[0][0][1], DIRECTIVE)

    def test_only_main_js_runs_anything_at_load(self):
        for name, src in self.sources.items():
            if name == MAIN:
                continue
            with self.subTest(script=name):
                self.assertEqual([], problems_at_load(src),
                                 f"{name} runs code as it loads: only main.js may")
        self.assertTrue(problems_at_load(self.sources[MAIN]), "main.js no longer starts the page")

    def test_no_two_scripts_declare_the_same_name(self):
        where = {}
        for name, src in self.sources.items():
            for statement in statements(src):
                if statement[0][1] not in DECLARES:
                    continue
                for word in declared(statement):
                    with self.subTest(name=word):
                        if word in where:
                            self.fail(f"{word} is declared in {where[word]} and {name}")
                    where[word] = name
        for word in ("$", "state", "call", "wire", "refresh", "METER_HZ", "reportUiError"):
            self.assertIn(word, where)                # the reading sees the page's names


class TokeniserTests(unittest.TestCase):
    """The rules' reading of a script finds what they forbid, and is not misled
    by strings, template literals, regular expressions or comments."""

    def test_declarations_only(self):
        src = ('"use strict";\n'
               "// wire();\n"
               "const a = 1, b = [2, 3];\n"
               "let c;\n"
               "const re = /[;}]\\/x/g, t = `x ${a + `${b}`} }; y`;\n"
               "const f = (x) => { go(x); return x; };\n"
               "async function g() { await h(); }\n"
               "function h() {\n  if (a) {\n    i();\n  }\n}\n"
               "class K { m() { return 1; } }\n"
               "const s = \"; wire();\";\n"
               "const long = a\n  || b;\n")
        self.assertEqual([], problems_at_load(src))
        names = [n for st in statements(src) if st[0][1] in DECLARES for n in declared(st)]
        self.assertEqual(names, ["a", "b", "c", "re", "t", "f", "g", "h", "K", "s", "long"])

    def test_it_finds_code_that_runs_at_load(self):
        for src in ("wire();\n", "window.x = 1;\n", "if (a) b();\n", "for (;;) {}\n",
                    'document.addEventListener("x", () => {});\n',
                    "const a = 1\nwire()\n",             # no semicolons
                    "function f() {}\nf();\n", "(() => {})();\n", "async () => {};\n",
                    'const a = 1;\n"use strict";\n'):
            with self.subTest(src=src):
                self.assertTrue(problems_at_load(src), src)

    def test_it_finds_a_name_declared_twice(self):
        names = [n for st in statements("const a = 1;\nfunction a() {}\nlet b, a;\n")
                 for n in declared(st)]
        self.assertEqual(names, ["a", "a", "b", "a"])


class ScriptLineTests(unittest.TestCase):
    """A page error's line carries its script's number (main.js, scriptLine)."""

    def test_script_line_numbers_page_scripts_only(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js not installed")
        main = (UI / MAIN).read_text(encoding="utf-8")
        wanted = [re.search(pattern, main, re.M | re.S) for pattern in (
            r"^const PAGE_SCRIPTS = \[.*?\];$", r"^const SCRIPT_LINES = \d+;$",
            r"^function scriptLine\(.*?^\}$")]
        self.assertTrue(all(wanted), "PAGE_SCRIPTS, SCRIPT_LINES or scriptLine not found")
        wanted = [found.group(0) for found in wanted]
        base = "file:///C:/Program%20Files/Unminute/app/workspace/ui/"
        page = json.dumps(base + "index.html#meetings")
        script = ("const document = { baseURI: " + page + " };\n"
                  + "\n".join(wanted) + "\n"
                  + "console.log(JSON.stringify(["
                  + f"scriptLine({json.dumps(base + 'core.js')}, 7), "
                  + f"scriptLine({json.dumps(base + 'polling.js')}, 120), "
                  + f"scriptLine({json.dumps(base + 'main.js')}, 0), "
                  + "scriptLine('file:///C:/elsewhere/polling.js', 120), "
                  + "scriptLine('', 5)]));\n")
        done = subprocess.run([node, "-"], input=script, capture_output=True, text=True,
                              encoding="utf-8", timeout=60, check=False)
        self.assertEqual(0, done.returncode, done.stderr)
        polling = page_scripts.names().index("polling.js") + 1
        main_no = page_scripts.names().index(MAIN) + 1
        self.assertEqual(json.loads(done.stdout),
                         [100007, polling * 100000 + 120, main_no * 100000, 120, 5])
        self.assertLess(len(page_scripts.names()) * 100000 + 99999, 10 ** 7)   # api._int's limit


if __name__ == "__main__":
    unittest.main()
