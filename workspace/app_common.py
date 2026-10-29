# workspace/app_common.py
# What workspace/app.py and the modules split out of it (#414) share: the
# refused request (WorkspaceError, with its message kinds), how an import's
# failure is told to the user and logged, and the check on an id the page
# sends (_hold_id). Moved here unchanged from app.py,
# which imports them back, so app.WorkspaceError, app.ERROR_KINDS and the rest
# are the same objects as before (workspace/api.py imports them from app).

from workspace import media
from workspace.safe_paths import UnsafePathError

MANAGED_OFF = "{what} is turned off by your organisation. Ask your IT team if you need it."


# What a refused request's message is, for the page (#416): "error" (something
# went wrong or was refused) or "info" (nothing went wrong, such as a request
# repeated too soon). The bridge passes it to the page as "kind", and the page
# styles a message by it, never by its wording. Success messages are the
# page's own. workspace/ui/core.js MESSAGE_KINDS lists the same kinds and
# success (tests/test_message_kinds.py).
ERROR_KINDS = ("error", "info")


class WorkspaceError(ValueError):
    """A refused request. The message is safe to show in the UI; kind is one
    of ERROR_KINDS."""

    def __init__(self, message="", kind="error"):
        if kind not in ERROR_KINDS:
            raise ValueError("Unknown message kind.")
        super().__init__(message)
        self.kind = kind


def _expected_import_error(exc):
    """A refusal or a bad file, explained to the user; anything else is a bug.
    The decoder's container not starting (#299) is not the file's fault: it
    is logged with a code for IT (_import_incident)."""
    if isinstance(exc, media.SandboxUnavailable):
        return False
    return (isinstance(exc, (media.SourceChanged, media.DecodeError, UnsafePathError,
                             WorkspaceError))
            or (isinstance(exc, ValueError) and "no audio" in str(exc)))


def _import_incident(log, area, exc):
    """Log an unexpected import failure; returns its code. For the decoder's
    container (#299) the record also says which step failed and its Windows
    error number: no path, name or text (P3)."""
    fields = {}
    if isinstance(exc, media.SandboxUnavailable):
        fields = {"step": exc.step, "winerror": exc.winerror}
    return log.incident(area, exc, **fields)


def _import_message(exc, code=None):
    if isinstance(exc, media.SandboxUnavailable):
        return f"{exc} (code {code})" if code else str(exc)
    if isinstance(exc, (media.SourceChanged, media.DecodeError, UnsafePathError,
                        WorkspaceError)):
        return str(exc)
    if isinstance(exc, ValueError) and "no audio" in str(exc):
        return "That file contains no audio."
    if code:                              # unexpected: the code finds it in the log
        return f"That recording could not be imported (code {code})."
    return "That recording could not be imported (unsupported or damaged file)."


def _hold_id(value):
    if not isinstance(value, str) or len(value) != 32 or not value.isalnum():
        raise WorkspaceError("Not found.")
    return value
