# workspace/api.py
# The js_api the workspace page may call (#20):
#   - the shell's dispatch guard allows only the names in API_METHODS;
#   - the facade pywebview sees has no state; the Workspace lives in a closure;
#   - every method returns {"ok": True, "value": ...} or {"ok": False,
#     "error": "..."} and never raises (pywebview would send a Python stack
#     trace, with file paths, to the page);
#   - the page never supplies file paths: recordings come from the native
#     file dialog or a native drop, which pywebview resolves in Python.

import base64
import io
import math
import sys
import threading
import time
import wave

import numpy as np

from workspace import cleanup, consent, diag
from workspace.app import AUDIO_EXTENSIONS, WorkspaceError
from workspace.media import SAMPLE_RATE

# pywebviewEventHandler delivers DOM events to handlers registered from Python
# (used only for drag and drop of recordings onto the window).
API_METHODS = frozenset({
    "status", "meetings", "detail", "transcript", "search", "edit", "undo", "clip",
    "pause", "stop_now", "resume", "rename", "retranscribe", "rerun_speakers",
    "skip_speakers",
    "add_hold", "release_hold", "keep_hold", "confirm_reviewed", "confirm_final_saved",
    "confirm_cleanup", "retry_cleanup", "reminders", "receipts", "settings",
    "set_settings", "import_dialog", "clear_import_errors", "pywebviewEventHandler",
    "speakers", "speaker_examples", "rename_speaker", "merge_speakers", "assign_speaker",
    "undo_speakers", "speaker_panel", "handoff_templates", "save_template", "reset_template",
    "handoff_preview", "set_handoff_options", "copy_handoff", "copy_and_open_assistant",
    "save_handoff",
    "setup_state", "download_model", "remove_token", "finish_setup", "open_help",
    "verify_model", "cancel_download", "watched_folders", "add_folder_dialog",
    "set_folder_active", "remove_folder", "scan_now", "set_folder_authority", "zoom",
    "microphone_devices", "start_recording", "pause_recording", "resume_recording",
    "stop_recording", "discard_recording", "recording_status", "discard_recording_report",
    "test_microphone",
    "retry_recording_import", "delete_pending_recording", "remove_model", "discard_meeting",
    "close_app", "report_ui_error", "copy_diagnostics", "continue_despite_damage",
})

FILE_TYPES = ("Recordings ({})".format(";".join("*" + e for e in AUDIO_EXTENSIONS)),)


