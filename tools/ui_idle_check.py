# tools/ui_idle_check.py
# UI idle behaviour (docs/RELEASE_STANDARD.md, section 3): what the page does
# when nobody is using it, counted rather than timed, so the result is the same
# on any machine. The real page (workspace/ui) runs in headless Microsoft Edge
# against the design check's fake bridge (tools/design_check/fixtures.js) in its
# perf mode, which counts bridge calls per method, timers and meeting-list
# rebuilds over a minute of page time. Edge's virtual time budget lets that
# minute pass in a few seconds of real time. Run once visible and once with the
# page reporting itself hidden, as a minimised window should. A third run
# throws page errors before the bridge is ready, which the page must keep and
# report once it is (#171). A fourth opens a long meeting while it is being
# transcribed and counts what the page fetches and draws as passages arrive,
# with an earlier chunk done again, an edit, speakers and a search on the way;
# the rows shown must then be each passage once, in order (#127). The last two
# open the Meetings page with a recording going (#340), visible and hidden, on a
# simulated 120 Hz display (fixtures.js: headless Edge runs almost no animation
# frames under virtual time), and count the clock and level polls and the level
# meter's draws: about METER_HZ a second while visible (workspace/ui/app.js; the
# owner's decision of 23 Oct 2026, #216), none while hidden, and drawing again
# within a second of being shown.
#
#   python tools/ui_idle_check.py [report.json] [--page-ms 60000]
#
# Exit code 1 when a figure is over its ratchet in tests/budgets.json ("ui_idle",
# "ui_transcribing", "ui_recording"). Lower a ratchet when the figure drops (#127);
# raise one only with a reason in the pull request. Needs Microsoft Edge; no
# Python packages.

import argparse
import http.server
import json
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import design_check  # noqa: E402  (its server and Edge helpers; Pillow is not needed here)

BUDGETS = ROOT / "tests" / "budgets.json"
MODES = ("visible", "hidden")
SCENARIO = "home"            # the Meetings view with the sample meetings, nothing busy
GROW_SCENARIO = "meeting-transcribing-long"   # a long meeting open while transcribing
GROW_PAGE_MS = 90000         # its steps (12 x 4 s) after start-up, with room to spare
RECORDING_SCENARIO = "recording"   # the Meetings view with a recording going
DISPLAY_HZ = 120             # the simulated display: the meter draws every 2nd frame
# The level meter's draws a second may be this share either side of METER_HZ.
METER_TOLERANCE = 0.1
APP_JS = design_check.UI / "app.js"
RESULT = re.compile(r'<pre id="perf-result">(.*?)</pre>', re.DOTALL)
EARLY_RESULT = re.compile(r'<pre id="early-result">(.*?)</pre>', re.DOTALL)
# Ratchets are maxima, except where a smaller value is worse.
AT_LEAST = {"fastest_interval_ms"}


