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



def _group_end(toks, i):
    """The index just past the bracket that closes the one at toks[i]."""
    depth = toks[i][2]
    return next(j for j in range(i + 1, len(toks))
                if toks[j][0] == "punct" and toks[j][2] == depth and toks[j][1] in ")]}") + 1


def names_read_at_load(statement):
    """The names a top-level declaration reads as its script loads: in a
    const, let or var initializer, or a class's extends clause, outside any
    function, arrow function or method body, and not as a property (.name) or
    an object key (name:). A class's static members are not read."""
    head = statement[0][1]
    if head in ("function", "async"):
        return set()
    toks = statement[1:]
    if head == "class":
        toks = toks[1:next(i for i, t in enumerate(toks) if t[1] == "{" and t[0] == "punct")]
    read, i, prev = set(), 0, None
    while i < len(toks):
        kind, text, depth, _ = toks[i]
        if kind == "punct" and text == "{" and prev is not None and prev[1] in (")", "=>"):
            i, prev = _group_end(toks, i), None        # a function or method body
            continue
        if kind == "punct" and text == "=>" and i + 1 < len(toks) and toks[i + 1][1] != "{":
            i += 1                                     # an arrow function's expression body
            while i < len(toks) and not (toks[i][2] < depth or (
                    toks[i][2] == depth and toks[i][1] in (",", ";"))):
                i += 1
            prev = None
            continue
        if (kind == "name" and not (prev and prev[1] in (".", "?."))
                and not (prev and prev[1] in ("{", ",") and i + 1 < len(toks)
                         and toks[i + 1][1] == ":")):
            read.add(text)
        prev, i = toks[i], i + 1
    return read


def allowlisted_scripts():
    allowlist = json.loads((ROOT / "packaging" / "app-allowlist.json").read_text(encoding="utf-8"))
    return {p[len("workspace/ui/"):] for p in allowlist["app"]
            if p.startswith("workspace/ui/") and p.endswith(".js")}


def tag_problems(html, in_folder, allowlisted):
    """What is wrong with a page's script tags, as a list of messages: the
    scripts it loads, the .js files in the folder and the allowlist's entries
    must be the same set, and each tag a plain <script src="x.js"></script>
    (no inline, async, defer or module script: each runs in turn, in the
    order listed) with main.js last."""
    found, ends = page_scripts.script_tags(html)
    loaded = page_scripts.names(html)
    problems = []
    if len(loaded) != len(set(loaded)):
        problems.append("index.html loads a script twice")
    if set(loaded) != in_folder:
        problems.append(f"index.html and the .js files in workspace/ui differ: "
                        f"{sorted(map(str, set(loaded) ^ in_folder))}")
    if set(loaded) != allowlisted:
        problems.append(f"index.html and packaging/app-allowlist.json name different scripts: "
                        f"{sorted(map(str, set(loaded) ^ allowlisted))}")
    if ends != len(found):
        problems.append(f"{len(found)} script tags but {ends} end tags")
    for element, name in zip(found, loaded, strict=True):
        if (element["attrs"] != [("src", name)] or element["tag"] != f'<script src="{name}">'
                or element["text"]):
            problems.append(f"not a plain script tag: {element['tag']}")
    if not loaded or loaded[-1] != MAIN:
        problems.append("main.js is not the last script")
    return problems


