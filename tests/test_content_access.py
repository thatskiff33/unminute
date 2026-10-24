# Meeting content is read and written only through the Store's codec (#111
# C2a; docs/ENCRYPTION.md), which seals every content column (C2b-2).
#   - Static: no SQL in workspace/ outside store.py, and none in tools/ but
#     the known sites (TOOLS_RAW_SQL), names a content column
#     (store.CONTENT_COLUMNS: the logical or the sealed name), selects or
#     returns * with a table that has one, inserts into one without naming its
#     columns, or is built at run time around one. Conditions given to the
#     store's meetings(), holds() and folders() name plain columns only
#     (store.plain_condition). Only store.py names the key tables
#     (store.KEY_TABLES) in SQL; nothing in workspace/ passes protector= but
#     the Workspace handing its own to the Store, and nothing in workspace/ or
#     tools/ names the test key protector (tests/keyprotect.py). Every Store
#     and Workspace built in tests/ has the test protector, apart from the
#     named tests that use the app's own (REAL_PROTECTOR_TESTS).
#   - With the real codec and the test protector, a whole meeting's life
#     leaves no content readable in the database file or its log, every
#     content column is sealed, and everything the app shows is still right.
#   - While those runs go, a SQLite authorizer (ContentGuard) refuses any
#     statement outside store.py that reads or changes a content column.
# Synthetic text only.
#   python -m unittest discover -s tests -v

import ast
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import keyprotect  # noqa: E402

from workspace import store  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
HAVE_CRYPTO = importlib.util.find_spec("cryptography") is not None

# Each table's content column names: the logical ones (rows come back with
# them) and the sealed ones (the columns that exist). Either named in SQL
# outside the store is a problem: the second reads ciphertext around the
# codec, the first fails at run time (#111 C2b-2).
COLUMNS = {t: tuple(dict.fromkeys([*cols, *cols.values()]))
           for t, cols in store.CONTENT_COLUMNS.items()}

# -- the static check -----------------------------------------------------------------
# Every string the parser sees is a candidate. Pieces joined with "+" are
# folded into one text first, and so are the pieces of a name built up in one
# function or at module level ("q += piece", "q = q + piece"): a name assigned
# a string earlier in the same function, or at module level, stands for its
# text there, in f-string placeholders too. Other pieces known only at run
# time become HOLE. A text is SQL if it has a statement's shape or names a
# content table after FROM, JOIN, INTO or UPDATE (so half a query held in one
# name still counts). A string that is itself Python (a script for a child
# process) is parsed and checked as code. Matching ignores case, quoted and
# schema-qualified names ("meetings", [meetings], main.meetings, "{t}") are
# unquoted, and a table written into the text counts as used wherever its name
# appears (comma joins and subqueries included). A table named at run time is
# seen only as a slot straight after FROM, JOIN, INTO, UPDATE or TABLE.
# Names are followed in line order within one function, so of a name assigned
# on two branches the later text counts. An expression with a literal piece is
# also checked with each name as a piece known only at run time, so with a
# content table written in it, a name outside an "IN (...)" slot is flagged
# as a run-time build whatever text it held.
# It does not see: a run-time table after a comma, "(" or a comment, or after
# UPDATE OR <conflict> ("UPDATE OR REPLACE {t} SET ..."); a name assigned by
# tuple unpacking; the earlier text of a name assigned on two branches, where
# the expression is made only of names, or where the name is then extended
# with += (only the later text is extended); a str.format field whose index
# holds characters other than letters, digits, "_", ".", "[" and "]" (such as
# "{0[a-b]}", which is not taken as a slot); SQL passed between functions,
# held in attributes or containers, read from data or joined with str.join.
# The authorizer below covers only the paths the scrambling tests run. The
# backstop is C2b, planned to rename the sealed columns (#111), so a raw read
# missed here will fail loudly.
#   - Deletion (#111 C2b-1): no SQL in workspace/ outside store.py, or in
#     tools/, removes rows from a table that has content, or from a table
#     named at run time (DELETE_FROM_CONTENT): DELETE FROM, a replacing
#     conflict resolution (INSERT OR REPLACE INTO, REPLACE INTO, UPDATE OR
#     REPLACE) or DROP TABLE, with comments allowed between the words. Every
#     deletion of a meeting's rows goes through the store, so C2b can delete
#     its keys in the same place. It does not see rows removed by a table's
#     own ON CONFLICT REPLACE constraint (none has one; a plain INSERT would
#     then replace rows) or by a trigger (there are none), and it shares the
#     limits above (a table after "(" or a comma, SQL passed between
#     functions, and so on).

HOLE = " \x00 "
LOOKS_LIKE_SQL = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|REPLACE)\b"
    r"|(?i:\bselect\b[\s\S]*\bfrom\b|\binsert\s+(?:or\s+\w+\s+)?into\b|\breplace\s+into\b"
    r"|\bupdate\s+\S+\s+set\b|\bdelete\s+from\b)")
NAMES_CONTENT_TABLE = re.compile(
    r"\b(?:from|join|into|update)\s+(?:" + "|".join(store.CONTENT_COLUMNS) + r")\b")
# A key table (store.KEY_TABLES) after a keyword that names a table.
NAMES_KEY_TABLE = re.compile(
    r"\b(?:from|join|into|update|table)\s+(?:" + "|".join(store.KEY_TABLES) + r")\b")
QUOTED = re.compile(r'"(\w+)"|\[(\w+)\]|`(\w+)`')
QUOTED_HOLE = re.compile(r"[\"'`\[]\s*\x00\s*[\"'`\]]")
QUOTED_TABLE = re.compile(r"\b(from|join|into|update|table)\s*'(\w+)'")
SCHEMA_NAME = re.compile(r"\b(?:main|temp)\s*\.\s*")
# A %-conversion (with a mapping key, flags, width or precision) or a
# str.format field (with an index, attribute, conversion or spec). It errs on
# the strict side: a LIKE pattern such as '%d%', or an escaped "%%s" or
# "{{x}}", counts as a slot too.
FORMAT_SLOT = re.compile(r"%(?:\(\w+\))?[-#0 +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?"
                         r"[diouxXeEfFgGcrsa]|\{[\w.\[\]]*(?:![rsa])?(?::[^{}]*)?\}")
TABLE_SLOT = re.compile(r"\b(?:from|join|into|update|table)\s+\x00")
STAR = re.compile(r"(?:\bselect\s+(?:distinct\s+|all\s+)?|,\s*|\.\s*|\breturning\s+)\*")
LIST_SLOT = re.compile(r"\bin\s*\(\s*\x00\s*\)")
POSITIONAL = (r"\b(?:insert|replace)\s+(?:or\s+\w+\s+)?into\s+{}\s*(?:as\s+\w+\s*)?"
              r"(?:values|select|default)\b")
