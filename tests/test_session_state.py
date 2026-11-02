# tests/test_session_state.py
# The evidence checks stop when the session locks (#507). On Windows 11 26H2
# (10.0.26300) the inactivity lock left the input desktop named "Default", so
# perf_check's old lock test said "unlocked"; the UI, accessibility and
# packaged checks had no lock test at all, and passed, or failed a clipboard
# step, on a locked session. These tests use stand-ins for Windows' answers
# and for the windows: no real window opens.
#   python tools/run_tests.py --only test_session_state

import io
import json
import shutil
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import evidence  # noqa: E402
import session_state  # noqa: E402

LOCK, UNLOCK = session_state.WTS_SESSIONSTATE_LOCK, session_state.WTS_SESSIONSTATE_UNLOCK


def wtsinfoex(level=1, session_id=3, state=0, flags=LOCK):
    """The start of a WTSINFOEXW as Windows returns it: Level, 4 bytes of
    padding (the union is 8-byte aligned), then SessionId, SessionState and
    SessionFlags; the rest (names, times, counters) as zeros."""
    head = struct.pack("<I4xIIi", level, session_id, state, flags)
    return head + bytes(64)


def windows_says(flags, desktop="Default"):
    """Patch Windows' two answers: the WTS flag (None: unreadable) and the
    input desktop's name."""
    return mock.patch.multiple(session_state, wts_flags=mock.Mock(return_value=flags),
                               input_desktop=mock.Mock(return_value=desktop))


def reader(*states):
    """A Watch reader answering locked states in turn (the last repeats)."""
    answers = list(states)

    def read():
        locked = answers.pop(0) if len(answers) > 1 else answers[0]
        return {"locked": locked, "how": None if locked is None else session_state.WTS}
    return read


class StructureTests(unittest.TestCase):
    """WTSINFOEXW as Microsoft Learn documents it (WTSINFOEXW,
    WTSINFOEX_LEVEL1_W, WTS_INFO_CLASS), and as read on the owner's laptop."""

    def test_the_layout_and_constants(self):
        self.assertEqual(25, session_state.WTS_SESSION_INFO_EX)     # 26th member, from 0
        self.assertEqual((0, 1, -1), (LOCK, UNLOCK, session_state.WTS_SESSIONSTATE_UNKNOWN))
        self.assertEqual((0, 8, 16), (session_state.LEVEL_OFFSET,
                                      session_state.SESSION_ID_OFFSET,
                                      session_state.FLAGS_OFFSET))

    def test_the_layout_matches_the_structure_with_its_alignment(self):
        # The union holds LARGE_INTEGERs, so it starts at 8, not 4.
        import ctypes

        class Level1(ctypes.Structure):
            _fields_ = [("SessionId", ctypes.c_uint32), ("SessionState", ctypes.c_int),
                        ("SessionFlags", ctypes.c_int32),
                        ("WinStationName", ctypes.c_wchar * 33),
                        ("UserName", ctypes.c_wchar * 21), ("DomainName", ctypes.c_wchar * 18),
                        ("LogonTime", ctypes.c_int64)]

        class InfoEx(ctypes.Structure):
            _fields_ = [("Level", ctypes.c_uint32), ("Data", Level1)]
        self.assertEqual(8, InfoEx.Data.offset)
        self.assertEqual(8 + Level1.SessionId.offset, session_state.SESSION_ID_OFFSET)
        self.assertEqual(8 + Level1.SessionFlags.offset, session_state.FLAGS_OFFSET)

    def test_flags_are_read_only_from_a_level_1_record_of_this_session(self):
        self.assertEqual(LOCK, session_state.flags_from(wtsinfoex(flags=LOCK), 3))
        self.assertEqual(UNLOCK, session_state.flags_from(wtsinfoex(flags=UNLOCK), 3))
        self.assertIsNone(session_state.flags_from(wtsinfoex(flags=-1), 3))       # unknown
        self.assertIsNone(session_state.flags_from(wtsinfoex(flags=7), 3))
        self.assertIsNone(session_state.flags_from(wtsinfoex(level=2), 3))
        self.assertIsNone(session_state.flags_from(wtsinfoex(session_id=4), 3))
        self.assertIsNone(session_state.flags_from(wtsinfoex()[:19], 3))         # too short
        self.assertIsNone(session_state.flags_from(None, 3))
        # Read at 4, as an unaligned structure would put it, a lock would be missed.
        packed = struct.pack("<IIii", 1, 3, 0, LOCK) + bytes(64)
        self.assertNotEqual(LOCK, session_state.flags_from(packed, 3))

    @unittest.skipUnless(sys.platform == "win32", "asks this Windows session")
    def test_this_session_answers_without_failing(self):
        # Only the shape: whether this session is locked is not checked.
        self.assertIn(session_state.wts_flags(), (LOCK, UNLOCK, None))
        state = session_state.read()
        self.assertEqual({"locked", "how"}, set(state))
        self.assertIn(state["how"], (session_state.WTS, session_state.DESKTOP, None))


