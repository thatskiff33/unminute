# tools/accessibility_check.py
# #27 technical accessibility audit of the real workspace window (WebView2),
# on a throwaway workspace with synthetic audio and fake engines, against the
# WCAG 2.2 AA criteria that can be measured in the live page (#298). For every
# view and dialog it checks, in the live page:
#   - accessible names for every visible control and dialog (4.1.2, names only);
#   - keyboard reachability (no visible control taken out of the tab order,
#     no pointer-only clickable elements) (2.1.1, in part);
#   - no keyboard trap: Tab and Shift+Tab are not swallowed, and focus can move
#     on (2.1.2);
#   - a visible focus indicator on each focusable control (2.4.7), with 3:1
#     contrast against what it is drawn on (1.4.11), not entirely hidden by
#     other content once focused (2.4.11), and focusing opens nothing (3.2.1);
#   - text contrast against its effective background (1.4.3: 4.5:1, 3:1 for
#     large text), and field boundaries at 3:1 (1.4.11);
#   - pointer targets at least 24 by 24 CSS px, or spaced (2.5.8);
#   - a visible label contained in the accessible name (2.5.3);
#   - images with a text alternative (1.1.1, in part);
#   - no text newly cut off with the WCAG text spacing applied (1.4.12);
#   - reflow at 640 and 320 CSS px wide, as at 200% and 400% zoom (no
#     horizontal scrolling or cut-off content) (1.4.10);
#   - status badges that carry text, not colour alone (1.4.1, in part);
#   - once: Ctrl+plus zoom (1.4.4, in part), the page language (3.1.1), the
#     window title (2.4.2) and a polite or assertive status region (4.1.3).
# docs/RELEASE_STANDARD.md, "Accessibility against WCAG 2.2 AA", says what
# each check leaves out. It does not replace testing with screen-reader users
# and pilot users (#27), and passing it is not conformance.
#
#   python tools/accessibility_check.py <report.json> [--colour-mode both]
#
# By default the page is audited in the PC's own colour mode. --colour-mode
# light or dark draws it in that mode instead, and both audits each view in
# light and then in dark (reported as "<view> (light)" and "<view> (dark)").
# The checks are the same in every mode. The mode is set for this window's
# page only (the browser's media emulation and WebView2's preferred colour
# scheme), whatever mode Windows is in, and Windows' setting is never
# changed (#328). A mode the page does not show, in its media query and in
# its styles, stops the run. In the PC's own mode the page is read once,
# with no switch to wait for, and its media query and styles must agree.
#
# Against an installed or extracted package, as release evidence
# (tools/evidence.py), with that package's own Python:
#
#   <v>\runtime\python.exe -I -B -X disable-remote-debug tools\accessibility_check.py
#       <report.json> --app <v>\app --zip <the zip it came from> --colour-mode both

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