RUN_TIME = "<run-time table>"
# A statement that removes rows from a table with content, or from a table
# named at run time (a slot): DELETE FROM; a conflict resolution that replaces
# rows (INSERT OR REPLACE INTO, REPLACE INTO, UPDATE OR REPLACE); DROP TABLE.
# Comments may stand between the words and before the table.
# A comment cannot run past its own end ("*/"), so a text with many comments
# is matched in linear time (C2b-1 review, carried into C2b-2).
_SQL_GAP = r"(?:\s|/\*(?:[^*]|\*(?!/))*\*/|--[^\n]*(?:\n|$))+"
DELETE_FROM_CONTENT = re.compile(
    r"\b(?:delete" + _SQL_GAP + r"from|replace" + _SQL_GAP + r"into"
    r"|update" + _SQL_GAP + r"or" + _SQL_GAP + r"replace"
    r"|drop" + _SQL_GAP + r"table(?:" + _SQL_GAP + r"if" + _SQL_GAP + r"exists)?)"
    + _SQL_GAP + r"(?:(?:" + "|".join(store.CONTENT_COLUMNS) + r")\b|\x00)")


sys.path.append(str(Path(__file__).resolve().parents[1] / "tools"))
import decoder_runtime  # noqa: E402  (tools/decoder_runtime.py)


def setUpModule():
    # #299: the decoder runs in an AppContainer that cannot read this Python:
    # decodes here use a scratch runtime it can read and a throwaway profile.
    decoder_runtime.use()


def tearDownModule():
    decoder_runtime.stop()


def _fold(node, env=None):
    """The text of a string expression, or None if it is not one. With env,
    a name it holds stands for that text."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return (env or {}).get(node.id)
    if isinstance(node, ast.JoinedStr):
        return "".join(_placeholder(v, env) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left, env), _fold(node.right, env)
        if left is None and right is None:
            return None
        return (HOLE if left is None else left) + (HOLE if right is None else right)
    return None


def _placeholder(value, env):
    """One piece of an f-string: its text, a known name's text, or HOLE."""
    if isinstance(value, ast.Constant):
        return value.value
    if (isinstance(value, ast.FormattedValue) and value.conversion == -1
            and value.format_spec is None and isinstance(value.value, ast.Name)
            and (text := (env or {}).get(value.value.id)) is not None):
        return text
    return HOLE


def _texts(tree):
    """(node, text) for each string expression not part of a larger one."""
    parts = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            parts.update((id(node.left), id(node.right)))
        elif isinstance(node, ast.JoinedStr):
            parts.update(id(v) for v in ast.walk(node) if v is not node)
    for node in ast.walk(tree):
        if id(node) not in parts and (text := _fold(node)) is not None:
            yield node, text


SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _own_nodes(scope):
    """A module's or function's nodes, without those of nested functions and classes."""
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, (*SCOPES, ast.Lambda)):
            todo.extend(ast.iter_child_nodes(node))


def _scopes(tree, prefix):
    """(node, qualified name) for the module and every function and class in it."""
    out, todo = [(tree, prefix or "<module>")], [(tree, prefix)]
    while todo:
        node, name = todo.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, SCOPES):
                qual = f"{name}.{child.name}" if name else child.name
                out.append((child, qual))
                todo.append((child, qual))
            else:
                todo.append((child, name))
    return out


def _with_names(scope, env):
    """(line, text) for each string expression in a scope that uses a name
    assigned a string before it (in env, which starts with the module's names
    and is updated in place, in line order): the names' texts put in."""
    nodes = sorted((n for n in _own_nodes(scope) if hasattr(n, "lineno")),
                   key=lambda n: (n.lineno, n.col_offset))
    parts, out = set(), []
    for node in nodes:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            parts.update((id(node.left), id(node.right)))
        elif isinstance(node, ast.JoinedStr):
            parts.update(id(v) for v in ast.walk(node) if v is not node)

    def joined(node):
        """The node's text with names put in, if that differs from without."""
        if isinstance(node, (ast.BinOp, ast.JoinedStr)):
            text = _fold(node, env)
            if text is not None and text != _fold(node):
                out.append((node.lineno, text))
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            text = None
            if node.value is not None:
                parts.add(id(node.value))
                joined(node.value)
                text = _fold(node.value, env)
            for target in targets:
                if isinstance(target, ast.Name):
                    env.pop(target.id, None)
                    if text is not None and len(targets) == 1:
                        env[target.id] = text
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            name, before = node.target.id, env.pop(node.target.id, None)
            if isinstance(node.op, ast.Add) and before is not None:
                parts.add(id(node.value))
                env[name] = before + (_fold(node.value, env) or HOLE)
                out.append((node.lineno, env[name]))
        elif id(node) not in parts:
            joined(node)
    return out


def _script(text):
    """The parsed tree when a string is a Python program, else None."""
    if "\n" not in text or not re.search(r"^(?:def|import|from)\s", text, re.M):
        return None
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def sql_in_source(source, prefix=""):
    """(line, scope, text) for every SQL text in Python source."""
    tree = ast.parse(source)
    scopes = _scopes(tree, prefix)
    owner = {id(node): name for scope, name in scopes for node in _own_nodes(scope)}
    module = {}
    for i, (scope, name) in enumerate(scopes):
        # The module comes first: its names are known in every function and class.
        for line, text in _with_names(scope, module if i == 0 else dict(module)):
            if _is_sql(text):
                yield line, name, text
    for node, text in _texts(tree):
        name = owner.get(id(node), prefix or "<module>")
        if isinstance(node, ast.Constant) and _script(text) is not None:
            for line, scope, sql in sql_in_source(text, f"{name}:script"):
                yield node.lineno + line - 1, scope, sql
        elif _is_sql(text):
            yield node.lineno, name, text


def _is_sql(text):
    """Whether a text has a statement's shape, names a content or key table
    or removes rows from one (DELETE_FROM_CONTENT, such as "drop table meetings")."""
    normal = _normal(text)
    return bool(LOOKS_LIKE_SQL.search(text) or NAMES_CONTENT_TABLE.search(normal)
                or NAMES_KEY_TABLE.search(normal) or DELETE_FROM_CONTENT.search(normal))


def _normal(text):
    """Lower case; names unquoted (a quoted slot as HOLE) and unqualified;
    format slots (FORMAT_SLOT) as HOLE."""
    text = FORMAT_SLOT.sub(HOLE, text).lower()
    text = QUOTED_HOLE.sub(HOLE, text)
    # A space before an unquoted name: FROM"meetings" is FROM meetings (C2b-1
    # review, carried into C2b-2).
    text = QUOTED.sub(lambda m: " " + m.group(m.lastindex), text)
    text = QUOTED_TABLE.sub(r"\1 \2", text)
    return SCHEMA_NAME.sub("", text)