class ReadTests(unittest.TestCase):
    def test_the_wts_flag_decides_even_when_the_desktop_is_named_default(self):
        # #507: locked, with the input desktop still "Default".
        with windows_says(LOCK, "Default"):
            self.assertEqual({"locked": True, "how": "wts"}, session_state.read())
        with windows_says(UNLOCK, "Winlogon"):
            self.assertEqual({"locked": False, "how": "wts"}, session_state.read())

    def test_the_input_desktop_only_when_the_flag_cannot_be_read(self):
        for name, locked in (("Default", False), ("default", False), ("Winlogon", True),
                             ("", True), (None, None)):
            with self.subTest(name=name), windows_says(None, name):
                self.assertEqual(locked, session_state.read()["locked"])
                self.assertEqual(None if name is None else "input desktop",
                                 session_state.read()["how"])


class WatchTests(unittest.TestCase):
    def test_unlocked_throughout(self):
        watch = session_state.Watch(reader(False))
        for step in ("before", "after"):
            watch.check(step)
        self.assertEqual({"record": session_state.RECORD, "readings": 2, "wts": 2,
                          "input_desktop": 0, "unread": 0, "locked": False, "locked_at": None},
                         watch.record())
        self.assertTrue(session_state.valid(watch.record()))

    def test_a_lock_stops_and_stays_stopped(self):
        watch = session_state.Watch(reader(False, True, False))
        watch.step = "the handoff"
        watch.check("before the handoff")
        with self.assertRaises(session_state.SessionLocked) as caught:
            watch.check("after the handoff")
        self.assertEqual("the session locked (after the handoff)", str(caught.exception))
        with self.assertRaises(session_state.SessionLocked):     # unlocked again: still stopped
            watch.check("before the next step")
        record = watch.record()
        self.assertEqual((True, "after the handoff", 3), (record["locked"], record["locked_at"],
                                                          record["readings"]))
        self.assertIn("STOPPED: the session locked (after the handoff)",
                      session_state.stopped_message(watch))
        self.assertNotIn("PASS", session_state.stopped_message(watch))

    def test_an_unreadable_state_stops_as_a_lock(self):
        watch = session_state.Watch(reader(None))
        with self.assertRaises(session_state.SessionLocked) as caught:
            watch.check("as the check started")
        self.assertEqual(1, watch.record()["unread"])
        # Said as it is: it may not have been a lock (review of 9c54272).
        self.assertEqual("the session locked, or its state could not be read "
                         "(as the check started)", str(caught.exception))
        self.assertIn("STOPPED: the session locked, or its state could not be read "
                      "(as the check started)", session_state.stopped_message(watch))

    def test_a_child_process_record_is_added(self):
        child = session_state.Watch(reader(False, True))
        child.reading("before the locked screen")
        child.reading("after the locked screen")
        parent = session_state.Watch(reader(False))
        parent.reading("as the check started")
        parent.merge(child.record())
        self.assertEqual((3, True, "after the locked screen"),
                         (parent.record()["readings"], parent.stopped, parent.locked_at))
        # A child that wrote no report adds nothing; anything else unreadable stops.
        quiet = session_state.Watch(reader(False))
        quiet.merge(None)
        self.assertFalse(quiet.stopped)
        quiet.merge({"record": 0})
        self.assertTrue(quiet.stopped)
        self.assertIn("or its state could not be read", session_state.stopped_message(quiet))
        # A child's lock is said as one unless the child had an unread reading.
        self.assertNotIn("could not be read", session_state.stopped_message(parent))
        unread = session_state.Watch(reader(None))
        unread.reading("before the locked screen")
        merged = session_state.Watch(reader(False))
        merged.merge(unread.record())
        self.assertIn("or its state could not be read", session_state.stopped_message(merged))

    def test_a_fallback_reading_is_said_once(self):
        # #514: a development run may use the input desktop's name, but the
        # check says so, once, naming the step (a fixed name, P3).
        said = []
        states = iter([{"locked": False, "how": session_state.WTS},
                       {"locked": False, "how": session_state.DESKTOP},
                       {"locked": False, "how": session_state.DESKTOP}])
        watch = session_state.Watch(lambda: next(states), say=said.append)
        watch.check("as the check started")
        self.assertEqual([], said)
        watch.check("before the view meetings")
        watch.check("after the view meetings")
        self.assertEqual([session_state.FALLBACK.format(where="before the view meetings")], said)
        self.assertIn("not Windows' session lock flag (WTS)", said[0])
        self.assertIn("refuses its report as release evidence", said[0])
        self.assertFalse(watch.stopped)                 # the run goes on
        self.assertEqual((3, 1, 2), (watch.record()["readings"], watch.record()["wts"],
                                     watch.record()["input_desktop"]))
        # All by WTS: nothing said.
        quiet = []
        session_state.Watch(reader(False), say=quiet.append).check("as the check started")
        self.assertEqual([], quiet)

    def test_a_fallback_reading_in_the_child_process_is_said(self):
        # The child's own note goes to its captured output, so the parent says it.
        child = session_state.Watch(lambda: {"locked": False, "how": session_state.DESKTOP},
                                    say=lambda text: None)
        child.reading("before the locked screen")
        said = []
        parent = session_state.Watch(reader(False), say=said.append)
        parent.reading("as the check started")
        parent.merge(child.record())
        self.assertEqual([session_state.FALLBACK.format(
            where="in the check's own child process")], said)
        parent.merge(child.record())                    # said once a run
        self.assertEqual(1, len(said))
        # A child all by WTS: nothing said.
        quiet = []
        clean = session_state.Watch(reader(False))
        clean.reading("before the locked screen")
        session_state.Watch(reader(False), say=quiet.append).merge(clean.record())
        self.assertEqual([], quiet)

    def test_the_default_note_goes_to_stderr(self):
        # Standard output keeps the report and the verdict as before.
        err, out = io.StringIO(), io.StringIO()
        watch = session_state.Watch(lambda: {"locked": False, "how": session_state.DESKTOP})
        with redirect_stdout(out), mock.patch.object(sys, "stderr", err):
            watch.reading("as the check started")
        self.assertEqual("", out.getvalue())
        self.assertIn("NOTE: the session's lock state was read from the input desktop's name",
                      err.getvalue())

    def test_the_record_version_is_the_one_evidence_checks(self):
        self.assertEqual(session_state.RECORD, evidence.SESSION_RECORD)


