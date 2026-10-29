# tests/test_event_text.py
# How the page words a recording's events (#462, the owner's look of 28 Oct
# 2026): an event's note ends in a full stop, and the page adds when it
# happened and how long it lasted after it. The note's own full stop must not
# come before " at": the card's line reads "...switched to it at 8:32." and
# "...while the microphone switched at 8:32 (about 0.4 s).", and a report
# joins its events with "; " and one full stop at the end. The page's own
# eventLine and eventText run in Node.js with the page's real scripts loaded
# (tests/page_scripts.py run; the CI images have Node.js). The notes are the
# recorder's own fixed texts (workspace/recorder.py), which name no device (P3).
#   python tools/run_tests.py --only test_event_text

import json
import re
import sys
import unittest
from pathlib import Path
import page_scripts

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The recorder's notes, read from its source so the test follows any rewording
# without importing the recorder (numpy) here.
RECORDER = (ROOT / "workspace" / "recorder.py").read_text(encoding="utf-8")
SWITCHED = re.search(r'"mic_switched": "([^"]+)"', RECORDER).group(1)
GAP = re.search(r'SWITCH_GAP_NOTE = "([^"]+)"', RECORDER).group(1)


def joined(match):
    """A note written across several string literals, as one string."""
    return "".join(re.findall(r'"([^"]*)"', match.group(1)))


SLEEP = joined(re.search(r'self\._event\("sleep", note=((?:\s*"[^"]*")+)\)', RECORDER))
MIXDOWN = (ROOT / "workspace" / "mixdown.py").read_text(encoding="utf-8")
MIX_BY_START = joined(re.search(r'"kind": "mix_by_start".*?"note": ((?:\s*"[^"]*")+)\}',
                                MIXDOWN, re.S))


# 8:32 is 512 seconds into the recording, as the design check's
# recording-switched case (tools/design_check/fixtures.js).
EVENTS = [
    {"kind": "mic_switched", "at_s": 512, "seconds": None, "note": SWITCHED},
    {"kind": "gap", "at_s": 512, "seconds": 0.4, "note": GAP},
    {"kind": "gap", "at_s": 75, "seconds": 3.2, "note": "No audio came from the microphone here."},
    {"kind": "paused", "at_s": 60, "seconds": None, "note": ""},
    {"kind": "time_limit", "at_s": None, "seconds": None, "note": "Stopped after 8 hours."},
    # Nit 2 of the review of 325a564: notes of several sentences, and the mix's.
    {"kind": "sleep", "at_s": 512, "seconds": None, "note": SLEEP},
    {"kind": "write_failed", "at_s": 512, "seconds": 3.2,
     "note": "The recording could not be saved any further: the disk is full or not "
             "writable. Both tracks stopped."},
    {"kind": "mix_by_start", "at_s": 0.0, "seconds": None, "note": MIX_BY_START},
]

SCRIPT = r"""
const events = __EVENTS__;
console.log(JSON.stringify({
  lines: events.map(eventLine),
  texts: events.map(eventText),
  report: ` ${events.slice(0, 2).map(eventText).join("; ")}.`,
}));
"""


class EventTextTests(unittest.TestCase):
    out = None

    def run_page(self):
        if EventTextTests.out is None:                    # one run serves every test here
            EventTextTests.out = page_scripts.run(SCRIPT.replace("__EVENTS__",
                                                                 json.dumps(EVENTS)))
        return json.loads(json.dumps(EventTextTests.out))

    def test_the_card_lines_read_as_the_owner_approved(self):
        lines = self.run_page()["lines"]
        self.assertEqual(lines[0], "Windows changed its default microphone, so the recording "
                                   "switched to it at 8:32.")
        self.assertEqual(lines[1], "Silence was recorded here while the microphone switched "
                                   "at 8:32 (about 0.4 s).")

    def test_no_full_stop_comes_before_the_time_and_each_line_ends_in_one(self):
        result = self.run_page()
        self.assertEqual(result["lines"][2],
                         "No audio came from the microphone here at 1:15 (about 3 s).")
        self.assertEqual(result["lines"][3], "paused at 1:00.")       # no note: its kind
        self.assertEqual(result["lines"][4], "Stopped after 8 hours.")  # no time: one stop
        for line in result["lines"]:
            self.assertNotIn(". at ", line)
            self.assertNotIn("..", line)
            self.assertTrue(line.endswith("."), line)

    def test_the_time_follows_the_first_sentence_and_the_mix_has_none(self):
        # Nit 2 of the review of 325a564: after the last sentence the time
        # read as the time something was kept ("...what was recorded before
        # that is kept at 8:32."), and the mix's notes ended "at 0:00".
        lines = self.run_page()["lines"]
        self.assertEqual(lines[5], "The computer went to sleep, for example because the lid "
                                   "was closed at 8:32. Recording stopped; what was recorded "
                                   "before that is kept.")
        self.assertEqual(lines[6], "The recording could not be saved any further: the disk is "
                                   "full or not writable at 8:32 (about 3 s). Both tracks "
                                   "stopped.")
        self.assertEqual(lines[7], "The microphone and computer audio were lined up by when "
                                   "they started only; they may be slightly out of step.")

    def test_a_report_joins_its_events_with_one_full_stop_at_the_end(self):
        report = self.run_page()["report"]
        self.assertEqual(report, " Windows changed its default microphone, so the recording "
                                 "switched to it at 8:32; Silence was recorded here while the "
                                 "microphone switched at 8:32 (about 0.4 s).")


if __name__ == "__main__":
    unittest.main()