def content_problems(text):
    """What a SQL text does with content: names a content column of a table it
    uses, selects or returns * with one, inserts into one without naming the
    columns, or is built at run time around one (a slot other than "IN (?)").
    A table named only at run time (a slot straight after FROM, JOIN, INTO,
    UPDATE or TABLE) is taken to be any table with content."""
    t = _normal(text)
    tables = [name for name in store.CONTENT_COLUMNS if re.search(rf"\b{name}\b", t)]
    dynamic = bool(TABLE_SLOT.search(t))
    if not tables and not dynamic:
        return []
    out = [f"{table}.{column}" for table in tables for column in COLUMNS[table]
           if re.search(rf"\b{column}\b", t)]
    if dynamic:
        out += sorted({f"{RUN_TIME}.{column}" for columns in COLUMNS.values()
                       for column in columns if re.search(rf"\b{column}\b", t)})
    if STAR.search(t):
        out.append("* with " + ", ".join(tables + [RUN_TIME] * dynamic))
    out += [f"insert into {table} without columns" for table in tables
            if re.search(POSITIONAL.format(table), t)]
    if dynamic and re.search(POSITIONAL.format("\x00"), t):
        out.append(f"insert into {RUN_TIME} without columns")
    rest = LIST_SLOT.sub("", t)
    if tables and "\x00" in rest:
        out.append("built at run time with " + ", ".join(tables))
    if dynamic and "\x00" in TABLE_SLOT.sub(" ", rest):
        out.append(f"built at run time with {RUN_TIME}")
    return out


def problems_in(paths):
    """{(path, scope, problem)} for the SQL in these files."""
    found = set()
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        for _line, scope, text in sql_in_source(path.read_text(encoding="utf-8")):
            found.update((rel, scope, problem) for problem in content_problems(text))
    return found


def deletions_in(paths):
    """{(path, scope, text)} for each SQL text in these files that deletes
    from a table with content (DELETE_FROM_CONTENT)."""
    found = set()
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        for _line, scope, text in sql_in_source(path.read_text(encoding="utf-8")):
            if DELETE_FROM_CONTENT.search(_normal(text)):
                found.add((rel, scope, " ".join(text.split())))
    return found


def conditions_in(paths):
    """(path, line, text) for each constant condition given to the store's
    meetings(), holds() or folders()."""
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("meetings", "holds", "folders") and node.args
                    and (text := _fold(node.args[0])) is not None):
                yield path.relative_to(ROOT).as_posix(), node.lineno, text


def workspace_modules():
    return sorted(p for p in (ROOT / "workspace").rglob("*.py") if p.name != "store.py")


KEY_TABLE = re.compile(r"\b(?:" + "|".join(store.KEY_TABLES) + r")\b")


def key_table_sql_in(paths):
    """{(path, scope, text)} for each SQL text that names a key table."""
    found = set()
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        for _line, scope, text in sql_in_source(path.read_text(encoding="utf-8")):
            if KEY_TABLE.search(_normal(text)):
                found.add((rel, scope, " ".join(text.split())))
    return found


def protector_keywords(paths):
    """(path, function called, value) for each call with a protector= keyword."""
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                for k in node.keywords:
                    if k.arg == "protector":
                        f = node.func
                        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "?")
                        yield (path.relative_to(ROOT).as_posix(), name,
                               k.value.id if isinstance(k.value, ast.Name) else ast.dump(k.value))


# Tests that build a Store or Workspace with the app's own protector (Windows
# Data Protection) on purpose, by (file, test). Every other construction in
# tests/ names protector=, or passes **name where the same function sets
# name.setdefault("protector", ...) (keyprotect.open_store and its kind): on
# Linux CI the default protector refuses, and on Windows a test that leaves
# it out would use the real one without meaning to (#111 C2b-2).
REAL_PROTECTOR_TESTS = {
    # The default workspace under a patched LOCALAPPDATA, where the test
    # protector is refused (Windows only).
    ("test_cloud_intake.py",
     "SyncedSourceTests.test_a_recording_in_onedrive_still_imports_and_is_watched"),
    # No plain path: the default protector failing leaves no workspace.
    ("test_schema2.py", "OpeningTests.test_there_is_no_plain_path"),
    # The real protector's round trip (Windows only).
    ("test_schema2.py", "OpeningTests.test_windows_data_protection_round_trip"),
}


def _sets_protector(function):
    """The names n for which function calls n.setdefault("protector", ...)."""
    out = set()
    for node in ast.walk(function):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setdefault" and isinstance(node.func.value, ast.Name)
                and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "protector"):
            out.add(node.func.value.id)
    return out


def constructions_without_protector(source, filename):
    """(filename, enclosing function, line) for each Store(...) or
    Workspace(...) call that neither names protector= nor passes **name
    where its function sets name.setdefault("protector", ...)."""
    found = []

    def visit(node, scope, given):
        for child in ast.iter_child_nodes(node):
            inner, sets = scope, given
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = scope + [child.name]
                if not isinstance(child, ast.ClassDef):
                    sets = given | _sets_protector(child)
            if isinstance(child, ast.Call):
                f = child.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                if name in ("Store", "Workspace"):
                    named = any(k.arg == "protector" for k in child.keywords)
                    spread = any(k.arg is None and isinstance(k.value, ast.Name)
                                 and k.value.id in sets for k in child.keywords)
                    if not (named or spread):
                        found.append((filename, ".".join(scope), child.lineno))
            visit(child, inner, sets)
    visit(ast.parse(source), [], frozenset())
    return found


def test_modules():
    return sorted((ROOT / "tests").glob("*.py"))


def tool_modules():
    return sorted((ROOT / "tools").rglob("*.py"))


# Tools that still read or write content columns directly. C2b-1 (#111) moved
# the last of them onto the store, so none is left. The test keeps this list
# exact: a new site fails.
TOOLS_RAW_SQL = set()


def problems_in_source(source):
    return [p for _line, _scope, text in sql_in_source(source) for p in content_problems(text)]


