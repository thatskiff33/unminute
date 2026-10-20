# What the page polls costs little and does not grow with the workspace
# (R7b, #128): the Meetings list reads each table once, and what Settings and
# Setup show from disk or Credential Manager is kept for DISPLAY_CACHE_S.
# Real worker process, fake engines and synthetic audio.
#   python tools/run_tests.py

import importlib.util
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
SR = 16000

if HAVE_DEPS:
    import numpy as np

    from workspace import asr_engines
    from workspace import holds, speaker_names
    from workspace.app import Workspace


def fake(name):
    return f"test:fake_worker_engines:{name}"


def write_wav(path, seconds, seed):
    rng = np.random.default_rng(seed)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((rng.standard_normal(int(seconds * SR)) * 300).astype("<i2").tobytes())
    return path


def reference_meetings(ws):
    """The list as it was worked out before R7b, one meeting at a time with
    the store's own helpers: what meetings() must still return."""
    store, out = ws.store, []
    for row in holds.unfinished_work(store):
        m = store.meeting(row["id"])
        v = store.active_version(row["id"])
        done, total = store.progress(v["id"]) if v else (0, 0)
        runs = store.speaker_runs(v["id"]) if v else []
        if m["final_saved_at"]:
            stage = "cleanup"
        elif m["reviewed_at"]:
            stage = "handoff"
        elif v is None or store.chunks(v["id"], status="pending") or m["phase"] == "error":
            stage = "inbox"
        else:
            stage = "review"
        if v is None:
            facts = {"duration_s": None, "speaker_count": None, "model": None,
                     "model_label": None}
        else:
            src = store.source(v["source_id"])
            run = speaker_names.current_run(store, v["id"])
            facts = {"duration_s": round(src["samples"] / src["sample_rate"], 1),
                     "speaker_count": (len(speaker_names.resolve(store, v["id"])["order"])
                                       if run else None),
                     "model": v["model"], "model_label": asr_engines.label(v["model"])}
        busy = ws._busy and ws._busy[0] == row["id"] and ws._busy[1]
        out.append({**row, **facts, "audio_available": ws._audio_available(m),
                    "cleanup": [{"id": j["id"], "phase": j["phase"], "state": j["state"],
                                 "note": j["error"]} for j in store._all(
                        "SELECT * FROM cleanup_jobs WHERE meeting_id = ? ORDER BY intent_at",
                        (row["id"],))],
                    "progress": round(100 * done / total) if total else 0,
                    "paused": bool(m["paused"]),
                    "error": m["last_error"] if m["phase"] == "error" else None,
                    "busy": busy or None, "stage": stage,
                    # #193: no version, no import running, not yet discarded.
                    "unfinished": v is None and ws._imports.unfinished_tasks == 0
                    and not m["reviewed_at"],
                    "can_skip": bool(v) and ws._can_skip(m, runs),
                    # #244: also for a failed meeting, still in the inbox.
                    "can_retranscribe": bool(v) and ws._audio_available(m)
                    and not m["reviewed_at"] and (stage != "inbox" or m["phase"] == "error"),
                    "action": ws._action(m, stage)})
    return out


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class MeetingListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Eight meetings in different states: identified twice (the second
        time with 3 speakers) and a hold released; a renamed and a merged
        speaker; reviewed; final saved but held; failed; paused with a chunk
        still to do; skipped while a pass was running; and one with no
        transcript yet."""
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        ws = Workspace(root / "ws", root / "models", start=False, allow_test_engines=True,
                       speaker_model=fake("fake_diarizer"), battery=False,
                       protector=keyprotect.TestProtector())
        ws.settings.update(model=fake("sentences"), speakers=True)
        ws.start()
        paths = [str(write_wav(root / f"Synthetic {i}.wav", 30, seed=i)) for i in range(6)]
        assert ws.import_paths(paths)["accepted"] == 6
        assert ws.wait_idle(300)
        mids = [m["id"] for m in ws.meetings()]
        ws.meetings()                    # speaker counts kept: the rerun must replace them
        ws.rerun_speakers(mids[0], 3)
        assert ws.wait_idle(120)
        ws.release_hold(ws.add_hold(mids[0], "audio", "Synthetic reason"))
        ws.store.set_paused(mids[5], True)
        with ws.store.tx() as db:
            db.execute("UPDATE chunks SET status = 'pending' WHERE version_id = ? AND idx = 0",
                       (ws.store.active_version(mids[5])["id"],))
        # Skipped while a pass was running: its last run is the skip.
        ws.settings.update(speakers=False)
        assert ws.import_paths([str(write_wav(root / "Synthetic 6.wav", 30, seed=6))])[
            "accepted"] == 1
        assert ws.wait_idle(120)
        skipped = next(m["id"] for m in ws.meetings() if m["id"] not in mids)
        svid = ws.store.active_version(skipped)["id"]
        ws.store.start_speaker_run(svid)
        assert ws.store.skip_speaker_run(svid) == "skipped"
        ws.settings.update(speakers=True)
        vid = ws.store.active_version(mids[1])["id"]
        first, second = speaker_names.resolve(ws.store, vid)["order"][:2]
        ws.rename_speaker(mids[1], first, "Synthetic speaker")
        ws.merge_speakers(mids[1], second, first)
        ws.confirm_reviewed(mids[2])
        ws.add_hold(mids[3], "all", "Synthetic reason")
        ws.confirm_reviewed(mids[3])
        ws.confirm_final_saved(mids[3])
        ws.store.set_phase(mids[4], "error", "A synthetic failure")
        ws.store.create_meeting("No transcript yet")
        assert ws.wait_idle(120)
        cls.ws = ws

    @classmethod
    def tearDownClass(cls):
        cls.ws.close()
        cls.tmp.cleanup()

    def test_the_list_is_unchanged(self):
        new, old = self.ws.meetings(), reference_meetings(self.ws)
        self.assertEqual(len(new), 8)
        self.assertEqual(new, old)
        stages = sorted(m["stage"] for m in new)
        self.assertEqual(stages, ["cleanup", "handoff", "inbox", "inbox", "inbox", "review",
                                  "review", "review"])
        self.assertIn(False, [m["can_skip"] for m in new if m["speaker_count"] is None])
        self.assertEqual(sum(m["held"] for m in new), 1)      # the released hold does not count
        inbox = {bool(m["error"]): m["can_retranscribe"] for m in new
                 if m["stage"] == "inbox" and m["model"]}
        self.assertEqual(inbox, {True: True, False: False})   # failed: yes; paused: no
        self.assertTrue(any(m["cleanup"] for m in new))
        self.assertTrue(any(m["paused"] and m["progress"] < 100 for m in new))
        counts = [m["speaker_count"] for m in new]
        self.assertIn(1, counts)                               # the merged meeting
        self.assertIn(3, counts)                               # identified again
        # One kept count per version, replaced when its run changes.
        self.assertEqual(len(self.ws._speaker_counts),
                         sum(c is not None for c in counts))

    def statements(self):
        seen = []
        self.ws.store.db.set_trace_callback(seen.append)
        try:
            self.ws.meetings()
        finally:
            self.ws.store.db.set_trace_callback(None)
        return len(seen)

    def test_a_finished_job_checks_setup_again(self):
        # A model can fail its check at load: Setup must not keep "Ready".
        with mock.patch.object(self.ws, "_forget_display") as forget:
            # The meeting identified with 3 speakers, again with 3: the shared
            # fixture is left as the other tests expect it.
            mid = next(m["id"] for m in self.ws.meetings() if m["speaker_count"] == 3)
            self.ws.rerun_speakers(mid, 3)
            self.assertTrue(self.ws.wait_idle(120))
        forget.assert_called()

    def test_the_meeting_view_counts_follow_corrections(self):
        # detail() works its count out on its own path: a merge and its undo
        # must each show, not a count kept from before.
        ws = self.ws
        mid = next(m["id"] for m in ws.meetings() if m["speaker_count"] == 3)
        self.assertEqual(ws.detail(mid)["speaker_count"], 3)
        first, second = speaker_names.resolve(ws.store, ws.store.active_version(mid)["id"])[
            "order"][:2]
        ws.merge_speakers(mid, second, first)
        try:
            self.assertEqual(ws.detail(mid)["speaker_count"], 2)
        finally:
            ws.undo_speakers(mid)                    # the shared fixture as it was
        self.assertEqual(ws.detail(mid)["speaker_count"], 3)

    def test_a_fixed_number_of_queries_however_many_meetings(self):
        self.ws.meetings()                               # speaker counts worked out
        six = self.statements()
        self.assertLessEqual(six, 9)                     # was about 9 per meeting
        mids = [self.ws.store.create_meeting(f"Extra {i}") for i in range(4)]
        try:
            self.assertEqual(self.statements(), six)
        finally:
            with self.ws.store.tx() as db:            # with their keys (#111 C2b-2)
                for m in mids:
                    self.ws.store.delete_meeting_row(db, m)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class DisplayCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = Workspace(root / "ws", root / "models", start=False, battery=False,
                            allow_test_engines=True, policy_reader=lambda: ({}, ()),
                            protector=keyprotect.TestProtector())
        self.now = [100.0]
        self.ws._clock = lambda: self.now[0]

    def tearDown(self):
        self.ws.close()
        self.tmp.cleanup()

    def polled(self, times=5, first=None):
        """Settings and Setup as the page polls them while they are open,
        after `first` (a change made from the page)."""
        from workspace import credentials, gpu, setup, update
        card = {"name": "x", "role": "speech", "state": "ready"}
        with mock.patch.object(gpu, "detected", return_value={"name": "Synthetic GPU",
                                                              "driver_ok": True}), \
                mock.patch.object(gpu, "status", return_value=("ready", "ok")) as status, \
                mock.patch.object(setup, "cards", side_effect=lambda d: [dict(card)]) as cards, \
                mock.patch.object(update, "blockers", return_value=[]) as blockers, \
                mock.patch.object(credentials, "has_token", return_value=True) as has:
            if first:
                first()
            for _ in range(times):
                self.ws.get_settings()
                s = self.ws.setup_state()
        return {"status": status.call_count, "cards": cards.call_count,
                "blockers": blockers.call_count, "token": has.call_count, "state": s}

    def test_disk_and_credential_checks_are_kept_between_polls(self):
        calls = self.polled()
        self.assertEqual({k: calls[k] for k in ("status", "cards", "blockers", "token")},
                         {"status": 1, "cards": 1, "blockers": 1, "token": 1})
        self.assertTrue(calls["state"]["token_remembered"])
        self.assertIsNone(calls["state"]["cards"][0]["download"])    # live, not kept

    def test_checked_again_after_the_cache_period(self):
        from workspace import app
        self.polled(1)
        self.now[0] += app.DISPLAY_CACHE_S + 1
        calls = self.polled(1)
        self.assertEqual((calls["status"], calls["cards"], calls["token"]), (1, 1, 1))

    def test_checked_again_at_once_after_a_change(self):
        for change in (self.ws._after_download,                   # a download or check ended
                       lambda: self.ws.set_settings({"speakers": False}),
                       self.ws._forget_display):
            self.polled(1)
            calls = self.polled(2, first=change)
            self.assertEqual((calls["status"], calls["cards"], calls["token"]), (1, 1, 1))

    def test_removing_the_token_or_a_model_checks_again(self):
        from workspace import credentials, setup
        self.polled(1)
        with mock.patch.object(credentials, "remove_token", return_value=True):
            self.ws.remove_token()
        self.assertEqual(self.polled(1)["token"], 1)
        with mock.patch.object(self.ws, "_check_removable"), \
                mock.patch.object(setup, "remove", return_value=0), \
                mock.patch.object(setup, "known", return_value=True):
            self.ws.remove_model("parakeet-tdt-0.6b-v2")
        self.assertEqual(self.polled(1)["cards"], 1)

    def test_update_blockers_follow_the_database(self):
        self.polled(1)
        self.ws.store.create_meeting("Synthetic")            # a commit: may block an update
        self.assertEqual(self.polled(1)["blockers"], 1)

    def test_update_blockers_follow_unimported_recordings(self):
        # A recording waiting to be imported blocks an update too; starting or
        # discarding one commits nothing.
        self.polled(1)
        (self.ws.recordings / "20261016-090000-synthetic").mkdir(parents=True)
        self.assertEqual(self.polled(1)["blockers"], 1)

    def test_status_reports_a_change_whenever_the_lists_could_change(self):
        ws = self.ws
        seen = ws.status()["changes"]
        self.assertEqual(ws.status()["changes"], seen)            # nothing happened
        self.assertIsInstance(seen, int)                         # opaque: no ids or names
        steps = (lambda: ws.store.create_meeting("Synthetic"),  # any database commit
                 lambda: setattr(ws, "_busy", ("m" * 32, "transcribe")),
                 lambda: setattr(ws, "_busy", None),
                 lambda: ws._reruns.update({"m" * 32: None}),     # Identify again asked for
                 lambda: ws.settings.update(mode="faster"))      # the list shows the mode
        for step in steps:
            step()
            now = ws.status()["changes"]
            self.assertNotEqual(now, seen)
            seen = now

    def test_a_value_worked_out_while_forgotten_is_not_kept(self):
        # A download finishing while the component is being checked: the
        # result may predate the new files, so it is not kept.
        def compute():
            self.ws._forget_display()
            return "old"
        self.assertEqual(self.ws._display_value("gpu", compute), "old")
        self.assertEqual(self.ws._display_value("gpu", lambda: "new"), "new")


if __name__ == "__main__":
    unittest.main()