class PageScriptSetTests(unittest.TestCase):
    """index.html, the folder and the package allowlist name the same scripts."""

    @classmethod
    def setUpClass(cls):
        cls.html = (UI / "index.html").read_text(encoding="utf-8")
        cls.in_folder = {p.relative_to(UI).as_posix() for p in page_scripts.every_script()}
        cls.allowlisted = allowlisted_scripts()

    def test_index_html_loads_every_script_in_the_folder_and_the_allowlist(self):
        self.assertGreaterEqual(len(page_scripts.names()), 10,
                                "the reading of index.html no longer finds its scripts")
        self.assertEqual([], tag_problems(self.html, self.in_folder, self.allowlisted))

    def test_a_script_tag_is_found_however_it_is_written(self):
        # Tag names are case-insensitive and an end tag may hold spaces, so a
        # pattern for one spelling misses a script the browser still loads
        # (CodeQL py/bad-tag-filter; #414 step 3a review). Each of these is
        # added before main.js and must be refused.
        main_tag = '<script src="main.js"></script>'
        self.assertEqual(1, self.html.count(main_tag))
        for extra in ('<SCRIPT src="../../tools/stray.js"></SCRIPT>',
                      '<script src="../../tools/stray.js"></script >',
                      '<Script\n  src="../../tools/stray.js"></sCrIpT\t>',
                      '<script src=../../tools/stray.js></script>',
                      '<script src="../../tools/stray.js"/>',
                      '<script>window.x = 1;</script>',
                      '<script src="core.js" defer></script>',
                      '<SCRIPT src="core.js"></SCRIPT>',
                      '<script src="core.js" type="module"></script>'):
            with self.subTest(extra=extra):
                html = self.html.replace(main_tag, extra + "\n" + main_tag)
                self.assertTrue(tag_problems(html, self.in_folder, self.allowlisted))
        moved = self.html.replace(main_tag, "").replace(
            '<script src="core.js"></script>', main_tag + '\n<script src="core.js"></script>')
        self.assertIn("main.js is not the last script",
                      tag_problems(moved, self.in_folder, self.allowlisted))

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

    def test_no_script_reads_a_later_scripts_name_as_it_loads(self):
        # Declarations hoist within a script but not across scripts, so an
        # initializer outside a function body that names something declared
        # in a later script would throw as the page loads (#414 step 3a
        # review: "const probeAtLoad = wire();" in polling.js).
        order = [(name, statements(src)) for name, src in self.sources.items()]
        for k, (name, parsed) in enumerate(order):
            if name == MAIN:
                continue
            later = {word for _, rest in order[k + 1:] for st in rest
                     if st[0][1] in DECLARES for word in declared(st)}
            for statement in parsed:
                if statement[0][1] not in DECLARES:
                    continue
                with self.subTest(script=name, statement=" ".join(t[1] for t in statement[:3])):
                    self.assertEqual(set(), names_read_at_load(statement) & later,
                                     f"{name} reads a later script's name as it loads")

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

    def test_it_finds_a_name_read_at_load(self):
        def read(src):
            return set().union(*(names_read_at_load(st) for st in statements(src)
                                 if st[0][1] in DECLARES))
        for src in ("const probeAtLoad = wire();\n", "let x = 1, y = wire;\n",
                    "const o = { a: wire() };\n", "const t = `${wire()}`;\n",
                    "class K extends wire {}\n", "const v = new wire();\n",
                    "var f = (a) => a, g = wire;\n", "const s = [() => 1, wire];\n"):
            with self.subTest(src=src):
                self.assertIn("wire", read(src))
        for src in ("const f = () => wire();\n", "const f = () => { wire(); };\n",
                    "const f = function () { wire(); };\n", "const o = { m() { wire(); } };\n",
                    "const o = x.wire;\n", "const o = x?.wire;\n", "const o = { wire: 1 };\n",
                    "function f() { wire(); }\n", "async function f() { await wire(); }\n",
                    "class K { m() { wire(); } }\n", "const s = \"wire()\";\n",
                    "const f = (a) => wire(a), b = 1;\n"):
            with self.subTest(src=src):
                self.assertNotIn("wire", read(src))

    def test_it_finds_a_name_declared_twice(self):
        names = [n for st in statements("const a = 1;\nfunction a() {}\nlet b, a;\n")
                 for n in declared(st)]
        self.assertEqual(names, ["a", "a", "b", "a"])


