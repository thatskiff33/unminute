# The recording level meter's frame rate (#216): it draws about 60 times a
# second on whole display frames (every Nth frame, N = round(display rate / 60),
# at least 1), and its easing is time-based so the motion does not depend on
# how often it draws. The pure helpers are taken from workspace/ui/app.js by
# name and run in Node.js (the CI images have it, as the JS syntax job uses).
#   python tools/run_tests.py

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "workspace" / "ui" / "app.js").read_text(encoding="utf-8")
HELPERS = ("meterStep", "meterFrame", "easeShare", "ease", "meterDt", "restartMeter")
CONSTANTS = ("METER_HZ", "EASE_REF_HZ")


def source_of(name):
    """A top-level function or constant from app.js, as written there."""
    if name in CONSTANTS:
        found = re.search(r"^const " + name + r" = [^;\n]+;$", APP_JS, re.M)
    else:
        found = (re.search(r"^function " + name + r"\(.*\}$", APP_JS, re.M)          # one line
                 if re.search(r"^function " + name + r"\(.*\}$", APP_JS, re.M)
                 else re.search(r"^function " + name + r"\(.*?^\}$", APP_JS, re.M | re.S))
    if found is None:
        raise AssertionError(f"{name} not found at the top level of app.js")
    return found.group(0)


# Each check runs in Node.js against the helpers and prints one JSON result.
SIMULATION = r"""
// Frame times for `hz` over `seconds`, with a fixed small wobble and, if asked,
// one dropped frame (a doubled gap) every `dropEvery` frames.
function frames(hz, seconds, dropEvery) {
  const out = []; let t = 1000;
  for (let i = 0; i < hz * seconds; i++) {
    const wobble = [0, 0.2, -0.15, 0.1, -0.2][i % 5];
    t += 1000 / hz + wobble + (dropEvery && i % dropEvery === dropEvery - 1 ? 1000 / hz : 0);
    out.push(t);
  }
  return out;
}
function run(hz, seconds, dropEvery) {
  const clock = { prev: 0, frameMs: 0, step: 0, count: 0 };
  const drawn = [];
  frames(hz, seconds, dropEvery).forEach((t, i) => { if (meterFrame(clock, t)) drawn.push(i); });
  const settled = drawn.filter((i) => i >= hz / 2);         // after half a second
  const gaps = new Set(settled.slice(1).map((v, k) => v - settled[k]));
  const times = frames(hz, seconds, dropEvery);
  const span = (times[settled[settled.length - 1]] - times[settled[0]]) / 1000;
  return { gaps: [...gaps], per_second: (settled.length - 1) / span, step: clock.step };
}
const rates = [30, 60, 75, 90, 120, 144, 165, 240];
const result = { meter_hz: METER_HZ, ease_ref_hz: EASE_REF_HZ, rates: {}, dropped: run(120, 4, 30),
                 steps: {}, ease: {} };
for (const hz of rates) result.rates[hz] = run(hz, 4, 0);
for (const hz of rates) result.steps[hz] = meterStep(hz, 0);
// Near the half-way point between two steps, the current step is kept.
result.steps.wobble_up = meterStep(91, 1);
result.steps.wobble_down = meterStep(89, 2);
for (const r of [0.3, 0.08, 0.25, 0.35, 0.1]) {
  const once = ease(0, 1, r, 1 / 60);
  const twice = ease(ease(0, 1, r, 1 / 120), 1, r, 1 / 120);
  result.ease[r] = { at_ref: easeShare(r, 1 / EASE_REF_HZ), once, twice,
                     long_gap: ease(0, 1, r, 0.1) };
}
// The display changes mid-run (the window moved to another monitor): 120, then
// 60, then 144 Hz, two seconds each. Draw gaps after each segment's first half second.
{
  const clock = { prev: 0, frameMs: 0, step: 0, count: 0 };
  let t = 1000, i = 0;
  result.switched = [];
  for (const hz of [120, 60, 144]) {
    const drawn = [];
    for (let k = 0; k < hz * 2; k++, i++) {
      t += 1000 / hz + [0, 0.2, -0.15, 0.1, -0.2][i % 5];
      if (meterFrame(clock, t) && k >= hz / 2) drawn.push(k);
    }
    result.switched.push({ hz, step: clock.step,
                           gaps: [...new Set(drawn.slice(1).map((v, k) => v - drawn[k]))] });
  }
}
// Drawing at 60 a second, then the meter stops (or the window is minimised)
// for five seconds and starts again: the first draw after must not jump.
{
  const v = { last: 0 }, clock = { prev: 0, frameMs: 0, step: 0, count: 0 };
  let t = 1000, dt = 0;
  for (let k = 0; k < 120; k++) { t += 1000 / 120; if (meterFrame(clock, t)) dt = meterDt(v, t); }
  const running = dt;
  restartMeter(v, clock);
  t += 5000;
  let after = null;
  for (let k = 0; k < 4 && after === null; k++) {
    t += 1000 / 120;
    if (meterFrame(clock, t)) after = meterDt(v, t);
  }
  result.restart = { running, after, share: easeShare(0.3, after), step: clock.step };
}
console.log(JSON.stringify(result));
"""


class LevelMeterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")

    def simulate(self):
        if not self.node:
            self.skipTest("Node.js not installed")
        script = "\n".join(source_of(n) for n in CONSTANTS + HELPERS) + "\n" + SIMULATION
        done = subprocess.run([self.node, "-"], input=script, capture_output=True, text=True,
                              timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_draws_on_every_nth_display_frame_and_never_unevenly(self):
        result = self.simulate()
        self.assertEqual(result["meter_hz"], 60)
        expected = {30: 1, 60: 1, 75: 1, 120: 2, 144: 2, 165: 3, 240: 4}
        for hz, step in expected.items():
            with self.subTest(hz=hz):
                self.assertEqual(result["steps"][str(hz)], step)
                run = result["rates"][str(hz)]
                self.assertEqual(run["gaps"], [step], "draws must be evenly spaced")
                self.assertAlmostEqual(run["per_second"], hz / step, delta=1)
        # 90 Hz is exactly half-way (1.5): whichever step it settles on, it keeps.
        self.assertEqual(len(result["rates"]["90"]["gaps"]), 1)
        self.assertLessEqual(result["rates"]["120"]["per_second"], 61)

    def test_a_dropped_frame_does_not_change_the_step(self):
        result = self.simulate()
        self.assertEqual(result["dropped"]["step"], 2)
        self.assertEqual(result["dropped"]["gaps"], [2])

    def test_a_rate_wobbling_at_the_half_way_point_keeps_its_step(self):
        result = self.simulate()
        self.assertEqual(result["steps"]["wobble_up"], 1)
        self.assertEqual(result["steps"]["wobble_down"], 2)

    def test_easing_is_time_based_and_matches_the_tuned_per_frame_rates(self):
        result = self.simulate()
        # The rates were tuned, and the motion approved, at 120 frames a second.
        self.assertEqual(result["ease_ref_hz"], 120)
        for rate, got in result["ease"].items():
            with self.subTest(rate=rate):
                self.assertAlmostEqual(got["at_ref"], float(rate), places=12)
                self.assertAlmostEqual(got["once"], got["twice"], places=12)
                self.assertLess(got["long_gap"], 1.0)

    def test_a_display_rate_change_settles_on_the_new_step_evenly(self):
        result = self.simulate()
        expected = {120: 2, 60: 1, 144: 2}
        for seg in result["switched"]:
            with self.subTest(hz=seg["hz"]):
                self.assertEqual(seg["step"], expected[seg["hz"]])
                self.assertEqual(seg["gaps"], [expected[seg["hz"]]], "draws must be evenly spaced")

    def test_the_first_draw_after_a_stop_or_minimise_does_not_jump(self):
        result = self.simulate()["restart"]
        self.assertAlmostEqual(result["running"], 1 / 60, delta=0.001)
        # One nominal frame's easing (as on the very first draw), not 0.1 s worth.
        self.assertAlmostEqual(result["after"], 0.016, places=6)
        self.assertLess(result["share"], 0.6)
        self.assertEqual(result["step"], 2, "the measured display rate is kept")
        # The page restarts the meter when it stops and when the window is hidden.
        stop = source_of("stopViz")
        self.assertIn("restartMeter(viz, meterClock);", stop)
        hidden = re.search(r'addEventListener\("visibilitychange", \(\) => \{\n'
                           r'  if \(document\.hidden\) \{\n(.*?)\n  \} else', APP_JS, re.S)
        self.assertIsNotNone(hidden, "the visibilitychange handler was not found")
        self.assertIn("restartMeter(viz, meterClock);", hidden.group(1))

    def test_the_meter_uses_the_helpers(self):
        draw = source_of("drawViz")
        self.assertIn("requestAnimationFrame(drawViz)", draw)
        # The next frame is asked for first, then frames not to be drawn end there.
        self.assertLess(draw.index("requestAnimationFrame(drawViz)"),
                        draw.index("meterFrame(meterClock, t)"))
        self.assertLess(draw.index("meterFrame(meterClock, t)"), draw.index("getContext"))
        self.assertIn("const dt = meterDt(viz, t);", draw)
        calls = re.findall(r"\bease\(([^;]*)\);", APP_JS)
        self.assertGreaterEqual(len(calls), 6)
        for args in calls:
            self.assertTrue(args.rstrip(")").endswith(", dt"), f"ease({args}) without dt")


if __name__ == "__main__":
    unittest.main()
