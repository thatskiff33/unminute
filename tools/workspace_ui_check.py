# tools/workspace_ui_check.py
# #20 UI check: run the real workspace window (WebView2) on a throwaway
# workspace with synthetic audio and fake engines, drive the page like a user
# and record evidence as JSON. Opens a window for about a minute; audio is muted.
#
#   python tools/workspace_ui_check.py <report.json>

import json
import os
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from webview_trial import listeners, process_tree  # noqa: E402

from workspace import api, shell  # noqa: E402
from workspace.app import Workspace  # noqa: E402

FAKE = "test:fake_worker_engines:"
VAULT = {}          # stands in for Windows Credential Manager during the check
HOSTILE = '<img src=x onerror="window.__pwned=1"> and <script>window.__pwned=2</script>'
SR = 16000


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


def checks(window, ws, mid, long_mid, report):
    try:
        window.events.loaded.wait(30)
        while not window.evaluate_js("!!(window.workspace && window.pywebview && "
                                     "window.pywebview.api && window.pywebview.api.meetings)"):
            time.sleep(0.2)
        report["listeners_idle"] = listeners(process_tree(os.getpid()))
        report["api_surface"] = evaluate(window, "return Object.keys(window.pywebview.api)"
                                                 ".filter(k => typeof window.pywebview.api[k]"
                                                 " === 'function').sort();")
        report["setup"] = evaluate(window, WAIT + """
            window.workspace.show('setup');
            for (let i = 0; i < 50 && !document.querySelector('#setup-cards .card'); i++)
                await wait(200);
            await wait(300);
            const cards = document.querySelectorAll('#setup-cards .card').length;
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
                    final_offered: document.getElementById('m-banner-action').textContent
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
            document.getElementById('ho-copy').click(); await wait(1500);
            const status = document.getElementById('ho-status').textContent
                || document.getElementById('status').textContent;
            document.getElementById('ho-close').click();
            return {has_markers: before.includes('\\nBEGIN TRANSCRIPT '),
                    named_before: before.includes('Aroha'), named_after: after.includes('Aroha'),
                    redacted: after.includes('[redacted]'),
                    left_out: after.includes('1 passage was left out'),
                    edited_prompt: edited.startsWith('Our own minutes prompt.'),
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
        tree = process_tree(os.getpid())
        report["listeners"] = listeners(tree)
        report["blocked_calls"] = list(shell.blocked_calls)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        window.destroy()


def main():
    out = Path(sys.argv[1])
    report = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ws = Workspace(root / "ws", root / "models", start=False, battery=False,
                       speaker_model=FAKE + "fake_diarizer")
        ws.settings.update(model=FAKE + "sentences", speakers=True)
        ws.start()
        ws.import_paths([str(write_wav(root / "synthetic-standup.wav", 120))])
        assert ws.wait_idle(180), "processing did not finish"
        mid = ws.meetings()[0]["id"]
        third = ws.transcript(mid)["items"][2]
        ws.edit(mid, third["id"], HOSTILE)
        # A second meeting with about four hours of short passages (responsiveness).
        ws._import_one(write_wav(root / "synthetic-long.wav", 10))
        long_mid = next(m["id"] for m in ws.meetings() if m["id"] != mid)
        vid = ws.store.active_version(long_mid)["id"]
        with ws.store.tx() as db:
            db.execute("UPDATE chunks SET status = 'done' WHERE version_id = ?", (vid,))
            db.executemany("INSERT INTO segments (version_id, chunk_idx, start_s, end_s, text) "
                           "VALUES (?, 0, ?, ?, ?)",
                           [(vid, i * 3.0, i * 3.0 + 2.5, f"passage {i} about the budget")
                            for i in range(5000)])
        ws.settings.update(speakers=False)
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
        shell.run(checks, (window, ws, mid, long_mid, report))
        ws.close()
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

    bad = []
    if report.get("error"):
        bad.append(report["error"])
    if [x for x in report.get("listeners", []) if x["proto"] == "TCP"]:
        bad.append("local TCP listener")
    expected = sorted(api.API_METHODS - {"pywebviewEventHandler"})
    if report.get("api_surface") != expected:
        bad.append("api surface")
    h = report.get("hostile") or {}
    if h.get("img") or h.get("pwned") or not h.get("shown_as_text"):
        bad.append("hostile markup")
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
            and h.get("redacted") and h.get("left_out") and h.get("edited_prompt")):
        bad.append("handoff")
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
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