class RealScriptsTests(unittest.TestCase):
    """tests/page_vm.js, which the page tests use to call the page's real
    functions (#414 step 3b): every script loads, in index.html's order, into
    one Node.js context where their top-level names are shared, as in the
    browser, and a test's code cannot quietly miss the page."""

    def test_every_script_loads_and_shares_its_names(self):
        out = page_scripts.run(r"""
console.log(JSON.stringify({
  first: typeof say, last: typeof wire, kinds: MESSAGE_KINDS,
  lets: [typeof pollTimer, typeof refreshing],
  started: Object.keys(loaded.window.listeners).sort(),
  workspace: Object.keys(loaded.window.workspace).sort(),
}));
""")
        self.assertEqual(out["first"], "function")            # core.js
        self.assertEqual(out["last"], "function")             # main.js
        self.assertEqual(out["kinds"], ["success", "info", "error"])
        self.assertEqual(out["lets"], ["object", "boolean"])   # polling.js, recording.js
        # main.js ran as it does in the page: its listeners and window.workspace.
        self.assertIn("pywebviewready", out["started"])
        self.assertIn("refresh", out["workspace"])

    def test_a_replacement_reaches_the_page_functions(self):
        out = page_scripts.run(r"""
const seen = [];
document = { getElementById: (id) => ({ id }) };
say = (text, opts) => seen.push([text, opts.kind]);
sayDone("Saved.");                                     // a page const that calls say
console.log(JSON.stringify({ el: $("status"), seen }));
""")
        self.assertEqual(out, {"el": {"id": "status"}, "seen": [["Saved.", "success"]]})

    def test_declaring_a_page_or_stand_in_name_again_fails_loudly(self):
        # A test that declared its own document or state would leave the page
        # using the real one, and test nothing: it stops instead.
        for code in ("const document = {};", "const state = {};", "let $ = null;"):
            with self.subTest(code=code):
                with self.assertRaisesRegex(AssertionError, "has already been declared"):
                    page_scripts.run(code)

    def test_a_stub_of_a_name_the_page_does_not_declare_fails_loudly(self):
        # #495: a stub of a page function that has since been renamed or
        # removed would create a new global the page never calls, and the test
        # would pass on the real function. It fails instead: at the top level
        # or later, a function or not, in the test's code or in a callback
        # that runs after it (Node.js's vm lets a function through strict
        # mode, so tests/page_vm.js also looks for the globals it added).
        out = "console.log(JSON.stringify(typeof sayDone));"
        for code in ("renamedAway = () => 0;", "renamedAway = function () {};",
                     "renamedAway = 1;", "renamedAway = {};",
                     "const later = () => { renamedAway = () => 0; };\nlater();",
                     "const later = () => { renamedAway = 1; };\nlater();",
                     "setTimeout(() => { renamedAway = () => 0; }, 0);",
                     "Promise.resolve().then(() => { renamedAway = () => 0; });"):
            with self.subTest(code=code):
                with self.assertRaisesRegex(AssertionError, "renamedAway is not defined"):
                    page_scripts.run(code + "\n" + out)
        # What a test may still do: replace a page function or let, and assign
        # its own let, var and function, declared anywhere in its code.
        self.assertEqual(page_scripts.run(
            "let mine = 0;\nmine = 1;\nsay = () => 2;\npollTimer = 3;\nown = () => 4;\n"
            "ownVar = 5;\nsetTimeout(() => { mine = 6; }, 0);\n"
            "console.log(JSON.stringify([mine, say(), pollTimer, own(), ownVar]));\n"
            "function own() { return 0; }\nvar ownVar;"), [1, 2, 3, 4, 5])

    def test_function_text_is_the_loaded_function(self):
        text = page_scripts.function_text("scriptLine")
        self.assertTrue(text.startswith("function scriptLine(url, line) {\n"), text)
        self.assertTrue(text.endswith("\n}"), text)
        self.assertNotIn("\r", text)
        with self.assertRaisesRegex(AssertionError, "not a function declared"):
            page_scripts.function_text("noSuchFunction")
        [handler] = page_scripts.listener_texts("document", "visibilitychange")
        self.assertIn("pollNow();", handler)


class ScriptLineTests(unittest.TestCase):
    """A page error's line carries its script's number (main.js, scriptLine)."""

    def test_script_line_numbers_page_scripts_only(self):
        # main.js's own scriptLine, with the page's real scripts loaded.
        base = "file:///C:/Program%20Files/Unminute/app/workspace/ui/"
        page = json.dumps(base + "index.html#meetings")
        script = ("document = { baseURI: " + page + " };\n"
                  + "console.log(JSON.stringify(["
                  + f"scriptLine({json.dumps(base + 'core.js')}, 7), "
                  + f"scriptLine({json.dumps(base + 'polling.js')}, 120), "
                  + f"scriptLine({json.dumps(base + 'main.js')}, 0), "
                  + "scriptLine('file:///C:/elsewhere/polling.js', 120), "
                  + "scriptLine('', 5)]));\n")
        lines = page_scripts.run(script)
        polling = page_scripts.names().index("polling.js") + 1
        main_no = page_scripts.names().index(MAIN) + 1
        self.assertEqual(lines,
                         [100007, polling * 100000 + 120, main_no * 100000, 120, 5])
        self.assertLess(len(page_scripts.names()) * 100000 + 99999, 10 ** 7)   # api._int's limit


BASE = "file:///C:/Program%20Files/Unminute/app/workspace/ui/"


def frames(*lines):
    """Stack frames as V8 writes them, after the error's own first line."""
    return json.dumps("".join("\n    at " + line for line in lines))


