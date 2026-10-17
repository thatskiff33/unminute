# tools/accessibility_check.py
# #27 technical accessibility audit of the real workspace window (WebView2),
# on a throwaway workspace with synthetic audio and fake engines. For every
# view and dialog it checks, in the live page:
#   - accessible names for every visible control and dialog;
#   - keyboard reachability (no visible control taken out of the tab order,
#     no pointer-only clickable elements);
#   - a visible focus indicator on each focusable control;
#   - text contrast against its effective background (WCAG AA 4.5:1, 3:1 for
#     large text);
#   - reflow at 640 and 320 CSS px wide, as at 200% and 400% zoom (no
#     horizontal scrolling or cut-off content);
#   - status badges that carry text, not colour alone.
# It does not replace testing with screen-reader users and pilot users (#27).
#
#   python tools/accessibility_check.py <report.json>
#
# Against an installed or extracted package, as release evidence
# (tools/evidence.py), with that package's own Python:
#
#   <v>\runtime\python.exe -I -B -X disable-remote-debug tools\accessibility_check.py
#       <report.json> --app <v>\app --zip <the zip it came from>

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))   # also under -I; after the stdlib
# workspace_ui_check puts the app checked (this checkout, or --app) on the path.
import evidence  # noqa: E402
from workspace_ui_check import APP, FAKE, WAIT, evaluate, write_wav  # noqa: E402

from workspace import api, shell  # noqa: E402
from workspace.app import Workspace  # noqa: E402

# One audit of whatever is currently shown. Returns findings, not a verdict.
AUDIT = r"""
const visible = (el) => {
  if (!el.isConnected) return false;
  const s = getComputedStyle(el);
  if (s.display === 'none' || s.visibility === 'hidden') return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && !el.closest('[hidden]');
};
const nameOf = (el) => {
  const by = el.getAttribute('aria-labelledby');
  if (by) return by.split(/\s+/).map(i => document.getElementById(i)?.textContent || '')
                   .join(' ').trim();
  if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
  if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`);
               if (l && l.textContent.trim()) return l.textContent.trim(); }
  const wrap = el.closest('label');
  if (wrap && wrap.textContent.trim()) return wrap.textContent.trim();
  if (['BUTTON', 'A', 'SUMMARY'].includes(el.tagName) && el.textContent.trim())
    return el.textContent.trim();
  if (el.tagName === 'INPUT' && ['button', 'submit'].includes(el.type) && el.value) return el.value;
  return (el.getAttribute('title') || '').trim();
};
const desc = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
  (el.className && typeof el.className === 'string' ? '.' + el.className.split(' ')[0] : '');
const controls = [...document.querySelectorAll(
  'button, input:not([type=hidden]), select, textarea, a[href], summary, ' +
  '[contenteditable], [role=button], [tabindex]')]
  .filter(el => visible(el) && !el.closest('dialog:not([open])'));
const unnamed = controls.filter(el => !nameOf(el) && el.getAttribute('contenteditable') === null)
  .map(desc);
const untabbable = controls.filter(el => el.tabIndex < 0 && !el.disabled).map(desc);
// Pointer-only: cursor:pointer on an element that is not itself focusable.
const pointerOnly = [...document.querySelectorAll('body *')].filter(el => visible(el)
  && getComputedStyle(el).cursor === 'pointer' && el.tabIndex < 0
  && !el.closest('button, a, label, summary, input, select, textarea')).map(desc);
// Focus indicator: keyboard-style focus on each control and compare styles.
const noFocusRing = [];
for (const el of controls.filter(e => !e.disabled && e.tabIndex >= 0).slice(0, 80)) {
  el.focus({focusVisible: true});
  const s = getComputedStyle(el);
  const ring = (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) ||
               (s.boxShadow && s.boxShadow !== 'none');
  if (document.activeElement === el && !ring) noFocusRing.push(desc(el));
}
document.activeElement?.blur();
// Contrast of visible text against its effective background.
const rgb = (c) => { const m = c.match(/[\d.]+/g); return m ? m.map(Number) : [0, 0, 0, 1]; };
const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 :
  Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
const bgOf = (el) => {
  for (let e = el; e; e = e.parentElement) {
    const c = rgb(getComputedStyle(e).backgroundColor);
    if ((c[3] ?? 1) > 0.5) return c;
  }
  return rgb(getComputedStyle(document.body).backgroundColor);
};
const lowContrast = [];
const seen = new Set();
for (const el of document.querySelectorAll('body *')) {
  if (!visible(el) || el.closest('dialog:not([open])')) continue;
  const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())
    || (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) && el.value
        && !['checkbox', 'radio', 'range', 'file'].includes(el.type));
  if (!own) continue;
  const s = getComputedStyle(el);
  if (el.disabled || el.closest('[disabled]')) continue;    // WCAG exempts inactive controls
  const fg = rgb(s.color), bg = bgOf(el);
  const a = fg[3] ?? 1;
  const mix = fg.slice(0, 3).map((v, i) => v * a + bg[i] * (1 - a));
  const L1 = lum(mix), L2 = lum(bg);
  const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
  const size = parseFloat(s.fontSize), bold = parseInt(s.fontWeight) >= 700;
  const large = size >= 24 || (bold && size >= 18.66);
  if (ratio < (large ? 3 : 4.5)) {
    const key = desc(el) + ' ' + ratio.toFixed(2);
    if (!seen.has(key)) { seen.add(key); lowContrast.push(key); }
  }
}
// Status badges: must carry words, not only colour.
const colourOnly = [...document.querySelectorAll('.badge, .status, .dot, [class*="state"]')]
  .filter(el => visible(el) && !el.textContent.trim() && !nameOf(el)).map(desc);
const dialogs = [...document.querySelectorAll('dialog[open]')]
  .filter(d => !nameOf(d)).map(desc);
return {controls: controls.length, unnamed, untabbable, pointer_only: pointerOnly,
        no_focus_ring: noFocusRing, low_contrast: lowContrast.slice(0, 40),
        colour_only: colourOnly, unnamed_dialogs: dialogs};
"""

