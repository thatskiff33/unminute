# The recording level meter's frame rate (#216): it draws about 60 times a
# second on whole display frames (every Nth frame, N = round(display rate / 60),
# at least 1), and its easing is time-based so the motion does not depend on
# how often it draws. Each frame is cheap: the layout is read only after a
# resize, the flow curves are cubics through one point every few pixels that
# keep the exact shape, and the frame allocates nothing. The pure helpers are
# taken from workspace/ui/app.js by name and run in Node.js (the CI images have
# it, as the JS syntax job uses).
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
# Module-level state the drawing helpers keep between frames.
OBJECTS = ("meter", "flowShape", "spectrumTarget", "wavePeak", "baseline")
# The functions that run on every drawn frame.
PER_FRAME = ("drawViz", "drawFlow", "flowPath", "drawSpectrum", "spectrumHalf", "drawWave",
             "drawBaseline", "fitMeter", "spectrumBands", "waveTargetPeak")


def source_of(name):
    """A top-level function or constant from app.js, as written there."""
    if name.isupper() or name in OBJECTS:
        found = re.search(r"^const " + name + r" = .*?;$", APP_JS, re.M | re.S)
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


# The flow curves, drawn into a context that records the path, against the
# exact curve: y = mid + a * taper(u) * sin(u * kk + ph), u = 4x / w - 2, and
# against the straight lines every 2 pixels they replace.
FLOW_SHAPE = r"""
function record() {
  const segs = []; let at = null;
  return { segs, moveTo(x, y) { at = [x, y]; segs.push(["M", x, y]); },
           lineTo(x, y) { segs.push(["L", at[0], at[1], x, y]); at = [x, y]; },
           bezierCurveTo(a, b, c, d, x, y) {
             segs.push(["C", at[0], at[1], a, b, c, d, x, y]); at = [x, y];
           } };
}
const exact = (x, w, mid, a, kk, ph) => {
  const u = (x / w) * 4 - 2;
  return mid + a * Math.pow(2 / (2 + Math.pow(u, 4)), 2) * Math.sin(u * kk + ph);
};
const out = { step: FLOW_STEP_PX, cases: [] };
const widths = [200, 600, 757, 1200], ks = [1.5, 3, 4.5], freqs = [0.7, 1, 1.35];
for (const w of widths) for (const k of ks) for (const freq of freqs) {
  const mid = 32, a = 29, kk = k * freq * Math.PI, ph = 0.7, g = record();
  flowPath(g, flowPoints(w), mid, a, kk, ph, w);
  let worst = 0, curves = 0, lines = 0, before = 0;
  const first = g.segs[0], last = g.segs[g.segs.length - 1];
  for (const s of g.segs) {
    if (s[0] === "L") lines++;
    if (s[0] !== "C") continue;
    curves++;
    const [, x0, y0, ax, ay, bx, by, x1, y1] = s;
    for (let i = 0; i <= 16; i++) {
      const t = i / 16, m = 1 - t;
      const x = m * m * m * x0 + 3 * m * m * t * ax + 3 * m * t * t * bx + t * t * t * x1;
      const y = m * m * m * y0 + 3 * m * m * t * ay + 3 * m * t * t * by + t * t * t * y1;
      worst = Math.max(worst, Math.abs(y - exact(x, w, mid, a, kk, ph)));
    }
  }
  // The old drawing: straight lines between points every 2 pixels.
  for (let x = 0; x + 2 <= w; x += 2) for (let i = 0; i <= 16; i++) {
    const y0 = exact(x, w, mid, a, kk, ph), y1 = exact(x + 2, w, mid, a, kk, ph);
    const off = y0 + (y1 - y0) * i / 16 - exact(x + 2 * i / 16, w, mid, a, kk, ph);
    before = Math.max(before, Math.abs(off));
  }
  const endX = Math.floor(w / 2) * 2;
  out.cases.push({ w, kk, worst, before, curves, lines, start: first.slice(1), end: last.slice(-2),
                   start_exact: exact(0, w, mid, a, kk, ph),
                   end_x: endX, end_exact: exact(endX, w, mid, a, kk, ph) });
}
// The points are worked out once per width.
out.cached = flowPoints(757) === flowPoints(757) && flowPoints(757).x === flowPoints(757).x;
console.log(JSON.stringify(out));
"""

# fitMeter with a canvas that counts layout reads and size changes.
FIT = r"""
let reads = 0, resizes = 0, cw = 640, ch = 64, pw = 0, ph = 0;
const canvas = {
  get clientWidth() { reads++; return cw; }, get clientHeight() { reads++; return ch; },
  get width() { return pw; }, set width(v) { resizes++; pw = v; },
  get height() { return ph; }, set height(v) { resizes++; ph = v; },
};
const out = {};
for (let i = 0; i < 100; i++) fitMeter(canvas, 1.5);
out.steady = { reads, resizes, w: meter.w, h: meter.h, pw, ph };
meter.stale = true; cw = 700;                    // the ResizeObserver saw a new size
for (let i = 0; i < 50; i++) fitMeter(canvas, 1.5);
out.resized = { reads, resizes, w: meter.w, pw, ph };
meter.stale = true;                              // observed again, same size
fitMeter(canvas, 1.5);
out.same = { reads, resizes };
for (let i = 0; i < 50; i++) fitMeter(canvas, 2);   // moved to a screen at 200%
out.dpr = { reads, resizes, pw, ph };
console.log(JSON.stringify(out));
"""