class PageErrorPlaceTests(unittest.TestCase):
    """#491: the window opens the page from file://, where WebView2 gives an
    uncaught error in the page's own scripts as "Script error." with line 0
    (real window, 30 Oct 2026). The page guards the callbacks the browser
    calls, reads the place from the caught error's stack (numbers only) and
    sends it with the error event."""

    def run_page(self, code):
        return page_scripts.run("document = { baseURI: " + json.dumps(BASE + "index.html")
                                + " };\n" + code)

    def number(self, name):
        return page_scripts.names().index(name) + 1

    def test_stack_place_reads_frames_only(self):
        out = self.run_page(
            "const made = (message, frames) => {\n"
            "  const err = new Error(message);\n"
            "  err.stack = String(err) + frames;\n"
            "  return err;\n"
            "};\n"
            "console.log(JSON.stringify([\n"
            # A named frame in a page script, after a built-in's frame.
            "  stackPlace(made('hidden', " + frames(
                "JSON.parse (<anonymous>)", f"groupOf ({BASE}meetings.js:38:9)",
                f"{BASE}main.js:5:1") + ")),\n"
            # An anonymous frame (no brackets), and an async one.
            "  stackPlace(made('hidden', " + frames(f"{BASE}core.js:7:3") + ")),\n"
            "  stackPlace(made('hidden', " + frames(f"async refresh ({BASE}polling.js:9:2)")
            + ")),\n"
            # A message that looks like frames is cut off unread.
            "  stackPlace(made(" + json.dumps(f"x\n    at f ({BASE}core.js:1:1)") + ", "
            + frames(f"g ({BASE}polling.js:120:4)") + ")),\n"
            # Frames only in other files, and errors with no usable stack.
            "  stackPlace(made('hidden', " + frames("f (file:///C:/elsewhere/meetings.js:38:9)")
            + ")),\n"
            "  stackPlace(Object.assign(new Error('hidden'), { stack: 'rewritten' })),\n"
            "  stackPlace('a string'), stackPlace(null), stackPlace(undefined), stackPlace({}),\n"
            "  stackPlace({ get stack() { throw new Error('no'); } }),\n"
            "]));\n")
        self.assertEqual(out, [
            [self.number("meetings.js") * 100000 + 38, 9],
            [self.number("core.js") * 100000 + 7, 3],
            [self.number("polling.js") * 100000 + 9, 2],
            [self.number("polling.js") * 100000 + 120, 4],
            None, None, None, None, None, None, None])

    def test_guard_notes_the_place_and_throws_the_same_error(self):
        out = self.run_page(
            "const err = new Error('hidden');\n"
            "err.stack = String(err) + " + frames(f"f ({BASE}speakers.js:12:5)") + ";\n"
            "const thrower = () => { throw err; };\n"
            "let same = false;\n"
            "try { guarded(thrower)(); } catch (e) { same = e === err; }\n"
            "const place = caughtAt;\n"
            "const self = { k: 2 };\n"
            "const value = guarded(function (a, b) { return this.k + a + b; }).call(self, 3, 4);\n"
            "const f = () => 0;\n"
            "const listener = { handleEvent() {} };\n"
            "console.log(JSON.stringify([same, place, value, guarded(f) === guarded(f),\n"
            "  guarded(listener) === listener, guarded('text'), guarded(null)]));\n")
        self.assertEqual(out, [True, [self.number("speakers.js") * 100000 + 12, 5], 9, True,
                               True, "text", None])

    def test_guard_sends_an_async_callbacks_rejection_once(self):
        # From file:// the browser sends no unhandledrejection event for the
        # page's own errors (#491), so the guard sends it, and rejects on with
        # the same reason; the page's own listener then skips that reason.
        meetings = self.number("meetings.js") * 100000
        out = self.run_page(
            "const sent = [], unhandled = [];\n"
            "reportUiError = (...args) => sent.push(args);\n"
            "process.on('unhandledRejection', (reason) => unhandled.push(reason));\n"
            "const [onRejection] = loaded.window.listeners.unhandledrejection;\n"
            "const err = new Error('hidden');\n"
            "err.stack = String(err) + " + frames(f"f ({BASE}meetings.js:40:3)") + ";\n"
            "const value = guarded(async () => { throw err; })();\n"
            "const fine = guarded(async () => 5)();\n"
            "setTimeout(async () => {\n"
            "  let same = false;\n"
            "  try { await value; } catch (e) { same = e === err; }\n"
            "  const rejectedOn = unhandled.length === 1 && unhandled[0] === err;\n"
            "  onRejection({ reason: err });          // the event for it: not sent again\n"
            "  console.log(JSON.stringify([sent, same, rejectedOn, await fine]));\n"
            "}, 20);\n")
        self.assertEqual(out, [[["rejection", meetings + 40, 3]], True, True, 5])

    def test_guard_callbacks_wraps_listeners_and_timers(self):
        out = self.run_page(
            "const added = [], removed = [], timed = [];\n"
            "class Target {}\n"
            "Target.prototype.addEventListener = function (type, fn, opts) {\n"
            "  added.push([this.name, type, fn, opts]); };\n"
            "Target.prototype.removeEventListener = function (type, fn, opts) {\n"
            "  removed.push([type, fn, opts]); };\n"
            "const win = { EventTarget: Target,\n"
            "  setTimeout(fn, ms, arg) { timed.push([this === win, fn, ms, arg]); return 7; } };\n"
            "guardCallbacks(win);\n"
            "const t = new Target();\n"
            "t.name = 'el';\n"
            "const fn = () => 0, other = () => 1, obj = { handleEvent() {} };\n"
            "t.addEventListener('click', fn, { once: true });\n"
            "t.addEventListener('click', obj);\n"
            "t.removeEventListener('click', fn, true);\n"
            "t.removeEventListener('click', other);\n"
            "const id = win.setTimeout(fn, 5, 'x');\n"
            "console.log(JSON.stringify([\n"
            "  added[0][0], added[0][1], added[0][2] === guarded(fn), added[0][2] !== fn,\n"
            "  added[0][3], added[1][2] === obj,\n"
            "  removed[0][1] === guarded(fn), removed[0][2], removed[1][1] === other,\n"
            "  id, timed[0][0], timed[0][1] === guarded(fn), timed[0][2], timed[0][3],\n"
            "  'requestAnimationFrame' in win]));\n")
        self.assertEqual(out, ["el", "click", True, True, {"once": True}, True,
                               True, True, True,
                               7, True, True, 5, "x", False])

    def test_error_event_sends_the_guarded_place(self):
        meetings = self.number("meetings.js") * 100000
        out = self.run_page(
            "const sent = [];\n"
            "reportUiError = (...args) => sent.push(args);\n"
            "const [onError] = loaded.window.listeners.error;\n"
            "const [onRejection] = loaded.window.listeners.unhandledrejection;\n"
            # From file://: no file, line 0; the guard's place is sent, once.
            f"caughtAt = [{meetings + 38}, 9];\n"
            "onError({ filename: '', lineno: 0, colno: 0 });\n"
            "const cleared = caughtAt;\n"
            "onError({ filename: '', lineno: 0, colno: 0 });\n"
            # Served over HTTP: the browser's own file and line win.
            "caughtAt = [1, 1];\n"
            "onError({ filename: " + json.dumps(BASE + "core.js") + ", lineno: 4, colno: 2 });\n"
            # A rejection: its reason's stack, or 0 for a reason that is not an Error.
            "const err = new Error('hidden');\n"
            "err.stack = String(err) + " + frames(f"f ({BASE}meetings.js:40:3)") + ";\n"
            "onRejection({ reason: err });\n"
            "onRejection({ reason: 'text' });\n"
            "console.log(JSON.stringify([sent, cleared]));\n")
        self.assertEqual(out, [[["error", meetings + 38, 9], ["error", 0, 0],
                                ["error", self.number("core.js") * 100000 + 4, 2],
                                ["rejection", meetings + 40, 3], ["rejection", 0, 0]], None])

    def test_callbacks_are_guarded_before_any_listener_is_added(self):
        # main.js guards the browser's ways to call the page before its first
        # listener, and wire() runs inside a guard.
        text = (UI / MAIN).read_text(encoding="utf-8")
        start = text.index("\nguardCallbacks(window);\n")
        self.assertNotIn("addEventListener(", text[text.index("\n// -- start-up"):start])
        self.assertIn("\nguarded(wire)();", text[start:])
        self.assertNotRegex(text[start:], r"\nwire\(\);")
        # The page's on... handlers run inside a guard too, or inside attempt(),
        # which catches everything.
        for name in page_scripts.names():
            source = (UI / name).read_text(encoding="utf-8")
            for m in re.finditer(r"\.on[a-z]+ = (.*)", source):
                with self.subTest(script=name, handler=m.group(0)):
                    self.assertTrue(m.group(1).startswith(("guarded(", "() => attempt(")),
                                    m.group(0))


if __name__ == "__main__":
    unittest.main()
