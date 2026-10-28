# tests/test_you_scenario.py
# The two-track scenario of the real-window accessibility check (#475), in
# tools/accessibility_check.py:
#   - seal_you_activity makes a throwaway workspace's meeting a two-track one:
#     it seals a synthetic track-activity file under the meeting's audio key,
#     from its speakers' passages, and the Name speakers panel then suggests
#     the first speaker as "You", through the app's own speaker_panel, with
#     a More examples button for YOU_FOCUS to press (#479); a meeting without
#     identified speakers stops the run;
#   - the view sits in VIEWS right after the names dialog, and release
#     evidence requires it (tools/evidence.py, ACCESSIBILITY_VIEWS);
#   - the verdicts (you_findings, you_focus_problems) pass a good answer and
#     fail on each broken point: the row not shown, the row left after Use
#     'You', focus not on the name box or the paging button, outside the
#     dialog, not :focus-visible, or not measured.
# YOU_FOCUS itself runs on the real page in headless Edge in
# tests/test_you_suggestion_page.py. Synthetic data only (P1, P2): invented
# passages, levels and flags; no audio, window or model.
#   python tools/run_tests.py --only test_you_scenario

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import accessibility_check as ac  # noqa: E402
import evidence  # noqa: E402
import keyprotect  # noqa: E402

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
RATE = 16000
SECONDS = 60
# Speakers alternate every 10 s, a 3 s passage every 4 s, and SPEAKER_01 talks
# over SPEAKER_00 from 25 s to 27 s (as the check's fake engines give,
# tests/fake_worker_engines.py: sentences and fake_diarizer): SPEAKER_00 has
# about 21 s of passages without overlap, SPEAKER_01 about 21 s.
PASSAGES = [(t + 0.5, t + 3.5) for t in range(0, SECONDS - 4, 4)]
EXCLUSIVE = [(float(t), float(t + 10), "SPEAKER_00" if (t // 10) % 2 == 0 else "SPEAKER_01")
             for t in range(0, SECONDS, 10)]
TURNS = EXCLUSIVE + [(25.0, 27.0, "SPEAKER_01")]


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class SealYouActivityTests(unittest.TestCase):
    def setUp(self):
        from workspace.app import Workspace
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ws = Workspace(root / "ws", root / "models", start=False, allow_test_engines=True,
                            battery=False, policy_reader=lambda: ({}, ()),
                            protector=keyprotect.TestProtector())
        self.addCleanup(self.ws.close)

    def meeting(self, speakers=True):
        from workspace.asr_engines import Segment
        store = self.ws.store
        mid = store.create_meeting("Synthetic call")
        sid = store.add_source(mid, original_path=None, original_sha256="a" * 64,
                               original_size=1, original_mtime_ns=0, audio_id="b" * 64,
                               samples=SECONDS * RATE, sample_rate=RATE)
        vid = store.ensure_version(mid, sid, engine="fake", model="fake", model_revision="1",
                                   precision="int8", config={},
                                   chunk_plan=[(0, SECONDS * RATE, 0, SECONDS * RATE)])
        store.commit_chunk(vid, 0, [Segment(a, b, f"passage {a}") for a, b in PASSAGES])
        if speakers:
            rid = store.start_speaker_run(vid)
            self.assertTrue(store.finish_speaker_run(rid, TURNS, EXCLUSIVE))
        return mid

    def test_the_first_speaker_is_suggested_through_the_apps_panel(self):
        from workspace import activity, media
        mid = self.meeting()
        self.assertIsNone(self.ws.speaker_panel(mid)["you"])     # one track: nothing yet
        self.assertEqual("SPEAKER_00", ac.seal_you_activity(self.ws, mid))
        panel = self.ws.speaker_panel(mid)
        self.assertEqual("SPEAKER_00", panel["you"])
        self.assertEqual(["Speaker 1", "Speaker 2"], [sp["name"] for sp in panel["speakers"]])
        self.assertFalse(panel["can_undo"])                    # nothing was named
        # The file is the app's own format, sealed under the meeting's audio key.
        folder = media.meeting_folder(self.ws.media, mid)
        flag = activity.read(folder, self.ws.store.meeting_key(mid, "audio"), mid)[3]
        self.assertGreater(int(((flag & activity.OWN) > 0).sum()) * activity.HOP_S,
                           activity.YOU_MIN_S)

    def test_the_suggested_speaker_has_a_paging_button(self):
        # YOU_FOCUS presses that speaker's More examples, then First examples
        # (#479): the panel offers More examples only past the first page of 3.
        from workspace import speaker_names
        mid = self.meeting()
        you = ac.seal_you_activity(self.ws, mid)
        vid = self.ws.store.active_version(mid)["id"]
        overlap = speaker_names.resolve(self.ws.store, vid)["overlap"]
        self.assertEqual(1, sum(overlap.values()), "the overlap turn marked no passage")
        sp = next(s for s in self.ws.speaker_panel(mid)["speakers"] if s["label"] == you)
        self.assertGreater(sp["examples"]["total"], 3, sp["examples"]["total"])

    def test_a_meeting_without_identified_speakers_stops_the_run(self):
        mid = self.meeting(speakers=False)
        with self.assertRaises(RuntimeError):
            ac.seal_you_activity(self.ws, mid)


class ViewTests(unittest.TestCase):
    def test_the_view_follows_the_names_dialog_and_release_evidence_needs_it(self):
        names = list(ac.VIEWS)
        self.assertEqual(names.index("names dialog") + 1, names.index(ac.YOU_VIEW))
        self.assertIn(ac.YOU_VIEW, evidence.ACCESSIBILITY_VIEWS)
        self.assertEqual(evidence.ACCESSIBILITY_VIEWS, tuple(ac.VIEWS))

    def test_the_view_reopens_the_panel_and_waits_for_the_suggestion(self):
        go = ac.VIEWS[ac.YOU_VIEW]
        self.assertIn("getElementById('names').close()", go)
        self.assertIn("getElementById('m-names').click()", go)
        self.assertIn(".n-you", go)
        self.assertIn(str(ac.YOU_WAIT_MS), go)


GOOD_FOCUS = {"window_focused": True, "in_dialog": True, "focus_visible": True}
GOOD = {
    "row_before": True,
    "use_you": {**GOOD_FOCUS, "on": "input", "on_name_box": True, "is_you": True,
                "paging": False, "speaker_is_you": True, "row_after": False},
    "paging": [{**GOOD_FOCUS, "pressed": "More examples", "on": "button.n-more",
                "on_name_box": False, "is_you": False, "paging": True, "speaker_is_you": True},
               {**GOOD_FOCUS, "pressed": "First examples", "on": "button.n-more",
                "on_name_box": False, "is_you": False, "paging": True, "speaker_is_you": True}],
}
ROW = {"shown": True, "in_dialog": True, "button": True}


def report(row=ROW, focus=GOOD, modes=("light", "dark")):
    views = {f"{ac.YOU_VIEW} ({m})": {"colour_mode": m, "you_row": row} for m in modes}
    return {"views": views, "you_focus": focus}


def with_(focus, part, **change):
    out = {**focus, "use_you": dict(focus["use_you"]), "paging": [dict(p) for p in focus["paging"]]}
    if part == "use_you":
        out["use_you"].update(change)
    else:
        out["paging"][part].update(change)
    return out


class VerdictTests(unittest.TestCase):
    def test_a_good_answer_passes(self):
        self.assertEqual([], ac.you_focus_problems(GOOD))
        self.assertEqual([], ac.you_findings(report()))

    def test_a_report_without_the_view_has_none_of_its_findings(self):
        self.assertEqual([], ac.you_findings({"views": {"names dialog (light)": {}}}))

    def test_the_view_fails_when_the_suggestion_was_not_shown(self):
        for row in ({}, {**ROW, "shown": False}, {**ROW, "in_dialog": False},
                    {**ROW, "button": False}, None):
            with self.subTest(row=row):
                found = ac.you_findings(report(row=row))
                self.assertEqual(2, len(found), found)           # light and dark
                self.assertTrue(all("not shown" in f for f in found), found)

    def test_focus_not_checked_fails(self):
        for focus in (None, {}, {"row_before": False}):
            with self.subTest(focus=focus):
                found = ac.you_findings(report(focus=focus))
                self.assertEqual(1, len(found), found)
                self.assertIn("not checked", found[0])

    def test_each_broken_point_fails(self):
        cases = {
            "the suggestion is still shown": with_(GOOD, "use_you", row_after=True),
            "not on the suggested speaker's name box": with_(GOOD, "use_you", on="page",
                                                             on_name_box=False, is_you=False,
                                                             in_dialog=False),
            "another box": with_(GOOD, "use_you", is_you=False),
            "left the names dialog": with_(GOOD, "use_you", in_dialog=False),
            "does not match :focus-visible": with_(GOOD, "use_you", focus_visible=False),
            "lost keyboard focus": with_(GOOD, "use_you", window_focused=False),
            "not on that speaker's paging button": with_(GOOD, 0, on="page", paging=False,
                                                         speaker_is_you=False, in_dialog=False),
            "another speaker's button": with_(GOOD, 1, speaker_is_you=False),
            "paging not :focus-visible": with_(GOOD, 1, focus_visible=False),
            "no More examples": {**GOOD, "paging": []},
        }
        for name, focus in cases.items():
            with self.subTest(name):
                found = ac.you_focus_problems(focus)
                self.assertTrue(found, f"{name}: passed")
                self.assertEqual(found, [f.split(": ", 1)[1] for f in
                                         ac.you_findings(report(focus=focus))
                                         if f.startswith(ac.YOU_VIEW + ": ")])

    def test_its_findings_fail_the_whole_report(self):
        broken = report(focus=with_(GOOD, "use_you", on_name_box=False))
        self.assertTrue(any(f.startswith(ac.YOU_VIEW + ": ") for f in ac.findings(broken)))


if __name__ == "__main__":
    unittest.main()
