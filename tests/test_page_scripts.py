# tests/test_page_scripts.py
# The page's scripts (#414 step 3, the owner's decision DEC-082). app.js was
# split along its section markers into several files, first ordered classic
# scripts sharing one global scope, and since #488 (the user's decision
# DEC-090) ES modules: index.html loads only
# main.js, as a module, and each module imports by name what it uses from the
# others. These rules keep that safe:
#   - index.html loads one script, main.js as a module; the modules main.js's
#     imports reach, the .js files in workspace/ui, the package allowlist's
#     entries and main.js's PAGE_SCRIPTS are the same set, so no file escapes
#     the guards that read every page script (I3, T1) or ships unlisted;
#   - imports and exports are plain lists ({ a, b }), an import names a ./x.js
#     file in the folder and only what it exports, and nothing imports by URL,
#     at run time (import()) or through import.meta;
#   - only main.js runs anything as the page loads. The others only declare,
#     and none reads an imported name as it loads: the modules import each
#     other in cycles, so one may be evaluated before a module it imports
#     from, whose const and let are not yet set. Functions are, and main.js,
#     the module index.html loads, is evaluated after every module it reaches;
#   - no two modules declare the same top-level name, so the tests (which
#     reach every module's names in one namespace, tests/page_vm.js) and a
#     reader never mistake one for another.
# The rules read the scripts with a small tokeniser (strings, template
# literals, regular expressions and comments skipped; brackets counted), not a
# full parser: the page uses plain modern JavaScript, and the tokeniser's own
# tests below show it finds what the rules forbid.

import json
import re
import tempfile
import unittest
from pathlib import Path

import page_scripts

ROOT = page_scripts.ROOT
UI = page_scripts.UI
MAIN = "main.js"
MODULE_TAG = '<script type="module" src="main.js"></script>'
PAGE_FILE = re.compile(r'^"\./([A-Za-z0-9_-]+\.js)"$')
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


def module_list(statement):
    """An import or export statement written as a plain list, which runs
    nothing: ("import", names, specifier) for import { a, b } from "x";, or
    ("export", names, None) for export { a, b };. None for any other
    statement, including every other form of import or export (a default, a
    renamed or starred name, export const, an import for its effects alone)."""
    words = [t[1] for t in statement]
    if words[-1] == ";":
        words = words[:-1]
    if len(words) < 3 or words[0] not in ("import", "export") or words[1] != "{":
        return None
    try:
        close = words.index("}")
    except ValueError:
        return None
    inside, tail = words[2:close], words[close + 1:]
    names = inside[0::2]
    if (not names or any(not NAME.fullmatch(n) or n in ("as", "default") for n in names)
            or any(c != "," for c in inside[1::2]) or len(inside) % 2 == 0):
        return None
    if words[0] == "export":
        return ("export", names, None) if not tail else None
    if len(tail) != 2 or tail[0] != "from" or not tail[1].startswith(('"', "'")):
        return None
    return ("import", names, tail[1])


def problems_at_load(src):
    """The top-level statements of a script that are not declarations (after
    the opening "use strict") or plain import and export lists, as their
    first few tokens."""
    found = []
    for k, statement in enumerate(statements(src)):
        head = statement[0][1]
        if k == 0 and head in DIRECTIVE:
            continue
        if head in ("import", "export") and module_list(statement):
            continue
        if head not in DECLARES or (head == "async" and statement[1][1] != "function"):
            found.append(" ".join(t[1] for t in statement[:4]))
    return found


def links(src):
    """A module's imports, [(specifier as written, names)], and its exports
    (names), from its plain import and export lists."""
    imports, exports = [], []
    for statement in statements(src):
        listed = module_list(statement)
        if listed and listed[0] == "import":
            imports.append((listed[2], listed[1]))
        elif listed:
            exports.extend(listed[1])
    return imports, exports


def unimported(src, own, others):
    """The names a module's code uses that another module declares at top
    level (others) but this one neither imports nor declares (own): each a
    name token that is not a property (.name, ?.name) or an object key
    (name:), outside the module's import and export lists. There is no scope
    analysis: a local of the same name counts as a use, so no module may give
    a local another module's top-level name (it would hide a missing import)."""
    imported = {w for _, names in links(src)[0] for w in names}
    found = set()
    for statement in statements(src):
        if module_list(statement):
            continue
        for i, (kind, text, _, _) in enumerate(statement):
            if kind != "name" or text not in others or text in own or text in imported:
                continue
            before = statement[i - 1][1] if i else None
            after = statement[i + 1][1] if i + 1 < len(statement) else None
            if before in (".", "?.") or (after == ":" and before in ("{", ",")):
                continue
            found.add(text)
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


