# Meeting content is read and written only through the Store's codec (#111
# C2a; docs/ENCRYPTION.md), so C2b can seal every content column in one place.
#   - Static: no SQL in workspace/ outside store.py, and none in tools/ but
#     the known sites C2b moves (TOOLS_RAW_SQL), names a content column
#     (store.CONTENT_COLUMNS), selects or returns * with a table that has one,
#     inserts into one without naming its columns, or is built at run time
#     around one. Conditions given to the store's meetings(), holds() and
#     folders() name plain columns only (store.plain_condition).
#   - With a codec that changes every value, a whole meeting's life leaves no
#     content readable in the database file, and everything the app shows is
#     still right: any read or write that went around the codec would fail.
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

from workspace import store  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))

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
# unquoted, and a table counts as used wherever its name appears (comma joins
# and subqueries included). Names are followed in line order within one
# function, so of a name assigned on two branches the later text counts; the
# expression is also checked with each name as a piece known only at run time
# (so the earlier text is not lost, at the cost of flagging a run-time build
# around a content table even where the name's text is plain).
# It cannot see SQL passed between functions, held in attributes, read from
# data or joined with str.join: the authorizer below covers the paths the
# scrambling tests run.

HOLE = " \x00 "
LOOKS_LIKE_SQL = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|REPLACE)\b"
    r"|(?i:\bselect\b[\s\S]*\bfrom\b|\binsert\s+(?:or\s+\w+\s+)?into\b|\breplace\s+into\b"
    r"|\bupdate\s+\S+\s+set\b|\bdelete\s+from\b)")
NAMES_CONTENT_TABLE = re.compile(
    r"\b(?:from|join|into|update)\s+(?:" + "|".join(store.CONTENT_COLUMNS) + r")\b")
QUOTED = re.compile(r'"(\w+)"|\[(\w+)\]|`(\w+)`')
QUOTED_HOLE = re.compile(r"[\"'`\[]\s*\x00\s*[\"'`\]]")
QUOTED_TABLE = re.compile(r"\b(from|join|into|update|table)\s+'(\w+)'")
SCHEMA_NAME = re.compile(r"\b(?:main|temp)\s*\.\s*")
FORMAT_SLOT = re.compile(r"%\(\w+\)s|%s|\{\w*\}")
TABLE_SLOT = re.compile(r"\b(?:from|join|into|update|table)\s+\x00")
STAR = re.compile(r"(?:\bselect\s+(?:distinct\s+|all\s+)?|,\s*|\.\s*|\breturning\s+)\*")
LIST_SLOT = re.compile(r"\bin\s*\(\s*\x00\s*\)")
POSITIONAL = (r"\b(?:insert|replace)\s+(?:or\s+\w+\s+)?into\s+{}\s*(?:as\s+\w+\s*)?"
              r"(?:values|select|default)\b")
RUN_TIME = "<run-time table>"


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
    """Whether a text has a statement's shape or names a content table."""
    return bool(LOOKS_LIKE_SQL.search(text) or NAMES_CONTENT_TABLE.search(_normal(text)))


def _normal(text):
    """Lower case; names unquoted (a quoted slot as HOLE) and unqualified; %s
    and {} slots as HOLE."""
    text = FORMAT_SLOT.sub(HOLE, text).lower()
    text = QUOTED_HOLE.sub(HOLE, text)
    text = QUOTED.sub(lambda m: m.group(m.lastindex), text)
    text = QUOTED_TABLE.sub(r"\1 \2", text)
    return SCHEMA_NAME.sub("", text)


