# Meeting content is read and written only through the Store's codec (#111
# C2a; docs/ENCRYPTION.md), so C2b can seal every content column in one place.
#   - No module but store.py names a content column (store.CONTENT_COLUMNS) in
#     its SQL, or selects * from a table that has one.
#   - With a codec that changes every value, a whole meeting's life leaves no
#     content readable in the database file, and everything the app shows is
#     still right: any read or write that went around the codec would fail.
# Synthetic text only.
#   python -m unittest discover -s tests -v

import ast
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace import store  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))

SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b")
TABLE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)")
STAR = re.compile(r"\bSELECT\s+(?:DISTINCT\s+)?(?:\w+\.)?\*")


def sql_in(path):
    """(line, text) of every string in a module that looks like SQL; the
    parts of an f-string between its placeholders are joined."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = " x ".join(v.value for v in node.values if isinstance(v, ast.Constant))
        else:
            continue
        if SQL.search(text):
            yield node.lineno, text


def content_problems(text):
    """What a SQL string does with content columns: names one, or selects *
    from a table that has one."""
    out = []
    for table in TABLE.findall(text):
        columns = store.CONTENT_COLUMNS.get(table, ())
        if not columns:
            continue
        if STAR.search(text):
            out.append(f"SELECT * with {table}")
        out += [f"{table}.{c}" for c in columns if re.search(rf"\b{c}\b", text)]
    return out


class StaticTests(unittest.TestCase):

    def test_only_the_store_names_content_columns(self):
        found = []
        for path in sorted((ROOT / "workspace").glob("*.py")):
            if path.name == "store.py":
                continue
            for line, text in sql_in(path):
                for problem in content_problems(text):
                    found.append(f"{path.name}:{line}: {problem}")
        self.assertEqual([], found)

    def test_the_check_sees_content_sql(self):
        caught = ["SELECT * FROM meetings", "SELECT m.* FROM meetings m",
                  "SELECT id, title FROM meetings", "UPDATE meetings SET title = ?",
                  "SELECT s.id FROM segments s JOIN edits e ON e.segment_id = s.id "
                  "WHERE e.text IS NULL", "SELECT wf.path FROM sources s JOIN watched_folders wf",
                  "INSERT INTO holds (id, reason) VALUES (?, ?)"]
        for text in caught:
            with self.subTest(text=text):
                self.assertTrue(content_problems(text))
        fine = ["SELECT COUNT(*) FROM meetings", "SELECT id, reviewed_at FROM meetings",
                "DELETE FROM segments WHERE version_id = ?",
                "UPDATE cleanup_jobs SET error = ? WHERE id = ?",
                "UPDATE watched_folders SET last_error = ? WHERE id = ?"]
        for text in fine:
            with self.subTest(text=text):
                self.assertEqual([], content_problems(text))

    def test_the_content_columns_are_the_schemas(self):
        db = __import__("sqlite3").connect(":memory:")
        db.executescript(store.SCHEMA)
        for table, columns in store.CONTENT_COLUMNS.items():
            have = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            with self.subTest(table=table):
                self.assertTrue(set(columns) <= have, (table, columns, have))


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
        with tempfile.TemporaryDirectory() as tmp:
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
            s.close()

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
        with tempfile.TemporaryDirectory() as tmp:
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
            raw = b"".join(p.read_bytes() for p in (root / "ws").iterdir()
                           if p.name.startswith("workspace.db"))
            for marker in ("MARKER-TITLE", "MARKER-EDIT", "MARKER-REASON"):
                with self.subTest(marker=marker):
                    self.assertFalse(marker.encode() in raw, "readable on disk")


if __name__ == "__main__":
    unittest.main()
