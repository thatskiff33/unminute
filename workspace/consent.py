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
# (a line from the app's records or its policy), limits (what it cannot
# remove), check (a statement the person must tick first) and choices (one
# of which the person must choose first, #554).
#
# The recording wording is versioned: a recording keeps the version it was
# confirmed with, and the sources it was confirmed for (since version 2,
# #108): the microphone alone, or the microphone and computer audio. Version 3
# (#554, the user's decisions DEC-099) asks which kind of meeting it is each
# time: "In person meeting" records the microphone, "Online meeting" the
# microphone and computer audio. Neither is chosen when the question opens.
# The text the person confirms is version 2's, for the kind they chose: the
# microphone-only text is version 1's, unchanged; the computer audio variant
# names people on the call and other sounds the computer plays. A question
# with choices lists them under "choices", each with its label, a note of
# what it records, its sources and, when the consent wording is shown, the
# body and statement to tick that follow the choice. Stdlib only.

import time
import unicodedata

RECORDING_WORDING_VERSION = 3
RECORDING_TITLE = "Start recording?"
DELETE_ORIGINALS_TITLE = "Allow deleting originals from this folder?"
DISCARD_TITLE = "Discard this meeting?"

# What a recording records, as the recorder names its tracks.
MICROPHONE = "microphone"
COMPUTER = "computer"
MICROPHONE_ONLY = (MICROPHONE,)
WITH_COMPUTER = (MICROPHONE, COMPUTER)

# The two kinds of meeting the recording question offers (#554), in order.
IN_PERSON = {"label": "In person meeting", "note": "Records your microphone.",
             "sources": MICROPHONE_ONLY}
ONLINE = {"label": "Online meeting",
          "note": ("Records your microphone and the sound from this computer, including "
                   "other people on the call."),
          "sources": WITH_COMPUTER}
# The body before a kind is chosen: with the consent wording, and without it
# (policy turns the confirmation off, the user's decision 2 of DEC-099).
CHOOSE_BODY = ("Choose the kind of meeting. Before you start, tell everyone in the meeting "
               "that it is being recorded, and why.")
CHOOSE_ONLY_BODY = "Choose the kind of meeting."
# Where policy does not allow computer audio, only the microphone is offered.
COMPUTER_OFF_LINE = ("Your organisation has turned off computer audio recording, so only "
                     "your microphone is recorded.")


def _sources(sources):
    """The sources as a tuple, if they are ones a recording can have."""
    if isinstance(sources, (list, tuple)) and tuple(sources) in (MICROPHONE_ONLY, WITH_COMPUTER):
        return tuple(sources)
    raise ValueError("Unknown recording sources.")


def recording_wording(sources=MICROPHONE_ONLY):
    """The text confirmed for these sources (version 2's): the body and the
    statement to tick."""
    if _sources(sources) == WITH_COMPUTER:
        return {"body": ("Unminute records your microphone and the sound playing on this "
                         "computer, including other people on a call, and transcribes it here. "
                         "Other sounds, such as notifications or a video, are recorded too. "
                         "Before you start, tell everyone in the meeting, in the room and on "
                         "the call, that it is being recorded, and why."),
                "check": ("Everyone in this meeting, including people on the call, has been "
                          "told that it is being recorded, and why.")}
    return {"body": ("Unminute records the meeting's audio on this computer and transcribes it "
                     "here. Before you start, tell everyone present that the meeting is being "
                     "recorded, and why."),
            "check": "Everyone in this meeting has been told that it is being recorded, and why."}


def recording_question(computer=True, confirm=True):
    """The question before a recording, or None when there is nothing to ask.

    computer: policy allows computer audio (Workspace.computer_audio_offered),
    so both kinds are offered; otherwise only the microphone is recorded, and
    with the consent wording a line says the organisation turned computer
    audio off (K3). confirm: the consent wording and its statement to tick
    are shown (Workspace.consent_required); without them, a question with
    both kinds still asks which (DEC-099 decision 2), and with neither there
    is nothing to ask. native_confirm answers a question with choices with
    the chosen kind's sources (question_answer)."""
    head = {"title": RECORDING_TITLE, "action": "Start recording", "style": "record",
            "icon": "mic"}
    if not computer:
        if not confirm:
            return None
        return {**head, **recording_wording(MICROPHONE_ONLY), "detail": COMPUTER_OFF_LINE}
    if not confirm:
        return {**head, "body": CHOOSE_ONLY_BODY, "choices": [dict(IN_PERSON), dict(ONLINE)]}
    # Before a choice, the body is neutral and the statement shown (not yet
    # tickable) is the microphone one; each choice brings its own.
    return {**head, "body": CHOOSE_BODY, "check": recording_wording(MICROPHONE_ONLY)["check"],
            "choices": [{**kind, **recording_wording(kind["sources"])}
                        for kind in (IN_PERSON, ONLINE)]}


def question_answer(question, answer):
    """An answer to a question as the bridge acts on it. For a question with
    choices: the sources of the kind chosen, as a tuple, if the answer is
    exactly one of them; False for anything else (Cancel, no window, or an
    answer that names no offered kind). For any other question: True for the
    action, False otherwise."""
    choices = question.get("choices")
    if not choices:
        return bool(answer)
    offered = [tuple(c["sources"]) for c in choices]
    if isinstance(answer, (list, tuple)) and tuple(answer) in offered:
        return tuple(answer)
    return False


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


def recording_record(sources=MICROPHONE_ONLY, clock=time.time):
    """What is kept with a recording: that the person confirmed, when, which
    wording they saw and the sources it named. No names or content."""
    return {"confirmed": True, "wording": RECORDING_WORDING_VERSION, "at": round(clock(), 3),
            "method": "app-window", "sources": list(_sources(sources))}


def sources_match(record, sources):
    """The record names exactly these sources (the question shown was for
    them). False for anything else, including a record without sources."""
    named = record.get("sources") if isinstance(record, dict) else None
    return isinstance(named, list) and tuple(named) == _sources(sources)


def valid_recording_record(record, sources=MICROPHONE_ONLY):
    """A confirmation of the current wording for exactly these sources. The
    check runs only when a recording starts (Workspace.start_recording), with
    the record the bridge has just made: records kept with earlier recordings
    stay as history and are never checked again (a recording's import carries
    its journal's record over as it is). So a version 2 record is not valid
    for a new start: the question version 2 described had no choice of
    meeting, and only a version 3 question is ever shown now. A recording in
    progress when the app was updated was confirmed, and keeps its version 2
    record, since nothing checks it after its start."""
    return (isinstance(record, dict) and record.get("confirmed") is True
            and record.get("wording") == RECORDING_WORDING_VERSION
            and isinstance(record.get("at"), (int, float))
            and sources_match(record, sources))