# One audit of whatever is currently shown. Returns findings, not a verdict.
# tests/test_accessibility_check.py runs it in headless Edge on synthetic
# fragments that break each check. It runs inside an async function.
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
// Colours: computed colours as [r, g, b, a], relative luminance, the nearest
// solid background behind an element, and the contrast of a colour (blended
// over bg by its alpha) against bg.
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
const ratioOf = (fg, bg) => {
  const a = fg[3] ?? 1;
  const mix = fg.slice(0, 3).map((v, i) => v * a + bg[i] * (1 - a));
  const L1 = lum(mix), L2 = lum(bg);
  return (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
};
const where = () => location.href + ' | ' + [...document.querySelectorAll('dialog[open]')]
  .map(d => d.id || desc(d)).join(',');
// 2.4.11: entirely hidden once focused (and scrolled into view) when no sample
// point on the visible part of the control is the control, or its label, on top.
const hiddenWhenFocused = (el) => {
  const r = el.getBoundingClientRect();
  const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
  const x0 = Math.max(r.left, 0), x1 = Math.min(r.right, vw);
  const y0 = Math.max(r.top, 0), y1 = Math.min(r.bottom, vh);
  if (x1 - x0 < 1 || y1 - y0 < 1) return true;
  const own = [el, ...(el.labels || [])];
  const through = getComputedStyle(el).pointerEvents === 'none';
  for (const fx of [0.1, 0.5, 0.9]) for (const fy of [0.1, 0.5, 0.9]) {
    const top = document.elementFromPoint(x0 + (x1 - x0) * fx, y0 + (y1 - y0) * fy);
    if (top && own.some(o => o.contains(top) || (through && top.contains(o)))) return false;
  }
  return true;
};
// 1.4.11 for the focus indicator: the outline (or, without one, the shadow)
// against what it is drawn on; null when the browser draws it (outline: auto).
const ringRatio = (el, s) => {
  if (s.outlineStyle === 'auto') return null;
  const next = el.parentElement || el;
  if (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0)
    return ratioOf(rgb(s.outlineColor), parseFloat(s.outlineOffset) < 0 ? bgOf(el) : bgOf(next));
  const c = (s.boxShadow || '').match(/rgba?\([^)]*\)/);
  return c ? ratioOf(rgb(c[0]), bgOf(next)) : null;
};
// Focus indicator: keyboard-style focus on each control and compare styles.
// The same pass checks that the focused control is not hidden (2.4.11), its
// ring's contrast (1.4.11) and that focusing it opens or navigates nothing (3.2.1).
// While the window does not have keyboard focus (another window is in front)
// the browser draws no focus style at all, so nothing can be measured: the
// pass stops and says so (focus_not_measured) rather than report every
// control as having no focus indicator (#298). It stops the same way when the
// browser does not treat the focus as keyboard focus (:focus-visible does not
// match, as after a mouse click in the window), since a page's ring usually
// shows only on keyboard focus.
const noFocusRing = [], focusObscured = [], lowContrastFocus = [], changeOnFocus = [];
const focusNotMeasured = [];
const focusable = [];
for (const el of controls.filter(e => !e.disabled && e.tabIndex >= 0).slice(0, 80)) {
  const was = where();
  el.focus({focusVisible: true});
  if (where() !== was) changeOnFocus.push(desc(el));
  if (!document.hasFocus()) {
    focusNotMeasured.push('the window lost keyboard focus at ' + desc(el) +
                          ': keep it in front and run again');
    break;
  }
  const s = getComputedStyle(el);
  const ring = (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) ||
               (s.boxShadow && s.boxShadow !== 'none');
  if (document.activeElement !== el) continue;
  if (!el.matches(':focus-visible')) {
    focusNotMeasured.push('the browser did not treat focus on ' + desc(el) +
                          ' as keyboard focus: press Tab once in the window and run again');
    break;
  }
  focusable.push(el);
  if (!ring) noFocusRing.push(desc(el));
  if (hiddenWhenFocused(el)) focusObscured.push(desc(el));
  const ratio = ring ? ringRatio(el, s) : null;
  if (ratio !== null && ratio < 3) lowContrastFocus.push(desc(el) + ' ' + ratio.toFixed(2));
}
document.activeElement?.blur();
// 2.1.2: Tab and Shift+Tab are not swallowed while focus stays put, and focus
// can move on to the next focusable control without being pulled back.
const keyboardTrap = [];
for (let i = 0; i < focusable.length; i++) {
  const el = focusable[i], other = focusable[(i + 1) % focusable.length];
  el.focus({focusVisible: true});
  if (document.activeElement !== el) continue;
  let stuck = false;
  for (const shiftKey of [false, true]) {
    const ev = new KeyboardEvent('keydown', {key: 'Tab', code: 'Tab', keyCode: 9, shiftKey,
                                             bubbles: true, cancelable: true});
    el.dispatchEvent(ev);
    if (ev.defaultPrevented && document.activeElement === el) stuck = true;
    if (document.activeElement !== el) el.focus({focusVisible: true});
  }
  if (other !== el && !el.contains(other)) {
    other.focus({focusVisible: true});
    await new Promise(r => setTimeout(r, 0));
    if (document.activeElement === el) stuck = true;
  }
  if (stuck) keyboardTrap.push(desc(el));
}
document.activeElement?.blur();
// Contrast of visible text against its effective background.
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
// 1.4.11 for the boundary of a field (or of a checkbox the page draws itself):
// its border or its fill reaches 3:1 against what is around it, on the field or
// on a wrapper (up to two levels) that contains no other control.
const FIELD = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio])' +
  ':not([type=button]):not([type=submit]):not([type=reset]):not([type=image])' +
  ':not([type=range]):not([type=file]):not([type=color]), select, textarea';
