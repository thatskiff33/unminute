# tools/design_check.py
# Design fidelity check (#89). Renders each approved design board
# (docs/design/boards) and the matching app screen in headless Microsoft Edge
# at the same window size, then writes board | app | difference images and a
# score: the share of pixels that differ visibly. The app runs from
# workspace/ui with a fake bridge (tools/design_check/fixtures.js) that serves
# fixed sample data, so no workspace, model or network is involved.
#
#   python tools/design_check.py <out-dir> [case ...]
#
# Needs Microsoft Edge and Pillow (Pillow only for the comparison, so
# tools/ui_idle_check.py can reuse the server and Edge helpers on CI without
# it). Development tool only; not shipped.
#
# Exit codes: 0 every case rendered (the scores are for a person to read; none
# fails the run); 2 headless Edge timed out (EDGE_LIMIT_S) and returned
# nothing, so that case was not measured: the tool says which case in one
# paragraph and keeps results.json for the cases before it; 1 any other error.

"""Compare the approved design boards with the app, screen by screen."""

import argparse
import http.server
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "workspace" / "ui"
SERVED = ("docs/design/", "docs/screenshots/", "tools/design_check/", "workspace/ui/")
EDGE = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]
APP_TAG = '<script src="app.js"></script>'   # the fake bridge is injected before it
VISIBLE = 40         # a channel difference above this counts as a visible change
EDGE_LIMIT_S = 120   # this tool's own limit for one headless Edge screenshot
NOT_MEASURED = 2     # exit code: headless Edge timed out, so nothing was measured