class StaticTests(unittest.TestCase):

    def test_only_the_store_names_content_columns(self):
        self.assertEqual(set(), problems_in(workspace_modules()))

    def test_tools_with_raw_content_sql_are_exactly_the_known_ones(self):
        self.assertEqual(TOOLS_RAW_SQL, problems_in(tool_modules()))

    def test_only_the_store_deletes_rows_with_content(self):
        self.assertEqual(set(), deletions_in(workspace_modules() + tool_modules()))

    def test_only_the_store_names_the_key_tables(self):
        # crypto_meta, content_keys and recording_keys are the erase points
        # (#111 C2b-2): only store.py reads, writes or deletes them.
        self.assertEqual(set(), key_table_sql_in(workspace_modules() + tool_modules()))
        caught = ['def f(db):\n    db.execute("DELETE FROM content_keys WHERE meeting_id = ?")\n',
                  'def f(db):\n    return db.execute("SELECT root FROM crypto_meta")\n',
                  'def f(db):\n    db.execute(\'DROP TABLE "recording_keys"\')\n']
        for source in caught:
            with self.subTest(source=source):
                self.assertTrue([t for _l, _s, t in sql_in_source(source)
                                 if KEY_TABLE.search(_normal(t))])

    def test_only_the_workspace_passes_a_protector_and_only_its_own(self):
        # The test protector keeps the root key in effect in clear text: the
        # app's code never chooses a protector (the Store's default is Windows
        # Data Protection), and nothing it ships names the test one.
        self.assertEqual([("workspace/app.py", "Store", "protector")],
                         list(protector_keywords(workspace_modules()
                                                 + [ROOT / "workspace" / "store.py"])))
        named = [p.relative_to(ROOT).as_posix()
                 for p in workspace_modules() + tool_modules() + [ROOT / "workspace" / "store.py"]
                 if re.search(r"keyprotect|TestProtector", p.read_text(encoding="utf-8"))]
        self.assertEqual([], named)

    def test_every_test_store_and_workspace_has_the_test_protector(self):
        # Only the named tests use the app's own protector, and each still does.
        found = [c for p in test_modules()
                 for c in constructions_without_protector(p.read_text(encoding="utf-8"),
                                                          p.name)]
        self.assertEqual([], [c for c in found if c[:2] not in REAL_PROTECTOR_TESTS])
        self.assertEqual(REAL_PROTECTOR_TESTS, {c[:2] for c in found})

    def test_the_protector_check_sees_a_missing_protector(self):
        source = (
            "class T:\n"
            "    def setUp(self):\n"
            "        self.kw = dict(battery=False)\n"
            "    def a(self):\n"
            "        return Workspace(root, start=False, **self.kw)\n"     # not seen to set it
            "    def b(self, **kw):\n"
            "        return store.Store(path, **kw)\n"                     # no setdefault
            "    def c(self, **kw):\n"
            "        kw.setdefault('protector', keyprotect.TestProtector())\n"
            "        return Store(path, **kw)\n"                           # passes
            "    def d(self):\n"
            "        return Store(path, protector=keyprotect.TestProtector())\n")  # passes
        self.assertEqual([("x.py", "T.a", 5), ("x.py", "T.b", 7)],
                         constructions_without_protector(source, "x.py"))

    def test_the_comment_gap_takes_linear_time(self):
        # Many comments and no match: the gap must not try every way of
        # joining them (it took seconds before the C2b-2 change).
        import time
        text = "delete " + "/* a */ " * 26 + "x"
        started = time.perf_counter()
        self.assertIsNone(DELETE_FROM_CONTENT.search(_normal(text)))
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_the_check_sees_deletions(self):
        caught = ["DELETE FROM meetings WHERE id = ?", "delete  from\n  Meetings where id = ?",
                  'DELETE FROM "meetings" WHERE id = ?', "DELETE FROM main.meetings",
                  "DELETE FROM [sources] WHERE meeting_id = ?", "Delete From `holds`",
                  "DELETE FROM segments WHERE version_id = ?",
                  "DELETE FROM speaker_corrections WHERE id = ?",
                  "DELETE FROM watched_folders WHERE id = ?",
                  "DELETE FROM {} WHERE id = ?", 'DELETE FROM "%s"',
                  # Comments between the words or before the table.
                  "DELETE FROM /* again */ meetings WHERE id = ?",
                  "delete /* a */ from -- b\n meetings", "DELETE FROM /**/ {}",
                  # Conflict resolutions that replace (delete) rows.
                  "INSERT OR REPLACE INTO holds (id, meeting_id, scope, created_at) "
                  "VALUES (?, ?, ?, ?)",
                  "REPLACE INTO sources (id, meeting_id) VALUES (?, ?)",
                  "insert or replace into {} (id) values (?)",
                  "UPDATE OR REPLACE meetings SET phase = ? WHERE id = ?",
                  # Dropping a table.
                  "DROP TABLE meetings", "drop table if exists main.segments",
                  'DROP TABLE IF EXISTS "{t}"',
                  # A quoted or bracketed name with no space after the keyword.
                  'DELETE FROM"meetings" WHERE id = ?', "DELETE FROM[sources]",
                  "delete from`holds`", "DELETE FROM'meetings'",
                  # Tables added by schema 2.
                  "DELETE FROM recording_sources WHERE meeting_id = ?",
                  "DELETE FROM intake_meta"]
        for text in caught:
            with self.subTest(text=text):
                self.assertTrue(DELETE_FROM_CONTENT.search(_normal(text)), text)
        for shape, source in {
                "f-string table": 'def f(db, t):\n'
                                  '    db.execute(f"DELETE FROM {t} WHERE id = ?")\n',
                "name": 'def f(db):\n    q = "DELETE FROM meetings"\n'
                        '    db.execute(q + " WHERE id = ?")\n',
                "comment": 'def f(db, mid):\n'
                           '    db.execute("DELETE FROM /* again */ meetings WHERE id = ?",'
                           ' (mid,))\n',
                "drop, lower case": 'def f(db):\n    db.execute("drop table meetings")\n',
                "replace": 'def f(db):\n'
                           '    db.execute("REPLACE INTO sources (id) VALUES (?)", (1,))\n',
                }.items():
            with self.subTest(shape=shape):
                self.assertTrue([t for _l, _s, t in sql_in_source(source)
                                 if DELETE_FROM_CONTENT.search(_normal(t))], shape)
        for text in ("DELETE FROM cleanup_jobs WHERE id = ?", "DELETE FROM receipts",
                     "DELETE FROM intake_seen WHERE folder_id = ?",
                     "DELETE FROM intake_content WHERE imported_at < ?",
                     "DELETE FROM meetings_old", "SELECT id FROM meetings",
                     "INSERT OR REPLACE INTO intake_content VALUES (?, ?)",
                     "INSERT OR IGNORE INTO holds (id) VALUES (?)",
                     "UPDATE meetings SET phase = ? WHERE id = ?",
                     "UPDATE OR IGNORE meetings SET phase = ?",
                     "INSERT INTO meetings (id) VALUES (?) ON CONFLICT(id) DO UPDATE SET "
                     "phase = excluded.phase",
                     "DROP TABLE intake_seen", "DROP INDEX meetings_phase",
                     "DELETE FROM /* meetings */ receipts"):
            with self.subTest(text=text):
                self.assertIsNone(DELETE_FROM_CONTENT.search(_normal(text)), text)

    def test_conditions_given_to_the_store_are_plain(self):
        found = list(conditions_in(workspace_modules() + tool_modules()))
        self.assertTrue(found)
        for path, line, text in found:
            with self.subTest(at=f"{path}:{line}"):
                self.assertEqual(text, store.plain_condition(text))

    def test_the_check_sees_content_sql(self):
        caught = {
            "SELECT * FROM meetings": "* with meetings",
            "SELECT m.* FROM meetings m": "* with meetings",
            "SELECT id, title FROM meetings": "meetings.title",
            "SELECT id, title_sealed FROM meetings": "meetings.title_sealed",
            "SELECT text_sealed FROM segments": "segments.text_sealed",
            "SELECT title FROM\"meetings\"": "meetings.title",
            "UPDATE watched_folders SET path_sealed = ?": "watched_folders.path_sealed",
            "SELECT info_sealed FROM recording_sources": "recording_sources.info_sealed",
            "SELECT v_sealed FROM intake_meta WHERE k = 'salt'": "intake_meta.v_sealed",
            "UPDATE meetings SET title = ?": "meetings.title",
            "SELECT s.id FROM segments s JOIN edits e ON e.segment_id = s.id "
            "WHERE e.text IS NULL": "edits.text",
            "SELECT wf.path FROM sources s JOIN watched_folders wf": "watched_folders.path",
            "INSERT INTO holds (id, reason) VALUES (?, ?)": "holds.reason",
            # lower and mixed case
            "select title from meetings": "meetings.title",
            "Delete From holds Where Reason = ?": "holds.reason",
            # comma joins, and alias.* or * anywhere in the list
            "SELECT m.title FROM versions v, meetings m WHERE m.id = v.meeting_id":
                "meetings.title",
            "SELECT v.id, m.* FROM versions v, meetings m": "* with meetings",
            "SELECT v.id, m.* FROM versions v JOIN meetings m ON m.id = v.meeting_id":
                "* with meetings",
            "SELECT id, * FROM holds": "* with holds",
            "SELECT DISTINCT h.* FROM holds h": "* with holds",
            # qualified and quoted names
            "SELECT * FROM main.meetings": "* with meetings",
            'SELECT * FROM "main"."meetings"': "* with meetings",
            'SELECT "title" FROM "meetings"': "meetings.title",
            "SELECT [title] FROM [meetings]": "meetings.title",
            "SELECT `reason` FROM `holds`": "holds.reason",
            "SELECT * FROM 'holds'": "* with holds",
            "SELECT h.reason FROM temp.holds h": "holds.reason",
            # inserts without a column list, REPLACE and RETURNING
            "INSERT OR REPLACE INTO reviewed_snapshots VALUES (?,?,?,?)":
                "insert into reviewed_snapshots without columns",
            "INSERT INTO holds VALUES (?, ?, ?, ?, ?, ?, ?)":
                "insert into holds without columns",
            "insert into meetings select * from old_meetings":
                "insert into meetings without columns",
            "REPLACE INTO edits VALUES (?, ?, ?)": "insert into edits without columns",
            "REPLACE INTO edits (segment_id, text, edited_at) VALUES (?, ?, ?)": "edits.text",
            "DELETE FROM holds WHERE id = ? RETURNING *": "* with holds",
            "UPDATE holds SET released_at = ? WHERE id = ? RETURNING reason": "holds.reason",
            # a content column in a condition or a subquery
            "DELETE FROM holds WHERE reason LIKE ?": "holds.reason",
            "SELECT id FROM versions WHERE meeting_id IN "
            "(SELECT id FROM meetings WHERE title = ?)": "meetings.title",
            # format slots: a column chosen at run time
            "SELECT %s FROM meetings": "built at run time with meetings",
            "SELECT {} FROM holds": "built at run time with holds",
            "SELECT %.5s FROM meetings": "built at run time with meetings",
            "SELECT {c!s:>9} FROM holds": "built at run time with holds",
            # a table named at run time, quoted or not, with another slot or
            # a positional insert
            'SELECT title FROM "{}"': "<run-time table>.title",
            "SELECT * FROM [{}]": "* with <run-time table>",
            "SELECT * FROM main.`{}`": "* with <run-time table>",
            "SELECT %s FROM {}": "built at run time with <run-time table>",
            "SELECT id FROM {} WHERE {} = ?": "built at run time with <run-time table>",
            "INSERT INTO {} VALUES (?, ?)": "insert into <run-time table> without columns",
            "INSERT OR REPLACE INTO '{}' VALUES (?)":
                "insert into <run-time table> without columns",
        }
        for text, problem in caught.items():
            with self.subTest(text=text):
                self.assertIn(problem, content_problems(text))
        fine = ["SELECT COUNT(*) FROM meetings", "SELECT id, reviewed_at FROM meetings",
                "select count(*) from holds where released_at is null",
                "SELECT phase, COUNT(*) FROM meetings GROUP BY phase",
                "DELETE FROM segments WHERE version_id = ?",
                "UPDATE cleanup_jobs SET error = ? WHERE id = ?",
                "UPDATE watched_folders SET last_error = ? WHERE id = ?",
                "SELECT 1 FROM watched_folders WHERE path_fp = ?",
                "SELECT 1 FROM intake_content WHERE fp = ?",
                "SELECT 1 FROM sources WHERE original_fp = ?",
                "INSERT INTO segment_speakers VALUES (?,?,?,?)",
                "INSERT INTO speaker_turns VALUES (?,?,?,?,?)",
                "SELECT (end_sample - start_sample) * 2 FROM chunks",
                "UPDATE speaker_runs SET status = 'superseded' WHERE version_id = ?",
                "SELECT * FROM versions WHERE state = 'active'",
                'SELECT COUNT(*) FROM "{}"', "SELECT id FROM [{}] WHERE id IN ({})",
                "DELETE FROM {} WHERE id = ?"]
        for text in fine:
            with self.subTest(text=text):
                self.assertEqual([], content_problems(text))

    def test_the_check_sees_sql_put_together_in_code(self):
        sources = {
            "+": 'def f(db):\n    db.execute("SELECT wf.path FROM sources s " +\n'
                 '               "JOIN watched_folders wf ON wf.id = s.intake_folder")\n',
            "+=": 'def f(db, mid):\n    q = "SELECT id FROM holds WHERE meeting_id = ?"\n'
                  '    q += " AND reason LIKE ?"\n    return db.execute(q, (mid, "%x%"))\n',
            "f-string column": 'def f(db, col):\n    db.execute(f"SELECT {col} FROM meetings")\n',
            "f-string table": 'def f(db, t):\n    db.execute(f"SELECT title FROM {t}")\n',
            "f-string star": 'def f(db, t):\n    db.execute(f"SELECT * FROM {t} WHERE id = ?")\n',
            "% slot": 'def f(db, c):\n    db.execute("SELECT %s FROM meetings" % c)\n',
            "variable piece": 'def f(db, w):\n    db.execute("SELECT id FROM holds WHERE " + w)\n',
            "child script": 'CHILD = r"""\nimport sqlite3\n\n\ndef inspect(db):\n'
                            '    return db.execute("SELECT text FROM segments").fetchall()\n"""\n',
        }
        for shape, source in sources.items():
            with self.subTest(shape=shape):
                self.assertTrue(problems_in_source(source), shape)
        # Names followed to their text, half a query, and a table named at run time.
        named = {
            "q = q + piece": (
                'def f(db):\n    q = "SELECT id FROM holds"\n'
                '    q = q + " WHERE reason = ?"\n    return db.execute(q)\n',
                "holds.reason"),
            "annotated, then +=": (
                'def f(db):\n    q: str = "SELECT id FROM meetings"\n'
                '    q += " WHERE title = ?"\n    return db.execute(q)\n',
                "meetings.title"),
            "module prefix": (
                'BASE = "SELECT id, title "\n\n\ndef f(db):\n'
                '    return db.execute(BASE + "FROM meetings")\n',
                "meetings.title"),
            "module name in an f-string": (
                'COLS = "id, title"\n\n\ndef f(db):\n'
                '    return db.execute(f"SELECT {COLS} FROM meetings")\n',
                "meetings.title"),
            "two names": (
                'def f(db):\n    a = "SELECT id, "\n'
                '    b = a + "path FROM watched_folders"\n    return db.execute(b)\n',
                "watched_folders.path"),
            "half a query": (
                'def f(db, cols):\n'
                '    return db.execute(cols + " FROM watched_folders WHERE path = ?")\n',
                "watched_folders.path"),
            "quoted run-time table": (
                'def f(db, t):\n    return db.execute(f\'SELECT title FROM "{t}"\')\n',
                "<run-time table>.title"),
            "run-time table and column": (
                'def f(db, t, c):\n    return db.execute(f"SELECT {c} FROM {t}")\n',
                "built at run time with <run-time table>"),
            "run-time positional insert": (
                'def f(db, t):\n    db.execute(f"INSERT INTO [{t}] VALUES (?, ?)", (1, 2))\n',
                "insert into <run-time table> without columns"),
        }
        for shape, (source, problem) in named.items():
            with self.subTest(shape=shape):
                self.assertIn(problem, problems_in_source(source))
        fine = ('def f(db, marks):\n    db.execute(f"SELECT id FROM meetings WHERE id IN '
                '({marks})")\n',
                'def f(db, t):\n    return db.execute(f"SELECT COUNT(*) FROM {t}")\n',
                'def f(db, t):\n    return db.execute(f\'SELECT COUNT(*) FROM "{t}"\')\n',
                'MESSAGE = "Holds never expire on their own; the update waits."\n')
        for source in fine:
            with self.subTest(source=source):
                self.assertEqual([], problems_in_source(source))

    def test_store_conditions_refuse_content_and_quotes(self):
        for where in ("title LIKE ?", "LAST_ERROR IS NULL", '"title" = ?', "phase != 'error'",
                      "title_sealed IS NOT NULL", "path_sealed = ?", "reason_sealed = ?",
                      "id = ?; DROP TABLE meetings", "id = ? -- the rest", "id = ? /* x */",
                      "id IN (SELECT meeting_id FROM holds WHERE reason = ?)", "[path] = ?",
                      "`reason` = ?", "(?, ?, ?, ?, ?, ?, ?) IN (SELECT * FROM holds)",
                      "id IN (SELECT meeting_id FROM versions)",
                      "id IN (select meeting_id from versions)",
                      # IN with a table or table-valued function, not a list
                      "(?, ?, ?, ?, ?, ?, ?) IN holds", "id IN json_each(?)",
                      # * on its own, with no subquery
                      "COUNT(*) > 0", None):
            with self.subTest(where=where), self.assertRaises(ValueError):
                store.plain_condition(where)
        with tempfile.TemporaryDirectory() as tmp:
            s = store.Store(Path(tmp) / "w.db", protector=keyprotect.TestProtector())
            try:
                s.create_meeting("synthetic")
                for call, where in ((s.meetings, "title LIKE ?"), (s.holds, "reason = ?"),
                                    (s.folders, "path = ?")):
                    with self.subTest(call=call.__name__), self.assertRaises(ValueError):
                        call(where, ("x",))
                self.assertEqual(1, len(s.meetings("phase != ?", ("error",))))
                self.assertEqual(1, len(s.meetings("phase NOT IN (?, ?)", ("error", "x"))))
            finally:
                s.close()

    def test_the_content_columns_are_the_schemas(self):
        # Every sealed column exists and ends "_sealed"; no column keeps a
        # logical name, so a raw read or write that went around the codec
        # fails with "no such column" (#111 C2b-2).
        db = sqlite3.connect(":memory:")
        db.executescript(store.SCHEMA)
        for table, columns in store.CONTENT_COLUMNS.items():
            have = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            with self.subTest(table=table):
                self.assertTrue(set(columns.values()) <= have, (table, columns, have))
                self.assertFalse(set(columns) & have, (table, columns, have))
                self.assertTrue(all(c.endswith("_sealed") for c in columns.values()))
                self.assertEqual(columns.keys(), {c[:-len("_sealed")] if c != "words_sealed"
                                                  else "words_json" for c in columns.values()})
        # Nothing else in the schema is sealed.
        sealed = {(t, r[1]) for (t,) in db.execute(
                      "SELECT name FROM sqlite_master WHERE type = 'table'")
                  for r in db.execute(f'PRAGMA table_info("{t}")') if r[1].endswith("_sealed")}
        self.assertEqual(sealed, {(t, c) for t, cols in store.CONTENT_COLUMNS.items()
                                  for c in cols.values()})
        db.close()

    def test_diagnostics_can_name_every_content_column(self):
        # A damaged_value event names the table and logical column (P3: never
        # a value); diag keeps only names it knows.
        from workspace import diag
        for table, columns in store.CONTENT_COLUMNS.items():
            self.assertEqual(table, diag._clean("table", table))
            for column in columns:
                self.assertEqual(column, diag._clean("column", column))