const ANY_CONTROL = 'input:not([type=hidden]), select, textarea, button, a[href]';
const edgeRatio = (el) => {
  const s = getComputedStyle(el), around = bgOf(el.parentElement || el);
  let best = ratioOf(rgb(s.backgroundColor), around);
  for (const side of ['Top', 'Right', 'Bottom', 'Left'])
    if (s[`border${side}Style`] !== 'none' && parseFloat(s[`border${side}Width`]) >= 1)
      best = Math.max(best, ratioOf(rgb(s[`border${side}Color`]), around));
  return best;
};
const lowContrastBoundary = [];
for (const el of controls) {
  if (el.disabled || !(el.matches(FIELD) || (el.matches('input[type=checkbox], input[type=radio]')
      && getComputedStyle(el).appearance === 'none'))) continue;
  let best = edgeRatio(el);
  for (let up = el.parentElement, n = 0; up && n < 2 && best < 3; up = up.parentElement, n++) {
    if (up === document.body || up.querySelectorAll(ANY_CONTROL).length > 1) break;
    best = Math.max(best, edgeRatio(up));
  }
  if (best < 3) lowContrastBoundary.push(desc(el) + ' ' + best.toFixed(2));
}
// 2.5.8: each pointer target is at least 24 by 24 CSS px (a label that operates
// the control counts as its target), unless it is inline in text, sized by the
// browser (a checkbox or radio button left as the browser draws it), or spaced
// so that a 24 px circle on its centre meets no other target and no other such
// circle. Behind a modal dialog nothing is a target.
let modal = null;
try { modal = document.querySelector('dialog:modal'); } catch (e) { modal = null; }
const native = {};
{
  const host = document.createElement('div');
  document.body.append(host);
  const root = host.attachShadow({mode: 'open'});     // no page styles reach the probe
  for (const type of ['checkbox', 'radio']) {
    const probe = document.createElement('input');
    probe.type = type;
    root.append(probe);
    const r = probe.getBoundingClientRect();
    native[type] = [r.width, r.height];
  }
  host.remove();
}
const targetBox = (el) => {
  let r = el.getBoundingClientRect();
  for (const l of el.labels || []) {
    const lr = l.getBoundingClientRect();
    if (visible(l) && lr.width * lr.height > r.width * r.height) r = lr;
  }
  return r;
};
const exempt = (el) => {
  const s = getComputedStyle(el);
  if (s.display === 'inline' && el.parentElement &&
      el.parentElement.textContent.trim().length > el.textContent.trim().length) return true;
  if (['checkbox', 'radio'].includes(el.type) && s.appearance !== 'none') {
    const r = el.getBoundingClientRect(), n = native[el.type];
    if (Math.abs(r.width - n[0]) < 0.5 && Math.abs(r.height - n[1]) < 0.5) return true;
  }
  return false;
};
const targets = controls.filter(el => !el.disabled && (!modal || modal.contains(el)) &&
  (el.tabIndex >= 0 || el.matches('input, select, textarea, button, a[href], summary')))
  .map(el => ({el, r: targetBox(el)}));
const centre = (r) => [r.left + r.width / 2, r.top + r.height / 2];
const toRect = ([x, y], r) => Math.hypot(Math.max(r.left - x, 0, x - r.right),
                                         Math.max(r.top - y, 0, y - r.bottom));
const under = targets.filter(t => (t.r.width < 24 || t.r.height < 24) && !exempt(t.el));
const smallTargets = [];
for (const t of under) {
  const c = centre(t.r);
  const apart = (o) => o !== t && !o.el.contains(t.el) && !t.el.contains(o.el);
  const clash = targets.some(o => apart(o) && toRect(c, o.r) < 12) ||
    under.some(o => apart(o) && Math.hypot(c[0] - centre(o.r)[0], c[1] - centre(o.r)[1]) < 24);
  if (clash) smallTargets.push(`${desc(t.el)} ${t.r.width.toFixed(0)}x${t.r.height.toFixed(0)}`);
}
// 2.5.3: a control named by aria-label or aria-labelledby has its visible label
// in that name, as whole words (letters and digits, ignoring case and
// punctuation), so a label that is only part of a word in the name fails.
const words = (t) => (t || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
const shownText = (root) => {
  let out = '';
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walk.nextNode()) {
    const p = walk.currentNode.parentElement;
    if (!p || !visible(p)) continue;
    const inner = p.closest('input, select, textarea, button, option');
    if (inner && inner !== root && root.contains(inner)) continue;
    out += ' ' + walk.currentNode.textContent;
  }
  return out;
};
const labelNotInName = [];
for (const el of controls) {
  if (!el.hasAttribute('aria-label') && !el.hasAttribute('aria-labelledby')) continue;
  const own = ['BUTTON', 'A', 'SUMMARY'].includes(el.tagName) ||
    el.getAttribute('role') === 'button';
  const label = words(own ? shownText(el) : [...(el.labels || [])].map(shownText).join(' '));
  if (label && !(' ' + words(nameOf(el)) + ' ').includes(' ' + label + ' '))
    labelNotInName.push(desc(el));
}
// 1.1.1 (in part): every image has a text alternative (alt="" for decoration).
const noAlt = [...document.querySelectorAll('img, input[type=image], svg[role=img], [role=img]')]
  .filter(el => visible(el) && !el.closest('dialog:not([open])') &&
    (el.tagName === 'IMG' || el.type === 'image' ? !el.hasAttribute('alt')
     : !nameOf(el) && !el.querySelector(':scope > title')?.textContent.trim()))
  .map(desc);