def tag_problems(html):
    """What is wrong with a page's script tags, as a list of messages: it
    loads one script, main.js as a module, written exactly as MODULE_TAG (no
    inline, classic, async or defer script, and no other module); main.js's
    imports load the rest."""
    found, ends = page_scripts.script_tags(html)
    problems = []
    if ends != len(found):
        problems.append(f"{len(found)} script tags but {ends} end tags")
    if len(found) != 1:
        problems.append(f"{len(found)} script elements: index.html loads main.js only")
    for element in found:
        if (element["attrs"] != [("type", "module"), ("src", MAIN)] or element["text"]
                or element["tag"] + "</script>" != MODULE_TAG):
            problems.append(f"not the page's module tag: {element['tag']}")
    return problems


def set_problems(reached, listed, in_folder, allowlisted):
    """What is wrong with the page's set of modules: those main.js's imports
    reach, PAGE_SCRIPTS (listed, in order), the .js files in the folder and
    the allowlist's entries must be the same set, each listed once."""
    problems = []
    if len(listed) != len(set(listed)):
        problems.append("PAGE_SCRIPTS lists a module twice")
    for name, other in (("PAGE_SCRIPTS", set(listed)), ("the .js files in workspace/ui", in_folder),
                        ("packaging/app-allowlist.json", allowlisted)):
        if set(reached) != other:
            problems.append(f"main.js's imports and {name} differ: "
                            f"{sorted(map(str, set(reached) ^ other))}")
    return problems


class PageScriptSetTests(unittest.TestCase):
    """index.html loads main.js as a module, and its imports, PAGE_SCRIPTS, the
    folder and the package allowlist name the same scripts."""

    @classmethod
    def setUpClass(cls):
        cls.html = (UI / "index.html").read_text(encoding="utf-8")
        cls.in_folder = {p.relative_to(UI).as_posix() for p in page_scripts.every_script()}
        cls.allowlisted = allowlisted_scripts()
        # The modules tests/page_vm.js loaded from main.js, as the browser does.
        cls.reached = page_scripts.run("console.log(JSON.stringify(Object.keys(loaded.modules)));")

    def test_index_html_loads_main_js_as_its_one_module(self):
        self.assertEqual(page_scripts.tag_sources(), [MAIN])
        self.assertEqual([], tag_problems(self.html))

    def test_a_script_tag_is_found_however_it_is_written(self):
        # Tag names are case-insensitive and an end tag may hold spaces, so a
        # pattern for one spelling misses a script the browser still loads
        # (CodeQL py/bad-tag-filter; #414 step 3a review). Each of these is
        # added before main.js and must be refused, as must main.js loaded any
        # other way.
        self.assertEqual(1, self.html.count(MODULE_TAG))
        for extra in ('<SCRIPT src="../../tools/stray.js"></SCRIPT>',
                      '<script src="../../tools/stray.js"></script >',
                      '<Script\n  src="../../tools/stray.js"></sCrIpT\t>',
                      '<script src=../../tools/stray.js></script>',
                      '<script src="../../tools/stray.js"/>',
                      '<script>window.x = 1;</script>',
                      '<script type="module">import "./core.js";</script>',
                      '<script src="core.js" defer></script>',
                      '<SCRIPT src="core.js"></SCRIPT>',
                      '<script type="module" src="core.js"></script>'):
            with self.subTest(extra=extra):
                html = self.html.replace(MODULE_TAG, extra + "\n" + MODULE_TAG)
                self.assertTrue(tag_problems(html))
        for main in ('<script src="main.js"></script>',
                     '<script src="main.js" type="module"></script>',
                     '<script type="module" src="main.js" async></script>',
                     '<SCRIPT type="module" src="main.js"></SCRIPT>', ""):
            with self.subTest(main=main):
                self.assertTrue(tag_problems(self.html.replace(MODULE_TAG, main)))

    def test_main_js_reaches_every_script_in_the_folder_and_the_allowlist(self):
        # PAGE_SCRIPTS numbers a page error's file (docs/DEVELOPER_NOTES.md,
        # "Page scripts"), so it lists every module, each once.
        self.assertGreaterEqual(len(self.reached), 10, "the harness no longer loads the modules")
        self.assertEqual(self.reached[-1], MAIN, "main.js is evaluated last")
        self.assertEqual([], set_problems(self.reached, page_scripts.names(), self.in_folder,
                                          self.allowlisted))

    def test_a_module_left_out_of_any_list_is_found(self):
        listed = page_scripts.names()
        for reached, names, folder, allowed in (
                (self.reached[1:], listed, self.in_folder, self.allowlisted),   # not imported
                (self.reached, listed[1:], self.in_folder, self.allowlisted),   # not numbered
                (self.reached, listed + listed[:1], self.in_folder, self.allowlisted),
                (self.reached, listed, self.in_folder | {"stray.js"}, self.allowlisted),
                (self.reached, listed, self.in_folder, self.allowlisted - {"core.js"})):
            with self.subTest(case=len(reached) + len(names) + len(folder) + len(allowed)):
                self.assertTrue(set_problems(reached, names, folder, allowed))