class PerfDisplayTests(unittest.TestCase):
    """perf_check's display reads the lock with the shared test (#507)."""

    @unittest.skipUnless(sys.platform == "win32", "reads the screen saver's state")
    def test_a_lock_with_the_desktop_still_named_default_is_seen(self):
        import perf_check
        display = perf_check.WindowsDisplay()
        with windows_says(LOCK, "Default"):
            state = display.session()        # as the old test was called
        self.assertIsNotNone(state)
        self.assertIs(True, state["locked"])
        self.assertIn("the session was locked", perf_check.session_problems(state))
        record = perf_check.session_record(display)
        self.assertEqual((True, "a visible state", 1),
                         (record["locked"], record["locked_at"], record["readings"]))

    @unittest.skipUnless(sys.platform == "win32", "reads the screen saver's state")
    def test_unlocked_by_the_flag_is_unlocked(self):
        import perf_check
        display = perf_check.WindowsDisplay()
        with windows_says(UNLOCK, "Winlogon"):
            self.assertIs(False, display.session("before meetings")["locked"])
        self.assertFalse(perf_check.session_record(display)["locked"])

    def test_an_unreadable_session_is_not_drawn(self):
        import perf_check
        display = perf_check.WindowsDisplay()
        with windows_says(None, None):
            self.assertIsNone(display.session("before meetings"))
        self.assertTrue(perf_check.session_record(display)["locked"])


def fake_window(answers, calls):
    """A window whose page answers evaluate_js at once, recording each call."""
    window = mock.Mock()

    def evaluate_js(js, callback):
        calls.append(js)
        callback(answers)
    window.evaluate_js.side_effect = evaluate_js
    return window


