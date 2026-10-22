# tools/workspace_ui_check.py
# #20 UI check: run the real workspace window (WebView2) on a throwaway
# workspace with synthetic audio and fake engines, drive the page like a user
# and record evidence as JSON. Opens a window for about a minute; audio is muted.
# Includes a meeting that stops because its speech model is not ready and is
# moved to another model (#244); no real model is downloaded or loaded.
#
#   python tools/workspace_ui_check.py <report.json>
#
# Against an installed or extracted package, as release evidence
# (tools/evidence.py), with that package's own Python:
#
#   <v>\runtime\python.exe -I -B -X disable-remote-debug tools\workspace_ui_check.py
#       <report.json> --app <v>\app --zip <the zip it came from>

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import types
import wave
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
# evidence.py, process_listeners.py and the fake engines (tests/), also under -I,
# after the standard library and the app's packages so they cannot shadow one.
sys.path.append(str(TOOLS))
sys.path.append(str(TOOLS.parent / "tests"))
import evidence  # noqa: E402

# The app checked: this checkout, or a package's app folder with --app.
APP = evidence.app_folder(TOOLS.parent)
evidence.add_app(APP)
from process_listeners import listeners, process_tree  # noqa: E402

from workspace import asr_engines  # noqa: E402
from workspace import api, shell  # noqa: E402
from workspace.app import Workspace  # noqa: E402

FAKE = "test:fake_worker_engines:"
# #244: a meeting that stops because its chosen speech model is not ready, then
# moves to a ready model. The not-ready model is named only: this check's model
# folder is a new, empty one, so nothing is downloaded or loaded.
FAILED_TITLE = "synthetic-model-not-ready"
NOT_READY_MODEL = asr_engines.RECOMMENDED_PARAKEET
READY_MODEL = FAKE + "sentences"
ANOTHER_MODEL = "Transcribe with another model…"
VAULT = {}          # stands in for Windows Credential Manager during the check
OPENED = []         # addresses Copy and open (#109) asked to open: no browser starts
HOSTILE = '<img src=x onerror="window.__pwned=1"> and <script>window.__pwned=2</script>'
SR = 16000
# A confirmation still open this long after its script has finished (a lost
# click, or a locked screen, where it cannot be answered) is closed, so the
# check fails instead of waiting for ever.
STUCK_DIALOG_S = 20


def write_wav(path, seconds):
    rng = np.random.default_rng(3)
    pcm = (rng.standard_normal(int(seconds * SR)) * 300).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


def evaluate(window, js, timeout=60):
    """Run an async JS expression and wait for its value."""
    done, box = threading.Event(), {}

    def cb(value):
        box["v"] = value
        done.set()
    window.evaluate_js(f"(async () => {{ {js} }})()", cb)
    if not done.wait(timeout):
        raise TimeoutError(js[:60])
    return box.get("v")


WAIT = "const wait = (ms) => new Promise(r => setTimeout(r, ms));"
# Ends an edit as a person leaving the passage would (#308). Chromium sends
# `blur` only while the page has focus, and a check started from a terminal
# often cannot bring its window to the front (docs/DEVELOPER_NOTES.md), so the
# page's stopEditing would never run. So when `el` held the focus and no
# native `blur` arrived during blur() (Chromium sends it synchronously), the
# event a focused window would deliver is dispatched here: exactly one blur,
# even if the window gains or loses focus mid-call, and none for an element
# that was not focused. Returns whether the page had focus, for the step's
# report. Focus itself needs no help: focus() and blur() move
# document.activeElement whether or not the window has focus.
BLUR_JS = """
    const blurLikeFocused = (el) => {
        const focused = document.hasFocus(), held = document.activeElement === el;
        let sent = false;
        const seen = () => { sent = true; };
        el.addEventListener('blur', seen, { once: true });
        el.blur();
        el.removeEventListener('blur', seen);
        if (held && !sent) el.dispatchEvent(new FocusEvent('blur'));
        return focused; };"""
# A copy fails when another program holds the Windows clipboard for longer
# than the app waits for it (about a second, workspace/clipboard.py), and the
# app then says so in the copy's status. Concurrent test runs on the same
# desktop did that to every check on 23 October 2026 (#342). So a copy step
# whose status is that message waits and clicks again, up to COPY_TRIES clicks
# over about 30 s, and reports how many retries it needed. Any other status
# ends the retries at once, and a copy that stays busy keeps the busy status,
# so the step fails exactly as it did before. `attempt` clicks once and
# resolves to the status the click left.
CLIPBOARD_BUSY = "The clipboard is in use by another program. Try again."
COPY_TRIES = 10
COPY_GAP_MS = 2000
COPY_RETRY_JS = f"""
    const copyRetrying = async (attempt) => {{
        let retries = 0, status = await attempt();
        while (String(status).includes('{CLIPBOARD_BUSY}') && retries < {COPY_TRIES - 1}) {{
            retries++;
            await wait({COPY_GAP_MS});
            status = await attempt();
        }}
        return {{status, retries}}; }};"""


def page_dialogs():
    """Visible top-level dialog windows (#32770) of this process."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    found, pid = set(), os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, name, 64)
        if owner.value == pid and name.value == "#32770" and user32.IsWindowVisible(hwnd):
            found.add(hwnd)
        return True
    user32.EnumWindows(each, 0)
    return found


def find_dialog(title, timeout=15):
    """The top-level window of a confirmation with this title, once shown, in
    this process only: a real Unminute's confirmation open at the same time
    (such as "Discard this meeting?") is never pressed or closed."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    user32.FindWindowExW.restype = wintypes.HWND
    user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR,
                                     wintypes.LPCWSTR]
    pid = os.getpid()
    end = time.time() + timeout
    while time.time() < end:
        hwnd = None
        while True:                           # every top-level window with the title
            hwnd = user32.FindWindowExW(None, hwnd, None, title)
            if not hwnd:
                break
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                return hwnd
        time.sleep(0.05)
    return 0


