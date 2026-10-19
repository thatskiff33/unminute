# The store's state accessors, transcript paging and deletions (#111 C2b-1).
#   - With a codec that counts what it opens, state checks open nothing, a
#     transcript page opens only its own passages, search opens each passage's
#     text once, and nothing that needs only the text opens the word timings.
#     C2b seals every value, so each one opened costs a decryption.
#   - Paging in SQL returns exactly what slicing the whole transcript did, and
#     search returns the same passages as before, by id.
#   - Every deletion of a meeting's rows goes through the store: an active hold
#     refuses it, and a failed import leaves nothing behind.
#   - tools/packaged_crash_check.py's inspect step reads through the store, and
#     tools/workspace_ui_check.py's long meeting is never transcribed by the
#     processing loop before its passages are committed through the store.
#   - That tool's failed meeting (#244) stops because its model is not ready
#     without anything downloaded or loaded, and moves to a ready model; its
#     verdict on the failed-meeting report names each thing that went wrong.
# Synthetic text only.
#   python tools/run_tests.py

import ast
import collections
import hashlib
import importlib.util
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
SR = 16000
N = 5000

if HAVE_DEPS:
    from workspace import cleanup, handoff, holds, runner, speaker_names, store
    from workspace.store import Store, StoreError


class Counting:
    """A plain codec that counts the values it opens, by (table, column)."""

    def __init__(self):
        self.opened = collections.Counter()

    def seal(self, meeting, table, column, row, value):
        return value

    def unseal(self, meeting, table, column, row, value):
        self.opened[(table, column)] += 1
        return value


def passage(start, end, text, words=()):
    return types.SimpleNamespace(start=start, end=end, text=text, words=words)


def long_meeting(s, n=N, title="synthetic standup"):
    """A meeting with n passages committed through the store: written latest
    first (ids run against time) with two passages sharing a start time, and
    an edit on every seventh passage. Returns (mid, vid)."""
    mid = s.create_meeting(title)
    total = int((n + 1) * 3.0 * SR)
    sid = s.add_source(mid, original_path=None, original_sha256="a" * 64, original_size=1,
                       original_mtime_ns=0, audio_sha256="b" * 64, samples=total,
                       sample_rate=SR)
    vid = s.ensure_version(mid, sid, engine="fake", model="fake", model_revision="1",
                           precision="int8", config={}, chunk_plan=[(0, total, 0, total)])
    rows = [passage(i * 3.0, i * 3.0 + 2.5, f"passage {i} about the Budget",
                    ((i * 3.0, i * 3.0 + 0.4, " passage"),)) for i in range(n)]
    rows.append(passage(6.0, 6.5, "a second passage at the same time"))
    assert s.commit_chunk(vid, 0, list(reversed(rows)))
    for r in s.transcript(vid)[::7]:
        s.save_edit(r["id"], r["text"].replace("Budget", "BUDGET, edited"))
    return mid, vid