class PageScriptRuleTests(unittest.TestCase):
    """Only main.js runs anything at load, imports and exports are plain lists
    of names between the page's own modules, and no top-level name is declared
    twice."""

    @classmethod
    def setUpClass(cls):
        cls.sources = {p.name: p.read_text(encoding="utf-8") for p in page_scripts.paths()}

    def test_each_script_is_strict(self):
        # A module is strict anyway; the directive keeps a file strict if it is
        # ever read as a classic script, as it was before #488.
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

    def test_imports_and_exports_link_only_the_page_modules(self):
        # Each import and export is a plain list (main.js's too), an import
        # names another module in the folder and only names it exports, and an
        # export names a top-level declaration of its own module. Nothing is
        # imported at run time (import()) or reached through import.meta.
        found = {}
        for name, src in self.sources.items():
            for statement in statements(src):
                if statement[0][1] in ("import", "export"):
                    with self.subTest(script=name, statement=" ".join(t[1] for t in statement[:4])):
                        self.assertIsNotNone(module_list(statement), "not a plain list")
            words = [t[1] for t in tokens(src)]
            for k, word in enumerate(words[:-1]):
                if word == "import":
                    with self.subTest(script=name, after=words[k + 1]):
                        self.assertEqual(words[k + 1], "{", "an import other than a list")
            found[name] = links(src)
        for name, (imports, exports) in found.items():
            own = {w for st in statements(self.sources[name]) if st[0][1] in DECLARES
                   for w in declared(st)}
            with self.subTest(script=name, exports=exports):
                self.assertEqual(len(exports), len(set(exports)))
                self.assertLessEqual(set(exports), own, "an export the module does not declare")
            imported = [w for _, names in imports for w in names]
            with self.subTest(script=name, imported="twice or its own"):
                self.assertEqual(len(imported), len(set(imported)), "a name imported twice")
                self.assertEqual(set(), set(imported) & own, "a name both imported and declared")
            for specifier, names in imports:
                with self.subTest(script=name, specifier=specifier):
                    m = PAGE_FILE.match(specifier)
                    self.assertIsNotNone(m, "not ./name.js")
                    self.assertIn(m.group(1), found, "not one of the page's modules")
                    self.assertNotEqual(m.group(1), name, "a module imports itself")
                    self.assertLessEqual(set(names), set(found[m.group(1)][1]),
                                         f"{m.group(1)} does not export these")
        self.assertIn('"./core.js"', [s for s, _ in found["meetings.js"][0]],
                      "the reading no longer finds the imports")

    def test_every_name_from_another_module_is_imported(self):
        # In the browser a name a module uses without importing it throws "is
        # not defined" only when that code runs, and the tests run only some
        # of it (#488 review: settings.js without its import of whenWord
        # passed every page test). This reads every module's code instead. It
        # has no scope analysis, so a local may not take another module's
        # top-level name either (it would hide a missing import).
        owner = {w: name for name, src in self.sources.items() for st in statements(src)
                 if st[0][1] in DECLARES for w in declared(st)}
        for name, src in self.sources.items():
            own = {w for w, home in owner.items() if home == name}
            with self.subTest(script=name):
                self.assertEqual(set(), unimported(src, own, set(owner) - own),
                                 f"{name} uses these without importing them, or has a local "
                                 "of the same name as another module's")

    def test_no_module_reads_an_imported_name_as_it_loads(self):
        # The modules import each other in cycles, so a module may be
        # evaluated before one it imports from, whose const, let and class are
        # not set until then (a function is): an initializer outside a function
        # body that reads an imported name could throw as the page loads
        # (#414 step 3a review: "const probeAtLoad = wire();" in polling.js).
        # main.js, evaluated after every module it reaches, may.
        for name, src in self.sources.items():
            if name == MAIN:
                continue
            imported = {w for _, names in links(src)[0] for w in names}
            for statement in statements(src):
                if statement[0][1] not in DECLARES:
                    continue
                with self.subTest(script=name, statement=" ".join(t[1] for t in statement[:3])):
                    self.assertEqual(set(), names_read_at_load(statement) & imported,
                                     f"{name} reads an imported name as it loads")

    def test_no_two_scripts_declare_the_same_name(self):
        # Modules could each have their own, but the tests reach every
        # module's top-level names in one namespace (tests/page_vm.js), and a
        # stub or a reader must not mistake one for another.
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
               'import { x, y } from "./x.js";\n'
               "import {\n  z, w\n} from './z.js';\n"
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
        self.assertEqual(links(src + "export { a, f };\nexport {\n  K\n};\n"),
                         ([('"./x.js"', ["x", "y"]), ("'./z.js'", ["z", "w"])], ["a", "f", "K"]))

    def test_it_finds_code_that_runs_at_load(self):
        for src in ("wire();\n", "window.x = 1;\n", "if (a) b();\n", "for (;;) {}\n",
                    'document.addEventListener("x", () => {});\n',
                    "const a = 1\nwire()\n",             # no semicolons
                    "function f() {}\nf();\n", "(() => {})();\n", "async () => {};\n",
                    # an import or export other than a plain list
                    'import "./x.js";\n', 'import x from "./x.js";\n',
                    'import * as x from "./x.js";\n', 'import { a as b } from "./x.js";\n',
                    'import { a, } from "./x.js";\n', "import { a } from x;\n",
                    "export default wire();\n", "export const a = wire();\n",
                    "export function f() {}\n", "export { a as b };\n",
                    'export { a } from "./x.js";\n', "export * from './x.js';\n",
                    'import("./x.js");\n',
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

    def test_it_finds_a_name_used_without_its_import(self):
        others = {"say", "wire", "when"}
        def found(src):
            return unimported(src, {"own"}, others)
        # Used, and not imported: in a call, a template, a shorthand property,
        # as a value; a local or parameter of the same name counts as a use.
        for src in ("function own() { say(1); }\n", "const own = () => `${wire()}`;\n",
                    "function own() { return { when }; }\n", "const own = [when];\n",
                    "function own() { const say = 1; return say; }\n",
                    "function own(wire) { return wire; }\n"):
            with self.subTest(src=src):
                self.assertTrue(found(src), src)
        # Imported, a property, an object key, a string, a comment or the
        # export list: not a missing import.
        for src in ('import { say } from "./core.js";\nfunction own() { say(1); }\n',
                    "function own(x) { return x.wire + x?.when; }\n",
                    "const own = { say: 1, wire: 2 };\n", 'const own = "say(1)";\n',
                    "// wire()\nfunction own() {}\n", "function own() {}\nexport { own };\n"):
            with self.subTest(src=src):
                self.assertEqual(set(), found(src), src)


class RealScriptsTests(unittest.TestCase):
    """tests/page_vm.js, which the page tests use to call the page's real
    functions (#414 step 3b, #488): every module loads, from main.js through
    its imports, into one Node.js context, where a test reaches each module's
    top-level names by name, and a test's code cannot quietly miss the page."""

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
        self.assertEqual(out["lets"], ["object", "boolean"])   # both polling.js
        # main.js ran as it does in the page: its listeners and window.workspace,
        # which holds what the check tools call (#488: no page name is global).
        self.assertIn("pywebviewready", out["started"])
        self.assertEqual(out["workspace"], sorted([
            "call", "show", "openMeeting", "refresh", "state", "play", "say", "setStep",
            "askClose", "errorHome", "showError", "groupOf"]))

    def test_the_harness_reads_every_top_level_name(self):
        # tests/page_vm.js reads each module's top-level names with a simple
        # reading of its lines; it must find exactly what the rules' tokeniser
        # finds, or a test could not reach a name the page declares.
        out = page_scripts.run("console.log(JSON.stringify(loaded.modules));")
        for path in page_scripts.paths():
            src = path.read_text(encoding="utf-8")
            own = [w for st in statements(src) if st[0][1] in DECLARES for w in declared(st)]
            with self.subTest(script=path.name):
                self.assertEqual(sorted(out[path.name]), sorted(own))

    def test_a_replacement_reaches_the_page_functions(self):
        out = page_scripts.run(r"""
const seen = [];
document = { getElementById: (id) => ({ id }) };
say = (text, opts) => seen.push([text, opts.kind]);
sayDone("Saved.");                                     // a page const that calls say
console.log(JSON.stringify({ el: $("status"), seen }));
""")
        self.assertEqual(out, {"el": {"id": "status"}, "seen": [["Saved.", "success"]]})

    def test_a_replacement_reaches_the_modules_that_import_it(self):
        # el is core.js's; deviceOption, in recording.js, imports it.
        out = page_scripts.run(r"""
el = (tag, props) => ({ tag, value: props.value });
console.log(JSON.stringify(deviceOption({ index: 3, name: "Synthetic microphone" })));
""")
        self.assertEqual(out, {"tag": "option", "value": "3"})

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
        self.assertTrue(text.startswith("function scriptLine(url, lineNo) {\n"), text)
        self.assertTrue(text.endswith("\n}"), text)
        self.assertNotIn("\r", text)
        with self.assertRaisesRegex(AssertionError, "not a function declared"):
            page_scripts.function_text("noSuchFunction")
        [handler] = page_scripts.listener_texts("document", "visibilitychange")
        self.assertIn("pollNow();", handler)


class HarnessModuleTests(unittest.TestCase):
    """What tests/page_vm.js refuses, as the browser would, shown with
    synthetic modules in a folder of their own. Plain node --check passes a
    file with import or export without checking it (Node.js 24, 6 Nov 2026),
    so CI's syntax job checks each page module from standard input with
    --input-type=module. The harness also stops on a module that does not
    parse, and every page test loads every module."""

    FILES = {
        "main.js": '"use strict";\nimport { twice } from "./a.js";\n'
                   "function start() { return twice(2); }\nexport { start };\n",
        "a.js": '"use strict";\nimport { one } from "./b.js";\n'
                "function twice(x) { return x * 2 * one(); }\n"
                "function lost() { return two(); }           // two is b.js's, not imported\n"
                "export { twice };\n",
        "b.js": '"use strict";\nfunction one() { return 1; }\nfunction two() { return 2; }\n'
                "export { one, two };\n",
    }

    def run_modules(self, code, **changed):
        with tempfile.TemporaryDirectory() as folder:
            for name, text in {**self.FILES, **changed}.items():
                Path(folder, name).write_text(text, encoding="utf-8")
            return page_scripts.run(code, main=Path(folder) / "main.js")

    def test_a_replacement_reaches_its_own_module_and_every_importer(self):
        out = self.run_modules("const first = start();\none = () => 10;\n"
                               "const second = [twice(2), start()];\ntwice = (x) => -x;\n"
                               "console.log(JSON.stringify([first, second, start(), two()]));")
        self.assertEqual(out, [4, [40, 40], -2, 2])

    def test_a_name_used_without_its_import_is_not_defined(self):
        with self.assertRaisesRegex(AssertionError,
                                    "two is not defined: a.js uses it without importing it"):
            self.run_modules("lost();")

    def test_what_the_browser_would_refuse_stops_the_run(self):
        good = self.FILES["a.js"]
        for case, a_js, error in (
                ("not exported", good.replace("{ one }", "{ one, three }"),
                 "does not provide an export named 'three'"),
                ("outside the folder", good.replace('"./b.js"', '"../b.js"'), "imports ../b.js"),
                ("by address", good.replace('"./b.js"', '"https://example.invalid/b.js"'),
                 "imports https://example.invalid/b.js"),
                ("no such file", good.replace('"./b.js"', '"./missing.js"'), "missing.js"),
                ("does not parse", good.replace("one(); }", "one();"), "SyntaxError")):
            with self.subTest(case=case):
                with self.assertRaisesRegex(AssertionError, re.escape(error)):
                    self.run_modules("console.log(1);", **{"a.js": a_js})


class StartUpTests(unittest.TestCase):
    """#488: a module script runs once the document is parsed, and pywebview
    sends pywebviewready only once the page has loaded, so main.js is listening
    by then. The page calls nothing until the event, and starts on it."""

    # Before the page loads: a bridge that records each call and refuses it
    # (so the start-up goes no further than its first view), timers that never
    # fire (no polling), a player that pauses (the view's change stops it),
    # and ready(), which sends the event to the window's listeners.
    BRIDGE = r"""
const called = [], realSetTimeout = setTimeout;
setTimeout = () => 0;
clearTimeout = () => {};
document.getElementById("player").pause = () => {};
window.pywebview = { api: {} };
const methods = { status: null, setup_state: null };
const answer = (name) => async () => { called.push(name); return { ok: false, error: "refused" }; };
const ready = () => {
  for (const fn of window.listeners.pywebviewready) fn(new Event("pywebviewready"));
};
"""

    def test_the_page_waits_for_the_event_and_starts_on_it(self):
        # pywebview's first script leaves window.pywebview.api empty; its
        # second adds the methods and sends the event.
        called = page_scripts.run("const before = called.slice();\n"
                                  "for (const name of Object.keys(methods))\n"
                                  "  window.pywebview.api[name] = answer(name);\n"
                                  "ready();\n"
                                  "realSetTimeout(() =>\n"
                                  "  console.log(JSON.stringify([before, called])), 50);",
                                  before=self.BRIDGE)
        self.assertEqual(called[0], [])
        self.assertEqual(called[1][:2], ["status", "setup_state"])
        self.assertEqual(called[1].count("setup_state"), 1)


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
        # the same reason; the page's own listener then skips that promise.
        meetings = self.number("meetings.js") * 100000
        out = self.run_page(
            "const sent = [], unhandled = [];\n"
            "reportUiError = (...args) => sent.push(args);\n"
            "process.on('unhandledRejection', (reason, promise) =>\n"
            "  unhandled.push([reason, promise]));\n"
            "const [onRejection] = loaded.window.listeners.unhandledrejection;\n"
            "const err = new Error('hidden');\n"
            "err.stack = String(err) + " + frames(f"f ({BASE}meetings.js:40:3)") + ";\n"
            "const value = guarded(async () => { throw err; })();\n"
            "const fine = guarded(async () => 5)();\n"
            "setTimeout(async () => {\n"
            "  let same = false;\n"
            "  try { await value; } catch (e) { same = e === err; }\n"
            "  const rejectedOn = unhandled.length === 1 && unhandled[0][0] === err;\n"
            "  onRejection({ reason: err, promise: unhandled[0][1] });   // not sent again\n"
            "  console.log(JSON.stringify([sent, same, rejectedOn, await fine]));\n"
            "}, 20);\n")
        self.assertEqual(out, [[["rejection", meetings + 40, 3]], True, True, 5])

    def test_guard_sends_a_string_or_number_rejection_once(self):
        # #501: the guard's sent rejections were known by their reason, in a
        # WeakSet, which cannot hold a string or a number, so the event for
        # one the guard had sent sent it again. Each is sent once now, and a
        # rejection no guard sent still reaches the listener.
        meetings = self.number("meetings.js") * 100000
        out = self.run_page(
            "const sent = [];\n"
            "reportUiError = (...args) => sent.push(args);\n"
            "const [onRejection] = loaded.window.listeners.unhandledrejection;\n"
            "process.on('unhandledRejection', (reason, promise) =>\n"
            "  onRejection({ reason, promise }));\n"
            "const err = new Error('hidden');\n"
            "err.stack = String(err) + " + frames(f"f ({BASE}meetings.js:40:3)") + ";\n"
            "guarded(async () => { throw 'text'; })();\n"
            "guarded(async () => { throw 7; })();\n"
            "guarded(async () => { throw err; })();\n"
            "Promise.reject(8);                       // no guard: its event sends it\n"
            "setTimeout(() => console.log(JSON.stringify(sent)), 30);\n")
        self.assertCountEqual(out, [["rejection", 0, 0], ["rejection", 0, 0],
                                    ["rejection", meetings + 40, 3], ["rejection", 0, 0]])

    def test_guard_skips_a_promise_the_page_handles(self):
        # #501: a callback may return a promise the page also handles (a catch
        # on it before it is returned, or after in the same task). That
        # rejection is not unhandled, so it is not sent; one nobody handles
        # is. Once guardCallbacks has run, Promise.prototype.then (which catch
        # and finally call) notes the promises it is called on.
        meetings = self.number("meetings.js") * 100000
        out = self.run_page(
            "guardCallbacks({ Promise });\n"
            "const sent = [], unhandled = [];\n"
            "reportUiError = (...args) => sent.push(args);\n"
            "process.on('unhandledRejection', (reason) => unhandled.push(reason));\n"
            "const err = new Error('hidden');\n"
            "err.stack = String(err) + " + frames(f"f ({BASE}meetings.js:40:3)") + ";\n"
            "const early = Promise.reject(err);\n"
            "early.catch(() => {});                   // handled before it is returned\n"
            "guarded(() => early)();\n"
            "const late = Promise.reject('text');\n"
            "guarded(() => late)();\n"
            "late.then(undefined, () => {});          // handled after, in the same task\n"
            "const after = Promise.reject(7);\n"
            "guarded(() => after)();\n"
            "queueMicrotask(() => after.finally(() => {}).catch(() => {}));   // a moment later\n"
            "const lone = Promise.reject(err);        // nobody handles it: sent, once\n"
            "guarded(() => lone)();\n"
            "setTimeout(async () => {\n"
            "  const thenWorks = await Promise.resolve(5).then((x) => x + 1);\n"
            "  console.log(JSON.stringify([sent, unhandled.length, unhandled[0] === err,\n"
            "                              thenWorks]));\n"
            "}, 30);\n")
        self.assertEqual(out, [[["rejection", meetings + 40, 3]], 1, True, 6])

    def test_guard_follows_a_rejection_the_page_passes_on(self):
        # #505 review: a then with no rejection handler, or a finally, marks
        # the promise handled but passes its rejection on to the promise it
        # returns, which no guard watched, so from file:// it was not sent at
        # all. The guard now watches that one too. And a promise two callbacks
        # return is watched once, so it is sent and rejected on once.
        meetings = self.number("meetings.js") * 100000
        out = self.run_page(
            "guardCallbacks({ Promise });\n"
            "const sent = [], unhandled = [];\n"
            "reportUiError = (...args) => sent.push(args);\n"
            "process.on('unhandledRejection', (reason) => unhandled.push(reason));\n"
            "const at = (line) => {\n"
            "  const err = new Error('hidden');\n"
            "  err.stack = String(err) + " + frames(f"f ({BASE}meetings.js:") + " + line + ':3)';\n"
            "  return err;\n"
            "};\n"
            "const passed = Promise.reject(at(41));\n"
            "guarded(() => passed)();\n"
            "passed.then((x) => x);                   // no rejection handler: passed on\n"
            "const fin = Promise.reject(at(42));\n"
            "guarded(() => fin)();\n"
            "fin.finally(() => {});                   // passed on\n"
            "const caught = Promise.reject(at(43));\n"
            "guarded(() => caught)();\n"
            "caught.then((x) => x).finally(() => {}).catch(() => {});   // handled at the end\n"
            "const twice = Promise.reject(at(44));\n"
            "guarded(() => twice)();\n"
            "guarded(() => twice)();                  // a second callback returns it\n"
            "setTimeout(() => console.log(JSON.stringify([sent, unhandled.length])), 30);\n")
        self.assertCountEqual(out[0], [["rejection", meetings + 41, 3],
                                       ["rejection", meetings + 42, 3],
                                       ["rejection", meetings + 44, 3]])
        self.assertEqual(out[1], 3)

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
            # On the virtual host (#488): for the error a guard threw on, the
            # browser's line is the guard's own throw, so the guard's place wins.
            "const thrown = new Error('hidden');\n"
            f"caughtAt = [{meetings + 38}, 9]; caughtErr = thrown;\n"
            "onError({ filename: " + json.dumps(BASE + "main.js") + ", lineno: 82, colno: 9,"
            " error: thrown });\n"
            "console.log(JSON.stringify([sent, cleared, caughtErr]));\n")
        self.assertEqual(out, [[["error", meetings + 38, 9], ["error", 0, 0],
                                ["error", self.number("core.js") * 100000 + 4, 2],
                                ["rejection", meetings + 40, 3], ["rejection", 0, 0],
                                ["error", meetings + 38, 9]], None, None])

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