# -- the runtime check -------------------------------------------------------------------
# Test only. While a ContentGuard is open, every SQLite connection opened gets
# an authorizer that refuses, unless the statement is store.py's own, to read
# or change a content column or to insert into a table that has one. The
# statement cache is off, so every statement is checked as it is prepared, not
# only its first use. A caller's SQL run through the store's _one() or _all()
# counts as the caller's.

STORE_FILE = os.path.normcase(os.path.abspath(store.__file__))
PASS_THROUGH = {"_one", "_all"}


def _in_store(frame):
    return os.path.normcase(os.path.abspath(frame.f_code.co_filename)) == STORE_FILE


def _author(frame):
    """The frame whose SQL this is: past the store's pass-through helpers."""
    while frame.f_back is not None and _in_store(frame) and frame.f_code.co_name in PASS_THROUGH:
        frame = frame.f_back
    return frame


class ContentGuard:
    def __init__(self):
        self.refused = []

    def __enter__(self):
        real = sqlite3.connect

        def connect(*args, **kwargs):
            kwargs["cached_statements"] = 0
            con = real(*args, **kwargs)
            con.set_authorizer(self._check)
            return con
        self._patch = mock.patch("sqlite3.connect", connect)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()

    def _check(self, action, table, column, _db, _source):
        if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_UPDATE):
            content = column in store.SEALED_COLUMNS.get(table, ())
        elif action == sqlite3.SQLITE_INSERT:
            content = table in store.CONTENT_COLUMNS
        else:
            return sqlite3.SQLITE_OK
        caller = _author(sys._getframe(1))   # sys._getframe(1) ran the statement
        if not content or _in_store(caller):
            return sqlite3.SQLITE_OK
        self.refused.append(f"{Path(caller.f_code.co_filename).name}:{caller.f_lineno} "
                            f"{caller.f_code.co_name}: {table}.{column or '(insert)'}")
        return sqlite3.SQLITE_DENY