def write_wav(path, seconds):
    import numpy as np
    t = np.arange(int(seconds * SR)) / SR
    audio = (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(audio.tobytes())
    return path


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class StoreAccessorTests(unittest.TestCase):
    """Paging, search and run lookups give the same results as before."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.tmp.name) / "w.db")
        cls.mid, cls.vid = long_meeting(cls.store)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_a_page_is_the_same_slice_of_the_transcript(self):
        s, vid = self.store, self.vid
        whole = s.transcript(vid)
        self.assertEqual(N + 1, len(whole))
        self.assertEqual(N + 1, s.passage_count(vid))
        self.assertEqual([r["id"] for r in whole], s.passage_ids(vid))
        # Time order, then id: the two passages starting at 6.0 s.
        tied = [r for r in whole if r["start_s"] == 6.0]
        self.assertEqual(2, len(tied))
        self.assertLess(tied[0]["id"], tied[1]["id"])
        for start, count in ((0, 100), (4000, 1000), (N - 10, 100), (N + 1, 10),
                             (N + 500, 5), (0, 0), (123, 1), (0, N + 1)):
            with self.subTest(start=start, count=count):
                self.assertEqual(whole[start:start + count],
                                 s.transcript_page(vid, start, count))
                text_only = [{**r, "words_json": None} for r in whole[start:start + count]]
                self.assertEqual(text_only, s.transcript_page(vid, start, count, words=False))
        self.assertEqual([{**r, "words_json": None} for r in whole],
                         s.transcript(vid, words=False))

    def test_search_finds_the_same_passages_by_id(self):
        s, vid = self.store, self.vid
        whole = s.transcript(vid)
        for query, limit in (("budget", 500), ("BUDGET, EDITED", 500), ("passage 4999", 500),
                             ("same time", 500), ("budget", 3), ("no such words", 500),
                             ("budget", 0)):
            with self.subTest(query=query, limit=limit):
                # What app.search returned before: positions mapped to ids.
                needle = query.casefold()
                old = [i for i, r in enumerate(whole) if needle in r["text"].casefold()][:limit]
                self.assertEqual([whole[i]["id"] for i in old], s.search(vid, query, limit))

    def test_the_current_run_is_the_latest_finished_one(self):
        s, vid = self.store, self.vid
        self.assertIsNone(s.current_run(vid))
        runs = []
        for i in range(2):
            rid = s.start_speaker_run(vid)
            s.end_speaker_run(rid, "failed", f"synthetic failure {i}")
            runs.append(rid)
            done = s.start_speaker_run(vid)
            self.assertTrue(s.finish_speaker_run(done, [(0.0, 9.0, "SPEAKER_00")],
                                                 [(0.0, 9.0, "SPEAKER_00")]))
        last_failed = s.start_speaker_run(vid)
        s.end_speaker_run(last_failed, "failed", "synthetic failure after")
        old = [r for r in s.speaker_runs(vid) if r["status"] == "done"][-1]
        self.assertEqual(done, old["id"])
        self.assertEqual(old, s.current_run(vid))

    def test_the_intake_salt_is_made_once(self):
        first = self.store.intake_salt()
        self.assertIsInstance(first, bytes)
        self.assertEqual(32, len(first))
        self.assertEqual(first, self.store.intake_salt())
        self.assertEqual(1, self.store._one("SELECT COUNT(*) FROM intake_meta")[0])


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class OpenedValueTests(unittest.TestCase):
    """What each path opens, counted with a codec (each would be a decryption
    once C2b seals the values)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.codec = Counting()
        self.store = Store(Path(self.tmp.name) / "w.db", codec=self.codec)
        self.mid, self.vid = long_meeting(self.store)
        self.codec.opened.clear()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def opened(self, fn, *args, **kwargs):
        self.codec.opened.clear()
        fn(*args, **kwargs)
        return dict(self.codec.opened)

    def test_a_page_opens_only_its_own_passages(self):
        got = self.opened(self.store.transcript_page, self.vid, 2500, 100, words=False)
        self.assertLessEqual(got.get(("segments", "text"), 0), 100)
        self.assertLessEqual(got.get(("edits", "text"), 0), 100)
        self.assertNotIn(("segments", "words_json"), got)

    def test_search_opens_each_passage_text_once_and_no_words(self):
        got = self.opened(self.store.search, self.vid, "no such words")   # a full pass
        self.assertEqual(N + 1, got.get(("segments", "text"), 0) + got.get(("edits", "text"), 0))
        self.assertGreater(got.get(("edits", "text"), 0), 0)       # the edit, where there is one
        self.assertNotIn(("segments", "words_json"), got)

    def test_the_reviewed_text_and_the_handoff_open_no_words(self):
        got = self.opened(cleanup.render_reviewed, self.store, self.vid)
        self.assertNotIn(("segments", "words_json"), got)
        with self.store.tx() as db:      # reviewed: a plain column
            db.execute("UPDATE meetings SET reviewed_at = 1 WHERE id = ?", (self.mid,))
        template = {"id": "synthetic", "title": "Synthetic", "prompt": "Summarise.",
                    "version": 1}
        got = self.opened(handoff.build, self.store, self.mid, template)
        self.assertNotIn(("segments", "words_json"), got)
        self.assertGreater(got.get(("segments", "text"), 0), 0)      # it did read the text

    def test_state_checks_open_no_title_or_passage(self):
        ids = self.store.passage_ids(self.vid)
        for name, call in (
                ("meeting_state", lambda: self.store.meeting_state(self.mid)),
                ("meetings(content=False)", lambda: self.store.meetings(content=False)),
                ("passage_ids", lambda: self.store.passage_ids(self.vid)),
                ("handoff.set_options",
                 lambda: handoff.set_options(self.store, self.mid, ids[:3], ["synthetic"])),
                ("holds.add_hold",
                 lambda: holds.add_hold(self.store, self.mid, "audio", "synthetic reason"))):
            with self.subTest(call=name):
                got = self.opened(call)
                self.assertNotIn(("meetings", "title"), got)
                self.assertNotIn(("segments", "text"), got)
                self.assertNotIn(("edits", "text"), got)
        state = self.store.meeting_state(self.mid)
        self.assertEqual(set(store.MEETING_STATE), set(state))
        self.assertEqual(state, {k: v for k, v in self.store.meeting(self.mid).items()
                                 if k in store.MEETING_STATE})
        self.assertIsNone(self.store.meeting_state("0" * 32))
        with self.assertRaises(ValueError):
            self.store.meetings(content=("phase",))      # not a content column

    def test_the_current_run_opens_one_error_at_most(self):
        for i in range(3):
            rid = self.store.start_speaker_run(self.vid)
            self.store.end_speaker_run(rid, "failed", f"synthetic failure {i}")
        done = self.store.start_speaker_run(self.vid)
        self.store.finish_speaker_run(done, [(0.0, 9.0, "SPEAKER_00")],
                                      [(0.0, 9.0, "SPEAKER_00")])
        got = self.opened(self.store.current_run, self.vid)
        self.assertLessEqual(got.get(("speaker_runs", "error"), 0), 1)

    def test_the_runner_opens_no_title_per_chunk(self):
        mid = self.store.create_meeting("synthetic short")
        total = 4 * SR * 10
        sid = self.store.add_source(mid, original_path=None, original_sha256="a" * 64,
                                    original_size=1, original_mtime_ns=0,
                                    audio_sha256="b" * 64, samples=total, sample_rate=SR)
        plan = [(i * 10 * SR, (i + 1) * 10 * SR, i * 10 * SR, (i + 1) * 10 * SR)
                for i in range(4)]
        vid = self.store.ensure_version(mid, sid, engine="fake", model="fake",
                                        model_revision="1", precision="int8", config={},
                                        chunk_plan=plan)

        class Reader:
            def __len__(self):
                return total

            def read(self, start, end):
                import numpy as np
                return np.zeros(end - start, dtype=np.float32)

        class Engine:
            device, precision = "cpu", "int8"

            def transcribe(self, audio):
                from asr_engines import Segment
                return [Segment(1.0, 2.0, "synthetic words", ())], len(audio) / SR

        got = self.opened(runner.run_version, self.store, mid, vid, Reader(), Engine())
        self.assertEqual(4, len(self.store.transcript(vid)))
        self.assertNotIn(("meetings", "title"), got)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class AppOpenedValueTests(unittest.TestCase):
    """The same through the app: the Meetings list, a page and search."""

    def setUp(self):
        from workspace.app import Workspace
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.codec = Counting()
        self.ws = Workspace(root / "ws", root / "models", start=False, battery=False,
                            allow_test_engines=True, codec=self.codec)

    def tearDown(self):
        self.ws.close()
        self.tmp.cleanup()

    def opened(self, fn, *args):
        self.codec.opened.clear()
        out = fn(*args)
        return out, dict(self.codec.opened)

    def test_the_meetings_list_opens_each_title_once(self):
        for i in range(5):
            self.ws.store.create_meeting(f"synthetic {i}")
        rows, got = self.opened(self.ws.meetings)
        self.assertEqual(5, len(rows))
        self.assertEqual(5, got.get(("meetings", "title"), 0))
        _, got = self.opened(self.ws.reminders)
        self.assertLessEqual(got.get(("meetings", "title"), 0), 5)

    def test_a_page_and_search_through_the_app(self):
        mid, vid = long_meeting(self.ws.store)
        whole = self.ws.store.transcript(vid)
        page, got = self.opened(self.ws.transcript, mid, 0, 100)
        self.assertEqual(N + 1, page["total"])
        self.assertEqual([r["id"] for r in whole[:100]], [r["id"] for r in page["items"]])
        self.assertEqual([r["text"] for r in whole[:100]], [r["text"] for r in page["items"]])
        self.assertLessEqual(got.get(("segments", "text"), 0), 100)
        self.assertNotIn(("segments", "words_json"), got)
        self.assertNotIn(("meetings", "title"), got)
        page, _ = self.opened(self.ws.transcript, mid, N + 50, 10)
        self.assertEqual((N + 1, []), (page["total"], page["items"]))
        hits, got = self.opened(self.ws.search, mid, "passage 4999")
        self.assertEqual([r["id"] for r in whole if "passage 4999" in r["text"]], hits)
        self.assertEqual(N + 1, got.get(("segments", "text"), 0) + got.get(("edits", "text"), 0))
        self.assertNotIn(("segments", "words_json"), got)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class DeletionTests(unittest.TestCase):
    """Deleting a meeting's rows: only the store does it, and holds win."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "w.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def rows_of(self, mid):
        """Rows left for a meeting in every table that refers to one."""
        q = self.store._one
        out = {t: q(f"SELECT COUNT(*) FROM {t} WHERE meeting_id = ?", (mid,))[0]
               for t in ("sources", "versions", "reviewed_snapshots", "handoff_options",
                         "holds", "cleanup_jobs")}
        out["meetings"] = q("SELECT COUNT(*) FROM meetings WHERE id = ?", (mid,))[0]
        vids = "SELECT id FROM versions WHERE meeting_id = ?"
        for t in ("chunks", "attempts", "segments", "speaker_runs"):
            out[t] = q(f"SELECT COUNT(*) FROM {t} WHERE version_id IN ({vids})", (mid,))[0]
        return out

    def full_meeting(self, title):
        mid, vid = long_meeting(self.store, n=20, title=title)
        self.store.start_attempt(vid, 0)
        rid = self.store.start_speaker_run(vid)
        self.store.finish_speaker_run(rid, [(0.0, 60.0, "SPEAKER_00")],
                                      [(0.0, 60.0, "SPEAKER_00")])
        speaker_names.rename(self.store, vid, "SPEAKER_00", "Synthetic Name")
        handoff.set_options(self.store, mid, self.store.passage_ids(vid)[:2], ["synthetic"])
        cleanup.confirm_transcript_reviewed(self.store, mid)
        hid = holds.add_hold(self.store, mid, "audio", "synthetic reason")
        holds.release_hold(self.store, hid)
        return mid

    def test_all_of_a_meeting_and_nothing_else_is_deleted(self):
        mid, other = self.full_meeting("synthetic one"), self.full_meeting("synthetic two")
        before = self.rows_of(other)
        segments = self.store._one("SELECT COUNT(*) FROM segments")[0]
        with self.store.tx() as db:
            self.store.delete_meeting_content(db, mid)
        self.assertEqual({t: 0 for t in before if t != "cleanup_jobs"},
                         {t: n for t, n in self.rows_of(mid).items() if t != "cleanup_jobs"})
        self.assertEqual(before, self.rows_of(other))
        self.assertEqual(segments - 21, self.store._one("SELECT COUNT(*) FROM segments")[0])
        for table in ("edits", "edit_history", "speaker_corrections", "segment_speakers",
                      "speaker_turns"):
            with self.subTest(table=table):
                self.assertGreater(self.store._one(f"SELECT COUNT(*) FROM {table}")[0], 0)

    def test_an_active_hold_refuses_every_deletion_of_the_meeting(self):
        # Invariant C2: each caller checks holds first; the store refuses too.
        mid = self.full_meeting("synthetic held")
        holds.add_hold(self.store, mid, "audio", "synthetic reason")
        before = self.rows_of(mid)
        for name, delete in (("content", self.store.delete_meeting_content),
                             ("row", self.store.delete_meeting_row)):
            with self.subTest(delete=name), self.assertRaises(StoreError):
                with self.store.tx() as db:
                    delete(db, mid)
            self.assertEqual(before, self.rows_of(mid))
        new = self.store.create_meeting("synthetic new, held")
        holds.add_hold(self.store, new, "all", "synthetic reason")
        with self.assertRaises(StoreError):
            self.store.delete_new_meeting(new)
        self.assertIsNotNone(self.store.meeting_state(new))

    def test_a_failed_import_with_a_released_hold_is_kept(self):
        # #193, invariant C2: releasing a hold deletes nothing. A failed
        # import (media.import_recording -> delete_new_meeting) keeps a
        # meeting that has ever had a hold, as discard_unfinished_import does,
        # so its released hold row is never left without a meeting.
        mid = self.store.create_meeting("synthetic new, hold released")
        holds.release_hold(self.store, holds.add_hold(self.store, mid, "all", "reason"))
        with self.assertRaises(StoreError):
            self.store.delete_new_meeting(mid)
        self.assertIsNotNone(self.store.meeting_state(mid))
        self.assertEqual(1, self.store._one(
            "SELECT COUNT(*) FROM holds WHERE meeting_id = ?", (mid,))[0])
        # A brand-new meeting that never had a hold is still removed.
        plain = self.store.create_meeting("synthetic new, never held")
        self.store.delete_new_meeting(plain)
        self.assertIsNone(self.store.meeting_state(plain))

    def test_an_unfinished_import_is_removed_in_one_transaction(self):
        mid = self.store.create_meeting("synthetic unfinished")
        self.store.add_source(mid, original_path=None, original_sha256="a" * 64,
                              original_size=1, original_mtime_ns=0, audio_sha256="b" * 64,
                              samples=SR, sample_rate=SR)
        calls = []

        def locked():
            calls.append("folder")
            raise PermissionError("synthetic: locked")
        with self.assertRaises(PermissionError):
            self.store.discard_unfinished_import(mid, locked)
        self.assertEqual(1, self.rows_of(mid)["meetings"])       # rolled back
        self.assertEqual(1, self.rows_of(mid)["sources"])
        self.assertTrue(self.store.discard_unfinished_import(mid, lambda: calls.append("ok")))
        self.assertEqual(["folder", "ok"], calls)
        self.assertEqual(0, self.rows_of(mid)["meetings"])
        self.assertEqual(0, self.rows_of(mid)["sources"])
        # Kept: a meeting with a version, or with any hold, even released (#193).
        kept = self.full_meeting("synthetic with a version")
        self.assertFalse(self.store.discard_unfinished_import(kept, lambda: calls.append("x")))
        released = self.store.create_meeting("synthetic, hold released")
        holds.release_hold(self.store, holds.add_hold(self.store, released, "all", "reason"))
        self.assertFalse(self.store.discard_unfinished_import(released,
                                                              lambda: calls.append("x")))
        self.assertNotIn("x", calls)
        self.assertIsNotNone(self.store.meeting_state(kept))
        self.assertIsNotNone(self.store.meeting_state(released))


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class FailedImportTests(unittest.TestCase):

    def test_a_failure_after_the_source_is_saved_leaves_nothing(self):
        # The import's planning step fails once the source row exists: the
        # meeting, its source and its media folder are all removed (through
        # store.discard_unfinished_import).
        from workspace.app import Workspace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = Workspace(root / "ws", root / "models", start=False, battery=False,
                           allow_test_engines=True)
            try:
                rec = write_wav(root / "synthetic.wav", 5)
                seen = {}

                def fail(mid, sid):
                    seen["source"] = ws.store.source(sid) is not None
                    seen["folder"] = (ws.media / mid).is_dir()
                    raise RuntimeError("synthetic decode failure")
                with mock.patch.object(ws, "_plan_version", side_effect=fail), \
                        self.assertRaises(RuntimeError):
                    ws._import_one(rec)
                self.assertEqual({"source": True, "folder": True}, seen)
                self.assertEqual(0, ws.store._one("SELECT COUNT(*) FROM meetings")[0])
                self.assertEqual(0, ws.store._one("SELECT COUNT(*) FROM sources")[0])
                self.assertEqual([], list(ws.media.iterdir()) if ws.media.exists() else [])
            finally:
                ws.close()


def ui_check_long_meeting():
    """tools/workspace_ui_check.py's add_long_meeting(), taken by ast without
    importing the tool (which opens windows), with this file's write_wav."""
    tool = ast.parse((ROOT / "tools" / "workspace_ui_check.py").read_text(encoding="utf-8"))
    fn = next(n for n in tool.body
              if isinstance(n, ast.FunctionDef) and n.name == "add_long_meeting")
    space = {"types": types, "write_wav": write_wav}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "workspace_ui_check", "exec"), space)
    return space["add_long_meeting"]


