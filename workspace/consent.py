# workspace/consent.py
# Native confirmations for actions page script must not be able to approve on
# its own (#111 slice A6): starting a recording, allowing the app to delete
# originals from a watched folder, and discarding a meeting. The bridge
# (workspace/api.py) shows them as Windows message boxes, outside the web page,
# so even a compromised page needs a person to click Yes.
#
# The recording wording is versioned: a recording keeps the version it was
# confirmed with. The wording is a draft, to confirm with the firm's privacy
# adviser (Privacy Act 2020 IPP 3; the recording position under the Crimes
# Act, to confirm). Stdlib only.

import time

RECORDING_WORDING_VERSION = 1
RECORDING_TITLE = "Start recording?"
RECORDING_TEXT = (
    "Before you record: has everyone in this meeting been told that it is being "
    "recorded, and why?\n\n"
    "Select Yes only if they have. Select No to go back without recording.")

DELETE_ORIGINALS_TITLE = "Allow deleting originals?"
DELETE_ORIGINALS_TEXT = (
    "After you confirm a meeting's transcript is reviewed, the app may delete the "
    "original recording from this folder (only if it is exactly the file that was "
    "imported).\n\n"
    "In a synced folder such as OneDrive or SharePoint, the deletion also reaches "
    "everyone the folder is shared with.\n\n"
    "Allow it for this folder?")

DISCARD_TITLE = "Discard this meeting?"


def discard_text(title):
    # The same facts as the page's own Discard dialog, which comes first.
    return (f"“{title}”: its audio, transcript, edits and speaker names are deleted from "
            "this workspace and cannot be recovered. The original recording file is not "
            "deleted.\n\nDiscard it?")


def recording_record(clock=time.time):
    """What is kept with a recording: that the person confirmed, when, and
    which wording they saw. No names or content."""
    return {"confirmed": True, "wording": RECORDING_WORDING_VERSION, "at": round(clock(), 3),
            "method": "windows-dialog"}