def dialog_button(hwnd, text):
    """A button inside the dialog, by its text (they sit in layout panels)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(child, _):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(child, buf, 64)
        if buf.value == text:
            found.append(child)
            return False
        return True
    user32.EnumChildWindows(hwnd, each, 0)
    return found[0] if found else 0


PRESS_STEPS = ("wait", "click", "enter", "tab", "space-on", "toggle-on", "enter-on", "shift-on",
               "esc-on", "invoke-on", "until-enabled")
ENABLE_WAIT_S = 5          # "until-enabled" gives up after this (the action needs about 1 s)


def press(script, title, seen=None):
    """Act on the confirmation as a person would, through Windows messages
    (the page has no way to): "click:<button or text>", "enter", "tab",
    "enter-on:<control>", "esc-on:<control>", "space-on:<statement>",
    "toggle-on:<statement>", "invoke-on:<button>" (BM_CLICK: a Click with no
    key, mouse or focus, the path of an assistive tool's Invoke),
    "shift-on:<control>" (a key that does nothing but count as a key press),
    "until-enabled:<button>" (waits until Windows shows the button enabled;
    the script fails if it is not within ENABLE_WAIT_S),
    "wait:1.3", "open?" (notes whether the window is still open).

    The statement is never ticked by a posted click. WinForms' CheckBox acts
    on a mouse-up only if WindowFromPoint at that point on screen is the check
    box itself. A check started from a terminal usually cannot take the
    foreground (Windows' foreground lock), so the window the user has in front
    covers the confirmation, the tick is lost and the confirmation stays open
    until it is closed as stuck. Instead:
    - "space-on" posts Space to the check box itself, as a keyboard user's
      Space would arrive. It ticks the box whatever has the focus, so it
      proves nothing about the tab order: the keyboard script's Tab count,
      which must leave the focus on the action, does that.
    - "toggle-on" posts BM_CLICK, which WinForms turns into the check box's
      Click with no key or mouse input. Only the tick itself (CheckedChanged)
      can then restart the wait, so the "tick first" script shows that it does.
    The action button is not affected: the app answers its mouse-up from the
    button's own client rectangle (workspace/shell.py, mouse_up_on_action).
    tests/test_evidence.py shows all of this on a stand-in form: a posted
    click ticks the statement when nothing covers it and is lost when
    something does, and Space and BM_CLICK tick it while it is covered, BM_CLICK
    with no key or mouse event (docs/DEVELOPER_NOTES.md)."""
    import ctypes
    # A step not listed here would do nothing, and the confirmation would only
    # show up later as stuck: refuse the script instead (the caller then closes
    # the confirmation and the check fails on "pressed").
    unknown = [s for s in script if s != "open?" and s.partition(":")[0] not in PRESS_STEPS]
    if unknown:
        print(f"press: unknown step(s) {unknown}", file=sys.stderr)
        return False
    user32 = ctypes.WinDLL("user32")
    hwnd = find_dialog(title)
    if not hwnd:
        return False
    for step in script:
        if step == "open?":
            if seen is not None:
                seen.append(bool(user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd)))
            continue
        kind, _, arg = step.partition(":")
        if kind == "wait":
            time.sleep(float(arg))
        elif kind == "click":
            # The mouse messages a real click sends, at the control's centre.
            # (WinForms turns BM_CLICK into a Click with no mouse event, which
            # would skip the app's mouse rule for the action, on MouseUp.)
            from ctypes import wintypes
            button = dialog_button(hwnd, arg)
            rect = wintypes.RECT()
            user32.GetClientRect(button, ctypes.byref(rect))
            at = (rect.bottom // 2) << 16 | (rect.right // 2)
            user32.PostMessageW(button, 0x0201, 1, at)                        # WM_LBUTTONDOWN
            user32.PostMessageW(button, 0x0202, 0, at)                        # WM_LBUTTONUP
            time.sleep(0.2)
        elif kind == "enter":                                             # Cancel has focus
            cancel = dialog_button(hwnd, "Cancel")
            user32.PostMessageW(cancel, 0x0100, 0x0D, 0)                      # WM_KEYDOWN Enter
            user32.PostMessageW(cancel, 0x0101, 0x0D, 0)
        elif kind == "tab":                                               # to the next control
            user32.PostMessageW(hwnd, 0x0100, 0x09, 0)
            user32.PostMessageW(hwnd, 0x0101, 0x09, 0)
            time.sleep(0.2)
        elif kind == "space-on":                                          # Space on that control
            control = dialog_button(hwnd, arg)
            user32.PostMessageW(control, 0x0100, 0x20, 0)                     # WM_KEYDOWN Space
            user32.PostMessageW(control, 0x0101, 0x20, 0)
            time.sleep(0.2)
        elif kind in ("toggle-on", "invoke-on"):                          # Click, with no input
            control = dialog_button(hwnd, arg)
            user32.PostMessageW(control, 0x00F5, 0, 0)                        # BM_CLICK
            time.sleep(0.2)
        elif kind == "esc-on":                                            # Esc on that control
            control = dialog_button(hwnd, arg)
            user32.PostMessageW(control, 0x0100, 0x1B, 0)                     # WM_KEYDOWN Esc
            user32.PostMessageW(control, 0x0101, 0x1B, 0)
            time.sleep(0.2)
        elif kind == "until-enabled":                                     # the wait is over
            control = dialog_button(hwnd, arg)
            end = time.time() + ENABLE_WAIT_S
            while not (control and user32.IsWindowEnabled(control)):
                if time.time() >= end:
                    print(f"press: {arg!r} was not enabled within {ENABLE_WAIT_S} s",
                          file=sys.stderr)
                    return False
                time.sleep(0.05)
        elif kind == "shift-on":                                          # a key, no effect
            control = dialog_button(hwnd, arg)
            user32.PostMessageW(control, 0x0100, 0x10, 0)                     # WM_KEYDOWN Shift
            user32.PostMessageW(control, 0x0101, 0x10, 0)
            time.sleep(0.05)
        elif kind == "enter-on":                                          # Enter on that button
            button = dialog_button(hwnd, arg)
            user32.PostMessageW(button, 0x0100, 0x0D, 0)
            user32.PostMessageW(button, 0x0101, 0x0D, 0)
            time.sleep(0.2)
    return True


def native_checks(window, ws, mid):
    """The app's own confirmation window, through the scripts below (each rule
    is checked only as far as its script goes): a click on the action is
    ignored at first and until a statement is ticked; Enter posted to Cancel,
    and Enter or Esc posted to the ticked statement, mean Cancel; on the
    action, Enter answers only after the action has held the focus for the
    wait; an Invoke without the focus (BM_CLICK) answers only once the action
    has been enabled for the wait; a key or click before the action is
    available, and a tick, start the wait again; the bridge reports a Cancel without
    acting; and a click on the action works after the wait. The recording
    indicator changes the title and the taskbar overlay and restores them."""
    import ctypes
    import threading

    from System import Func, IntPtr

    from workspace import consent
    out = {}

    def run_with(script, title, call, seen=None):
        done = {}

        def click():
            done["ok"] = press(script, title, seen)
            # Wait for the confirmation to close. If the script found none, keep
            # looking: one that shows late is closed too, rather than waited on.
            end = time.time() + STUCK_DIALOG_S
            while time.time() < end:
                if not find_dialog(title, timeout=0.2) and done["ok"]:
                    return
                time.sleep(0.5)
            hwnd = find_dialog(title, timeout=0.2)
            if hwnd:
                out.setdefault("stuck_dialogs", []).append(title)
                ctypes.WinDLL("user32").PostMessageW(hwnd, 0x0010, 0, 0)       # WM_CLOSE
        clicker = threading.Thread(target=click, daemon=True)
        clicker.start()
        result = call()
        clicker.join(STUCK_DIALOG_S + 15)
        return result, done.get("ok", False)

    title = "Workspace UI check: confirm"
    plain = {"title": title, "body": "Synthetic check. Nothing changes.", "action": "Confirm",
             "style": "primary", "icon": "tick"}
    # An immediate click on the action is ignored (disabled); Enter means Cancel.
    out["early_yes_then_enter"], out["pressed_1"] = run_with(
        ["click:Confirm", "enter"], title, lambda: shell.native_confirm(window, plain))
    # Through the bridge: a No for Discard leaves the meeting in place.
    bridge = api.make_api(ws, lambda: window, decline_pause_s=0)
    result, out["pressed_2"] = run_with(["enter"], consent.DISCARD_TITLE,
                                        lambda: bridge.discard_meeting(mid))
    out["bridge_no"] = result.get("value") == api.CANCELLED
    out["meeting_kept"] = ws.store.meeting(mid) is not None
    # After the delay, Yes answers Yes.
    out["yes_after_delay"], out["pressed_3"] = run_with(
        ["wait:1.3", "click:Confirm"], title, lambda: shell.native_confirm(window, plain))
    # A statement to tick: the action is ignored until it is ticked, and for a
    # moment after ticking; then it answers. Ticked with BM_CLICK, which works
    # behind a foreground window and comes with no key or mouse input, so only
    # the tick itself can have restarted the wait (see press()).
    ticked = dict(plain, title="Workspace UI check: tick first",
                  check="I have read this synthetic question.")
    seen = []
    out["tick_first"], out["pressed_4"] = run_with(
        ["wait:1.3", "click:Confirm", "open?", "toggle-on:" + ticked["check"],
         "click:Confirm", "open?", "wait:1.3", "click:Confirm"], ticked["title"],
        lambda: shell.native_confirm(window, ticked), seen)
    out["open_until_ticked"] = seen
    # Keyboard (and Narrator or voice control): Tab from Cancel to the statement
    # (the disabled action is skipped), tick it with Space, wait, Tab to the
    # action; Enter at once is ignored, Enter after holding the focus answers.
    # Space is posted to the statement, so it ticks whatever has the focus; the
    # Tab count matters because Enter is the action's only if it has the focus.
    # The quick Enter comes long after the action was enabled (2.5 s after the
    # tick) but just after it took the focus, so only the focus hold ignores it.
    keyed = dict(ticked, title="Workspace UI check: keyboard")
    seen_keys = []
    out["keyboard_after_hold"], out["pressed_5"] = run_with(
        ["tab", "space-on:" + keyed["check"], "wait:2.5", "tab", "tab",
         "enter-on:Confirm", "open?", "wait:1.3", "enter-on:Confirm"], keyed["title"],
        lambda: shell.native_confirm(window, keyed), seen_keys)
    out["open_after_quick_enter"] = seen_keys
    # A key press or a click on the dialog before the action is available
    # starts the wait again (keys a person was typing as it appeared): pressed
    # at 0.2, 0.6 and 1.0 s, the action is still ignored at 1.5 s, although
    # without the restart it would have been enabled at 1.0 s.
    keys = dict(plain, title="Workspace UI check: keys wait again")
    seen_keys_restart = []
    out["keys_restart_wait"], out["pressed_6"] = run_with(
        ["wait:0.2", "shift-on:Cancel", "wait:0.35", "shift-on:Cancel", "wait:0.35",
         "shift-on:Cancel", "wait:0.45", "click:Confirm", "open?", "wait:1.3",
         "click:Confirm"], keys["title"],
        lambda: shell.native_confirm(window, keys), seen_keys_restart)
    out["open_after_keys"] = seen_keys_restart
    clicks = dict(plain, title="Workspace UI check: clicks wait again")
    seen_clicks = []
    out["clicks_restart_wait"], out["pressed_7"] = run_with(
        ["wait:0.2", "click:" + plain["body"], "wait:0.2", "click:" + plain["body"],
         "wait:0.2", "click:" + plain["body"], "wait:0.3", "click:Confirm", "open?",
         "wait:1.3", "click:Confirm"], clicks["title"],
        lambda: shell.native_confirm(window, clicks), seen_clicks)
    out["open_after_clicks"] = seen_clicks
    # Enter means Cancel unless the action has the focus: Enter on the ticked
    # statement, long after the action was enabled, cancels.
    on_statement = dict(ticked, title="Workspace UI check: Enter on the statement")
    out["enter_on_statement"], out["pressed_8"] = run_with(
        ["tab", "space-on:" + on_statement["check"], "wait:2.5",
         "enter-on:" + on_statement["check"]], on_statement["title"],
        lambda: shell.native_confirm(window, on_statement))
    # Esc means Cancel (CancelButton): Esc on the ticked statement, long after
    # the action was enabled, closes the window with no answer. "open?" shows
    # Esc closed it, not the stuck-window clean-up.
    escaped = dict(ticked, title="Workspace UI check: Esc on the statement")
    seen_esc = []
    out["esc_on_statement"], out["pressed_9"] = run_with(
        ["tab", "space-on:" + escaped["check"], "wait:2.5",
         "esc-on:" + escaped["check"], "wait:0.5", "open?"], escaped["title"],
        lambda: shell.native_confirm(window, escaped), seen_esc)
    out["open_after_esc"] = seen_esc
    # An Invoke without the focus (Cancel keeps it; BM_CLICK, as an assistive
    # tool's Invoke arrives) answers only once the action has been enabled for
    # the wait (act_clicked, CONFIRM_DELAY_MS): about 0.3 s after it is enabled
    # it is ignored; about 1.8 s after (0.3 + invoke-on's 0.2 + 1.3) it answers.
    # No key or mouse input, so nothing restarts the wait.
    invoked = dict(plain, title="Workspace UI check: Invoke without the focus")
    seen_invoke = []
    out["invoke_after_enabled_wait"], out["pressed_10"] = run_with(
        ["until-enabled:Confirm", "wait:0.3", "invoke-on:Confirm", "open?", "wait:1.3",
         "invoke-on:Confirm"], invoked["title"],
        lambda: shell.native_confirm(window, invoked), seen_invoke)
    out["open_after_quick_invoke"] = seen_invoke
    form = window.native
    hwnd = form.Invoke(Func[IntPtr](lambda: form.Handle)).ToInt64()

    def caption():
        buf = ctypes.create_unicode_buffer(256)
        ctypes.WinDLL("user32").GetWindowTextW(ctypes.c_void_p(hwnd), buf, 256)
        return buf.value
    before = caption()
    out["overlay_on"] = shell.show_recording(window, True)
    time.sleep(0.5)
    out["title_recording"] = caption()
    out["overlay_off"] = shell.show_recording(window, False)
    time.sleep(0.5)
    out["title_restored"] = caption() == before
    return out


def handoff_passed(h, opened):
    """Whether the handoff step's evidence passes. A copy whose clipboard
    stayed busy through every retry (#342) leaves the busy message as its
    status, so it fails here as it did before."""
    return bool(h.get("has_markers") and h.get("named_before") and not h.get("named_after")
                and h.get("redacted") and h.get("left_out") and h.get("edited_prompt")
                and h.get("copy_blocked_until_applied") and h.get("anonymised")
                and h.get("open_label") == "Copy and open ChatGPT"
                and h.get("open_hidden") is False
                and "opened ChatGPT in your browser" in h.get("open_status", "")
                and opened == ["https://chatgpt.com/"])

def native_problems(nv):
    """What the native confirmation scripts found wrong, one entry per rule
    (an empty list: nothing). A script that could not press its window
    ("pressed_N" false) fails its rule too."""
    bad = []
    if nv.get("stuck_dialogs"):
        bad.append("a confirmation was left open (is the screen locked?)")
    rules = (
        ("an early click or Enter answered",
         nv.get("early_yes_then_enter") is False and nv.get("pressed_1")),
        ("a Cancel through the bridge acted",
         nv.get("bridge_no") and nv.get("meeting_kept") and nv.get("pressed_2")),
        ("a click after the wait did not answer",
         nv.get("yes_after_delay") is True and nv.get("pressed_3")),
        ("the action answered before, or just after, the tick",
         nv.get("tick_first") is True and nv.get("open_until_ticked") == [True, True]
         and nv.get("pressed_4")),
        ("Enter answered before the action held the focus",
         nv.get("keyboard_after_hold") is True and nv.get("open_after_quick_enter") == [True]
         and nv.get("pressed_5")),
        ("a key press did not start the wait again",
         nv.get("keys_restart_wait") is True and nv.get("open_after_keys") == [True]
         and nv.get("pressed_6")),
        ("a click did not start the wait again",
         nv.get("clicks_restart_wait") is True and nv.get("open_after_clicks") == [True]
         and nv.get("pressed_7")),
        ("Enter on the statement did not mean Cancel",
         nv.get("enter_on_statement") is False and nv.get("pressed_8")),
        ("Esc on the statement did not mean Cancel",
         nv.get("esc_on_statement") is False and nv.get("open_after_esc") == [False]
         and nv.get("pressed_9")),
        ("an Invoke without the focus answered early, or not after the wait",
         nv.get("invoke_after_enabled_wait") is True
         and nv.get("open_after_quick_invoke") == [True] and nv.get("pressed_10")),
    )
    bad.extend("native confirmation: " + rule for rule, ok in rules if not ok)
    return bad


def checks(window, ws, mid, long_mid, grow_mid, fail_mid, report):
    try:
        window.events.loaded.wait(30)
        while not window.evaluate_js("!!(window.workspace && window.pywebview && "
                                     "window.pywebview.api && window.pywebview.api.meetings)"):
            time.sleep(0.2)
        report["listeners_idle"] = listeners(process_tree(os.getpid()))
        # The WebView2 profile is in the workspace, not %TEMP% (#111 A2).
        profiles = [p for p in (ws.root / "webview").iterdir() if any(p.iterdir())]
        report["webview_profile_in_workspace"] = len(profiles) == 1
        report["api_surface"] = evaluate(window, "return Object.keys(window.pywebview.api)"
                                                 ".filter(k => typeof window.pywebview.api[k]"
                                                 " === 'function').sort();")
        report["setup"] = evaluate(window, WAIT + """
            window.workspace.show('setup');
            for (let i = 0; i < 50 && !window.workspace.state.setup; i++) await wait(200);
            await wait(300);
            const cards = window.workspace.state.setup.cards.length;
            const token = document.getElementById('s-token');
            token.value = 'hf_UIcheckNOTreal123';
            document.getElementById('s-download-speakers').click(); await wait(4000);
            await window.workspace.refresh(); await wait(500);
            const page = document.documentElement.outerHTML;
            return {cards, token_field_type: token.type, token_cleared: token.value === '',
                    token_in_page: page.includes('hf_UIcheckNOTreal123'),
                    remembered_text: document.getElementById('s-token-note').textContent,
                    remove_visible: !document.getElementById('s-remove-token').hidden};""")
        report["setup_vault"] = sorted(VAULT)
        report["lists"] = evaluate(window, WAIT + """
            window.workspace.show('meetings'); await wait(800);
            return {active: [...document.querySelectorAll('#groups li')].map(l =>
                l.textContent),
                    empty_shown: !document.getElementById('meetings-empty').hidden};""")
        report["open_meeting"] = evaluate(window, WAIT + f"""
            await window.workspace.openMeeting('{mid}'); await wait(1500);
            const rows = document.querySelectorAll('#transcript li');
            return {{rows: rows.length, who: rows[0].querySelector('.who')?.textContent,
                    steps: [...document.querySelectorAll('#steps li')].map(l =>
                        l.textContent)}};""")
        report["hostile"] = evaluate(window, WAIT + """
            const rows = document.querySelectorAll('#transcript li');
            const text = rows[2].querySelector('.text');
            return {img: document.querySelectorAll('#transcript img, #transcript script').length,
                    pwned: window.__pwned || 0,
                    shown_as_text: text.textContent.includes('<img src=x')};""")
        # The page cannot put its own text in a native or browser box: its
        # alert is dropped and the browser's dialogs are off (confirm answers
        # false at once instead of waiting for a person).
        before = page_dialogs()
        report["page_dialogs"] = evaluate(window, """
            window.alert('Synthetic: tick the box, then Allow');
            window.chrome.webview.postMessage(['_pywebviewAlert', '"Synthetic"', 'alert']);
            const t0 = performance.now();
            const answer = window.confirm('Synthetic: allow?');
            const confirm_ms = Math.round(performance.now() - t0);
            // The browser's own alert and prompt, reached through a fresh frame
            // (pywebview's alert replacement is not there): each returns at once.
            const frame = document.createElement('iframe');
            document.body.appendChild(frame);
            const t1 = performance.now();
            frame.contentWindow.alert('Synthetic');
            const prompted = frame.contentWindow.prompt('Synthetic', 'x');
            const native_ms = Math.round(performance.now() - t1);
            frame.remove();
            // Browser permissions and screen capture are refused at once, with
            // no prompt or picker: the page can never record or notify outside
            // the app (#111 A6), from the page or from a frame of its own.
            const outcome = async (ask) => {
                try { await ask(); return 'granted'; } catch (err) { return err.name; }
            };
            const t2 = performance.now();
            const microphone = await outcome(
                () => navigator.mediaDevices.getUserMedia({audio: true}));
            const inner = document.createElement('iframe');
            document.body.appendChild(inner);
            const frame_microphone = await outcome(
                () => inner.contentWindow.navigator.mediaDevices.getUserMedia({audio: true}));
            inner.remove();
            const screen = await outcome(
                () => navigator.mediaDevices.getDisplayMedia({video: true, audio: true}));
            const notifications = await Notification.requestPermission();
            const permission_ms = Math.round(performance.now() - t2);
            return {confirm: answer, confirm_ms, prompt: prompted, native_ms,
                    microphone, frame_microphone, screen, notifications, permission_ms};""")
        time.sleep(1.0)
        report["page_dialogs"]["new_windows"] = sorted(page_dialogs() - before)
        report["page_dialogs"]["alert_blocked"] = "_pywebviewAlert" in shell.blocked_calls
        # The refusals came from the app's handlers, not from Windows privacy
        # settings or a missing device.
        report["page_dialogs"]["refused"] = sorted(
            {c for c in shell.blocked_calls if c.startswith(("permission", "screen"))})
        report["page_dialogs"]["microphone_refusals"] = shell.blocked_calls.count(
            "permission Microphone")
        report["edit_autosave"] = evaluate(window, WAIT + BLUR_JS + """
            const span = document.querySelectorAll('#transcript .text')[0];
            span.click(); span.textContent = 'Edited in the page'; span.dispatchEvent(new
                Event('input'));
            await wait(1500); const focused = blurLikeFocused(span); await wait(500);
            return {focused, status: document.getElementById('status').textContent,
                    edited_class: span.classList.contains('edited'),
                    undo_enabled: !document.getElementById('m-undo').disabled};""")
        report["edit_in_db"] = ws.transcript(mid)["items"][0]["text"]
        report["undo"] = evaluate(window, WAIT + """
            document.getElementById('m-undo').click(); await wait(1200);
            return document.querySelectorAll('#transcript .text')[0].textContent;""")
        report["search"] = evaluate(window, WAIT + """
            const box = document.getElementById('m-search');
            box.value = 'sentence 12'; box.dispatchEvent(new Event('input')); await wait(900);
            return {hits: document.getElementById('m-hits').textContent,
                    highlighted: document.querySelectorAll('#transcript li.hit').length,
                    current: document.querySelector('#transcript li.current')?.textContent};""")
        report["playback"] = evaluate(window, WAIT + """
            const p = document.getElementById('player'); p.muted = true;
            document.querySelectorAll('#transcript .t')[1].click(); await wait(1500);
            return {src_is_data_url: p.src.startsWith('data:audio/wav;base64,'),
                    duration: p.duration, playing: !p.paused || p.ended};""")
        report["hold_ui"] = evaluate(window, WAIT + """
            document.getElementById('h-scope').value = 'transcript';
            document.getElementById('h-reason').value = 'Synthetic dispute';
            document.getElementById('h-add').click(); await wait(2500);
            return {holds: [...document.querySelectorAll('#m-holds li')].map(l => l.textContent),
                    badge: document.getElementById('m-badges').textContent};""")
        report["names"] = evaluate(window, WAIT + """
            document.getElementById('m-names').click(); await wait(1500);
            const dlg = document.getElementById('names');
            const sections = dlg.querySelectorAll('.speaker');
            const first = sections[0];
            const examples = first.querySelectorAll('.examples li').length;
            const p = document.getElementById('player'); p.muted = true;
            first.querySelector('.examples button').click(); await wait(1200);
            const clip_s = p.duration;
            const input = first.querySelector('input');
            input.value = 'Aroha'; input.dispatchEvent(new Event('change')); await wait(1500);
            const more = [...dlg.querySelectorAll('button')]
                .find(b => b.textContent === 'More examples');
            if (more) { more.click(); await wait(1000); }
            const after_more = dlg.querySelector('.speaker .examples li span')?.textContent;
            document.getElementById('n-close').click(); await wait(1500);
            const who = document.querySelector('#transcript .who .who-name')?.textContent;
            return {speakers: sections.length, examples, clip_s, after_more,
                    open_after_close: dlg.open, first_label: who};""")
        report["reassign"] = evaluate(window, WAIT + """
            const btn = [...document.querySelectorAll('#transcript .who')]
                .find(b => b.querySelector('.who-name').textContent !== 'Aroha');
            const id = btn.closest('li').dataset.id;
            btn.click(); await wait(800);
            const sel = document.querySelector('#transcript select');
            sel.value = 'new'; sel.dispatchEvent(new Event('change')); await wait(500);
            const unchanged = !!document.querySelector('#transcript select');
            [...document.querySelectorAll('#transcript .reassign button')]
                .find(b => b.textContent === 'Move').click();
            await wait(2000);
            if (!unchanged) return 'moved without Move';
            const row = document.querySelector(`#transcript li[data-id="${id}"]`);
            return row?.querySelector('.who .who-name')?.textContent;""")
        report["confirm_review"] = evaluate(window, WAIT + """
            const primary = document.getElementById('m-primary');
            if (!primary.textContent.includes('Transcript reviewed'))
                return {no_review_button: true};
            primary.click();
            await wait(300);
            const dlg = document.getElementById('confirm');
            const okBefore = document.getElementById('c-ok').disabled;
            const limits = document.getElementById('c-limits').textContent;
            document.getElementById('c-check').click();
            dlg.returnValue = 'ok'; dlg.close('ok'); await wait(4000);
            return {ok_disabled_until_ticked: okBefore, limits_shown: limits.includes('OneDrive'),
                    playback_text: document.getElementById('m-playback').textContent,
                    final_offered: !document.getElementById('m-banner-action').hidden
                        && document.getElementById('m-banner-action').textContent
                        === 'Final document saved…',
                    time_buttons_disabled: document.querySelectorAll('#transcript .t')[0].disabled,
                    edit_refused: (await window.pywebview.api.edit(window.workspace.state.mid,
                        Number(document.querySelector('#transcript .text').dataset.id),
                            'x')).error};""",
                                       timeout=90)
        report["handoff"] = evaluate(window, WAIT + COPY_RETRY_JS + """
            const primary = document.getElementById('m-primary');
            if (!primary.textContent.includes('Hand off')) return {no_handoff_button: true};
            primary.click();
            await wait(2500);
            const pre = document.getElementById('ho-preview');
            const before = pre.textContent;
            document.getElementById('ho-redactions').value = 'Aroha';
            document.querySelector('#ho-passages input').checked = true;
            document.getElementById('ho-apply').click(); await wait(2000);
            const after = pre.textContent;
            document.getElementById('ho-edit').click();
            document.getElementById('ho-t-prompt').value = 'Our own minutes prompt.';
            document.getElementById('ho-t-save').click(); await wait(2000);
            const edited = pre.textContent;
            const tpl = document.getElementById('ho-template').selectedOptions[0].textContent;
            document.getElementById('ho-anon').click();
            const copy_blocked_until_applied = document.getElementById('ho-copy').disabled;
            document.getElementById('ho-apply').click(); await wait(2000);
            const anonymised = pre.textContent;
            // Each click starts from an empty status, so a slow answer is not
            // read as the last click's (#342).
            const copied = await copyRetrying(async () => {
                document.getElementById('ho-status').textContent = '';
                document.getElementById('ho-copy').click(); await wait(1500);
                return document.getElementById('ho-status').textContent
                    || document.getElementById('status').textContent; });
            const status = copied.status;
            const open_label = document.getElementById('ho-open').textContent.trim();
            const open_hidden = document.getElementById('ho-open').hidden;
            const opened = await copyRetrying(async () => {
                document.getElementById('ho-status').textContent = '';
                document.getElementById('ho-open').click(); await wait(1500);
                return document.getElementById('ho-status').textContent; });
            const open_status = opened.status;
            document.getElementById('ho-close').click();
            return {has_markers: before.includes('\\nBEGIN TRANSCRIPT '),
                    named_before: before.includes('Aroha'), named_after: after.includes('Aroha'),
                    redacted: after.includes('[redacted]'),
                    left_out: after.includes('1 passage was left out'),
                    edited_prompt: edited.startsWith('Our own minutes prompt.'),
                    copy_blocked_until_applied,
                    anonymised: anonymised.includes('Speaker 1:') && !anonymised.includes('Aroha'),
                    template_label: tpl, copy_status: status, copy_retries: copied.retries,
                    open_label, open_hidden, open_status, open_retries: opened.retries};""",
                                     timeout=150)
        t0 = time.perf_counter()
        report["long_transcript"] = evaluate(window, WAIT + f"""
            const t = performance.now();
            await window.workspace.openMeeting('{long_mid}');
            while (document.querySelectorAll('#transcript li').length < 5000) await wait(50);
            await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
            const render_ms = performance.now() - t;
            const box = document.getElementById('m-search'); const s = performance.now();
            box.value = 'passage 4321'; await window.workspace.refresh();
            box.dispatchEvent(new Event('input')); await wait(600);
            const lastRow = document.querySelectorAll('#transcript li')[4999];
            lastRow.scrollIntoView(); const scroll_ms = performance.now() - s;
            await wait(300);
            // Wide window: the transcript scrolls in its own pane; the search box
            // and the steps panel stay in view.
            const search = document.getElementById('m-search').getBoundingClientRect();
            const wrap = document.querySelector('.transcript-wrap');
            return {{rows: document.querySelectorAll('#transcript li').length,
                    wide: matchMedia('(min-width: 901px) and (min-height: 600px)').matches,
                    pane_scrolled: wrap.scrollTop > 0, page_scroll_y: Math.round(window.scrollY),
                    search_visible: search.top >= 0 && search.bottom <= window.innerHeight,
                    render_ms: Math.round(render_ms),
                    hit: document.getElementById('m-hits').textContent}};""", timeout=120)
        report["long_transcript"]["wall_s"] = round(time.perf_counter() - t0, 1)
        report["growing"] = growing_checks(window, ws, grow_mid)
        report["native"] = native_checks(window, ws, long_mid)
        report["failed_meeting"] = failed_meeting_checks(window, ws, fail_mid)
        tree = process_tree(os.getpid())
        report["listeners"] = listeners(tree)
        report["blocked_calls"] = list(shell.blocked_calls)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        window.destroy()


GROW_FIRST = 3000      # passages in the growing meeting's first chunk
GROW_MORE = 20         # passages in each chunk after it
GROW_TEXT = "Edited while transcribing"
# The growing transcript's edit ends (grow_update's `first`, #308): unfocused
# or not, the page must see the blur, so the rows held back can be appended.
GROW_END_EDIT = "focused = blurLikeFocused(g.span); await wait(1500);"
# Page helpers for the growing transcript: the rows shown, and a wait for a condition.
GROW_JS = WAIT + BLUR_JS + """
    const rows = () => document.querySelectorAll('#transcript > li');
    const until = async (ok, ms = 20000) => {
        const end = performance.now() + ms;
        while (!ok() && performance.now() < end) await wait(25);
        return ok(); };
    const frames = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const g = window.__grow;
    const edited = () => document.querySelector(`#transcript .text[data-id="${g.id}"]`);"""


def grow_update(window, expect, extra="", first=""):
    """Refresh the page after a chunk was committed and wait until it shows
    `expect` rows: how long that took, what it drew, and whether it redrew
    the whole list. `extra` adds fields (JavaScript, `name: value, ...`).
    `first` (JavaScript) runs after the starting counts are taken, so rows a
    page poll draws during it still count towards this step; it may set
    `focused` to the page's focus at the moment that matters to the step."""
    return evaluate(window, GROW_JS + f"""
        const added = g.added, redraws = g.redraws;
        let focused = document.hasFocus();
        {first}
        const t = performance.now();
        await window.workspace.refresh();
        const ok = await until(() => rows().length === {expect});
        await frames();
        return {{ok, focused, rows: rows().length, ms: Math.round(performance.now() - t),
                 added: g.added - added, redrawn: g.redraws > redraws, {extra}}};""")


def growing_checks(window, ws, grow_mid):
    """#127: a long meeting open while it is transcribed. The check commits
    chunks itself while the page shows the meeting: new passages at the end
    must be appended, not the whole list redrawn, with an edit held back and
    then kept, an undo kept, and the whole list redrawn when appending is not
    safe (a search open, an earlier chunk done after a later one, speakers
    identified). Each step records how long the page took to show the rows
    and what it fetched (counted in Python) and drew (counted in the page)."""
    out, seen = {}, {"calls": 0, "rows": 0}
    real = ws.transcript
    vid = ws.store.active_version(grow_mid)["id"]

    def counted(mid, start=0, count=500, version=None):
        page = real(mid, start, count, version)
        if mid == grow_mid:
            seen["calls"] += 1
            seen["rows"] += len(page["items"])
        return page

    def step(name, result):
        """Record a step with what the page fetched for it."""
        result.update(calls=seen["calls"] - mark["calls"], fetched=seen["rows"] - mark["rows"])
        out[name] = result
        mark.update(seen)
        return result

    ws.transcript = counted                     # the bridge looks it up on each call
    mark = dict(seen)
    try:
        shown = GROW_FIRST
        step("open", evaluate(window, GROW_JS + f"""
            const t = performance.now();
            await window.workspace.openMeeting('{grow_mid}');
            await until(() => rows().length === {shown}, 60000);
            await frames();
            const ms = Math.round(performance.now() - t);
            const list = document.getElementById('transcript');
            window.__grow = {{added: 0, redraws: 0}};
            new MutationObserver(records => {{ for (const r of records) {{
                window.__grow.added += r.addedNodes.length;
                if (r.removedNodes.length) window.__grow.redraws++; }} }})
                .observe(list, {{childList: true}});
            return {{rows: rows().length, ms, focused: document.hasFocus()}};""", timeout=90))
        pending = [c["idx"] for c in ws.store.chunks(vid, status="pending")]
        for n in (1, 2):                        # more passages at the end: appended
            commit_passages(ws, grow_mid, pending.pop(0), GROW_MORE)
            shown += GROW_MORE
            step(f"append_{n}", grow_update(window, shown))
        # An edit starts; transcription goes on. Nothing is drawn under it.
        evaluate(window, GROW_JS + f"""
            const span = document.querySelectorAll('#transcript .text')[1];
            window.__grow.span = span; window.__grow.original = span.textContent;
            window.__grow.id = span.dataset.id;
            span.click(); span.textContent = '{GROW_TEXT}';
            span.dispatchEvent(new Event('input'));""")
        commit_passages(ws, grow_mid, pending.pop(0), GROW_MORE)
        step("while_editing", evaluate(window, GROW_JS + f"""
            const added = g.added, redraws = g.redraws;
            await window.workspace.refresh(); await wait(1500);
            return {{rows: rows().length, focused: document.hasFocus(),
                     held: rows().length === {shown} && g.added === added
                     && g.redraws === redraws && document.activeElement === g.span}};"""))
        # The edit ends. A page poll may append the held-back rows during the
        # wait, so the step's starting counts are taken before the blur.
        shown += GROW_MORE
        step("after_edit", grow_update(
            window, shown, f"kept: edited().textContent === '{GROW_TEXT}'",
            first=GROW_END_EDIT))
        out["edit_saved"] = real(grow_mid, 1, 1)["items"][0]["text"] == GROW_TEXT
        # Undo the edit, then more passages: the undone text stays.
        out["undone"] = evaluate(window, GROW_JS + """
            document.getElementById('m-undo').click(); await wait(1500);
            return edited().textContent === g.original;""")
        commit_passages(ws, grow_mid, pending.pop(0), GROW_MORE)
        shown += GROW_MORE
        step("after_undo", grow_update(
            window, shown, "kept: edited().textContent === g.original"))
        # A search open: the whole list is redrawn, so the matches include the new rows.
        target = pending.pop(0)
        evaluate(window, GROW_JS + f"""
            const box = document.getElementById('m-search');
            box.value = 'growing passage {target}.'; box.dispatchEvent(new Event('input'));
            await wait(800);""")
        commit_passages(ws, grow_mid, target, GROW_MORE)
        shown += GROW_MORE
        step("searching", grow_update(window, shown,
                                      "hits: document.getElementById('m-hits').textContent"))
        evaluate(window, GROW_JS + """
            const box = document.getElementById('m-search');
            box.value = ''; box.dispatchEvent(new Event('input')); await wait(800);""")
        # The last chunk done before the ones in between (one done again after
        # a failure, say): appended at the end; then the earlier ones land
        # mid-list, so the whole list is redrawn.
        commit_passages(ws, grow_mid, pending.pop(), GROW_MORE)
        shown += GROW_MORE
        step("later_chunk", grow_update(window, shown))
        for idx in pending:
            commit_passages(ws, grow_mid, idx, GROW_MORE)
            shown += GROW_MORE
        step("earlier_chunk", grow_update(window, shown,
                                          "ids: [...rows()].map(li => Number(li.dataset.id))"))
        out["earlier_chunk"]["in_order"] = (out["earlier_chunk"].pop("ids")
                                            == ws.store.passage_ids(vid))
        # Speakers identified: every row gets its label, so the list is redrawn.
        evaluate(window, GROW_JS + "g.redrawsBeforeSpeakers = g.redraws;")
        ws.store.set_paused(grow_mid, False)
        ws.rerun_speakers(grow_mid)
        end = time.time() + 120
        while time.time() < end and ((ws.detail(grow_mid)["speakers"] or {}).get("status")
                                     != "done"):
            time.sleep(0.25)
        step("speakers", evaluate(window, GROW_JS + """
            await window.workspace.refresh();
            const labelled = await until(() => !!document.querySelector('#transcript .who'));
            await frames();
            return {labelled, focused: document.hasFocus(),
                    redrawn: g.redraws > g.redrawsBeforeSpeakers,
                    turns: document.querySelectorAll('#transcript li.turn').length,
                    ids: [...rows()].map(li => Number(li.dataset.id))};"""))
        out["speakers"]["in_order"] = out["speakers"].pop("ids") == ws.store.passage_ids(vid)
    finally:
        ws.__dict__.pop("transcript", None)     # the method again
    return out


def growing_problems(gr):
    """What the growing-transcript check found wrong (an empty list: nothing)."""
    bad = []
    if (gr.get("open") or {}).get("rows") != GROW_FIRST:
        bad.append("the first rows were not shown")
    # after_edit: the chunk committed while editing, held back until the edit ended.
    for name in ("append_1", "append_2", "after_edit", "after_undo", "later_chunk"):
        s = gr.get(name) or {}
        if not (s.get("ok") and s.get("redrawn") is False and s.get("added") == GROW_MORE):
            # Whether the window had focus, so a failure shows that cause (#308).
            focus = " (the window had no focus)" if s.get("focused") is False else ""
            bad.append(f"{name}: new passages were not appended{focus}")
    if not (gr.get("while_editing") or {}).get("held"):
        bad.append("rows were drawn while a passage was being edited")
    if not ((gr.get("after_edit") or {}).get("kept") and gr.get("edit_saved")):
        bad.append("the edit was not kept")
    if not (gr.get("undone") and (gr.get("after_undo") or {}).get("kept")):
        bad.append("the undo was not kept")
    s = gr.get("searching") or {}
    if not (s.get("ok") and s.get("redrawn") and s.get("hits") == f"{GROW_MORE} found"):
        bad.append("searching: the list was not redrawn with the new matches")
    s = gr.get("earlier_chunk") or {}
    if not (s.get("ok") and s.get("redrawn") and s.get("in_order")):
        bad.append("earlier chunk: the list was not redrawn in order")
    s = gr.get("speakers") or {}
    if not (s.get("labelled") and s.get("redrawn") and s.get("in_order") and s.get("turns", 0) > 1):
        bad.append("speakers: the list was not redrawn with the speakers")
    return bad


# Page helpers for the failed meeting: its row in the list, and the row's
# Transcribe with another model button.
FAILED_JS = WAIT + f"""
    const row = () => [...document.querySelectorAll('#groups li')]
        .find(li => li.textContent.includes('{FAILED_TITLE}'));
    const another = (li) => li && [...li.querySelectorAll('button')]
        .find(b => b.textContent === {json.dumps(ANOTHER_MODEL)});
    const meetings = async () => {{
        window.workspace.show('meetings'); await window.workspace.refresh();
        for (let i = 0; i < 50 && !row(); i++) await wait(200);
        await wait(300); }};"""


def failed_meeting_checks(window, ws, fail_mid):
    """#244: a meeting that stopped because its chosen speech model is not
    ready (add_failed_meeting). Its row offers Transcribe with another model;
    with the same model still chosen, that says the model is still not ready
    and changes nothing. Settings marks the model; a ready model is chosen
    there as a person would; the row's confirmed Transcribe again then takes
    the meeting out of Needs attention, with the failed version kept as an
    earlier version."""
    out = {"model_label": asr_engines.label(NOT_READY_MODEL)}
    chosen = next(c for c in ws.get_settings()["choices"] if c["name"] == NOT_READY_MODEL)
    out["downloads_offered"] = chosen["offered"]       # False when policy turns downloads off
    versions = [v["id"] for v in ws.store.versions(fail_mid)]
    out["failed"] = evaluate(window, FAILED_JS + """
        await meetings();
        const li = row(), b = another(li);
        const out = {row_found: !!li, needs_attention: !!li && li.textContent
                         .includes('Needs attention'), offered: !!b};
        if (!b) return out;
        b.click();
        const dlg = document.getElementById('model-dlg');
        for (let i = 0; i < 50 && !dlg.open; i++) await wait(100);
        await wait(300);
        Object.assign(out, {dialog_open: dlg.open,
            title: document.getElementById('md-title').textContent,
            body: document.getElementById('md-body').textContent,
            downloads_shown: !document.getElementById('md-downloads').hidden,
            confirm_open: document.getElementById('confirm').open});
        dlg.returnValue = 'cancel'; dlg.close('cancel'); await wait(300);
        return out;""")
    out["unchanged_by_not_ready"] = ([v["id"] for v in ws.store.versions(fail_mid)] == versions
                                     and ws.detail(fail_mid)["error"] is not None)
    out["settings"] = evaluate(window, WAIT + f"""
        window.workspace.show('settings');
        const sel = document.getElementById('s-model');
        for (let i = 0; i < 50 && !sel.options.length; i++) await wait(200);
        await wait(500);
        const hint = document.getElementById('s-model-hint');
        const out = {{marked: sel.selectedOptions[0]?.textContent || '',
                     hint_shown: !hint.hidden && hint.textContent.length > 0,
                     downloads_primary: document.getElementById('s-downloads')
                         .classList.contains('primary')}};
        // A ready model, chosen as a person would (the picker's change event).
        // The picker lists only real models, so the check's synthetic one is
        // added first; the select keeps the focus, so a poll does not refill it.
        // So transcribeAgain's "chosen model has a label" branch goes uncovered:
        // a real ready model would need a download, and this check stays offline (N2).
        // The page reads document.activeElement here, not focus events, so an
        // unfocused window needs no help (BLUR_JS).
        out.focused = document.hasFocus();
        sel.focus();
        sel.append(new Option('Synthetic test model', {json.dumps(READY_MODEL)}));
        sel.value = {json.dumps(READY_MODEL)};
        sel.dispatchEvent(new Event('change')); await wait(1500);
        sel.blur();
        out.chosen = (await window.workspace.call('settings')).model;
        return out;""")
    out["confirm"] = evaluate(window, FAILED_JS + """
        await meetings();
        const b = another(row());
        if (!b) return {offered: false};
        b.click();
        const dlg = document.getElementById('confirm');
        for (let i = 0; i < 50 && !dlg.open; i++) await wait(100);
        await wait(300);
        const out = {offered: true, open: dlg.open,
                     not_ready_open: document.getElementById('model-dlg').open,
                     title: document.getElementById('c-title').textContent,
                     body: document.getElementById('c-body').textContent,
                     ok_disabled_until_ticked: document.getElementById('c-ok').disabled};
        // Tick, then confirm with the button itself, as a person would: a
        // Confirm button that stays disabled confirms nothing.
        document.getElementById('c-check').click();
        out.ok_enabled_after_tick = document.getElementById('c-ok').disabled === false;
        document.getElementById('c-ok').click(); await wait(1500);
        return out;""")
    end = time.time() + 180                     # the synthetic engine takes seconds
    while time.time() < end and ws.detail(fail_mid)["stage"] != "review":
        time.sleep(0.25)
    d = ws.detail(fail_mid)
    out["after"] = {"stage": d["stage"], "error": d["error"], "model": d["model"],
                    "older_models": [x["model"] for x in d["older_versions"]],
                    "passages": ws.transcript(fail_mid)["total"]}
    out["after"].update(evaluate(window, FAILED_JS + """
        await meetings();
        const li = row();
        return {row_found: !!li,
                row_needs_attention: !!li && li.textContent.includes('Needs attention')};"""))
    return out


def failed_meeting_problems(fm):
    """What the failed-meeting check found wrong (an empty list: nothing)."""
    bad = []
    s = fm.get("failed") or {}
    if not (s.get("row_found") and s.get("needs_attention") and s.get("offered")):
        bad.append("the failed row did not offer " + ANOTHER_MODEL)
    label = fm.get("model_label")
    if not (s.get("dialog_open") and not s.get("confirm_open")
            and s.get("title") == f"{label} is still not ready"
            and (s.get("body") or "").startswith("This meeting stopped because its speech "
                                                 "model is not ready")
            and s.get("downloads_shown") == fm.get("downloads_offered")):
        bad.append("the not-ready dialog for the same model")
    if not fm.get("unchanged_by_not_ready"):
        bad.append("the not-ready dialog changed the meeting")
    st = fm.get("settings") or {}
    if not ((st.get("marked") or "").endswith("(not downloaded)") and st.get("hint_shown")
            and st.get("downloads_primary") == fm.get("downloads_offered")):
        bad.append("Settings did not mark the model that is not ready")
    if st.get("chosen") != READY_MODEL:
        bad.append("the ready model was not chosen in Settings")
    c = fm.get("confirm") or {}
    if not (c.get("offered") and c.get("open") and not c.get("not_ready_open")
            and c.get("title") == "Transcribe again with the current model?"
            and c.get("ok_disabled_until_ticked") and c.get("ok_enabled_after_tick") is True):
        bad.append("the Transcribe again confirmation")
    a = fm.get("after") or {}
    if not (a.get("stage") == "review" and a.get("error") is None
            and a.get("model") == READY_MODEL and (a.get("passages") or 0) > 0):
        bad.append("the meeting did not leave the error state")
    if a.get("older_models") != [NOT_READY_MODEL]:
        bad.append("the earlier version was not kept")
    if not (a.get("row_found") and a.get("row_needs_attention") is False):
        bad.append("the row still needs attention")
    return bad


def add_growing_meeting(ws, root):
    """A third meeting, part transcribed: six minutes of audio planned as
    several chunks, the first holding GROW_FIRST short passages (a long
    transcript so far), written through the store. Paused before its plan
    exists, so the processing thread transcribes none of it: growing_checks
    commits the other chunks one at a time while the page shows the meeting.
    Returns its id, still paused."""
    from workspace import media
    path = write_wav(root / "synthetic-growing.wav", 360)
    grow_mid, sid = media.import_recording(ws.store, ws.media, path, path.stem)
    ws.store.set_paused(grow_mid, True)
    ws.store.set_source_intake(sid, None, may_delete=False)
    ws._plan_version(grow_mid, sid)
    assert ws.wait_idle(180), "processing did not stop"
    vid = ws.store.active_version(grow_mid)["id"]
    assert len(ws.store.chunks(vid, status="pending")) >= 8, "too few chunks planned"
    commit_passages(ws, grow_mid, 0, GROW_FIRST)
    return grow_mid


def commit_passages(ws, mid, idx, n):
    """Commit chunk idx of the meeting's active version, as transcription
    would: n synthetic passages spread over the chunk's own time range."""
    from workspace.media import SAMPLE_RATE
    vid = ws.store.active_version(mid)["id"]
    chunk = next(c for c in ws.store.chunks(vid) if c["idx"] == idx)
    start = chunk["start_sample"] / SAMPLE_RATE
    gap = (chunk["end_sample"] - chunk["start_sample"]) / SAMPLE_RATE / n
    assert ws.store.commit_chunk(vid, idx, [
        types.SimpleNamespace(start=start + i * gap, end=start + (i + 0.8) * gap,
                              text=f"growing passage {idx}.{i} about the plan", words=())
        for i in range(n)]), f"chunk {idx} was not committed"


def add_long_meeting(ws, root, passages=5000):
    """A second meeting with about four hours of short passages (responsiveness),
    written through the store (commit_chunk) into its first pending chunk. It is
    paused before its transcript plan exists, so the processing thread never
    transcribes any of it (as _import_one does, but paused first). Returns its
    id, still paused."""
    from workspace import media
    path = write_wav(root / "synthetic-long.wav", 10)
    long_mid, sid = media.import_recording(ws.store, ws.media, path, path.stem)
    ws.store.set_paused(long_mid, True)
    ws.store.set_source_intake(sid, None, may_delete=False)
    ws._plan_version(long_mid, sid)
    assert ws.wait_idle(180), "processing did not stop"
    vid = ws.store.active_version(long_mid)["id"]
    pending = ws.store.chunks(vid, status="pending")
    assert pending and ws.store.passage_count(vid) == 0, "the long meeting was transcribed"
    assert ws.store.commit_chunk(vid, pending[0]["idx"], [
        types.SimpleNamespace(start=i * 3.0, end=i * 3.0 + 2.5,
                              text=f"passage {i} about the budget", words=())
        for i in range(passages)]), "the passages were not committed"
    with ws.store.tx() as db:            # the rest of the plan: nothing more to do
        db.execute("UPDATE chunks SET status = 'done' WHERE version_id = ?", (vid,))
    return long_mid


def add_failed_meeting(ws, root):
    """A meeting that stops because its speech model is not ready (#244).
    NOT_READY_MODEL is chosen in Settings, but this check's model folder is a
    new, empty one, so the worker refuses to load it (nothing is downloaded or
    loaded) and the meeting needs attention. The model stays chosen, so the
    page's "still not ready" dialog for the same model can be checked. Returns
    the meeting's id."""
    ws.set_settings({"model": NOT_READY_MODEL})
    ws.import_paths([str(write_wav(root / f"{FAILED_TITLE}.wav", 30))])
    assert ws.wait_idle(180), "processing did not stop"
    m = next(m for m in ws.meetings() if m["title"] == FAILED_TITLE)
    assert (m["model"], m["stage"]) == (NOT_READY_MODEL, "inbox"), "unexpected failed meeting"
    assert "speech model is not ready" in (m["error"] or ""), "it did not stop on its model"
    return m["id"]


# -- the locked screen (#111 C4b) -------------------------------------------------------
# A second window, in a process of its own (pywebview starts once a process):
# a workspace made with the app's own key protection in this check's temporary
# folder, then opened as the app opens one whose key Windows will not unlock.
LOCKED_TITLE = "synthetic-locked-marker"
# NTE_BAD_KEY_STATE, as the signed number Windows reports after a password reset.
BAD_KEY_STATE = -2146893813
LOCKED_IDLE_S = 12          # longer than the page's idle poll (10 s): nothing may poll


class RefusingProtector:
    """Windows Data Protection refusing the workspace key, as after a password
    reset without the old one. It holds no key and protects nothing: every
    call raises. It carries the app's protector name, so the store reads the
    refusal as Windows' ("windows"), and like the tests' protector it refuses
    the real workspace folder."""

    def __init__(self):
        from workspace import keystore
        self.name = keystore.APP_PROTECTOR

    def allows(self, db_path):
        from workspace.store import under_workspace_root
        return not under_workspace_root(db_path)

    def protect(self, root):
        from workspace.dpapi import DpapiError
        raise DpapiError("Windows could not protect the workspace key.", BAD_KEY_STATE)

    def unprotect(self, blob):
        from workspace.dpapi import DpapiError
        raise DpapiError("Windows could not unlock the workspace key.", BAD_KEY_STATE)


def locked_workspace(root):
    """A workspace holding one synthetic meeting titled LOCKED_TITLE, made
    with the app's own protector under root, then opened locked: with the
    refusing protector and on_unavailable="lock", as __main__ opens it."""
    from workspace import media
    ws = Workspace(root / "ws", root / "models", start=False, battery=False)
    try:
        media.import_recording(ws.store, ws.media, write_wav(root / "synthetic-locked.wav", 5),
                               LOCKED_TITLE)
    finally:
        ws.close()
    locked = Workspace(root / "ws", root / "models", battery=False,
                       protector=RefusingProtector(), on_unavailable="lock")
    if locked.locked is None or locked.locked.reason != "windows":
        locked.close()
        raise RuntimeError("the workspace did not open locked")
    return locked


def logged_bridge(bridge, calls):
    """bridge with each call's method name appended to calls (the bridge
    log): the same methods and signatures, which pywebview reads."""
    import functools
    import inspect

    def logged(name, fn):
        @functools.wraps(fn)
        def call(self, *args, **kwargs):
            calls.append(name)
            return fn(bridge, *args, **kwargs)
        call.__signature__ = inspect.signature(fn)
        return call
    names = {name: logged(name, getattr(type(bridge), name))
             for name in sorted(api.API_METHODS - {"pywebviewEventHandler"})}
    return type("Api", (), {"__slots__": (), **names})()


def interpreter():
    """This Python as it was started, with its options (-I -B -X ... for a
    package), to start the locked scenario's own process the same way."""
    argv = list(getattr(sys, "orig_argv", None) or [])
    if sys.argv and sys.argv[0] in argv:
        return argv[:argv.index(sys.argv[0])]
    return [sys.executable]


def run_locked_child(args, flag, timeout=300, extra=()):
    """Run this script's locked scenario (flag) in a process of its own, with
    any extra arguments, and return its report, or {"error": ...}."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "locked.json"
        cmd = [*interpreter(), str(Path(sys.argv[0]).resolve()), "unused.json", flag, str(out)]
        if args.app:
            cmd += ["--app", args.app]
        cmd += list(extra)
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"error": f"the locked scenario did not finish in {timeout} s"}
        if not out.is_file():
            return {"error": f"the locked scenario wrote no report (exit {done.returncode}): "
                             f"{(done.stderr or '').strip()[-400:]}"}
        return json.loads(out.read_text(encoding="utf-8"))


def locked_checks(window, ws, calls, report):
    """The locked screen in the real window: shown on its own with the reason
    and codes, no meeting title, only LOCKED_METHODS called, nothing called
    while it sits there, and its buttons work (help opens no browser here)."""
    try:
        window.events.loaded.wait(30)
        while not window.evaluate_js("!!(window.workspace && window.pywebview && "
                                     "window.pywebview.api && window.pywebview.api.status)"):
            time.sleep(0.2)
        report.update(evaluate(window, WAIT + f"""
            for (let i = 0; i < 50 && !window.workspace.state.locked; i++) await wait(200);
            await wait(300);
            const view = document.getElementById('view-locked');
            const others = [...document.querySelectorAll('.view')]
              .filter(v => v !== view && !v.hidden);
            return {{shown: !!window.workspace.state.locked && !view.hidden && !others.length
                            && document.getElementById('sidebar').hidden,
                    heading_focused: document.activeElement === document.getElementById('h-locked'),
                    reason: (window.workspace.state.locked || {{}}).reason,
                    reason_text: document.getElementById('locked-reason').textContent,
                    codes: document.getElementById('locked-codes').textContent,
                    marker_shown: document.body.textContent.includes(
                      {json.dumps(LOCKED_TITLE)})}};"""))
        before = len(calls)
        time.sleep(LOCKED_IDLE_S)
        report["calls_while_idle"] = calls[before:]
        report["copied"] = evaluate(window, WAIT + """
            document.getElementById('locked-copy').click(); await wait(1000);
            return document.getElementById('status').textContent;""")
        evaluate(window, WAIT + "document.getElementById('locked-help').click(); await wait(800);"
                                "return true;")
        report["help_opened"] = list(OPENED)
        report["close_clicked"] = evaluate(window, WAIT + """
            setTimeout(() => document.getElementById('locked-close').click(), 0);
            return true;""")
        for _ in range(50):                  # close_app destroys the window
            if "close_app" in calls:
                break
            time.sleep(0.2)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["calls"] = list(calls)
        if "close_app" not in calls:         # the button did not close it: never hang
            window.destroy()


def locked_main(out):
    """The locked scenario's own process: writes its report to out."""
    import webbrowser
    webbrowser.open = lambda url, *a, **k: OPENED.append(url) or True   # no browser starts
    report, calls = {}, []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            ws = locked_workspace(root)
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
        else:
            holder = {}
            window = shell.create_window(
                logged_bridge(api.make_api(ws, lambda: holder.get("window")), calls),
                title="Workspace UI check, locked (synthetic data)", allowed=api.API_METHODS)
            holder["window"] = window
            shell.run(locked_checks, (window, ws, calls, report),
                      storage_path=shell.storage_folder(ws.root))
            ws.close()
    Path(out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


def locked_problems(lk):
    """What the locked scenario found wrong (empty when it passed)."""
    from workspace import setup
    if lk.get("error"):
        return [f"locked screen ({lk['error']})"]
    problems = []
    if not lk.get("shown"):
        problems.append("locked screen not shown on its own")
    if lk.get("marker_shown"):
        problems.append("locked screen shows a meeting title")
    if not lk.get("heading_focused"):
        problems.append("locked screen heading not focused")
    if lk.get("reason") != "windows" or "0x8009000B" not in (lk.get("codes") or "") \
            or "U-" not in (lk.get("codes") or "") \
            or not (lk.get("reason_text") or "").startswith("Windows could not unlock") \
            or "nothing has been deleted" not in (lk.get("reason_text") or ""):
        problems.append("locked screen reason or codes")
    calls = set(lk.get("calls") or [])
    if calls - api.LOCKED_METHODS or "status" not in calls:
        problems.append(f"locked screen calls ({', '.join(sorted(calls)) or 'none'})")
    if lk.get("calls_while_idle"):
        problems.append(f"locked screen polled ({', '.join(lk['calls_while_idle'][:5])})")
    if "Copied the details for IT" not in (lk.get("copied") or ""):
        problems.append("locked screen Copy details for IT")
    if lk.get("help_opened") != [setup.HELP_LINKS["recovery"]]:
        problems.append("locked screen How to recover")
    if "close_app" not in calls:
        problems.append("locked screen Close Unminute")
    return problems


def main():
    ap = argparse.ArgumentParser(description="Real-window UI check on synthetic data (#20).",
                                 allow_abbrev=False)
    ap.add_argument("report", help="where to write the JSON report")
    ap.add_argument("--app", help="an installed or extracted package's app folder (run with "
                    "that package's Python: runtime\\python.exe -I -B -X disable-remote-debug)")
    ap.add_argument("--zip", help="the zip that package came from: records it as release "
                    "evidence (tools/evidence.py); exit 1 unless that evidence passes")
    ap.add_argument("--locked-only", metavar="REPORT", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.locked_only:                  # the locked scenario's own process (#111 C4b)
        return locked_main(args.locked_only)
    before = evidence.prepare(ap, args, APP if args.app else None)
    out = Path(args.report)
    report = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ws = Workspace(root / "ws", root / "models", start=False,
                       allow_test_engines=True, battery=False,
                       speaker_model=FAKE + "fake_diarizer", opener=OPENED.append)
        ws.settings.update(model=FAKE + "sentences", speakers=True)
        ws.start()
        ws.import_paths([str(write_wav(root / "synthetic-standup.wav", 120))])
        assert ws.wait_idle(180), "processing did not finish"
        mid = ws.meetings()[0]["id"]
        third = ws.transcript(mid)["items"][2]
        ws.edit(mid, third["id"], HOSTILE)
        long_mid = add_long_meeting(ws, root)
        grow_mid = add_growing_meeting(ws, root)
        ws.settings.update(speakers=False)
        fail_mid = add_failed_meeting(ws, root)
        ws.store.set_paused(long_mid, False)
        # Setup: fake downloads, and never touch the real Credential Manager.
        import fake_worker_engines

        from workspace import credentials
        ws.downloads.target = fake_worker_engines.fake_download
        credentials.save_token = lambda t, target=None: VAULT.__setitem__("token", t)
        credentials.load_token = lambda target=None: VAULT.get("token")
        credentials.has_token = lambda target=None: "token" in VAULT
        holder = {}
        window = shell.create_window(api.make_api(ws, lambda: holder.get("window")),
                                     title="Workspace UI check (synthetic data)",
                                     allowed=api.API_METHODS)
        holder["window"] = window
        storage = shell.storage_folder(ws.root)
        shell.run(checks, (window, ws, mid, long_mid, grow_mid, fail_mid, report),
                  storage_path=storage)
        report["webview_profile_removed_on_close"] = not storage.exists()
        ws.close()
    report["handoff_opened"] = OPENED
    # The locked screen (#111 C4b), in a window and process of its own.
    report["locked_screen"] = run_locked_child(args, "--locked-only")
    report["growing_problems"] = growing_problems(report.get("growing") or {})
    report["failed_meeting_problems"] = failed_meeting_problems(
        report.get("failed_meeting") or {})
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

    bad = []
    if report.get("error"):
        bad.append(report["error"])
    if [x for x in report.get("listeners", []) if x["proto"] == "TCP"]:
        bad.append("local TCP listener")
    if not (report.get("webview_profile_in_workspace")
            and report.get("webview_profile_removed_on_close")):
        bad.append("webview profile location")
    expected = sorted(api.API_METHODS - {"pywebviewEventHandler"})
    if report.get("api_surface") != expected:
        bad.append("api surface")
    h = report.get("hostile") or {}
    if h.get("img") or h.get("pwned") or not h.get("shown_as_text"):
        bad.append("hostile markup")
    pd = report.get("page_dialogs") or {}
    if not (pd.get("confirm") is False and pd.get("confirm_ms", 9999) < 1000
            and pd.get("prompt") is None and pd.get("native_ms", 9999) < 1000
            and pd.get("microphone") == pd.get("frame_microphone") == "NotAllowedError"
            and pd.get("screen") == "NotAllowedError" and pd.get("notifications") == "denied"
            and {"screen capture", "permission Notifications"} <= set(pd.get("refused", []))
            and pd.get("microphone_refusals", 0) >= 2               # the page and its frame
            and pd.get("permission_ms", 9999) < 2000
            and pd.get("new_windows") == [] and pd.get("alert_blocked")):
        bad.append("page dialogs")
    if report.get("edit_in_db") != "Edited in the page":
        bad.append("autosave")
    if report.get("undo", "").startswith("Edited"):
        bad.append("undo")
    if not (report.get("playback") or {}).get("src_is_data_url"):
        bad.append("playback")
    c = report.get("confirm_review") or {}
    if not (c.get("ok_disabled_until_ticked") and c.get("limits_shown")
            and c.get("time_buttons_disabled") and "removed" in c.get("playback_text", "")
            and c.get("final_offered")):
        bad.append("review confirmation")
    st = report.get("setup") or {}
    from workspace import gpu          # the graphics card component cards need NVIDIA
    # Four speech models (Parakeet v2, Parakeet v3, turbo, large-v3) and the speaker model,
    # plus two graphics card components (speakers, Whisper) on an NVIDIA machine.
    if not (st.get("cards") == 5 + 2 * (gpu.detected() is not None)
            and st.get("token_field_type") == "password"
            and st.get("token_cleared") and not st.get("token_in_page")
            and st.get("remove_visible") and report.get("setup_vault") == ["token"]):
        bad.append("setup")
    n = report.get("names") or {}
    if not (n.get("examples") == 3 and n.get("clip_s") == 3 and n.get("first_label") == "Aroha"):
        bad.append("speaker naming")
    if report.get("reassign") != "Speaker 3":
        bad.append("passage reassignment")
    if not handoff_passed(report.get("handoff") or {}, report.get("handoff_opened")):
        bad.append("handoff")
    nv = report.get("native") or {}
    bad.extend(native_problems(nv))
    if not (shell.RECORDING_MARK in nv.get("title_recording", "") and nv.get("title_restored")
            and nv.get("overlay_on") and nv.get("overlay_off")):
        bad.append("recording indicator")
    lt = report.get("long_transcript") or {}
    if lt.get("rows") != 5000:
        bad.append("long transcript")
    # Split scrolling applies only to a wide, tall enough window; high display
    # scaling can make the check window narrower in page pixels.
    if not lt.get("wide"):
        print("note: window too small in page pixels for split scrolling; not checked")
    elif not (lt.get("pane_scrolled") and lt.get("search_visible")
              and lt.get("page_scroll_y") == 0):
        bad.append("transcript pane scrolling")
    if report["growing_problems"]:
        bad.append("growing transcript (" + "; ".join(report["growing_problems"]) + ")")
    if report["failed_meeting_problems"]:
        bad.append("failed meeting (" + "; ".join(report["failed_meeting_problems"]) + ")")
    bad.extend(locked_problems(report["locked_screen"]))
    print("FAILED: " + ", ".join(bad) if bad else "ALL CHECKS PASSED")
    if args.zip:
        ev = evidence.stamp_file(out, "ui", args.zip, APP.parent, passed=not bad,
                                 before=before)
        return 0 if ev["pass"] else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
