# tools/workspace_ui_check.py
# #20 UI check: run the real workspace window (WebView2) on a throwaway
# workspace with synthetic audio and fake engines, drive the page like a user
# and record evidence as JSON. Opens a window for about a minute; audio is muted.
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

from workspace import api, shell  # noqa: E402
from workspace.app import Workspace  # noqa: E402

FAKE = "test:fake_worker_engines:"
VAULT = {}          # stands in for Windows Credential Manager during the check
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


PRESS_STEPS = ("wait", "click", "enter", "tab", "space-on", "toggle-on", "enter-on", "shift-on")


def press(script, title, seen=None):
    """Act on the confirmation as a person would, through Windows messages
    (the page has no way to): "click:<button or text>", "enter", "tab",
    "enter-on:<control>", "space-on:<statement>", "toggle-on:<statement>",
    "shift-on:<control>" (a key that does nothing but count as a key press),
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
        elif kind == "toggle-on":                                         # tick, with no input
            control = dialog_button(hwnd, arg)
            user32.PostMessageW(control, 0x00F5, 0, 0)                        # BM_CLICK
            time.sleep(0.2)
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
    """The app's own confirmation window: the action is ignored at first and
    until a statement is ticked, Enter means Cancel unless the action has held
    the focus, a key or click before the action is available starts the wait
    again, the bridge reports a Cancel without acting, and the action works
    after the wait. The
    recording indicator changes the title and the taskbar overlay and
    restores them."""
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


def checks(window, ws, mid, long_mid, report):
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
        report["edit_autosave"] = evaluate(window, WAIT + """
            const span = document.querySelectorAll('#transcript .text')[0];
            span.click(); span.textContent = 'Edited in the page'; span.dispatchEvent(new
                Event('input'));
            await wait(1500); span.blur(); await wait(500);
            return {status: document.getElementById('status').textContent,
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
        report["handoff"] = evaluate(window, WAIT + """
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
            document.getElementById('ho-copy').click(); await wait(1500);
            const status = document.getElementById('ho-status').textContent
                || document.getElementById('status').textContent;
            document.getElementById('ho-close').click();
            return {has_markers: before.includes('\\nBEGIN TRANSCRIPT '),
                    named_before: before.includes('Aroha'), named_after: after.includes('Aroha'),
                    redacted: after.includes('[redacted]'),
                    left_out: after.includes('1 passage was left out'),
                    edited_prompt: edited.startsWith('Our own minutes prompt.'),
                    copy_blocked_until_applied,
                    anonymised: anonymised.includes('Speaker 1:') && !anonymised.includes('Aroha'),
                    template_label: tpl, copy_status: status};""")
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
        report["native"] = native_checks(window, ws, long_mid)
        tree = process_tree(os.getpid())
        report["listeners"] = listeners(tree)
        report["blocked_calls"] = list(shell.blocked_calls)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        window.destroy()


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


def main():
    ap = argparse.ArgumentParser(description="Real-window UI check on synthetic data (#20).",
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
        ws.settings.update(model=FAKE + "sentences", speakers=True)
        ws.start()
        ws.import_paths([str(write_wav(root / "synthetic-standup.wav", 120))])
        assert ws.wait_idle(180), "processing did not finish"
        mid = ws.meetings()[0]["id"]
        third = ws.transcript(mid)["items"][2]
        ws.edit(mid, third["id"], HOSTILE)
        long_mid = add_long_meeting(ws, root)
        ws.settings.update(speakers=False)
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
        shell.run(checks, (window, ws, mid, long_mid, report), storage_path=storage)
        report["webview_profile_removed_on_close"] = not storage.exists()
        ws.close()
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
    from workspace import gpu          # the graphics card component card needs NVIDIA
    if not (st.get("cards") == 4 + (gpu.detected() is not None)
            and st.get("token_field_type") == "password"
            and st.get("token_cleared") and not st.get("token_in_page")
            and st.get("remove_visible") and report.get("setup_vault") == ["token"]):
        bad.append("setup")
    n = report.get("names") or {}
    if not (n.get("examples") == 3 and n.get("clip_s") == 3 and n.get("first_label") == "Aroha"):
        bad.append("speaker naming")
    if report.get("reassign") != "Speaker 3":
        bad.append("passage reassignment")
    h = report.get("handoff") or {}
    if not (h.get("has_markers") and h.get("named_before") and not h.get("named_after")
            and h.get("redacted") and h.get("left_out") and h.get("edited_prompt")
            and h.get("copy_blocked_until_applied") and h.get("anonymised")):
        bad.append("handoff")
    nv = report.get("native") or {}
    if nv.get("stuck_dialogs"):
        bad.append("a confirmation was left open (is the screen locked?)")
    if not (nv.get("early_yes_then_enter") is False and nv.get("yes_after_delay") is True
            and nv.get("bridge_no") and nv.get("meeting_kept")
            and nv.get("tick_first") is True and nv.get("open_until_ticked") == [True, True]
            and nv.get("keyboard_after_hold") is True
            and nv.get("open_after_quick_enter") == [True]
            and nv.get("keys_restart_wait") is True and nv.get("open_after_keys") == [True]
            and nv.get("clicks_restart_wait") is True and nv.get("open_after_clicks") == [True]
            and nv.get("enter_on_statement") is False
            and nv.get("pressed_1") and nv.get("pressed_2") and nv.get("pressed_3")
            and nv.get("pressed_4") and nv.get("pressed_5") and nv.get("pressed_6")
            and nv.get("pressed_7") and nv.get("pressed_8")):
        bad.append("native confirmation")
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
    print("FAILED: " + ", ".join(bad) if bad else "ALL CHECKS PASSED")
    if args.zip:
        ev = evidence.stamp_file(out, "ui", args.zip, APP.parent, passed=not bad,
                                 before=before)
        return 0 if ev["pass"] else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