class UiCheckTests(unittest.TestCase):
    """tools/workspace_ui_check.py (and so tools/accessibility_check.py, whose
    page steps go through its evaluate) read the session before and after
    each window step."""

    def setUp(self):
        import workspace_ui_check
        self.ui = workspace_ui_check
        self.addCleanup(setattr, workspace_ui_check, "SESSION", None)

    def test_a_step_on_a_locked_session_is_never_run(self):
        self.ui.SESSION = session_state.Watch(reader(True))
        calls = []
        with self.assertRaises(session_state.SessionLocked):
            self.ui.evaluate(fake_window(1, calls), "return 1;")
        self.assertEqual([], calls)

    def test_a_lock_during_a_step_stops_the_check_after_it(self):
        self.ui.SESSION = session_state.Watch(reader(False, True))
        self.ui.session_step("the handoff")
        calls = []
        with self.assertRaises(session_state.SessionLocked) as caught:
            self.ui.evaluate(fake_window(1, calls), "return 1;")
        self.assertEqual(1, len(calls))
        self.assertEqual("after the handoff", caught.exception.where)

    def test_a_timeout_on_a_locked_session_is_a_lock(self):
        self.ui.SESSION = session_state.Watch(reader(False, True))
        window = mock.Mock()                     # never answers
        with self.assertRaises(session_state.SessionLocked):
            self.ui.evaluate(window, "return 1;", timeout=0.01)

    def test_unlocked_steps_run_as_before(self):
        self.ui.SESSION = session_state.Watch(reader(False))
        self.assertEqual(7, self.ui.evaluate(fake_window(7, []), "return 7;"))
        self.assertEqual(2, self.ui.SESSION.record()["readings"])

    def run_main(self, tool, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            printed = io.StringIO()
            with mock.patch.object(sys, "argv", [tool.__file__, str(out), *extra]), \
                    windows_says(LOCK, "Default"), redirect_stdout(printed):
                rc = tool.main()
            return rc, json.loads(out.read_text(encoding="utf-8")), printed.getvalue()

    def test_the_ui_check_stops_with_exit_2_when_locked_at_the_start(self):
        with mock.patch.object(self.ui, "Workspace", side_effect=AssertionError("set up")):
            rc, report, printed = self.run_main(self.ui)
        self.assertEqual(2, rc)
        self.assertTrue(report["session"]["locked"])
        self.assertEqual("as the check started", report["session"]["locked_at"])
        self.assertIn("STOPPED: the session locked", printed)
        self.assertNotIn("ALL CHECKS PASSED", printed)
        self.assertNotIn("FAILED", printed)

    def test_the_accessibility_check_stops_with_exit_2_when_locked_at_the_start(self):
        import accessibility_check
        with mock.patch("workspace.app.Workspace", side_effect=AssertionError("set up")):
            rc, report, printed = self.run_main(accessibility_check)
        self.assertEqual(2, rc)
        self.assertTrue(report["session"]["locked"])
        self.assertIn("STOPPED: the session locked", printed)
        self.assertNotIn("NO FINDINGS", printed)
        self.assertNotIn("FINDINGS:", printed)

    def test_the_locked_screens_process_records_the_session(self):
        # Its record reaches the parent's (merge), and a lock there stops it.
        source = Path(self.ui.__file__).read_text(encoding="utf-8")
        self.assertIn('report["session"] = SESSION.record()', source.split(
            "def locked_main(out):", 1)[1].split("\ndef ", 1)[0])
        self.assertIn('SESSION.merge(report["locked_screen"].pop("session", None))', source)


class PackagedCheckTests(unittest.TestCase):
    def setUp(self):
        import packaged_check
        self.tool = packaged_check
        self.addCleanup(setattr, packaged_check, "SESSION", None)

    def test_a_lock_removes_the_throwaway_base_and_stops(self):
        self.tool.SESSION = session_state.Watch(reader(True))
        base = Path(tempfile.mkdtemp(prefix="umpkg-test-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)   # if the check fails
        (base / "windows.bin").write_bytes(b"synthetic")
        with self.assertRaises(session_state.SessionLocked):
            self.tool.session_check("after the window step", base)
        self.assertFalse(base.exists())

    def test_the_packaged_check_stops_with_exit_2_when_locked(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            out = Path(tmp) / "report.json"
            printed = io.StringIO()
            argv = [self.tool.__file__, str(Path(tmp) / "v"), str(Path(tmp) / "cache"),
                    str(Path(tmp) / "a.wav"), str(out)]
            # Any throwaway base goes in tmp, so a failure leaves none in %TEMP%.
            with mock.patch.object(sys, "argv", argv), windows_says(LOCK, "Default"), \
                    mock.patch.object(tempfile, "tempdir", tmp), redirect_stdout(printed):
                rc = self.tool.main()
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual([], list(Path(tmp).glob("umpkg-*")))
        self.assertEqual(2, rc)
        self.assertTrue(report["session"]["locked"])
        self.assertNotIn("pass", report)
        self.assertNotIn("failed", report)
        self.assertIn("STOPPED: the session locked", printed.getvalue())

    def test_a_lock_seen_after_a_window_step_that_raised_still_stops(self):
        # The reading after the window step runs even when the step raises,
        # so the lock wins over the step's error and the base goes.
        self.tool.SESSION = session_state.Watch(reader(False, False, True))
        done = mock.Mock(stdout='{"ok": true}', stderr="", returncode=0)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            with mock.patch.object(tempfile, "tempdir", tmp), \
                    mock.patch.object(self.tool.evidence, "add_app"), \
                    mock.patch.object(self.tool.residue, "names", return_value={}), \
                    mock.patch.object(self.tool.residue, "real_places", return_value=[]), \
                    mock.patch.object(self.tool.residue, "snapshot", return_value={}), \
                    mock.patch.object(self.tool.subprocess, "run", return_value=done), \
                    mock.patch.object(self.tool, "window_step",
                                      side_effect=RuntimeError("the step broke")), \
                    self.assertRaises(session_state.SessionLocked) as caught:
                self.tool.check(root / "v", root / "cache", root / "a.wav", {})
            self.assertEqual([], list(root.glob("umpkg-*")))
        self.assertEqual("after the window step", caught.exception.where)


class EvidenceTests(unittest.TestCase):
    """tools/evidence.py check refuses a window check's report that records a
    lock, or no session state."""

    UNLOCKED = {"record": evidence.SESSION_RECORD, "readings": 4, "wts": 4, "input_desktop": 0,
                "unread": 0, "locked": False, "locked_at": None}

    def test_the_window_checks_are_the_ones_that_record_it(self):
        for kind, tool in evidence.KINDS.items():
            source = (ROOT / tool.split()[0]).read_text(encoding="utf-8")
            with self.subTest(kind=kind):
                self.assertEqual(kind in evidence.SESSION_KINDS, "session_state" in source)

    def test_an_unlocked_record_passes(self):
        self.assertIsNone(evidence.session_problem({"session": dict(self.UNLOCKED)}))

    def test_a_lock_or_no_record_is_refused(self):
        locked = dict(self.UNLOCKED, locked=True, locked_at="after the view meetings")
        cases = {"no block": {}, "an older record": {"session": dict(self.UNLOCKED, record=0)},
                 "a lock": {"session": locked},
                 "a lock named only": {"session": dict(self.UNLOCKED, locked_at="before x")},
                 "unread": {"session": dict(self.UNLOCKED, unread=1, readings=5)},
                 "no readings": {"session": dict(self.UNLOCKED, readings=0, wts=0,
                                                 input_desktop=0)},
                 "counts that disagree": {"session": dict(self.UNLOCKED, wts=1)},
                 "not a count": {"session": dict(self.UNLOCKED, readings=True)}}
        for name, body in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(evidence.session_problem(body))
        self.assertIn("after the view meetings", evidence.session_problem({"session": locked}))
        self.assertIn("no record of the session's state", evidence.session_problem({}))

    def test_a_reading_from_the_input_desktop_is_refused(self):
        # #514 (the owner's decision, 2 Nov 2026): the input desktop's name
        # missed a lock on Windows 11 (#507), so release evidence counts only
        # WTS readings. One fallback reading among many is enough to refuse.
        for wts, desktop in ((3, 1), (0, 4), (39, 1)):
            body = {"session": dict(self.UNLOCKED, readings=wts + desktop, wts=wts,
                                    input_desktop=desktop)}
            with self.subTest(wts=wts, desktop=desktop):
                problem = evidence.session_problem(body)
                self.assertIsNotNone(problem)
                self.assertIn(f"records {desktop} of its {wts + desktop} readings of the "
                              "session's state from the input desktop's name", problem)
                self.assertIn("counts only WTS readings", problem)


if __name__ == "__main__":
    unittest.main()
