# tools/ui_idle_check.py
# UI idle behaviour (docs/RELEASE_STANDARD.md, section 3): what the page does
# when nobody is using it, counted rather than timed, so the result is the same
# on any machine. The real page (workspace/ui) runs in headless Microsoft Edge
# against the design check's fake bridge (tools/design_check/fixtures.js) in its
# perf mode, which counts bridge calls per method, timers and meeting-list
# rebuilds over a minute of page time. Edge's virtual time budget lets that
# minute pass in a few seconds of real time. Run once visible and once with the
# page reporting itself hidden, as a minimised window should.
#
#   python tools/ui_idle_check.py [report.json] [--page-ms 60000]
#
# Exit code 1 when a figure is over its ratchet in tests/budgets.json ("ui_idle").
# Lower a ratchet when the figure drops (#127); raise one only with a reason in
# the pull request. Needs Microsoft Edge; no Python packages.

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
RESULT = re.compile(r'<pre id="perf-result">(.*?)</pre>', re.DOTALL)
# Ratchets are maxima, except where a smaller value is worse.
AT_LEAST = {"fastest_interval_ms"}


def run_page(base, mode, page_ms):
    """The page's perf result for one mode, from Edge's dump of the final DOM."""
    url = f"{base}/app/index.html?perf={mode}&ms={page_ms}#{SCENARIO}"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        cmd = [str(design_check.edge()), "--headless=new", "--disable-gpu", "--no-first-run",
               "--disable-extensions", f"--user-data-dir={profile}", "--mute-audio",
               "--window-size=1100,760",
               # page time: the scenario's start-up steps plus the measured window
               # + up to 60 s for the next list fetch and 11 s for the redraw
               # after a change (visible), or 1 s of catch-up (hidden).
               f"--virtual-time-budget={page_ms + 90000}", "--dump-dom", url]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=300)
    found = RESULT.search(out.stdout or "")
    if not found:
        ready = 'data-ready="1"' in (out.stdout or "")
        raise RuntimeError(
            f"Edge returned no perf result for {mode} (exit {out.returncode}). The page "
            + ("finished its start-up steps, so the measured window did not fit in the page "
               "time budget" if ready else "never finished its start-up steps")
            + f". stderr: {(out.stderr or '').strip()[-500:] or '(empty)'}")
    import html
    return json.loads(html.unescape(found.group(1)))


def compare(results, ratchets):
    """[(mode, metric, value, limit)] for every figure past its ratchet. A metric
    the page did not report counts as past it (value None), except the interval
    floor, where None means no interval at all."""
    over = []
    for mode, limits in ratchets.items():
        for metric, limit in limits.items():
            if limit is None:
                continue
            value = results.get(mode, {}).get(metric, "missing")
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
    finally:
        server.shutdown()
    ratchets = json.loads(BUDGETS.read_text(encoding="utf-8"))["ui_idle"]["ratchets"]
    over = compare(results, ratchets)
    problems = sanity(results)
    report = {"scenario": SCENARIO, "page_ms": args.page_ms, "results": results,
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
    for problem in problems:
        print(f"PAGE PROBLEM: {problem}")
    for m, k, v, r in over:
        print(f"OVER RATCHET: {m} {k} {'not reported' if v is None else v} (ratchet {r})")
    if not over and not problems:
        print("WITHIN RATCHETS")
    return 1 if over or problems else 0


if __name__ == "__main__":
    sys.exit(main())