def final_dom(url, budget_ms):
    """Edge's dump of the page's DOM after budget_ms of page time."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        cmd = [str(design_check.edge()), "--headless=new", "--disable-gpu", "--no-first-run",
               "--disable-extensions", f"--user-data-dir={profile}", "--mute-audio",
               "--window-size=1100,760", f"--virtual-time-budget={budget_ms}",
               "--dump-dom", url]
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300)


def run_page(base, mode, page_ms, scenario=SCENARIO, display_hz=None):
    """The page's perf result for one mode, from Edge's dump of the final DOM."""
    # page time: the scenario's start-up steps plus the measured window + up to
    # 60 s for the next list fetch and 11 s for the redraw after a change
    # (visible), or 1 s of catch-up (hidden).
    display = f"&display={display_hz}" if display_hz else ""
    out = final_dom(f"{base}/app/index.html?perf={mode}&ms={page_ms}{display}#{scenario}",
                    page_ms + 90000)
    found = RESULT.search(out.stdout or "")
    if not found:
        ready = 'data-ready="1"' in (out.stdout or "")
        raise RuntimeError(
            f"Edge returned no perf result for {scenario} {mode} (exit {out.returncode}). The page "
            + ("finished its start-up steps, so the measured window did not fit in the page "
               "time budget" if ready else "never finished its start-up steps")
            + f". stderr: {(out.stderr or '').strip()[-500:] or '(empty)'}")
    import html
    return json.loads(html.unescape(found.group(1)))


def run_transcribing(base):
    """What the page fetched and drew while a long meeting was transcribed
    (fixtures.js, transcribing mode)."""
    out = final_dom(f"{base}/app/index.html?perf=transcribing#{GROW_SCENARIO}", GROW_PAGE_MS)
    found = RESULT.search(out.stdout or "")
    if not found:
        raise RuntimeError(f"Edge returned no transcribing result (exit {out.returncode}). "
                           f"stderr: {(out.stderr or '').strip()[-500:] or '(empty)'}")
    import html
    return json.loads(html.unescape(found.group(1)))


def transcribing_problems(result):
    """Reasons the page showed the growing transcript wrongly: a passage
    missing, repeated or out of order, speaker turns wrong, an edit lost or
    drawn over, or a whole redraw where appending was not safe missed (or one
    where it was)."""
    problems = []
    if result.get("page_errors"):
        problems.append(f"transcribing: the page threw: {'; '.join(result['page_errors'][:3])}")
    if not result.get("each_once_in_order"):
        problems.append(f"transcribing: the page shows {result.get('rows_shown')} rows, not each "
                        f"of the {result.get('rows_expected')} passages once and in order")
    if not result.get("turns_right"):
        problems.append("transcribing: speaker turns are marked wrongly")
    if result.get("edit_held_back") is not True:
        problems.append("transcribing: rows were drawn while a passage was being edited")
    held, expected = result.get("held_rows_drawn"), result.get("held_rows_expected")
    if held is None or held != expected:
        problems.append(f"transcribing: {held} of the {expected} passages held back during the "
                        "edit were drawn after it ended, before anything else changed")
    if not result.get("edit_kept"):
        problems.append("transcribing: the edited passage does not show the edit")
    if result.get("redraw_steps") != result.get("expected_redraw_steps"):
        problems.append(f"transcribing: the whole list was redrawn after steps "
                        f"{result.get('redraw_steps')}, not {result.get('expected_redraw_steps')} "
                        "(an earlier chunk done again, speakers identified, a search open)")
    return problems


def run_early(base):
    """What the page reported of the errors fixtures.js threw before the bridge
    was ready (early-error mode)."""
    out = final_dom(f"{base}/app/index.html?early=1#{SCENARIO}", 5000)
    found = EARLY_RESULT.search(out.stdout or "")
    if not found:
        raise RuntimeError(f"Edge returned no early-error result (exit {out.returncode}). "
                           f"stderr: {(out.stderr or '').strip()[-500:] or '(empty)'}")
    import html
    return json.loads(html.unescape(found.group(1)))


def early_problems(result):
    """Reasons the page mishandled the two errors thrown before its bridge was
    ready: it must report the first one once the bridge is ready, as the
    error's kind, line and column only, and the second not at all."""
    thrown, reports = result.get("thrown") or [], result.get("reports") or []
    if result.get("thrown_after_ready"):
        return [f"early: the page threw {result['thrown_after_ready']} error(s) of its own "
                "after its bridge was ready"]
    if len(thrown) != 2:
        return [f"early: the page saw {len(thrown)} error(s), not the 2 thrown before its "
                "bridge was ready"]
    if thrown[0] == thrown[1]:
        # Then a page keeping the last error instead of the first would pass.
        return ["early: the two errors were thrown from the same place, so the check cannot "
                "tell the first from the second"]
    if any(r.get("before_ready") for r in reports):
        return ["early: the page called the bridge before it was ready"]
    if len(reports) != 1:
        return [f"early: the page reported {len(reports)} of its early errors, not the "
                "first one once"]
    if reports[0].get("args") != ["error", *thrown[0]]:
        return [f"early: the page reported {reports[0].get('args')!r}, not the first "
                f"error's kind, line and column {['error', *thrown[0]]!r}"]
    return []