class GuardTests(unittest.TestCase):

    def test_the_guard_refuses_content_outside_the_store(self):
        with tempfile.TemporaryDirectory() as tmp, ContentGuard() as guard:
            s = store.Store(Path(tmp) / "w.db", protector=keyprotect.TestProtector())
            try:
                mid = s.create_meeting("synthetic")
                s.add_hold("h1", mid, "all", "synthetic reason", 1.0)
                self.assertEqual("synthetic", s.meeting(mid)["title"])
                self.assertEqual(1, len(s.holds()))
                s._one("SELECT id, reviewed_at FROM meetings")       # plain columns
                s._one("SELECT COUNT(*) FROM holds")
                self.assertEqual([], guard.refused)
                # The store has just run the first of these itself.
                for sql in ("SELECT * FROM meetings WHERE id = ?",
                            "SELECT title_sealed FROM meetings",
                            "SELECT id FROM holds WHERE reason_sealed = ?",
                            "UPDATE meetings SET title_sealed = ? WHERE id = ?",
                            "INSERT INTO holds VALUES (?, ?, ?, ?, ?, ?, ?)"):
                    before = len(guard.refused)
                    with self.subTest(sql=sql), self.assertRaises(store.StoreError):
                        s._all(sql, (mid,) * sql.count("?"))
                    self.assertGreater(len(guard.refused), before, sql)
                before = len(guard.refused)
                with self.assertRaises(store.StoreError):
                    with s.tx() as db:
                        db.execute("INSERT INTO meetings (id, created_at, updated_at, "
                                   "title_sealed) VALUES ('m2', 1, 1, 'x')")
                self.assertGreater(len(guard.refused), before)
                self.assertTrue(all(r.startswith("test_content_access.py:")
                                    for r in guard.refused), guard.refused)
                # A raw read or write by a logical name fails loudly (#111 C2b-2).
                for sql in ("SELECT title FROM meetings", "UPDATE holds SET reason = ?",
                            "SELECT path FROM watched_folders"):
                    with self.subTest(sql=sql), self.assertRaisesRegex(store.StoreError,
                                                                       "no such column"):
                        s._all(sql, ("x",) * sql.count("?"))
            finally:
                s.close()

    def test_a_run_whose_version_is_gone_is_still_ended(self):
        # A damaged database opened with Continue anyway can lose a version row:
        # its running speaker pass is still marked, with no note to bind.
        for ending in ("restart", "failed"):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as tmp:
                s = store.Store(Path(tmp) / "w.db", protector=keyprotect.TestProtector())
                try:
                    mid = s.create_meeting("synthetic")
                    sid = s.add_source(mid, original_path=None, original_sha256="0" * 64,
                                       original_size=1, original_mtime_ns=1, audio_id="0" * 64,
                                       samples=16000, sample_rate=16000)
                    vid = s.ensure_version(mid, sid, engine="e", model="m", model_revision="r",
                                           precision="p", config={},
                                           chunk_plan=[(0, 16000, 0, 16000)])
                    rid = s.start_speaker_run(vid)
                    s.db.execute("PRAGMA foreign_keys=OFF")
                    s.db.execute("DELETE FROM chunks WHERE version_id = ?", (vid,))
                    s.db.execute("DELETE FROM versions WHERE id = ?", (vid,))
                    s.db.execute("PRAGMA foreign_keys=ON")
                    if ending == "restart":
                        s.mark_interrupted()
                        want = "interrupted"
                    else:
                        s.end_speaker_run(rid, "failed", "synthetic failure")
                        want = "failed"
                    row = s._one("SELECT status, error_sealed FROM speaker_runs WHERE id = ?",
                                 (rid,))
                    self.assertEqual((want, None), tuple(row))
                finally:
                    s.close()


