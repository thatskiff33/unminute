# workspace/api.py
# The js_api the workspace page may call (#20). Same rules as the trial
# bridge (workspace/bridge.py):
#   - the shell's dispatch guard allows only the names in API_METHODS;
#   - the facade pywebview sees has no state; the Workspace lives in a closure;
#   - every method returns {"ok": True, "value": ...} or {"ok": False,
#     "error": "..."} and never raises (pywebview would send a Python stack
#     trace, with file paths, to the page);
#   - the page never supplies file paths: recordings come from the native
#     file dialog or a native drop, which pywebview resolves in Python.

import base64

from workspace.app import AUDIO_EXTENSIONS, WorkspaceError
from workspace.bridge import _wav_bytes

# pywebviewEventHandler delivers DOM events to handlers registered from Python
# (used only for drag and drop of recordings onto the window).
API_METHODS = frozenset({
    "ping", "status", "meetings", "detail", "transcript", "search", "edit", "undo", "clip",
    "pause", "stop_now", "resume", "rename", "retranscribe", "rerun_speakers",
    "add_hold", "release_hold", "keep_hold", "confirm_reviewed", "confirm_final_saved",
    "confirm_cleanup", "retry_cleanup", "reminders", "receipts", "settings",
    "set_settings", "import_dialog", "clear_import_errors", "pywebviewEventHandler",
    "speakers", "speaker_examples", "rename_speaker", "merge_speakers", "assign_speaker",
    "undo_speakers", "speaker_panel", "handoff_templates", "save_template", "reset_template",
    "handoff_preview", "set_handoff_options", "copy_handoff", "save_handoff",
})

FILE_TYPES = ("Recordings ({})".format(";".join("*" + e for e in AUDIO_EXTENSIONS)),)


def _safe(fn, *args):
    try:
        return {"ok": True, "value": fn(*args)}
    except WorkspaceError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        return {"ok": False, "error": "Something went wrong. Please try again."}


def _int(value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) \
            or not lo <= value <= hi:
        raise WorkspaceError("Out of range.")
    return int(value)