REFLOW = r"""
const vw = document.documentElement.clientWidth;
const over = document.documentElement.scrollWidth - vw;
const wide = [...document.querySelectorAll('body *')].filter(el => {
  if (el.closest('[hidden]') || el.closest('dialog:not([open])')) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && (r.right > vw + 2 || r.left < -2);
}).slice(0, 10).map(el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : ''));
// The element reaching furthest right, to explain any small overflow.
let far = null, farRight = 0;
for (const el of document.querySelectorAll('body *')) {
  if (el.closest('[hidden]') || el.closest('dialog:not([open])')) continue;
  const r = el.getBoundingClientRect();
  if (r.width > 0 && r.right > farRight) { farRight = r.right; far = el; }
}
return {css_width: vw, horizontal_overflow_px: over, cut_off: wide,
        furthest_right: far ? `${far.tagName.toLowerCase()}${far.id ? '#' + far.id : ''} ` +
                              `${farRight.toFixed(1)}` : null};
"""


def reflow(window):
    """Reflow like browser zoom: a narrower viewport at normal zoom. 640 CSS px
    is 200% zoom of a 1280 px window; 320 CSS px is the WCAG 1.4.10 width."""
    import time
    w0, h0 = window.width, window.height
    out = {}
    for target in (640, 320):
        cw = evaluate(window, "return document.documentElement.clientWidth;")
        window.resize(max(200, round(window.width * target / cw)), h0)
        time.sleep(0.8)
        out[str(target)] = evaluate(window, REFLOW)
    window.resize(w0, h0)
    time.sleep(0.8)
    return out


def checks(window, ws, mid, report):
    import time
    try:
        window.events.loaded.wait(30)
        while not window.evaluate_js("!!(window.workspace && window.pywebview && "
                                     "window.pywebview.api && window.pywebview.api.meetings)"):
            time.sleep(0.2)
        report["zoomable"] = window.zoomable
        report["keyboard_zoom"] = evaluate(window, WAIT + """
            const key = (k) => document.dispatchEvent(new KeyboardEvent('keydown',
                {key: k, ctrlKey: true, bubbles: true}));
            const before = window.devicePixelRatio;
            key('+'); await wait(800); const zoomed = window.devicePixelRatio;
            key('0'); await wait(800); const reset = window.devicePixelRatio;
            return {before, zoomed, reset};""")
        report["page"] = evaluate(window, "return {lang: document.documentElement.lang, "
                                          "title: document.title, live_status: "
                                          "document.getElementById('status')?.getAttribute"
                                          "('aria-live')};")
        views = {
            "setup": "window.workspace.show('setup'); await wait(1200);",
            "setup speech model": "window.workspace.setStep(2); await wait(500);",
            "setup speaker names": "window.workspace.setStep(3); await wait(500);",
            "setup microphone": "window.workspace.setStep(4); await wait(800);",
            "setup ready": "window.workspace.setStep(5); await wait(500);",
            "downloads": "window.workspace.show('downloads'); await wait(1200);",
            "meetings": "window.workspace.show('meetings'); await wait(1500);",
            "microphone chooser": "document.getElementById('mic-change').click(); await wait(400);",
            "settings": "window.workspace.show('settings'); await wait(800);",
            "help": "window.workspace.show('help'); await wait(400);",
            "meeting": f"await window.workspace.openMeeting('{mid}'); await wait(1500);",
            "versions tab": "document.querySelector('.wide-tabs [data-tab=versions]').click();"
                            "await wait(300);",
            "holds tab": "document.querySelector('.wide-tabs [data-tab=holds]').click();"
                         "await wait(300);",
            "identify again": "document.querySelector('.wide-tabs [data-tab=speakers]').click();"
                              "document.getElementById('m-again').click(); await wait(300);",
            "names dialog": "document.getElementById('m-names').click(); await wait(1500);",
            "confirm dialog": "document.getElementById('names').close(); await wait(300);"
                              "document.getElementById('m-primary').click();"
                              "await wait(500);",
            # The handoff opens only after the review confirmation.
            "handoff page": "document.getElementById('c-check').click();"
                              "const c = document.getElementById('confirm');"
                              "c.returnValue = 'ok'; c.close('ok'); await wait(4000);"
                              "document.getElementById('m-primary').click(); await wait(2500);",
        }
        report["views"] = {}
        for name, go in views.items():
            evaluate(window, WAIT + go + "return true;")
            audit = evaluate(window, AUDIT)
            audit["open_dialogs"] = evaluate(window, "return [...document.querySelectorAll("
                                                     "'dialog[open]')].map(d => d.id);")
            audit["reflow"] = reflow(window)
            report["views"][name] = audit
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        window.destroy()