# Each case pairs a board (and its props) with an app scenario from fixtures.js.
# An empty "app" renders the board only (no app screen to compare yet).
CASES = [
    {"name": "brand", "board": "Main.dc.html", "app": ""},
    {"name": "home", "board": "Home.dc.html", "app": "home"},
    {"name": "recording", "board": "Recording.dc.html", "app": "recording"},
    {"name": "home-empty", "board": "Home-Empty.dc.html", "app": "home-empty"},
    {"name": "home-drop", "board": "Home-Drop.dc.html", "app": "home-drop"},
    {"name": "home-states", "board": "Home-States.dc.html", "app": "home-states", "h": 1180},
    {"name": "recording-states", "board": "Recording-States.dc.html", "app": "", "h": 1180},
    # Retention reminders (#111 C6): the banner's wording with IT's thresholds
    # and with the app's own default (C6-5).
    {"name": "home-retention", "board": "Home-Retention.dc.html", "app": "home-retention",
     "h": 1100, "props": {"policy": "managed"}},
    {"name": "home-retention-default", "board": "Home-Retention.dc.html",
     "app": "home-retention-default", "h": 1100, "props": {"policy": "default"}},
    # Live drafts (#104): the recording card with draft text, light and dark.
    {"name": "recording-drafts", "board": "Recording-Drafts.dc.html", "app": "recording-drafts"},
    {"name": "recording-drafts-dark", "board": "Recording-Drafts-Dark.dc.html",
     "app": "recording-drafts", "dark": True},
    {"name": "recording-drafts-states", "board": "Recording-Drafts-States.dc.html", "app": "",
     "h": 1180},
    {"name": "meeting", "app": "meeting", "board": "Meeting.dc.html"},
    {"name": "meeting-transcribing", "app": "meeting-transcribing",
     "board": "Meeting-Transcribing.dc.html"},
    {"name": "meeting-edit", "app": "meeting-edit", "board": "Meeting-Edit.dc.html"},
    {"name": "meeting-reviewed", "app": "meeting-reviewed", "board": "Meeting-Reviewed.dc.html"},
    {"name": "meeting-earlier", "app": "meeting-earlier", "board": "Meeting-Earlier.dc.html"},
    {"name": "meeting-versions", "app": "meeting-versions", "board": "Meeting-Versions.dc.html"},
    {"name": "meeting-panels", "board": "Meeting-Panels.dc.html", "app": "", "w": 1340},
    {"name": "names", "app": "names", "board": "Names.dc.html"},
    {"name": "confirm", "app": "confirm", "board": "Confirm.dc.html"},
    {"name": "handoff", "app": "handoff", "board": "Handoff.dc.html"},
    {"name": "handoff-off", "app": "handoff-off", "board": "Handoff.dc.html",
     "props": {"assistant": "off"}},
    {"name": "handoff-template", "app": "handoff-template",
     "board": "Handoff-Template.dc.html"},
    {"name": "setup-1", "app": "setup-1", "board": "Setup-1.dc.html"},
    {"name": "setup-2", "app": "setup-2", "board": "Setup.dc.html"},
    {"name": "setup-3", "app": "setup-3", "board": "Setup-3.dc.html"},
    {"name": "setup-4", "app": "setup-4", "board": "Setup-4.dc.html"},
    {"name": "setup-5", "app": "setup-5", "board": "Setup-5.dc.html"},
    {"name": "settings", "board": "Settings.dc.html", "app": "settings", "h": 1320},
    {"name": "settings-assistant", "board": "Settings.dc.html", "app": "settings-assistant",
     "h": 1320, "props": {"assistant": "set"}},
    {"name": "settings-unrecognised", "board": "Settings.dc.html",
     "app": "settings-unrecognised", "h": 1320, "props": {"unrecognised": "set"}},
    # #104 with #108: the choice of what draft text follows, with computer audio on.
    {"name": "settings-drafts-mix", "board": "Settings.dc.html", "app": "settings-drafts-mix",
     "h": 1320, "props": {"drafts": "mix"}},
    {"name": "downloads", "app": "downloads", "board": "Models.dc.html"},
    {"name": "help", "board": "Help.dc.html", "app": "help"},
    {"name": "meeting-900", "board": "Meeting-Narrow.dc.html", "app": "meeting", "w": 900},
    {"name": "meeting-640", "board": "Meeting-640.dc.html", "app": "meeting", "w": 640},
    {"name": "meeting-320", "board": "Meeting-320.dc.html", "app": "meeting", "w": 320},
    {"name": "home-640", "board": "Home-640.dc.html", "app": "home", "w": 640},
    {"name": "home-320", "board": "Home-320.dc.html", "app": "home", "w": 320},
    {"name": "home-dark", "board": "Home-Dark.dc.html", "app": "home", "dark": True},
    {"name": "meeting-dark", "board": "Meeting-Dark.dc.html", "app": "meeting", "dark": True},
]


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the app under /app/ with the fake bridge, and a few repo folders."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = unquote(urlsplit(self.path).path).lstrip("/")
        if path.startswith("app/"):
            rel = path[4:] or "index.html"
            base = UI.resolve()
            if rel == "index.html":
                html = (UI / rel).read_text(encoding="utf-8")
                tag = APP_TAG
                html = html.replace(
                    tag, '<script src="/tools/design_check/fixtures.js"></script>\n' + tag)
                return self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            target = (base / rel).resolve()
        else:
            prefix = next((p for p in SERVED if path.startswith(p)), None)
            if prefix is None:
                return self.send_error(404)
            base = (ROOT / prefix).resolve()
            target = (ROOT / path).resolve()
        # Only files inside the matched folder, whatever ".." the request holds.
        if not target.is_relative_to(base) or not target.is_file():
            return self.send_error(404)
        return self._send(target.read_bytes(), self.guess_type(str(target)))

    def do_HEAD(self):
        self.send_error(405)

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def edge():
    for p in EDGE:
        if p.is_file():
            return p
    sys.exit("Microsoft Edge was not found.")


class EdgeTimeout(Exception):
    """Headless Edge did not return within the tool's own limit, so nothing was
    measured. Not a page result: the tools exit NOT_MEASURED with this message
    rather than a traceback that reads like a page failure."""