class Recording:
    """Wraps the store's codec: records each (table, sealed column) it seals,
    and that every value it seals is text."""

    def __init__(self, codec):
        self.codec = codec
        self.sealed = set()

    def seal(self, meeting, table, column, row, value):
        assert isinstance(value, str), (table, column)
        self.sealed.add((table, column))
        return self.codec.seal(meeting, table, column, row, value)

    def unseal(self, *args):
        return self.codec.unseal(*args)


MARKERS = ("MARKER-TITLE", "MARKER-PATH", "MARKER-PASSAGE", "MARKER-WORD", "MARKER-EDIT",
           "MARKER-ERROR", "MARKER-RUNERR", "MARKER-NAME", "MARKER-REDACT", "MARKER-REASON",
           "MARKER-FOLDER", "MARKER-INFO")


def passage(start, end, text, words=()):
    return types.SimpleNamespace(start=start, end=end, text=text, words=words)


def database_bytes(folder):
    return b"".join(p.read_bytes() for p in Path(folder).iterdir()
                    if p.name.startswith("w.db"))


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class SealedStoreTests(unittest.TestCase):
    """A meeting's whole life through the store and the modules that keep the
    rules (holds, handoff, speaker names, cleanup, intake), with every content
    value sealed on disk by the real codec (#111 C2b-2)."""

    def test_nothing_readable_on_disk_and_everything_right_in_the_app(self):
        import hashlib

        from workspace import cleanup, handoff, holds, intake, speaker_names
        original = hashlib.sha256(b"MARKER synthetic original").hexdigest()
        with tempfile.TemporaryDirectory() as tmp, ContentGuard() as guard:
            s = store.Store(Path(tmp) / "w.db", protector=keyprotect.TestProtector())
            codec = s.codec = Recording(s.codec)
            mid = s.create_meeting("MARKER-TITLE standup")
            sid = s.add_source(mid, original_path=r"C:\MARKER-PATH\a.wav",
                               original_sha256=original, original_size=1, original_mtime_ns=1,
                               audio_id="0" * 64, samples=160000, sample_rate=16000,
                               recording=("rec-1", '{"device": "MARKER-INFO"}'))
            salt, _ = s.intake_salt()
            vid = s.ensure_version(mid, sid, engine="e", model="m", model_revision="r",
                                   precision="p", config={}, chunk_plan=[(0, 160000, 0, 160000)])
            self.assertTrue(s.commit_chunk(vid, 0, [
                passage(0.0, 4.0, "MARKER-PASSAGE one", [(0.0, 0.5, " MARKER-WORD")]),
                passage(4.0, 8.0, "MARKER-PASSAGE two")]))
            first, second = (r["id"] for r in s.transcript(vid))
            s.save_edit(first, "first try")
            s.undo_edit(vid)
            s.save_edit(first, "MARKER-EDIT kept")
            s.save_edit(first, "MARKER-EDIT replaced")   # history: before is the kept edit
            self.assertEqual(first, s.undo_edit(vid))
            s.set_phase(mid, "error", "MARKER-ERROR")
            failed = s.start_speaker_run(vid)
            s.end_speaker_run(failed, "failed", "MARKER-RUNERR")
            stuck = s.start_speaker_run(vid)
            s.mark_interrupted()                       # seals the restart note
            self.assertEqual("interrupted", s.speaker_runs(vid)[-1]["status"])
            self.assertEqual(stuck, s.speaker_runs(vid)[-1]["id"])
            run = s.start_speaker_run(vid)
            self.assertTrue(s.finish_speaker_run(run, [(0.0, 8.0, "SPEAKER_00")],
                                                 [(0.0, 8.0, "SPEAKER_00")]))
            speaker_names.rename(s, vid, "SPEAKER_00", "MARKER-NAME Aroha")
            handoff.set_options(s, mid, [second], ["MARKER-REDACT"])
            hid = holds.add_hold(s, mid, "all", "MARKER-REASON for keeping")
            s.add_folder("f1", r"C:\MARKER-FOLDER", False)
            intake.set_delete_originals(s, "f1", True)
            s.set_source_intake(sid, "f1", may_delete=True)
            cleanup.confirm_transcript_reviewed(s, mid)

            # Everything the app reads comes back as written.
            self.assertEqual("MARKER-TITLE standup", s.meeting(mid)["title"])
            self.assertEqual(["MARKER-TITLE standup"], [m["title"] for m in s.meetings()])
            self.assertEqual(r"C:\MARKER-PATH\a.wav", s.source(sid)["original_path"])
            rows = s.transcript(vid)
            self.assertEqual(["MARKER-EDIT kept", "MARKER-PASSAGE two"], [r["text"] for r in rows])
            self.assertEqual("MARKER-PASSAGE one", rows[0]["generated"])
            self.assertIn("MARKER-WORD", rows[0]["words_json"])
            self.assertEqual({"id": first, "text": "MARKER-EDIT kept", "edited": 1},
                             {k: s.passage(first)[k] for k in ("id", "text", "edited")})
            runs = s.speaker_runs(vid)
            self.assertEqual("MARKER-RUNERR", runs[0]["error"])
            self.assertEqual(store.SPEAKER_RESTART_NOTE, runs[1]["error"])
            self.assertEqual(["MARKER-NAME Aroha"],
                             list(speaker_names.resolve(s, vid)["names"].values()))
            self.assertEqual([second], handoff.options(s, mid)["omitted"])
            self.assertEqual(["MARKER-REDACT"], handoff.options(s, mid)["redactions"])
            self.assertEqual("MARKER-REASON for keeping",
                             holds.active_holds(s, mid)[0]["reason"])
            self.assertEqual(hid, holds.active_holds(s)[0]["id"])
            self.assertEqual("MARKER-TITLE standup", holds.unfinished_work(s)[0]["title"])
            self.assertEqual([r"C:\MARKER-FOLDER"], [f["path"] for f in intake.folders(s)])
            snapshot = s.snapshot(mid)
            self.assertIn("MARKER-NAME Aroha: MARKER-EDIT kept", snapshot)
            self.assertEqual(snapshot, cleanup.render_reviewed(s, vid))
            self.assertEqual([r"C:\MARKER-FOLDER"], intake.deletion_roots(s, mid))
            self.assertEqual('{"device": "MARKER-INFO"}', s.recording_info(mid))
            self.assertTrue(s.recording_imported("rec-1"))
            self.assertEqual((salt, False), s.intake_salt())
            self.assertTrue(s.same_fingerprint("original", original, s.source(sid)["original_fp"]))
            s.close()
            self.assertEqual([], guard.refused)

            # Every content column was written through the codec, and none of
            # it is readable in the database file or its write-ahead log: not
            # the plain SHA-256 of the original or the intake salt either.
            want = {(t, c) for t, cols in store.CONTENT_COLUMNS.items() for c in cols.values()}
            self.assertEqual(set(), want - codec.sealed)
            raw = database_bytes(tmp)
            for marker in (*MARKERS, original, salt.decode()):
                with self.subTest(marker=marker):
                    self.assertFalse(marker.encode() in raw, "readable on disk")
            # What was read back above came from the file: a new store opens it.
            s = store.Store(Path(tmp) / "w.db", protector=keyprotect.TestProtector())
            try:
                self.assertEqual("MARKER-TITLE standup", s.meeting(mid)["title"])
            finally:
                s.close()