// 1.4.12: with the WCAG text spacing applied, no text is newly cut off by a box
// that hides its overflow (overlapping text, and text cut off before, are not
// measured). The spacing comes off again even if the measuring fails, so the
// page (and every later view) is audited as it is drawn.
const clipped = () => {
  const out = new Set();
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('dialog:not([open])') || !visible(el)) continue;
    const s = getComputedStyle(el);
    const x = /hidden|clip/.test(s.overflowX) && el.scrollWidth > el.clientWidth + 1;
    const y = /hidden|clip/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 1;
    if ((x || y) && el.innerText.trim()) out.add(el);
  }
  return out;
};
const before = clipped();
const spacing = document.createElement('style');
spacing.textContent = '*, *::before, *::after { line-height: 1.5 !important; ' +
  'letter-spacing: 0.12em !important; word-spacing: 0.16em !important; } ' +
  'p { margin-bottom: 2em !important; }';
let textSpacing;
document.head.append(spacing);
try { textSpacing = [...clipped()].filter(el => !before.has(el)).map(desc); }
finally { spacing.remove(); }
// Status badges: must carry words, not only colour.
const colourOnly = [...document.querySelectorAll('.badge, .status, .dot, [class*="state"]')]
  .filter(el => visible(el) && !el.textContent.trim() && !nameOf(el)).map(desc);
const dialogs = [...document.querySelectorAll('dialog[open]')]
  .filter(d => !nameOf(d)).map(desc);
return {controls: controls.length, unnamed, untabbable, pointer_only: pointerOnly,
        no_focus_ring: noFocusRing, low_contrast: lowContrast.slice(0, 40),
        colour_only: colourOnly, unnamed_dialogs: dialogs,
        keyboard_trap: keyboardTrap, focus_obscured: focusObscured,
        low_contrast_focus: lowContrastFocus, low_contrast_boundary: lowContrastBoundary,
        small_targets: smallTargets, label_not_in_name: labelNotInName, no_alt: noAlt,
        text_spacing_cut_off: textSpacing.slice(0, 40), change_on_focus: changeOnFocus,
        focus_not_measured: focusNotMeasured};
"""

# Each list in a view's audit that must be empty, with the criterion it serves.
CHECKS = {
    "unnamed": "4.1.2", "unnamed_dialogs": "4.1.2", "untabbable": "2.1.1",
    "pointer_only": "2.1.1", "keyboard_trap": "2.1.2", "no_focus_ring": "2.4.7",
    "focus_obscured": "2.4.11", "low_contrast": "1.4.3", "low_contrast_focus": "1.4.11",
    "low_contrast_boundary": "1.4.11", "small_targets": "2.5.8", "label_not_in_name": "2.5.3",
    "no_alt": "1.1.1", "text_spacing_cut_off": "1.4.12", "change_on_focus": "3.2.1",
    "colour_only": "1.4.1", "focus_not_measured": "2.4.7",
}

# The colour mode the page is drawn in (prefers-color-scheme).
COLOUR_MODE = "return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';"
# The same, and the colour scheme the page's own styles are drawn in:
# workspace/ui/tokens.css sets color-scheme with the media query, so both
# agree only once the page has really switched (#328).
PAGE_MODE = ("return {media: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : "
             "'light', drawn: getComputedStyle(document.documentElement).colorScheme};")

# --colour-mode: the modes each view is audited in, in order. "system" leaves
# the page in the PC's own colour mode (Windows' app mode) and is the default.
COLOUR_CHOICES = ("system", "light", "dark", "both")
_MODES = {"system": [None], "light": ["light"], "dark": ["dark"], "both": ["light", "dark"]}


def colour_modes(choice):
    """The colour modes to audit each view in: None is the PC's own."""
    return list(_MODES[choice])