# The per-block caches give the values the frame used to work out each time.
CACHES = r"""
const a = { bands: [-85, -50, -15, 0, -100], wave: [0.1, -0.4, 0.2] };
const b = { bands: [-20, -20], wave: [0.05, -0.01] };
const one = spectrumBands(a).slice(), again = spectrumBands(a) === spectrumBands(a);
const two = spectrumBands(b).slice();
console.log(JSON.stringify({ one, again, two, none: spectrumBands(null),
  peaks: [waveTargetPeak(a), waveTargetPeak(a), waveTargetPeak(b), waveTargetPeak(null)] }));
"""


class MeterDrawingTests(unittest.TestCase):
    """Each frame is cheap and looks the same (#216, owner's decision of 22 October 2026)."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")

    def run_node(self, names, body):
        if not self.node:
            self.skipTest("Node.js not installed")
        script = "\n".join(source_of(n) for n in names) + "\n" + body
        done = subprocess.run([self.node, "-"], input=script, capture_output=True, text=True,
                              timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_flow_curves_use_fewer_points_and_keep_the_exact_shape(self):
        result = self.run_node(("FLOW_STEP_PX", "flowShape", "flowPoints", "flowPath"), FLOW_SHAPE)
        # At least 3 pixels apart (it was a straight line every 2 pixels).
        self.assertGreaterEqual(result["step"], 3)
        self.assertTrue(result["cached"], "the points must be kept for the width")
        for case in result["cases"]:
            with self.subTest(w=case["w"], kk=round(case["kk"], 2)):
                self.assertEqual(case["lines"], 0)
                self.assertLessEqual(case["curves"], case["w"] / 3 + 1)
                # Closer to the exact curve than the old straight lines, by far.
                self.assertLess(case["worst"], case["before"] / 5)
                if case["w"] >= 600:            # the card's usual widths
                    self.assertLess(case["worst"], 0.02)
                self.assertEqual(case["start"][0], 0)
                self.assertAlmostEqual(case["start"][1], case["start_exact"], places=9)
                self.assertEqual(case["end"][0], case["end_x"], "ends where it always has")
                self.assertAlmostEqual(case["end"][1], case["end_exact"], places=9)

    def test_the_layout_is_read_only_after_a_resize_or_a_pixel_ratio_change(self):
        result = self.run_node(("meter", "fitMeter"), FIT)
        self.assertEqual(result["steady"], {"reads": 2, "resizes": 2, "w": 640, "h": 64,
                                            "pw": 960, "ph": 96})
        self.assertEqual(result["resized"], {"reads": 4, "resizes": 4, "w": 700, "pw": 1050,
                                             "ph": 96})
        self.assertEqual(result["same"], {"reads": 6, "resizes": 4}, "same size: not resized")
        self.assertEqual(result["dpr"], {"reads": 8, "resizes": 6, "pw": 1400, "ph": 128})
        # A ResizeObserver on the canvas marks the size stale; the frame itself
        # reads no layout.
        elements = source_of("meterElements")
        self.assertIn("new ResizeObserver(() => { meter.stale = true; })", elements)
        self.assertIn("meter.observer.observe(meter.canvas);", elements)
        draw = source_of("drawViz")
        for read in ("clientWidth", "clientHeight", "getBoundingClientRect", "offsetWidth",
                     "getComputedStyle", "$("):
            with self.subTest(read=read):
                self.assertNotIn(read, draw)
        self.assertIn("fitMeter(c, dpr);", draw)

    def test_per_block_values_are_worked_out_once_and_match(self):
        result = self.run_node(("spectrumTarget", "spectrumBands", "wavePeak", "waveTargetPeak"),
                               CACHES)
        norm = [max(0.0, min(1.0, (db + 85) / 70)) for db in (-85, -50, -15, 0, -100)]
        for got, want in zip(result["one"], norm, strict=True):
            self.assertAlmostEqual(got, want, places=12)
        self.assertTrue(result["again"])
        self.assertEqual(result["two"], [65 / 70, 65 / 70])
        self.assertIsNone(result["none"])
        self.assertEqual(result["peaks"], [0.4, 0.4, 0.05, 0])

    def test_a_frame_allocates_nothing_and_uses_no_blur_or_filter(self):
        for name in PER_FRAME:
            body = source_of(name)
            # Only a new band count or a new size allocates.
            body = re.sub(r"if \(viz\.(bands|wave)\.length !== n\) "
                          r"viz\.\1 = new Array\(n\)\.fill\(0\);", "", body)
            body = re.sub(r"(?s)if \(baseline\.w !== w.*?\n  \}", "", body)
            for made in (".map(", "Array.from", "new Array", "new Float", ".slice(", ".concat(",
                         "=>", "[[", "{ ...", "new Path2D"):
                with self.subTest(fn=name, made=made):
                    self.assertNotIn(made, body)
        for effect in ("shadowBlur", "shadowColor", ".filter =", "createLinearGradient",
                       "createRadialGradient"):
            self.assertNotIn(effect, APP_JS)
        # The centre line is a shape kept for the size.
        self.assertIn("g.stroke(baseline.path);", source_of("drawBaseline"))


if __name__ == "__main__":
    unittest.main()
