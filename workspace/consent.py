# workspace/consent.py
# Confirmations for actions page script must not be able to approve on its own
# (#111 slice A6): starting a recording, allowing the app to delete originals
# from a watched folder, and discarding a meeting. The bridge (workspace/api.py)
# shows them in a window drawn by the app itself, outside the web page
# (workspace/shell.py native_confirm), so even a compromised page needs a person
# to tick and click.
#
# Each question is a dict: title, body, action (the button), style ("primary",
# "danger" or "record"), icon ("tick", "bin" or "mic"), and optionally detail
# (a line from the app's records), limits (what it cannot remove) and check
# (a statement the person must tick first).
#
# The recording wording is versioned: a recording keeps the version it was
# confirmed with. Stdlib only.

import time
import unicodedata

RECORDING_WORDING_VERSION = 1
RECORDING_TITLE = "Start recording?"
DELETE_ORIGINALS_TITLE = "Allow deleting originals from this folder?"
DISCARD_TITLE = "Discard this meeting?"


def recording_question():
    return {
        "title": RECORDING_TITLE,
        "body": ("Unminute records the meeting's audio on this computer and transcribes it "
                 "here. Before you start, tell everyone present that the meeting is being "
                 "recorded, and why."),
        "check": "Everyone in this meeting has been told that it is being recorded, and why.",
        "action": "Start recording", "style": "record", "icon": "mic"}


def delete_originals_question(folder):
    # The folder comes from the app's own records, never from the page. The
    # wording is the page's earlier dialog, which this replaces.
    return {
        "title": DELETE_ORIGINALS_TITLE,
        "body": ("After you confirm a meeting's transcript is reviewed, the app may delete the "
                 "original recording from this folder, but only if it is exactly the file "
                 "that was imported. Do not allow this for shared folders, Teams or channel "
                 "storage, or recordings someone else (such as a meeting organiser) owns. "
                 "Copies elsewhere, such as on a phone, in the cloud recycle bin or in "
                 "backups, are not removed."),
        "detail": f"Folder: {_shown(folder, 240, middle=True)}",
        "check": ("I am responsible for recordings in this folder and allow the app to "
                  "delete reviewed originals from it."),
        "action": "Allow", "style": "primary", "icon": "tick"}


def discard_question(title, imported_at, limits):
    # The page's earlier Discard dialog, with the meeting named from the app's
    # records: the title (which the page can change) cleaned and shortened,
    # and the import time.
    when = time.strftime("%d %b %Y %H:%M", time.localtime(imported_at))
    return {
        "title": DISCARD_TITLE,
        "body": ("Its audio, transcript, edits and speaker names are deleted from this "
                 "workspace and cannot be recovered. Your original recording file is not "
                 "deleted. For real meetings, use Transcript reviewed and then the final "
                 "confirmation instead."),
        "detail": f"Meeting: {_shown(title, 80)}, imported {when}",
        "limits": limits,
        "check": "Delete this meeting and everything in it.",
        "action": "Discard", "style": "danger", "icon": "bin"}


def _shown(value, limit, middle=False):
    """Text from the page or the file system as one plain line: control,
    format and line-separator characters (which could reorder or push the
    warning out of view) become spaces, and at most two combining marks stay
    on a letter (more could be stacked over the text above). Long values are
    shortened at the end, or in the middle for a path, whose end tells two
    folders apart."""
    out, marks = [], 0
    for ch in str(value):
        kind = unicodedata.category(ch)
        if kind in ("Mn", "Me"):
            marks += 1
            if marks > 2:
                continue
        else:
            marks = 0
        out.append(" " if kind in ("Cc", "Cf", "Zl", "Zp") else ch)
    text = " ".join("".join(out).split())
    if len(text) <= limit:
        return text
    ellipsis = chr(0x2026)
    if middle:
        half = (limit - 1) // 2
        return text[:half] + ellipsis + text[-(limit - 1 - half):]
    return text[:limit - 1] + ellipsis


def recording_record(clock=time.time):
    """What is kept with a recording: that the person confirmed, when, and
    which wording they saw. No names or content."""
    return {"confirmed": True, "wording": RECORDING_WORDING_VERSION, "at": round(clock(), 3),
            "method": "app-window"}


def valid_recording_record(record):
    return (isinstance(record, dict) and record.get("confirmed") is True
            and record.get("wording") == RECORDING_WORDING_VERSION
            and isinstance(record.get("at"), (int, float)))