def colour_mode_shown(asked, shown, drawn=None):
    """The mode the page shows, checked against the one asked for (None: the
    PC's own, either), and, when given, against the colour scheme its styles
    are drawn in. A mode the page did not switch to stops the run, so a
    report never claims a mode that was not audited."""
    if shown not in ("light", "dark") or (asked and shown != asked):
        raise RuntimeError(f"colour mode asked for: {asked or 'system'}; the page shows: "
                           f"{shown!r}")
    if drawn is not None and drawn != shown:
        raise RuntimeError(f"colour mode asked for: {asked or 'system'}; the page shows: "
                           f"{shown!r} but its styles are drawn in {drawn!r}")
    return shown


def view_key(name, shown, modes):
    """A view's name in the report: with both modes, each is its own view."""
    return f"{name} ({shown})" if len(modes) > 1 else name


def emulated_media(mode):
    """DevTools Emulation.setEmulatedMedia parameters for a colour mode ("" for
    None: the PC's own again)."""
    return json.dumps({"features": [{"name": "prefers-color-scheme", "value": mode or ""}]})


def _outcome(task, wait):
    """What a DevTools call's .NET Task came to, waiting up to `wait` seconds.
    The task finishes on the window's UI thread, so it is never waited on there."""
    import time
    end = time.monotonic() + wait
    while not task.IsCompleted and time.monotonic() < end:
        time.sleep(0.05)
    if not task.IsCompleted:
        return "no answer"
    if task.IsFaulted or task.IsCanceled:
        return f"failed: {str(task.Exception)[:200]}"
    return "set"


def set_colour_mode(window, mode, wait=10.0):
    """Draw this window's page in a colour mode ("light", "dark", or None for
    the PC's own), in two ways, because WebView2's profile setting alone left
    the page in Windows' mode (#328):
    - the browser's media emulation for this page (the DevTools protocol's
      Emulation.setEmulatedMedia), which overrides prefers-color-scheme;
    - WebView2's preferred colour scheme for the window's profile.
    Both change only what this check's window reports to its page, never
    Windows' setting or the app, and the page's styles and checks are the
    same. Returns what each did (for the error if the page does not switch);
    show_colour_mode checks what the page shows."""
    from Microsoft.Web.WebView2.Core import CoreWebView2PreferredColorScheme as Scheme
    from System import Action
    form = window.native
    control = form.browser.webview
    scheme = {None: Scheme.Auto, "light": Scheme.Light, "dark": Scheme.Dark}[mode]
    done = {}

    def apply():
        core = control.CoreWebView2
        try:
            done["emulation"] = core.CallDevToolsProtocolMethodAsync(
                "Emulation.setEmulatedMedia", emulated_media(mode))
        except Exception as exc:
            done["emulation"] = f"failed: {type(exc).__name__}: {exc}"[:200]
        try:
            core.Profile.PreferredColorScheme = scheme
            done["profile"] = "set"
        except Exception as exc:
            done["profile"] = f"failed: {type(exc).__name__}: {exc}"[:200]
    form.Invoke(Action(apply))          # WebView2 must be used on the UI thread
    if not isinstance(done.get("emulation"), str):
        done["emulation"] = _outcome(done["emulation"], wait)
    return done


def show_colour_mode(window, mode, wait=10.0):
    """Draw the page in a colour mode (None: leave it in the PC's own) and
    return the mode it shows, once the media query and the page's styles both
    show it (within `wait` seconds); otherwise the run stops."""
    import time

    from workspace_ui_check import evaluate
    done = set_colour_mode(window, mode, wait) if mode else {}
    end = time.monotonic() + wait
    while True:
        page = evaluate(window, PAGE_MODE) or {}
        if not mode or (page.get("media") == page.get("drawn") == mode) \
                or time.monotonic() >= end:
            break
        time.sleep(0.2)
    try:
        return colour_mode_shown(mode, page.get("media"), page.get("drawn"))
    except RuntimeError as exc:
        raise RuntimeError(f"{exc} (switched by: {done})" if done else str(exc)) from None

PAGE = ("return {lang: document.documentElement.lang, title: document.title, "
        "live_status: document.getElementById('status')?.getAttribute('aria-live')};")

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