def main():
    ap = argparse.ArgumentParser(description="Technical accessibility audit (#27).",
                                 allow_abbrev=False)
    ap.add_argument("report", help="where to write the JSON report")
    ap.add_argument("--app", help="an installed or extracted package's app folder (run with "
                    "that package's Python: runtime\\python.exe -I -B -X disable-remote-debug)")
    ap.add_argument("--zip", help="the zip that package came from: records it as release "
                    "evidence (tools/evidence.py); exit 1 unless that evidence passes")
    args = ap.parse_args()
    before = evidence.prepare(ap, args, APP if args.app else None)
    out = Path(args.report)
    report = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ws = Workspace(root / "ws", root / "models", start=False,
                       allow_test_engines=True, battery=False,
                       speaker_model=FAKE + "fake_diarizer")
        # Microphone on, so the Inbox's recording panel (#30) is audited too.
        ws.settings.update(model=FAKE + "sentences", speakers=True, microphone=True)
        ws.start()
        ws.import_paths([str(write_wav(root / "synthetic-standup.wav", 60))])
        assert ws.wait_idle(180), "processing did not finish"
        mid = ws.meetings()[0]["id"]
        holder = {}
        # The app's minimum window size (640 px) is above the 320 CSS px that
        # 400% zoom gives; lift it for this check only, to test reflow there.
        import webview
        real = webview.create_window
        webview.create_window = lambda *a, **k: real(*a, **{**k, "min_size": (200, 200)})
        try:
            window = shell.create_window(api.make_api(ws, lambda: holder.get("window")),
                                         title="Accessibility check (synthetic data)",
                                         allowed=api.API_METHODS)
        finally:
            webview.create_window = real
        holder["window"] = window
        shell.run(checks, (window, ws, mid, report))
        ws.close()
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    problems = [] if not report.get("error") else [report["error"]]
    kz = report.get("keyboard_zoom") or {}
    if not (report.get("zoomable") and kz.get("zoomed", 0) > kz.get("before", 0) * 1.05
            and abs(kz.get("reset", 0) - kz.get("before", -1)) < 0.01):
        problems.append(f"keyboard zoom does not work ({kz})")
    for view, a in report.get("views", {}).items():
        for key in ("unnamed", "untabbable", "pointer_only", "no_focus_ring", "low_contrast",
                    "colour_only", "unnamed_dialogs"):
            if a.get(key):
                problems.append(f"{view}: {key} ({len(a[key])})")
        for width, r in a.get("reflow", {}).items():
            if r["css_width"] > int(width) + 1:
                problems.append(f"{view}: reflow width {width} not reached ({r['css_width']})")
            if r["horizontal_overflow_px"] > 0 or r["cut_off"]:
                problems.append(f"{view}: content cut off or scrolling at {width} CSS px "
                                f"({', '.join(r['cut_off'][:3]) or 'page'})")
    print("FINDINGS:\n- " + "\n- ".join(problems) if problems else "NO FINDINGS")
    if args.zip:
        ev = evidence.stamp_file(out, "accessibility", args.zip, APP.parent, passed=not problems,
                                 before=before)
        return 0 if ev["pass"] else 1
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