def figure(result, metric):
    """A metric from one run's result. "calls_per_minute.recording_status" is
    one method's calls: 0 when the page did not call it, "missing" when the run
    reported no per-method figures at all."""
    group, dot, key = metric.partition(".")
    if not dot:
        return result.get(metric, "missing")
    per = result.get(group)
    return per.get(key, 0) if isinstance(per, dict) else "missing"


def compare(results, ratchets):
    """[(mode, metric, value, limit)] for every figure past its ratchet. A metric
    the page did not report counts as past it (value None), except the interval
    floor, where None means no interval at all."""
    over = []
    for mode, limits in ratchets.items():
        for metric, limit in limits.items():
            if limit is None:
                continue
            value = figure(results.get(mode, {}), metric)
            if metric in AT_LEAST and value is None:
                continue                          # no interval: nothing polls
            if not isinstance(value, (int, float)):
                over.append((mode, metric, None, limit))
            elif (value < limit) if metric in AT_LEAST else (value > limit):
                over.append((mode, metric, value, limit))
    return over


def sanity(results):
    """Reasons the page cannot be idle-checked: it threw, or it is not running."""
    problems = []
    for mode, r in results.items():
        if r.get("page_errors"):
            problems.append(f"{mode}: the page threw: {'; '.join(r['page_errors'][:3])}")
    if "hidden" in results:
        caught_up = results["hidden"].get("catch_up_calls") or []
        if not caught_up:
            problems.append("hidden: the page did not refresh within 1 s of being shown again")
        elif "meetings" not in caught_up:
            problems.append("hidden: shown again, the page did not fetch the meeting list "
                            "(ages and hold reviews may have moved on while it was hidden)")
    visible = results.get("visible", {})
    if not visible.get("bridge_calls_per_minute") or not visible.get("meeting_rows"):
        problems.append("visible: the page made no bridge calls or showed no meetings, so it "
                        "is not running; an idle figure would be meaningless")
    else:
        # The ratchets are ceilings: these make sure the lists still refresh.
        if not visible.get("calls_per_minute", {}).get("meetings"):
            problems.append("visible: the page did not fetch the meeting list in a minute "
                            "(it must, every 6th poll, even when nothing changed)")
        if visible.get("redrawn_after_change") is not True:
            problems.append("visible: the page did not redraw the meeting list within one "
                            "poll of a change")
    return problems


def meter_hz():
    """METER_HZ in workspace/ui/app.js: the level meter's draws a second."""
    found = re.search(r"^const METER_HZ = (\d+);", APP_JS.read_text(encoding="utf-8"), re.M)
    if not found:
        raise RuntimeError("app.js no longer sets METER_HZ as expected; update meter_hz().")
    return int(found.group(1))


def recording_problems(results, hz):
    """Reasons the page mishandled an open recording (#340): it threw, stopped
    asking for the clock and level, drew the meter at other than about hz a
    second while visible, drew it at all while hidden, or did not ask and draw
    again within a second of being shown."""
    problems = []
    for mode, r in results.items():
        if r.get("page_errors"):
            problems.append(f"recording {mode}: the page threw: {'; '.join(r['page_errors'][:3])}")
    visible, hidden = results.get("visible", {}), results.get("hidden", {})
    if not (visible.get("calls_per_minute") or {}).get("recording_status"):
        problems.append("recording visible: the page did not ask for recording_status, so the "
                        "clock and level meter stood still")
    rate = visible.get("meter_draws_per_second")
    if not isinstance(rate, (int, float)) or abs(rate - hz) > hz * METER_TOLERANCE:
        problems.append(f"recording visible: the level meter drew {rate} times a second on a "
                        f"{DISPLAY_HZ} Hz display, not about {hz} (METER_HZ)")
    if hidden.get("meter_draws_per_second") != 0:
        problems.append(f"recording hidden: the level meter drew "
                        f"{hidden.get('meter_draws_per_second')} times a second while hidden")
    if ("recording_status" not in (hidden.get("catch_up_calls") or [])
            or not hidden.get("catch_up_meter_draws")):
        problems.append("recording hidden: shown again, the page did not ask for "
                        "recording_status and draw the meter within 1 s")
    return problems