def page_problems(page):
    """3.1.1 Language of Page, 2.4.2 Page Titled and 4.1.3 Status Messages (a
    status region the page announces through): recorded before #298, now failed."""
    page = page or {}
    out = []
    lang = page.get("lang") or ""
    if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*", lang):
        out.append(f"3.1.1: the page language is missing or not a language tag ({lang!r})")
    if not (page.get("title") or "").strip():
        out.append("2.4.2: the window has no title")
    if page.get("live_status") not in ("polite", "assertive"):
        out.append(f"4.1.3: the status region is missing or not live "
                   f"({page.get('live_status')!r})")
    return out


def findings(report):
    """Every finding in a report, as lines; empty when it passes."""
    out = [] if not report.get("error") else [report["error"]]
    kz = report.get("keyboard_zoom") or {}
    if not (report.get("zoomable") and kz.get("zoomed", 0) > kz.get("before", 0) * 1.05
            and abs(kz.get("reset", 0) - kz.get("before", -1)) < 0.01):
        out.append(f"keyboard zoom does not work ({kz})")
    out += page_problems(report.get("page"))
    for view, a in report.get("views", {}).items():
        for key in CHECKS:
            if a.get(key):
                out.append(f"{view}: {key} ({len(a[key])})")
        for width, r in a.get("reflow", {}).items():
            if r["css_width"] > int(width) + 1:
                out.append(f"{view}: reflow width {width} not reached ({r['css_width']})")
            if r["horizontal_overflow_px"] > 0 or r["cut_off"]:
                out.append(f"{view}: content cut off or scrolling at {width} CSS px "
                           f"({', '.join(r['cut_off'][:3]) or 'page'})")
    return out


def reflow(window):
    """Reflow like browser zoom: a narrower viewport at normal zoom. 640 CSS px
    is 200% zoom of a 1280 px window; 320 CSS px is the WCAG 1.4.10 width."""
    import time

    from workspace_ui_check import evaluate
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


# Each view the check opens, in order, with the page script that opens it
# ({mid}: the synthetic meeting's id). tools/evidence.py requires every one
# of them, in light and in dark, in release evidence (ACCESSIBILITY_VIEWS).
VIEWS = {
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
    "meeting": "await window.workspace.openMeeting('{mid}'); await wait(1500);",
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


def checks(window, ws, mid, report, modes=(None,)):
    import time

    from workspace_ui_check import WAIT, evaluate
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
        report["page"] = evaluate(window, PAGE)
        report["views"] = {}
        # Each view is opened once and audited in each colour mode asked for.
        for name, go in VIEWS.items():
            evaluate(window, WAIT + go.replace("{mid}", mid) + "return true;")
            for mode in modes:
                shown = show_colour_mode(window, mode)
                audit = evaluate(window, AUDIT)
                audit["colour_mode"] = shown
                audit["open_dialogs"] = evaluate(window, "return [...document.querySelectorAll("
                                                         "'dialog[open]')].map(d => d.id);")
                audit["reflow"] = reflow(window)
                report["views"][view_key(name, shown, modes)] = audit
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        window.destroy()


def main():
    sys.path.append(str(Path(__file__).resolve().parent))   # also under -I; after the stdlib
    # workspace_ui_check puts the app checked (this checkout, or --app) on the path.
    import evidence
    from workspace_ui_check import APP, FAKE, write_wav

    from workspace import api, shell
    from workspace.app import Workspace
    ap = argparse.ArgumentParser(description="Technical accessibility audit (#27, #298).",
                                 allow_abbrev=False)
    ap.add_argument("report", help="where to write the JSON report")
    ap.add_argument("--app", help="an installed or extracted package's app folder (run with "
                    "that package's Python: runtime\\python.exe -I -B -X disable-remote-debug)")
    ap.add_argument("--zip", help="the zip that package came from: records it as release "
                    "evidence (tools/evidence.py); exit 1 unless that evidence passes")
    ap.add_argument("--colour-mode", choices=COLOUR_CHOICES, default=COLOUR_CHOICES[0],
                    help="the colour mode to audit the page in: the PC's own (system, the "
                    "default), light, dark, or both (each view in light, then dark)")
    args = ap.parse_args()
    modes = colour_modes(args.colour_mode)
    before = evidence.prepare(ap, args, APP if args.app else None)
    out = Path(args.report)
    report = {"colour_mode": args.colour_mode}
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
        shell.run(checks, (window, ws, mid, report, modes))
        ws.close()
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    problems = findings(report)
    print("FINDINGS:\n- " + "\n- ".join(problems) if problems else "NO FINDINGS")
    if args.zip:
        ev = evidence.stamp_file(out, "accessibility", args.zip, APP.parent, passed=not problems,
                                 before=before)
        return 0 if ev["pass"] else 1
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
