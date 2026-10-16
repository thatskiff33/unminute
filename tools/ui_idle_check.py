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
               f"--virtual-time-budget={page_ms + 15000}", "--dump-dom", url]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=300)
    found = RESULT.search(out.stdout or "")
    if not found:
        raise RuntimeError(f"Edge returned no perf result for {mode} (exit {out.returncode}). "
                           f"stderr: {(out.stderr or '')[-500:]}")
    import html
    return json.loads(html.unescape(found.group(1)))


def compare(results, ratchets):
    """[(mode, metric, value, limit)] for every figure past its ratchet."""
    over = []
    for mode, limits in ratchets.items():
        for metric, limit in limits.items():
            value = results.get(mode, {}).get(metric)
            if value is None or limit is None:
                continue
            if (value < limit) if metric in AT_LEAST else (value > limit):
                over.append((mode, metric, value, limit))
    return over


def main():
    parser = argparse.ArgumentParser(description="Count what the idle page does.")
    parser.add_argument("report", nargs="?", type=Path)
    parser.add_argument("--page-ms", type=int, default=60000,
                        help="page time to measure over (default: one minute)")
    args = parser.parse_args()
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
    report = {"scenario": SCENARIO, "page_ms": args.page_ms, "results": results,
              "over_ratchet": [{"mode": m, "metric": k, "value": v, "ratchet": r}
                               for m, k, v, r in over]}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for mode in MODES:
        r = results[mode]
        print(f"{mode}: {r['bridge_calls_per_minute']} bridge calls/min, "
              f"{r['list_rebuilds_per_minute']} list rebuilds/min, "
              f"{r['timeouts_per_minute']} timeouts/min, {r['live_intervals']} interval(s), "
              f"fastest {r['fastest_interval_ms']} ms")
    for m, k, v, r in over:
        print(f"OVER RATCHET: {m} {k} {v} (ratchet {r})")
    print("WITHIN RATCHETS" if not over else "")
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