# The failed-meeting constants of tools/workspace_ui_check.py (#244).
UI_CHECK_FAILED = ("FAKE", "FAILED_TITLE", "NOT_READY_MODEL", "READY_MODEL", "ANOTHER_MODEL")


def ui_check_parts(*names):
    """Top-level functions and constants of tools/workspace_ui_check.py, by
    name, taken by ast without importing the tool (which opens windows), with
    this file's write_wav."""
    import asr_engines
    tool = ast.parse((ROOT / "tools" / "workspace_ui_check.py").read_text(encoding="utf-8"))
    wanted = [n for n in tool.body
              if (isinstance(n, ast.FunctionDef) and n.name in names)
              or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in names
                                                    for t in n.targets))]
    space = {"asr_engines": asr_engines, "write_wav": write_wav}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "workspace_ui_check", "exec"), space)
    missing = [n for n in names if n not in space]
    assert not missing, f"not in the tool: {missing}"
    return types.SimpleNamespace(**{n: space[n] for n in names})


class UiCheckVerdictTests(unittest.TestCase):
    """tools/workspace_ui_check.py's verdict on its failed-meeting report
    (failed_meeting_problems, #244): a report of the flow working passes, with
    downloads allowed or turned off by policy, and each thing that can go wrong
    is named."""

    def setUp(self):
        self.ui = ui_check_parts("failed_meeting_problems", *UI_CHECK_FAILED)

    def good(self, downloads=True):
        ui = self.ui
        return {
            "model_label": "Parakeet v2", "downloads_offered": downloads,
            "failed": {"row_found": True, "needs_attention": True, "offered": True,
                       "dialog_open": True, "confirm_open": False,
                       "title": "Parakeet v2 is still not ready",
                       "body": "This meeting stopped because its speech model is not ready, "
                               "and it is still the model chosen in Settings > Speech model. "
                               "Parakeet v2 is not downloaded.",
                       "downloads_shown": downloads},
            "unchanged_by_not_ready": True,
            "settings": {"marked": "Parakeet v2 (recommended) (not downloaded)",
                         "hint_shown": True, "downloads_primary": downloads,
                         "chosen": ui.READY_MODEL},
            "confirm": {"offered": True, "open": True, "not_ready_open": False,
                        "title": "Transcribe again with the current model?",
                        "body": "It uses the model chosen in Settings > Speech model.",
                        "ok_disabled_until_ticked": True},
            "after": {"stage": "review", "error": None, "model": ui.READY_MODEL,
                      "older_models": [ui.NOT_READY_MODEL], "passages": 12,
                      "row_found": True, "row_needs_attention": False}}

    def test_the_flow_working_passes(self):
        self.assertEqual([], self.ui.failed_meeting_problems(self.good()))
        self.assertEqual([], self.ui.failed_meeting_problems(self.good(downloads=False)))

    def test_an_empty_report_fails_on_every_step(self):
        # The window check stopped early (an error): nothing passes by default.
        self.assertEqual(9, len(self.ui.failed_meeting_problems({})))

    def test_each_problem_is_named(self):
        ui = self.ui
        cases = [
            ("failed", "offered", False, "did not offer " + ui.ANOTHER_MODEL),
            ("failed", "needs_attention", False, "did not offer"),
            ("failed", "title", "Parakeet v2 is not ready", "not-ready dialog for the same"),
            ("failed", "confirm_open", True, "not-ready dialog for the same"),
            ("failed", "downloads_shown", False, "not-ready dialog for the same"),
            (None, "unchanged_by_not_ready", False, "dialog changed the meeting"),
            ("settings", "marked", "Parakeet v2 (recommended)", "did not mark"),
            ("settings", "hint_shown", False, "did not mark"),
            ("settings", "downloads_primary", False, "did not mark"),
            ("settings", "chosen", ui.NOT_READY_MODEL, "ready model was not chosen"),
            ("confirm", "not_ready_open", True, "Transcribe again confirmation"),
            ("confirm", "ok_disabled_until_ticked", False, "Transcribe again confirmation"),
            ("after", "stage", "inbox", "did not leave the error state"),
            ("after", "error", "The speech model is not ready", "did not leave the error"),
            ("after", "model", ui.NOT_READY_MODEL, "did not leave the error state"),
            ("after", "passages", 0, "did not leave the error state"),
            ("after", "older_models", [], "earlier version was not kept"),
            ("after", "row_needs_attention", True, "still needs attention"),
        ]
        for part, key, value, problem in cases:
            with self.subTest(part=part, key=key):
                report = self.good()
                (report[part] if part else report)[key] = value
                found = ui.failed_meeting_problems(report)
                self.assertEqual(1, len(found), found)
                self.assertIn(problem, found[0])
        # Manage downloads shown although policy turns downloads off.
        report = self.good(downloads=False)
        report["failed"]["downloads_shown"] = True
        self.assertEqual(1, len(ui.failed_meeting_problems(report)))


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class UiCheckSetupTests(unittest.TestCase):

    def test_the_long_meeting_is_never_offered_for_transcription(self):
        # The UI check commits its long meeting's passages itself and asserts
        # nothing was transcribed. The meeting is paused before its plan
        # exists, so the processing loop never sees it as work, even if it
        # wakes (its 30 s timeout) straight after the plan is written.
        from workspace.app import Workspace
        add_long_meeting = ui_check_long_meeting()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = Workspace(root / "ws", root / "models", start=False, battery=False,
                           allow_test_engines=True)
            try:
                offered, plan = [], ws._plan_version

                def planned(mid, sid):
                    out = plan(mid, sid)
                    offered.append(ws._next_work())     # what a waking loop would take
                    return out
                with mock.patch.object(ws, "_plan_version", side_effect=planned):
                    mid = add_long_meeting(ws, root, passages=50)
                self.assertEqual([None], offered)
                self.assertTrue(ws.store.meeting_state(mid)["paused"])
                vid = ws.store.active_version(mid)["id"]
                self.assertEqual(50, ws.store.passage_count(vid))
                self.assertEqual([], ws.store.chunks(vid, status="pending"))
            finally:
                ws.close()

    def test_the_failed_meeting_stops_on_its_model_and_moves_to_a_ready_one(self):
        # #244: the UI check's failed meeting, without a window. Its real model
        # name fails at load because the check's model folder is new and empty:
        # nothing is downloaded or loaded. What the window then does by hand
        # (choose a ready model, confirm Transcribe again) leaves the error
        # state and keeps the failed version as an earlier one.
        from workspace.app import Workspace
        ui = ui_check_parts("add_failed_meeting", *UI_CHECK_FAILED)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = Workspace(root / "ws", root / "models", start=False, battery=False,
                           allow_test_engines=True)
            try:
                ws.settings.update(model=ui.READY_MODEL, speakers=False)
                ws.start()
                mid = ui.add_failed_meeting(ws, root)
                [row] = [m for m in ws.meetings() if m["id"] == mid]
                self.assertTrue(row["can_retranscribe"])
                self.assertEqual([], [p for p in (root / "models").rglob("*") if p.is_file()])
                # Settings marks it, so the window's "still not ready" dialog applies.
                [choice] = [c for c in ws.get_settings()["choices"]
                            if c["name"] == ui.NOT_READY_MODEL]
                self.assertEqual("missing", choice["state"])
                self.assertTrue(choice["note"])
                ws.set_settings({"model": ui.READY_MODEL})
                ws.retranscribe(mid)
                self.assertTrue(ws.wait_idle(120))
                d = ws.detail(mid)
                self.assertEqual(("review", None, ui.READY_MODEL),
                                 (d["stage"], d["error"], d["model"]))
                self.assertEqual([ui.NOT_READY_MODEL], [x["model"] for x in d["older_versions"]])
                self.assertGreater(ws.transcript(mid)["total"], 0)
            finally:
                ws.close()