def main():
    parser = argparse.ArgumentParser(description="Count what the idle page does.")
    parser.add_argument("report", nargs="?", type=Path)
    parser.add_argument("--page-ms", type=int, default=60000,
                        help="page time to measure over (default: one minute)")
    args = parser.parse_args()
    if args.page_ms <= 0:
        parser.error("--page-ms must be more than 0")
    if design_check.APP_TAG not in (design_check.UI / "index.html").read_text(encoding="utf-8"):
        sys.exit("index.html no longer loads app.js as expected; update design_check.APP_TAG.")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), design_check.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        results = {mode: run_page(base, mode, args.page_ms) for mode in MODES}
        early = run_early(base)
        growing = run_transcribing(base)
        recording = {mode: run_page(base, mode, args.page_ms, RECORDING_SCENARIO, DISPLAY_HZ)
                     for mode in MODES}
    finally:
        server.shutdown()
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    over = (compare(results, budgets["ui_idle"]["ratchets"])
            + compare({"transcribing": growing}, budgets["ui_transcribing"]["ratchets"])
            + [(f"recording {m}", k, v, r) for m, k, v, r in
               compare(recording, budgets["ui_recording"]["ratchets"])])
    problems = (sanity(results) + early_problems(early) + transcribing_problems(growing)
                + recording_problems(recording, meter_hz()))
    report = {"scenario": SCENARIO, "page_ms": args.page_ms, "results": results,
              "early_error": early, "transcribing": growing, "recording": recording,
              "over_ratchet": [{"mode": m, "metric": k, "value": v, "ratchet": r}
                               for m, k, v, r in over], "problems": problems}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for mode in MODES:
        r = results[mode]
        print(f"{mode}: {r.get('bridge_calls_per_minute')} bridge calls/min, "
              f"{r.get('list_rebuilds_per_minute')} list rebuilds/min, "
              f"{r.get('timeouts_per_minute')} timeouts/min, "
              f"{r.get('pending_animation_frames')} pending frame(s), "
              f"{r.get('live_intervals')} interval(s), fastest {r.get('fastest_interval_ms')} ms")
    if not early_problems(early):
        print("early: the first error before the bridge was ready was reported once, after it")
    print(f"transcribing: {growing.get('transcript_calls')} transcript calls, "
          f"{growing.get('rows_fetched')} rows fetched, {growing.get('rows_drawn')} rows drawn, "
          f"{growing.get('full_redraws')} full redraw(s) over {growing.get('steps')} steps; "
          f"{growing.get('rows_shown')} of {growing.get('rows_expected')} passages shown")
    for mode in MODES:
        r = recording[mode]
        print(f"recording {mode}: {r.get('bridge_calls_per_minute')} bridge calls/min "
              f"({(r.get('calls_per_minute') or {}).get('recording_status', 0)} recording_status), "
              f"{r.get('timeouts_per_minute')} timeouts/min, {r.get('live_intervals')} "
              f"interval(s), fastest {r.get('fastest_interval_ms')} ms, meter "
              f"{r.get('meter_draws_per_second')} draws/s at {r.get('display_hz')} Hz")
    for problem in problems:
        print(f"PAGE PROBLEM: {problem}")
    for m, k, v, r in over:
        print(f"OVER RATCHET: {m} {k} {'not reported' if v is None else v} (ratchet {r})")
    if not over and not problems:
        print("WITHIN RATCHETS")
    return 1 if over or problems else 0


if __name__ == "__main__":
    sys.exit(main())