def content_problems(text):
    """What a SQL text does with content: names a content column of a table it
    uses, selects or returns * with one, inserts into one without naming the
    columns, or is built at run time around one (a slot other than "IN (?)").
    A table named only at run time is taken to be any table with content."""
    t = _normal(text)
    tables = [name for name in store.CONTENT_COLUMNS if re.search(rf"\b{name}\b", t)]
    dynamic = bool(TABLE_SLOT.search(t))
    if not tables and not dynamic:
        return []
    out = [f"{table}.{column}" for table in tables for column in store.CONTENT_COLUMNS[table]
           if re.search(rf"\b{column}\b", t)]
    if dynamic:
        out += sorted({f"{RUN_TIME}.{column}" for columns in store.CONTENT_COLUMNS.values()
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


def tool_modules():
    return sorted((ROOT / "tools").rglob("*.py"))


# Tools that still read or write content columns directly (with the plain
# codec that works). C2b moves them onto the store, recorded in #111. The test
# keeps this list exact: a new site fails, and so does one that has gone.
TOOLS_RAW_SQL = {
    ("tools/packaged_crash_check.py", "<module>:script.inspect", "meetings.last_error"),
    ("tools/packaged_crash_check.py", "<module>:script.inspect", "segments.text"),
    ("tools/packaged_crash_check.py", "<module>:script.inspect", "edits.text"),
    ("tools/packaged_crash_check.py", "<module>:script.inspect", "built at run time with edits"),
    ("tools/workspace_ui_check.py", "main", "segments.text"),
}


def problems_in_source(source):
    return [p for _line, _scope, text in sql_in_source(source) for p in content_problems(text)]


class StaticTests(unittest.TestCase):

    def test_only_the_store_names_content_columns(self):
        self.assertEqual(set(), problems_in(workspace_modules()))

    def test_tools_with_raw_content_sql_are_exactly_the_known_ones(self):
        self.assertEqual(TOOLS_RAW_SQL, problems_in(tool_modules()))

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
                      "id = ?; DROP TABLE meetings", "id = ? -- the rest", "id = ? /* x */",
                      "id IN (SELECT meeting_id FROM holds WHERE reason = ?)", "[path] = ?",
                      "`reason` = ?", "(?, ?, ?, ?, ?, ?, ?) IN (SELECT * FROM holds)",
                      "id IN (SELECT meeting_id FROM versions)",
                      "id IN (select meeting_id from versions)", None):
            with self.subTest(where=where), self.assertRaises(ValueError):
                store.plain_condition(where)
        with tempfile.TemporaryDirectory() as tmp:
            s = store.Store(Path(tmp) / "w.db")
            try:
                s.create_meeting("synthetic")
                for call, where in ((s.meetings, "title LIKE ?"), (s.holds, "reason = ?"),
                                    (s.folders, "path = ?")):
                    with self.subTest(call=call.__name__), self.assertRaises(ValueError):
                        call(where, ("x",))
                self.assertEqual(1, len(s.meetings("phase != ?", ("error",))))
            finally:
                s.close()

    def test_the_content_columns_are_the_schemas(self):
        db = sqlite3.connect(":memory:")
        db.executescript(store.SCHEMA)
        for table, columns in store.CONTENT_COLUMNS.items():
            have = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            with self.subTest(table=table):
                self.assertTrue(set(columns) <= have, (table, columns, have))
        db.close()


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
            content = column in store.CONTENT_COLUMNS.get(table, ())
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
            s = store.Store(Path(tmp) / "w.db")
            try:
                mid = s.create_meeting("synthetic")
                s.add_hold("h1", mid, "all", "synthetic reason", 1.0)
                self.assertEqual("synthetic", s.meeting(mid)["title"])
                self.assertEqual(1, len(s.holds()))
                s._one("SELECT id, reviewed_at FROM meetings")       # plain columns
                s._one("SELECT COUNT(*) FROM holds")
                self.assertEqual([], guard.refused)
                # The store has just run the first of these itself.
                for sql in ("SELECT * FROM meetings WHERE id = ?", "SELECT title FROM meetings",
                            "SELECT id FROM holds WHERE reason = ?",
                            "UPDATE meetings SET title = ? WHERE id = ?",
                            "INSERT INTO holds VALUES (?, ?, ?, ?, ?, ?, ?)"):
                    before = len(guard.refused)
                    with self.subTest(sql=sql), self.assertRaises(store.StoreError):
                        s._all(sql, (mid,) * sql.count("?"))
                    self.assertGreater(len(guard.refused), before, sql)
                before = len(guard.refused)
                with self.assertRaises(store.StoreError):
                    with s.tx() as db:
                        db.execute("INSERT INTO meetings (id, created_at, updated_at, title) "
                                   "VALUES ('m2', 1, 1, 'x')")
                self.assertGreater(len(guard.refused), before)
                self.assertTrue(all(r.startswith("test_content_access.py:")
                                    for r in guard.refused), guard.refused)
            finally:
                s.close()

    def test_a_run_whose_version_is_gone_is_still_ended(self):
        # A damaged database opened with Continue anyway can lose a version row:
        # its running speaker pass is still marked, with no note to bind.
        for ending in ("restart", "failed"):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as tmp:
                s = store.Store(Path(tmp) / "w.db", codec=Scrambling())
                try:
                    mid = s.create_meeting("synthetic")
                    sid = s.add_source(mid, original_path=None, original_sha256="0",
                                       original_size=1, original_mtime_ns=1, audio_sha256="0",
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
                    row = s._one("SELECT status, error FROM speaker_runs WHERE id = ?", (rid,))
                    self.assertEqual((want, None), tuple(row))
                finally:
                    s.close()


class Scrambling:
    """A codec that changes every value it stores and checks each value it
    opens came from it, for the same place."""

    def __init__(self):
        self.sealed = set()

    @staticmethod
    def _tag(meeting, table, column, row):
        return f"{meeting}|{table}|{column}|{row}"

    def seal(self, meeting, table, column, row, value):
        assert isinstance(value, str), (table, column)
        self.sealed.add((table, column))
        return "~" + self._tag(meeting, table, column, row) + "~" + value[::-1]

    def unseal(self, meeting, table, column, row, value):
        tag = "~" + self._tag(meeting, table, column, row) + "~"
        assert value.startswith(tag), (table, column, "opened in the wrong place")
        return value[len(tag):][::-1]


MARKERS = ("MARKER-TITLE", "MARKER-PATH", "MARKER-PASSAGE", "MARKER-WORD", "MARKER-EDIT",
           "MARKER-ERROR", "MARKER-RUNERR", "MARKER-NAME", "MARKER-REDACT", "MARKER-REASON",
           "MARKER-FOLDER")


def passage(start, end, text, words=()):
    return types.SimpleNamespace(start=start, end=end, text=text, words=words)


def database_bytes(folder):
    return b"".join(p.read_bytes() for p in Path(folder).iterdir()
                    if p.name.startswith("w.db"))


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class ScramblingStoreTests(unittest.TestCase):
    """A meeting's whole life through the store and the modules that keep the
    rules (holds, handoff, speaker names, cleanup, intake), with every content
    value scrambled on disk."""

    def test_nothing_readable_on_disk_and_everything_right_in_the_app(self):
        from workspace import cleanup, handoff, holds, intake, speaker_names
        codec = Scrambling()
        with tempfile.TemporaryDirectory() as tmp, ContentGuard() as guard:
            s = store.Store(Path(tmp) / "w.db", codec=codec)
            mid = s.create_meeting("MARKER-TITLE standup")
            sid = s.add_source(mid, original_path=r"C:\MARKER-PATH\a.wav", original_sha256="0",
                               original_size=1, original_mtime_ns=1, audio_sha256="0",
                               samples=160000, sample_rate=16000)
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
            s.close()
            self.assertEqual([], guard.refused)

            # Every content column was written through the codec, and none of
            # it is readable in the database file or its write-ahead log.
            want = {(t, c) for t, cols in store.CONTENT_COLUMNS.items() for c in cols}
            self.assertEqual(set(), want - codec.sealed)
            raw = database_bytes(tmp)
            for marker in MARKERS:
                with self.subTest(marker=marker):
                    self.assertFalse(marker.encode() in raw, "readable on disk")
                    self.assertTrue(marker[::-1].encode() in raw, "not stored at all")


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
class ScramblingWorkspaceTests(unittest.TestCase):
    """The same through the app itself, with fake engines: import, transcribe,
    speakers, rename, edit, hold, handoff options and review."""

    def test_the_app_shows_content_that_is_scrambled_on_disk(self):
        from workspace.app import Workspace

        def fake(name):
            return f"test:fake_worker_engines:{name}"
        with tempfile.TemporaryDirectory() as tmp, ContentGuard() as guard:
            root = Path(tmp)
            rec = write_wav(root / "MARKER-TITLE synthetic.wav", 30)
            ws = Workspace(root / "ws", root / "models", start=False, allow_test_engines=True,
                           speaker_model=fake("fake_diarizer"), battery=False,
                           codec=Scrambling())
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
