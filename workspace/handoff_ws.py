# workspace/handoff_ws.py
# The Workspace's handoff (#22): the templates, the preview, Copy and Save,
# the audit record of a handoff that left the app (#111 C5), which assistant
# Copy and open uses (#109) and clearing the app's own copy from the clipboard
# (policy), moved unchanged out of workspace/app.py (#414). Workspace inherits
# HandoffMixin, so the bridge (workspace/api.py) calls the same methods on the
# Workspace as before. The mixin keeps no state of its own: every attribute
# and lock it uses is the Workspace's, set in Workspace.__init__, so the
# locking is unchanged. What stays in app.py: _open_address and
# copy_and_open_assistant (tests patch workspace.app.os), with
# ASSISTANT_PAUSE_S and ASSISTANT_WITHHELD, and the policy thread that runs
# the clipboard clears.

from pathlib import Path
from typing import TYPE_CHECKING, Any

from workspace import clipboard, handoff
from workspace.app_common import WorkspaceError
from workspace.safe_paths import UnsafePathError
from workspace.store import DAMAGED_REFUSAL, DamagedContent

if TYPE_CHECKING:                      # the type check only (tools/type_check.py)
    import threading

    from workspace import audit, diag, policy
    from workspace.store import Store


class HandoffMixin:
    """Handoff, for Workspace (workspace/app.py) only."""

    if TYPE_CHECKING:
        # For the type check only: what the methods below use of the
        # Workspace, typed as Workspace.__init__ sets it. Nothing here exists
        # when the app runs.
        _clipboard_clears: list[Any]
        _clipboard_lock: threading.Lock
        _clock: Any
        _policy_wake: threading.Event
        _templates: Any | None
        _templates_lock: threading.Lock
        audit: audit.AuditLog
        log: diag.Log
        policy: policy.PolicySource
        root: Path
        store: Store

        def _allowed(self, name): ...
        def _meeting(self, mid, content=False): ...

    # -- handoff (#22) ------------------------------------------------------------------

    def _handoff(self, fn, *args):
        try:
            return fn(*args)
        except (handoff.HandoffError, clipboard.ClipboardError) as exc:
            raise WorkspaceError(str(exc)) from exc
        except DamagedContent:
            # Part of the meeting cannot be read (#111 C2b-2): no preview, so
            # nothing is copied or saved either (_checked builds it first).
            raise WorkspaceError(DAMAGED_REFUSAL) from None

    @property
    def templates(self):
        """The handoff templates (templates.sealed, sealed under the workspace
        key; #111 C2c), built at their first use in a session, when a plain
        templates.json from an earlier version is converted. While the store
        is locked nothing is built, read, converted or logged: its
        KeyUnavailable is raised (C4a's bridge gate refuses first)."""
        with self._templates_lock:
            if self._templates is None:
                if self.store.locked is not None:
                    raise self.store.locked
                sealer = self.store.file_sealer(handoff.TEMPLATES_FILE)
                templates = handoff.open_templates(self.root, sealer)
                if templates.unreadable:
                    self.log.write("templates_unreadable")      # content-free (P3)
                self._templates = templates
            return self._templates

    def handoff_templates(self):
        return self.templates.all()

    def save_template(self, tid, title, prompt):
        if tid is not None and (not isinstance(tid, str) or len(tid) > 40):
            raise WorkspaceError("That template no longer exists.")
        if not isinstance(title, str) or not isinstance(prompt, str):
            raise WorkspaceError("Expected text.")
        return self._handoff(self.templates.save, tid, title, prompt)

    def reset_template(self, tid):
        if not isinstance(tid, str):
            raise WorkspaceError("That template no longer exists.")
        self._handoff(self.templates.reset, tid)

    def handoff_preview(self, mid, tid, anonymise=False):
        """Exactly what Copy and Save will produce, plus the options in use.
        anonymise (this handoff only, not stored) replaces speaker names with
        Speaker 1, Speaker 2."""
        self._meeting(mid)
        if not isinstance(anonymise, bool):
            raise WorkspaceError("Expected yes or no.")
        template = self._handoff(self.templates.get, tid)
        out = self._handoff(handoff.build, self.store, mid, template, anonymise)
        if self._allowed("HandoffFilenameIncludesTitle") and not anonymise:
            out = {**out, "filename": handoff.title_filename(self.store.meeting(mid)["title"],
                                                             out["filename"])}
        return {**out, "options": handoff.options(self.store, mid),
                "assistant": self._assistant_shown(self._assistant())}

    def set_handoff_options(self, mid, omitted, redactions):
        m = self._meeting(mid)
        if not m["reviewed_at"]:
            raise WorkspaceError("Confirm the transcript review first.")
        self._handoff(handoff.set_options, self.store, mid, omitted, redactions)

    def _checked(self, mid, tid, kind, expected, anonymise=False):
        """The handoff text, refused unless it is exactly what was previewed."""
        out = self.handoff_preview(mid, tid, anonymise)
        if not isinstance(expected, str) or out["digests"][kind] != expected:
            raise WorkspaceError("The handoff changed since the preview. Check the preview "
                                 "again, then copy or save.")
        return out

    def copy_handoff(self, mid, tid, kind, expected, anonymise=False):
        if kind not in ("combined", "prompt_only"):
            raise WorkspaceError("Choose what to copy.")
        out = self._checked(mid, tid, kind, expected, anonymise)
        sequence = self._handoff(clipboard.copy_text, out[kind])
        self._record_handoff("handoff_copied", mid, out, what=kind)
        minutes = self.policy.current().get("ClipboardClearMinutes")
        if minutes:
            self._schedule_clipboard_clear(sequence, minutes * 60)
        return {"chars": len(out[kind]), "long": out["long"] and kind == "combined",
                "clears_in_minutes": minutes or None}

    def _record_handoff(self, kind, mid, out, **extra):
        """The audit record of a handoff that left the app: the meeting,
        whether the template is a built-in one (possibly edited) or the
        user's own, and counts. Never the template's text or anything copied
        (#111 C5). A handoff whose details cannot be read for the record is
        logged as an incident and still goes ahead."""
        try:
            fields = {"meeting": mid,
                      "template": ("built_in" if out["template"]["id"] in handoff.BUILTIN
                                   else "custom"),
                      "names_replaced": bool(out["anonymised"]), "omitted": out["omitted"],
                      "redactions": out["redactions"], **extra}
        except (KeyError, TypeError) as exc:
            self.log.incident("audit", exc)
            return
        self.audit.record(kind, **fields)

    # -- copy and open the assistant (#109) ---------------------------------------------

    def _assistant(self, p=None):
        """(name, address) of the assistant Copy and open uses, or None when
        policy turns it off or sets it badly. IT sets the name and address
        together: one without the other, or either one invalid, withholds the
        button rather than falling back to the default, so the app never opens
        ChatGPT where IT meant something else. An unreadable policy withholds
        it too (AllowHandoffAssistant is a permission)."""
        p = p or self.policy.current()
        if not p.get("AllowHandoffAssistant"):
            return None
        keys = ("HandoffAssistantName", "HandoffAssistantUrl")
        if any(k in p.invalid for k in keys):
            return None
        name, url = (p.get(k) for k in keys)
        if name is None and url is None:
            return handoff.DEFAULT_ASSISTANT
        if name is None or url is None:
            return None
        return name, url

    @staticmethod
    def _assistant_shown(assistant):
        """What the page may show: the name and the host, never a path or query."""
        if assistant is None:
            return None
        from urllib.parse import urlsplit
        return {"name": assistant[0], "host": urlsplit(assistant[1]).hostname}

    def _assistant_setting(self, p):
        """For Settings: whether IT set the assistant, and what is in force."""
        keys = ("AllowHandoffAssistant", "HandoffAssistantName", "HandoffAssistantUrl")
        if not any(p.managed(k) for k in keys):
            return None
        assistant = self._assistant(p)
        if assistant is not None:
            return {"state": "set", **self._assistant_shown(assistant)}
        return {"state": self._assistant_withheld(p)}

    @staticmethod
    def _assistant_withheld(p):
        """Why policy withholds Copy and open: "unreadable" (the key exists
        but could not be read), "off" (IT turned it off) or "invalid" (a value
        is not valid, or only one of the name and address is set)."""
        if p.unreadable:
            return "unreadable"
        return "off" if p.values.get("AllowHandoffAssistant") is False else "invalid"

    def _schedule_clipboard_clear(self, sequence, delay_s):
        """Clear the app's own copy later (policy), unless something else was
        copied since. The policy thread waits until it is due, checking at
        least every POLICY_IDLE_WAIT_S against the monotonic clock, which
        counts time asleep (a plain timer's wait does not, so it would clear
        late after sleep). Pending clears also run when the app closes."""
        with self._clipboard_lock:
            self._clipboard_clears.append((self._clock() + delay_s, sequence))
        self._policy_wake.set()

    def _run_clipboard_clears(self, everything=False):
        now = self._clock()
        with self._clipboard_lock:
            due = [s for t, s in self._clipboard_clears if everything or t <= now]
            self._clipboard_clears = [] if everything else [
                (t, s) for t, s in self._clipboard_clears if t > now]
        for sequence in due:
            try:
                clipboard.clear_if_unchanged(sequence)
            except clipboard.ClipboardError:
                pass                    # busy: the copy stays, as without the policy

    def _flush_clipboard_clears(self):
        self._run_clipboard_clears(everything=True)

    def handoff_folder(self):
        """Where the handoff Save dialog starts: the organisation's folder if
        policy sets one that exists, else the dialog's own default."""
        folder = self.policy.current().get("HandoffDefaultFolder")
        return folder if folder and Path(folder).is_dir() else ""

    def save_handoff_to(self, mid, tid, path, expected, anonymise=False):
        """Write the Markdown to a file the user chose in the save dialog. The
        app keeps no record of it; the user files or deletes it. ".md" is
        appended (never substituted) when missing, and a file the dialog did
        not ask about is never overwritten."""
        from workspace.safe_paths import check_raw
        out = self._checked(mid, tid, "markdown", expected, anonymise)
        try:
            check_raw(path)
        except UnsafePathError as exc:
            raise WorkspaceError(str(exc)) from exc
        target = Path(path)
        if target.suffix.lower() != ".md":
            target = target.with_name(target.name + ".md")
            if target.exists():
                raise WorkspaceError(f"{target.name} already exists. Choose another name.")
        try:
            with open(target, "w", encoding="utf-8", newline="\n") as f:
                f.write(out["markdown"])
        except OSError as exc:
            reason = exc.strerror or "file error"
            raise WorkspaceError(f"Could not save the file: {reason}.") from exc
        self._record_handoff("handoff_saved", mid, out)
        return {"chars": len(out["markdown"]), "name": target.name}