def _wav_bytes(samples):
    """16-bit mono WAV of a clip (Workspace.clip keeps it to 30 seconds)."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _safe(fn, *args):
    try:
        return {"ok": True, "value": fn(*args)}
    except WorkspaceError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        # Recorded without its message (R9); the code lets support find it.
        # The bridge method that called _safe (fn can be a lambda): its name
        # is one of API_METHODS, never content.
        code = diag.incident("bridge", exc, method=sys._getframe(1).f_code.co_name)
        return {"ok": False, "error": f"Something went wrong (code {code}). Please try again."}


def _int(value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or (isinstance(value, float) and not math.isfinite(value)) \
            or value != int(value) or not lo <= value <= hi:
        raise WorkspaceError("Out of range.")
    return int(value)


def _device_name(value):
    if not isinstance(value, str) or len(value) > 120:
        raise WorkspaceError("Choose the microphone again.")
    return value


def _report_id(value):
    if not isinstance(value, str) or len(value) != 32 or not value.isalnum():
        raise WorkspaceError("Not found.")
    return value


CANCELLED = {"cancelled": True}           # the person cancelled the app's confirmation
# Asked again too soon after a No: nothing was shown.
CANCELLED_WAIT = {"cancelled": True, "retry_soon": True}
# After a No, the same question is not asked again for this long: a page
# cannot reopen it straight away until someone clicks Yes.
DECLINE_PAUSE_S = 3.0


def make_api(ws, get_window=lambda: None, confirm=None, decline_pause_s=DECLINE_PAUSE_S):
    """Return the js_api facade for Workspace `ws`. `get_window()` returns the
    pywebview window (for the native file dialog). `confirm(question)` asks a
    question (workspace.consent) outside the page (tests pass their own); by
    default a window the app draws itself, and No when there is no window."""
    diag.set_current(ws.log)              # bridge incidents go to this workspace's log
    declined = {}                         # question title -> when it was last answered No

    def ask(question):
        """True for the action, False for Cancel (or no window), None if the
        same question was cancelled a moment ago and is not shown again yet."""
        title = question["title"]
        if time.monotonic() - declined.get(title, float("-inf")) < decline_pause_s:
            return None
        if confirm is not None:
            answer = bool(confirm(question))
        else:
            window = get_window()
            if window is None:
                return False              # no window to own the question: not confirmed
            from workspace import shell
            answer = shell.native_confirm(window, question)
        if not answer:
            declined[title] = time.monotonic()
        return answer

    def indicate(recording):                # called by the Workspace, never by the page
        window = get_window()
        if window is not None:
            from workspace import shell
            shell.show_recording(window, recording)

    ws.recording_indicator = indicate

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

    def skip_speakers(self, mid):
        return _safe(ws.skip_speakers, mid)

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

    def handoff_preview(self, mid, tid, anonymise=False):
        return _safe(ws.handoff_preview, mid, tid, anonymise)

    def set_handoff_options(self, mid, omitted, redactions):
        return _safe(ws.set_handoff_options, mid, omitted, redactions)

    def copy_handoff(self, mid, tid, kind, expected=None, anonymise=False):
        return _safe(ws.copy_handoff, mid, tid, kind, expected, anonymise)

    def copy_and_open_assistant(self, mid, tid, expected=None, anonymise=False):
        # The page names no address: the app opens the one policy (or its
        # default) sets, after the copy, at most once every 3 seconds (#109).
        return _safe(ws.copy_and_open_assistant, mid, tid, expected, anonymise)

    def save_handoff(self, mid, tid, expected=None, anonymise=False):
        def run():
            import webview
            window = get_window()
            if window is None:
                raise WorkspaceError("The save dialog is not available.")
            name = ws.handoff_preview(mid, tid, anonymise)["filename"]
            chosen = window.create_file_dialog(webview.FileDialog.SAVE, save_filename=name,
                                               directory=ws.handoff_folder(),
                                               file_types=("Markdown (*.md)",))
            if not chosen:
                return None                       # cancelled
            path = chosen if isinstance(chosen, str) else chosen[0]
            return ws.save_handoff_to(mid, tid, path, expected, anonymise)
        return _safe(run)

    def setup_state(self):
        return _safe(ws.setup_state)

    def download_model(self, name, token=None, remember=True):
        # The token passes straight through; it is never returned or logged.
        return _safe(ws.download_model, name, token, bool(remember))

    def verify_model(self, name):
        return _safe(ws.verify_model, name)

    def cancel_download(self):
        return _safe(ws.cancel_download)

    def remove_token(self):
        return _safe(ws.remove_token)

    def finish_setup(self, speakers):
        return _safe(ws.finish_setup, speakers)

    def open_help(self, key):
        return _safe(ws.open_help, key)

    def watched_folders(self):
        return _safe(ws.watched_folders)

    def add_folder_dialog(self, include_existing=False):
        def run():
            import webview
            window = get_window()
            if window is None:
                raise WorkspaceError("The folder dialog is not available.")
            chosen = window.create_file_dialog(webview.FileDialog.FOLDER)
            if not chosen:
                return None                       # cancelled
            path = chosen if isinstance(chosen, str) else chosen[0]
            return bool(ws.add_watched_folder(path, bool(include_existing)))
        return _safe(run)

    def set_folder_active(self, fid, active):
        return _safe(ws.set_folder_active, fid, active)

    def remove_folder(self, fid):
        return _safe(ws.remove_folder, fid)

    def set_folder_authority(self, fid, delete_originals=None, fetch_cloud=None):
        def run():
            # Granting deletion needs a confirmation the page cannot give,
            # naming the folder from the app's records. A refused request (bad id,
            # unknown folder, policy) never shows the question.
            if delete_originals is True:
                folder = ws.deletion_grant_folder(fid)
                if folder is not None:
                    answer = ask(consent.delete_originals_question(folder))
                    if not answer:
                        return CANCELLED_WAIT if answer is None else CANCELLED
                    return ws.set_folder_authority(fid, True, fetch_cloud, confirmed=True)
            return ws.set_folder_authority(fid, delete_originals, fetch_cloud)
        return _safe(run)

    def scan_now(self):
        return _safe(ws.scan_now)

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

    def microphone_devices(self):
        return _safe(ws.microphone_devices)

    def start_recording(self, device, name):
        def run():
            index, label = _int(device, 0, 10_000), _device_name(name)
            ws.check_can_record(index, label)  # a refused request never shows the question
            record = None
            if ws.consent_required():
                answer = ask(consent.recording_question())
                if not answer:
                    return CANCELLED_WAIT if answer is None else CANCELLED
                record = consent.recording_record()
            return ws.start_recording(index, label, consent=record)
        return _safe(run)

    def test_microphone(self, device, name):
        return _safe(lambda: ws.test_microphone(_int(device, 0, 10_000), _device_name(name)))

    def pause_recording(self):
        return _safe(ws.pause_recording)

    def resume_recording(self):
        return _safe(ws.resume_recording)

    def stop_recording(self):
        return _safe(ws.stop_recording)

    def discard_recording(self):
        return _safe(ws.discard_recording)

    def recording_status(self):
        return _safe(ws.recording_status)

    def discard_recording_report(self, rid):
        return _safe(lambda: ws.discard_recording_report(_report_id(rid)))

    def retry_recording_import(self, rid):
        return _safe(lambda: ws.retry_recording_import(_report_id(rid)))

    def discard_meeting(self, mid):
        def run():
            title, imported_at = ws.meeting_for_discard(mid)   # also checks it exists
            answer = ask(consent.discard_question(title, imported_at, cleanup.LIMITS))
            if not answer:
                return CANCELLED_WAIT if answer is None else CANCELLED
            return ws.discard_meeting(mid)
        return _safe(run)

    def close_app(self):
        """Close the window after the page asked about an open recording."""
        def close():
            window = get_window()
            if window is not None:
                window.unminute_may_close = True
                window.destroy()
        threading.Thread(target=close, daemon=True, name="close-app").start()
        return {"ok": True, "value": None}

    def remove_model(self, name):
        return _safe(lambda: ws.remove_model(name))

    def delete_pending_recording(self, rid):
        return _safe(lambda: ws.delete_pending_recording(_report_id(rid)))

    def zoom(self, direction):
        def run():
            if direction not in ("in", "out", "reset"):
                raise WorkspaceError("Unknown zoom.")
            window = get_window()
            if window is None:
                raise WorkspaceError("Zoom is not available.")
            from workspace import shell
            return shell.set_zoom(window, direction)
        return _safe(run)

    def report_ui_error(self, event, line, col):
        """The page's own script errors (R9b): a fixed event and two numbers,
        nothing the page could fill with text. Rate-limited by the app."""
        def run():
            if event not in ("error", "rejection"):
                raise WorkspaceError("Unknown event.")
            ws.report_ui_error(event, _int(line, 0, 10**7), _int(col, 0, 10**7))
        return _safe(run)

    def copy_diagnostics(self):
        return _safe(ws.copy_diagnostics)

    def continue_despite_damage(self):
        return _safe(ws.continue_despite_damage)

    names = dict(status=status, meetings=meetings, detail=detail,
                 transcript=transcript, search=search, edit=edit, undo=undo, clip=clip,
                 pause=pause, stop_now=stop_now, resume=resume, rename=rename,
                 retranscribe=retranscribe, rerun_speakers=rerun_speakers,
                 skip_speakers=skip_speakers, add_hold=add_hold,
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
                 copy_handoff=copy_handoff, copy_and_open_assistant=copy_and_open_assistant,
                 save_handoff=save_handoff,
                 setup_state=setup_state, download_model=download_model,
                 remove_token=remove_token, finish_setup=finish_setup, open_help=open_help,
                 verify_model=verify_model, cancel_download=cancel_download,
                 watched_folders=watched_folders, add_folder_dialog=add_folder_dialog,
                 set_folder_active=set_folder_active, remove_folder=remove_folder,
                 scan_now=scan_now, set_folder_authority=set_folder_authority, zoom=zoom,
                 microphone_devices=microphone_devices, start_recording=start_recording,
                 pause_recording=pause_recording, resume_recording=resume_recording,
                 stop_recording=stop_recording, discard_recording=discard_recording,
                 recording_status=recording_status,
                 discard_recording_report=discard_recording_report,
                 test_microphone=test_microphone,
                 retry_recording_import=retry_recording_import,
                 delete_pending_recording=delete_pending_recording,
                 remove_model=remove_model, discard_meeting=discard_meeting,
                 close_app=close_app, report_ui_error=report_ui_error,
                 copy_diagnostics=copy_diagnostics,
                 continue_despite_damage=continue_despite_damage)
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