def child_inspect():
    """tools/packaged_crash_check.py's inspect(), taken from its child script
    without running the script (which installs the network guard)."""
    tool = ast.parse((ROOT / "tools" / "packaged_crash_check.py").read_text(encoding="utf-8"))
    script = next(n.value.value for n in tool.body if isinstance(n, ast.Assign)
                  and any(getattr(t, "id", None) == "CHILD" for t in n.targets))
    fn = next(n for n in ast.parse(script).body
              if isinstance(n, ast.FunctionDef) and n.name == "inspect")
    space = {"hashlib": hashlib}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "packaged_crash_check:CHILD", "exec"),
         space)
    return space["inspect"]


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class CrashCheckInspectTests(unittest.TestCase):

    def test_inspect_reads_counts_text_and_marker_through_the_store(self):
        marker = "syntheticmarkerword"
        inspect = child_inspect()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            s = Store(root / "workspace.db")
            try:
                mid, vid = long_meeting(s, n=30)
                generated = [r["generated"] for r in s.transcript(vid)]
                first = s.transcript(vid)[1]["id"]
                s.save_edit(first, f"Marker {marker.upper()} for the crash check")
                running = s.start_attempt(vid, 0)
                s.set_phase(mid, "error", "synthetic note")
            finally:
                s.close()
            out = inspect(root, marker)
            self.assertEqual(1, out["meetings"])
            self.assertEqual(["error"], out["phases"])
            self.assertEqual(["synthetic note"], out["errors"])
            self.assertEqual(31, out["segments"])
            self.assertEqual(1, out["duplicate_segments"])   # the two passages at 6.0 s
            self.assertEqual(hashlib.sha256(" ".join(generated).encode()).hexdigest(),
                             out["text_sha256"])
            self.assertTrue(out["marker_in_edits"])
            self.assertFalse(inspect(root, "not" + marker)["marker_in_edits"])
            # Opening did not mark the running attempt interrupted (housekeeping off).
            self.assertEqual({"running": 1}, out["attempts"])
            s = Store(root / "workspace.db", housekeeping=False)
            try:
                self.assertEqual("running", s._one("SELECT result FROM attempts WHERE id = ?",
                                                   (running,))[0])
            finally:
                s.close()


if __name__ == "__main__":
    unittest.main()
