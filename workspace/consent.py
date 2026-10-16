# workspace/consent.py
# Native confirmations for actions page script must not be able to approve on
# its own (#111 slice A6): starting a recording, allowing the app to delete
# originals from a watched folder, and discarding a meeting. The bridge
# (workspace/api.py) shows them in a Windows dialog outside the web page
# (workspace/shell.py native_confirm), so even a compromised page needs a
# person to click Yes.
#
# The recording wording is versioned: a recording keeps the version it was
# confirmed with. The wording is a draft, to confirm with the firm's privacy
# adviser (Privacy Act 2020 IPP 3; the recording position under the Crimes
# Act, to confirm). Stdlib only.

import time
import unicodedata

RECORDING_WORDING_VERSION = 1
RECORDING_TITLE = "Start recording?"
RECORDING_TEXT = (
    "Before you record: has everyone in this meeting been told that it is being "
    "recorded, and why?\n\n"
    "Select Yes only if they have. Select No to go back without recording.")

DELETE_ORIGINALS_TITLE = "Allow deleting originals?"
DISCARD_TITLE = "Discard this meeting?"


def _shown(value, limit):
    """Text from the page or the file system as one plain line: control,
    format and line-separator characters (which could reorder or push the
    warning out of view) become spaces; long values are shortened."""
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch
                   for ch in str(value))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def delete_originals_text(folder):
    # The folder comes from the app's own records, never from the page.
    return ("After you confirm a meeting's transcript is reviewed, the app may delete the "
            "original recording from this folder (only if it is exactly the file that was "
            "imported). In a synced folder such as OneDrive, SharePoint or Teams, the "
            "deletion also reaches everyone the folder is shared with.\n\n"
            f"Folder: {_shown(folder, 240)}\n\n"
            "Allow it for this folder?")


def discard_text(title, imported_at):
    # The warning comes first; the title (which the page can change) is
    # shortened and cleaned, and the import time comes from the app.
    when = time.strftime("%d %b %Y %H:%M", time.localtime(imported_at))
    return ("Its audio, transcript, edits and speaker names are deleted from this workspace "
            "and cannot be recovered. The original recording file is not deleted.\n\n"
            f"Meeting: “{_shown(title, 80)}”, imported {when}.\n\n"
            "Discard it?")


def recording_record(clock=time.time):
    """What is kept with a recording: that the person confirmed, when, and
    which wording they saw. No names or content."""
    return {"confirmed": True, "wording": RECORDING_WORDING_VERSION, "at": round(clock(), 3),
            "method": "windows-dialog"}


def valid_recording_record(record):
    return (isinstance(record, dict) and record.get("confirmed") is True
            and record.get("wording") == RECORDING_WORDING_VERSION
            and isinstance(record.get("at"), (int, float)))