def make_api(ws, get_window=lambda: None):
    """Return the js_api facade for Workspace `ws`. `get_window()` returns the
    pywebview window (for the native file dialog)."""

    def ping(self):
        return {"ok": True, "value": "ok"}

    def status(self):
        return _safe(ws.status)

    def meetings(self):
        return _safe(ws.meetings)

    def detail(self, mid):
        return _safe(ws.detail, mid)

    def transcript(self, mid, start=0, count=500, version=None):
        return _safe(lambda: ws.transcript(mid, _int(start, 0, 10**7), _int(count, 1, 2000),
                                           version))

    def search(self, mid, query, version=None):
        return _safe(ws.search, mid, query, version)

    def edit(self, mid, segment_id, text):
        return _safe(ws.edit, mid, segment_id, text)

    def undo(self, mid):
        return _safe(ws.undo, mid)

    def clip(self, mid, start, end):
        def run():
            data = _wav_bytes(ws.clip(mid, start, end))
            return "data:audio/wav;base64," + base64.b64encode(data).decode("ascii")
        return _safe(run)

    def pause(self, mid):
        return _safe(ws.pause, mid)

    def stop_now(self, mid):
        return _safe(ws.stop_now, mid)

    def resume(self, mid):
        return _safe(ws.resume, mid)

    def rename(self, mid, title):
        return _safe(ws.rename, mid, title)

    def retranscribe(self, mid):
        return _safe(ws.retranscribe, mid)

    def rerun_speakers(self, mid, count=None):
        return _safe(ws.rerun_speakers, mid, count)

    def add_hold(self, mid, scope, reason):
        return _safe(ws.add_hold, mid, scope, reason)

    def release_hold(self, hold_id):
        return _safe(lambda: len(ws.release_hold(hold_id)))

    def keep_hold(self, hold_id):
        return _safe(ws.keep_hold, hold_id)

    def confirm_reviewed(self, mid):
        return _safe(lambda: bool(ws.confirm_reviewed(mid)))

    def confirm_final_saved(self, mid):
        return _safe(lambda: bool(ws.confirm_final_saved(mid)))

    def confirm_cleanup(self, job_id):
        return _safe(ws.confirm_cleanup, job_id)

    def retry_cleanup(self, job_id):
        return _safe(ws.retry_cleanup, job_id)

    def reminders(self):
        return _safe(ws.reminders)

    def receipts(self):
        return _safe(ws.receipts)

    def settings(self):
        return _safe(ws.get_settings)

    def set_settings(self, changes):
        return _safe(ws.set_settings, changes)

    def clear_import_errors(self):
        return _safe(ws.clear_import_errors)

    def speakers(self, mid):
        return _safe(ws.speakers, mid)

    def speaker_panel(self, mid, offsets=None):
        return _safe(ws.speaker_panel, mid, offsets)

    def speaker_examples(self, mid, label, offset=0):
        return _safe(ws.speaker_examples, mid, label, offset)

    def rename_speaker(self, mid, label, name):
        return _safe(ws.rename_speaker, mid, label, name)

    def merge_speakers(self, mid, label, into):
        return _safe(ws.merge_speakers, mid, label, into)

    def assign_speaker(self, mid, segment_id, label):
        return _safe(ws.assign_speaker, mid, segment_id, label)

    def undo_speakers(self, mid):
        return _safe(ws.undo_speakers, mid)

    def handoff_templates(self):
        return _safe(ws.handoff_templates)

    def save_template(self, tid, title, prompt):
        return _safe(ws.save_template, tid, title, prompt)

    def reset_template(self, tid):
        return _safe(ws.reset_template, tid)

    def handoff_preview(self, mid, tid):
        return _safe(ws.handoff_preview, mid, tid)

    def set_handoff_options(self, mid, omitted, redactions):
        return _safe(ws.set_handoff_options, mid, omitted, redactions)

    def copy_handoff(self, mid, tid, kind):
        return _safe(ws.copy_handoff, mid, tid, kind)

    def save_handoff(self, mid, tid):
        def run():
            import webview
            window = get_window()
            if window is None:
                raise WorkspaceError("The save dialog is not available.")
            name = ws.handoff_preview(mid, tid)["filename"]
            chosen = window.create_file_dialog(webview.FileDialog.SAVE, save_filename=name,
                                               file_types=("Markdown (*.md)",))
            if not chosen:
                return None                       # cancelled
            path = chosen if isinstance(chosen, str) else chosen[0]
            return ws.save_handoff_to(mid, tid, path)
        return _safe(run)

    def import_dialog(self):
        def run():
            import webview
            window = get_window()
            if window is None:
                raise WorkspaceError("The file dialog is not available.")
            paths = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                              file_types=FILE_TYPES) or ()
            return ws.import_paths(paths)
        return _safe(run)

    names = dict(ping=ping, status=status, meetings=meetings, detail=detail,
                 transcript=transcript, search=search, edit=edit, undo=undo, clip=clip,
                 pause=pause, stop_now=stop_now, resume=resume, rename=rename,
                 retranscribe=retranscribe, rerun_speakers=rerun_speakers, add_hold=add_hold,
                 release_hold=release_hold, keep_hold=keep_hold,
                 confirm_reviewed=confirm_reviewed, confirm_final_saved=confirm_final_saved,
                 confirm_cleanup=confirm_cleanup, retry_cleanup=retry_cleanup,
                 reminders=reminders, receipts=receipts, settings=settings,
                 set_settings=set_settings, import_dialog=import_dialog,
                 clear_import_errors=clear_import_errors, speakers=speakers,
                 speaker_examples=speaker_examples, rename_speaker=rename_speaker,
                 merge_speakers=merge_speakers, assign_speaker=assign_speaker,
                 undo_speakers=undo_speakers, speaker_panel=speaker_panel,
                 handoff_templates=handoff_templates,
                 save_template=save_template, reset_template=reset_template,
                 handoff_preview=handoff_preview, set_handoff_options=set_handoff_options,
                 copy_handoff=copy_handoff, save_handoff=save_handoff)
    assert set(names) | {"pywebviewEventHandler"} == API_METHODS
    Api = type("Api", (), {"__slots__": (), **names})
    return Api()


def enable_drop(window, ws):
    """Accept recordings dropped anywhere on the window. pywebview fills in
    pywebviewFullPath from the native drop (WebView2), never from page data."""
    from webview.dom import DOMEventHandler
    if getattr(window, "_drop_enabled", False):
        return
    window._drop_enabled = True

    def on_drop(event):
        files = (event.get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        if paths:
            ws.import_paths(paths)

    window.dom.document.events.dragover += DOMEventHandler(lambda e: None, True, True)
    window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