def run_edge(cmd, timeout, page, mode, scenario, **kwargs):
    """subprocess.run for headless Edge, raising EdgeTimeout (naming the page,
    mode and scenario) when Edge does not return within timeout seconds."""
    try:
        return subprocess.run(cmd, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise EdgeTimeout(
            f"Headless Edge timed out on {page} ({mode} mode, scenario {scenario}) at this "
            f"tool's own limit of {timeout} s. Edge returned nothing, so nothing was measured: "
            "this is not a page result, and the check has neither passed nor failed the page. "
            "Edge does this when another headless Edge run competes with it "
            "(docs/DEVELOPER_NOTES.md, \"Checks that can fail for reasons other than the "
            "code\"). Rerun when no other headless Edge run is active.") from None


def shoot(url, out, width, height, dark, what):
    """Screenshot a page in headless Edge at width x height CSS pixels. what is
    (page, mode, scenario), for the message if Edge times out."""
    # Edge's crash handler can outlive it and keep the profile folder open.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        cmd = [str(edge()), "--headless=new", "--disable-gpu", "--hide-scrollbars",
               "--no-first-run", "--disable-extensions", f"--user-data-dir={profile}",
               "--force-device-scale-factor=1", f"--window-size={width},{height}",
               "--autoplay-policy=no-user-gesture-required", "--mute-audio",
               "--virtual-time-budget=15000", f"--screenshot={out}",
               # prefers-color-scheme: 0 dark, 1 light (headless otherwise follows Windows)
               f"--blink-settings=preferredColorScheme={0 if dark else 1}", url]
        run_edge(cmd, EDGE_LIMIT_S, *what, check=True, capture_output=True)
    if not Path(out).is_file():
        raise RuntimeError(f"Edge made no screenshot of {url}")


def compare(board_png, app_png, out_png):
    """Board | app | difference (red where they differ). Returns % of pixels changed."""
    from PIL import Image, ImageChops
    a = Image.open(board_png).convert("RGB")
    b = Image.open(app_png).convert("RGB")
    if b.size != a.size:
        bigger = b.width >= a.width and b.height >= a.height
        b = b.crop((0, 0, *a.size)) if bigger else b.resize(a.size)
    diff = ImageChops.difference(a, b)
    channels = diff.split()
    mask = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    mask = mask.point(lambda v: 255 if v > VISIBLE else 0)
    changed = mask.histogram()[255] / (a.width * a.height) * 100
    red = Image.new("RGB", a.size, (229, 0, 0))
    faded = Image.blend(a, Image.new("RGB", a.size, "white"), 0.7)
    overlay = Image.composite(red, faded, mask)
    sheet = Image.new("RGB", (a.width * 3 + 40, a.height), "white")
    for i, img in enumerate((a, b, overlay)):
        sheet.paste(img, (i * (a.width + 20), 0))
    sheet.save(out_png)
    return round(changed, 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("cases", nargs="*", help="case names (default: all)")
    args = parser.parse_args()
    cases = [c for c in CASES if not args.cases or c["name"] in args.cases]
    if not cases:
        sys.exit("No such case. Known: " + ", ".join(c["name"] for c in CASES))
    if APP_TAG not in (UI / "index.html").read_text(encoding="utf-8"):
        sys.exit("index.html no longer loads app.js as expected; update APP_TAG.")
    args.out.mkdir(parents=True, exist_ok=True)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    results = []
    try:
        for c in cases:
            w, h = c.get("w", 1100), c.get("h", 760)
            props = "".join(f"&{k}={quote(str(v))}" for k, v in c.get("props", {}).items())
            board_png = args.out / f"{c['name']}-board.png"
            shoot(f"{base}/tools/design_check/render.html?board={quote(c['board'])}{props}",
                  board_png, w, h, c.get("dark", False), (c["board"], "board", c["name"]))
            row = {"case": c["name"], "board": c["board"], "size": f"{w}x{h}"}
            if c["app"]:
                app_png = args.out / f"{c['name']}-app.png"
                url = f"{base}/app/index.html#{c['app']}"
                if w < 500:     # Edge's narrowest window: use a frame of the exact width
                    url = (f"{base}/tools/design_check/frame.html?w={w}&h={h}"
                           f"&src={quote(url, safe='')}")
                shoot(url, app_png, w, h, c.get("dark", False),
                      ("app/index.html", "app", c["name"]))
                row["changed_percent"] = compare(board_png, app_png,
                                                 args.out / f"{c['name']}-compare.png")
            results.append(row)
            print(json.dumps(row))
    except EdgeTimeout as timeout:
        print(f"NOT MEASURED: {timeout}", file=sys.stderr)
        return NOT_MEASURED
    finally:
        server.shutdown()
        (args.out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
