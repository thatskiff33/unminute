# The recording level meter's frame rate (#216): it draws about 60 times a
# second on whole display frames (every Nth frame, N = round(display rate / 60),
# at least 1), and its easing is time-based so the motion does not depend on
# how often it draws. About 60 a second is the owner's decision of 23 Oct 2026,
# after a short spell at 30 (#315) that saved too little. The page's own pure
# helpers (meterStep, meterFrame, easeShare, ease, meterDt, restartMeter) and
# constants (METER_HZ, EASE_REF_HZ) run in Node.js with the page's real
# scripts loaded (tests/page_scripts.py run; the CI images have Node.js, as
# the JS syntax job uses).
#   python tools/run_tests.py

import json
import re
import unittest
import page_scripts

APP_JS = page_scripts.source()


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
const rates = [24, 30, 48, 60, 75, 90, 120, 144, 150, 165, 240];
const result = { meter_hz: METER_HZ, ease_ref_hz: EASE_REF_HZ, rates: {}, dropped: run(120, 4, 30),
                 steps: {}, ease: {} };
for (const hz of rates) result.rates[hz] = run(hz, 4, 0);
for (const hz of rates) result.steps[hz] = meterStep(hz, 0);
// Near the half-way point between two steps, the current step is kept.
result.steps.wobble_up = meterStep(91, 1);
result.steps.wobble_down = meterStep(89, 2);
result.steps.wobble_150_down = meterStep(149, 3);
result.steps.wobble_150_up = meterStep(151, 2);
for (const r of [0.3, 0.08, 0.25, 0.35, 0.1]) {
  const once = ease(0, 1, r, 1 / 60);
  const twice = ease(ease(0, 1, r, 1 / 120), 1, r, 1 / 120);
  // One draw at 30 a second (a 30 Hz display) moves as far as four at 120.
  let four = 0;
  for (let k = 0; k < 4; k++) four = ease(four, 1, r, 1 / 120);
  result.ease[r] = { at_ref: easeShare(r, 1 / EASE_REF_HZ), once, twice,
                     at_30: ease(0, 1, r, 1 / 30), four,
                     long_gap: ease(0, 1, r, 0.1) };
}
// The display changes mid-run (the window moved to another monitor, or dynamic
// refresh): 120, 60, 144, 30, then 240 Hz, two seconds each. Draw gaps after
// each segment's first half second.
{
  const clock = { prev: 0, frameMs: 0, step: 0, count: 0 };
  let t = 1000, i = 0;
  result.switched = [];
  for (const hz of [120, 60, 144, 30, 240]) {
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
    out = None

    def simulate(self):
        if LevelMeterTests.out is None:                   # one run serves every test here
            LevelMeterTests.out = page_scripts.run(SIMULATION)
        return json.loads(json.dumps(LevelMeterTests.out))

    def test_draws_on_every_nth_display_frame_and_never_unevenly(self):
        result = self.simulate()
        # The owner's decision (23 Oct 2026): about 60 a second.
        self.assertEqual(result["meter_hz"], 60)
        # Every frame at 75 Hz or lower, every 2nd at 120 and 144 Hz, 3rd at
        # 165, 4th at 240.
        expected = {24: 1, 30: 1, 48: 1, 60: 1, 75: 1, 120: 2, 144: 2, 165: 3, 240: 4}
        for hz, step in expected.items():
            with self.subTest(hz=hz):
                self.assertEqual(result["steps"][str(hz)], step)
                run = result["rates"][str(hz)]
                self.assertEqual(run["gaps"], [step], "draws must be evenly spaced")
                self.assertAlmostEqual(run["per_second"], hz / step, delta=1)
        # 90 and 150 Hz are exactly half-way (1.5, 2.5): whichever step each
        # settles on, it keeps, and it is one of the two nearest.
        for hz, steps in ((90, (1, 2)), (150, (2, 3))):
            with self.subTest(hz=hz):
                run = result["rates"][str(hz)]
                self.assertEqual(len(run["gaps"]), 1, "draws must be evenly spaced")
                self.assertIn(run["gaps"][0], steps)
                self.assertEqual(run["step"], run["gaps"][0])
                self.assertAlmostEqual(run["per_second"], hz / run["step"], delta=1)
        # About 60 a second on 60, 120 and 240 Hz displays; never faster than
        # 90 (just below 90 Hz, every frame) and never below 45 on any display
        # faster than 45 Hz (at worst 90 Hz / 2).
        for hz in ("60", "120", "240"):
            self.assertAlmostEqual(result["rates"][hz]["per_second"], 60, delta=1)
        for hz, run in result["rates"].items():
            with self.subTest(hz=hz):
                self.assertLessEqual(run["per_second"], 91)
                self.assertGreaterEqual(run["per_second"], min(int(hz), 45) - 1)

    def test_a_dropped_frame_does_not_change_the_step(self):
        result = self.simulate()
        self.assertEqual(result["dropped"]["step"], 2)
        self.assertEqual(result["dropped"]["gaps"], [2])

    def test_a_rate_wobbling_at_the_half_way_point_keeps_its_step(self):
        result = self.simulate()
        self.assertEqual(result["steps"]["wobble_up"], 1)
        self.assertEqual(result["steps"]["wobble_down"], 2)
        self.assertEqual(result["steps"]["wobble_150_down"], 3)
        self.assertEqual(result["steps"]["wobble_150_up"], 2)

    def test_easing_is_time_based_and_matches_the_tuned_per_frame_rates(self):
        result = self.simulate()
        # The rates were tuned, and the motion approved, at 120 frames a second.
        self.assertEqual(result["ease_ref_hz"], 120)
        for rate, got in result["ease"].items():
            with self.subTest(rate=rate):
                self.assertAlmostEqual(got["at_ref"], float(rate), places=12)
                self.assertAlmostEqual(got["once"], got["twice"], places=12)
                # Drawing less often does not change the motion's speed.
                self.assertAlmostEqual(got["at_30"], got["four"], places=12)
                self.assertLess(got["long_gap"], 1.0)

    def test_a_display_rate_change_settles_on_the_new_step_evenly(self):
        result = self.simulate()
        expected = {120: 2, 60: 1, 144: 2, 30: 1, 240: 4}
        self.assertEqual([seg["hz"] for seg in result["switched"]], [120, 60, 144, 30, 240])
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
        stop = page_scripts.function_text("stopViz")
        self.assertIn("restartMeter(viz, meterClock);", stop)
        [handler] = page_scripts.listener_texts("document", "visibilitychange")
        hidden = re.search(r'^\(\) => \{\n'
                           r'  if \(document\.hidden\) \{\n(.*?)\n  \} else', handler, re.S)
        self.assertIsNotNone(hidden, "the visibilitychange handler was not found")
        self.assertIn("restartMeter(viz, meterClock);", hidden.group(1))

    def test_the_meter_uses_the_helpers(self):
        draw = page_scripts.function_text("drawViz")
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