def write_wav(path, seconds):
    import numpy as np
    t = np.arange(int(seconds * 16000)) / 16000
    audio = (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(audio.tobytes())
    return path


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class SealedWorkspaceTests(unittest.TestCase):
    """The same through the app itself, with fake engines: import, transcribe,
    speakers, rename, edit, hold, handoff options and review."""

    def test_the_app_shows_content_that_is_sealed_on_disk(self):
        from workspace.app import Workspace

        def fake(name):
            return f"test:fake_worker_engines:{name}"
        with tempfile.TemporaryDirectory() as tmp, ContentGuard() as guard:
            root = Path(tmp)
            rec = write_wav(root / "MARKER-TITLE synthetic.wav", 30)
            ws = Workspace(root / "ws", root / "models", start=False, allow_test_engines=True,
                           speaker_model=fake("fake_diarizer"), battery=False,
                           protector=keyprotect.TestProtector())
            try:
                ws.settings.update(model=fake("sentences"), speakers=True)
                ws.start()
                self.assertEqual(1, ws.import_paths([str(rec)])["accepted"])
                self.assertTrue(ws.wait_idle(120))
                [m] = ws.meetings()
                mid = m["id"]
                self.assertEqual("MARKER-TITLE synthetic", m["title"])
                ws.rename(mid, "MARKER-TITLE renamed")
                rows = ws.transcript(mid)["items"]
                ws.edit(mid, rows[0]["id"], "MARKER-EDIT by hand")
                hold = ws.add_hold(mid, "all", "MARKER-REASON client asked")
                self.assertEqual("MARKER-TITLE renamed", ws.meetings()[0]["title"])
                self.assertEqual("MARKER-EDIT by hand", ws.transcript(mid)["items"][0]["text"])
                detail = ws.detail(mid)
                self.assertEqual("MARKER-TITLE renamed", detail["title"])
                self.assertIn("MARKER-REASON client asked",
                              json.dumps(detail, ensure_ascii=False))
                ws.release_hold(hold)
            finally:
                ws.close()
            self.assertEqual([], guard.refused)
            raw = b"".join(p.read_bytes() for p in (root / "ws").iterdir()
                           if p.name.startswith("workspace.db"))
            for marker in ("MARKER-TITLE", "MARKER-EDIT", "MARKER-REASON"):
                with self.subTest(marker=marker):
                    self.assertFalse(marker.encode() in raw, "readable on disk")


if __name__ == "__main__":
    unittest.main()
