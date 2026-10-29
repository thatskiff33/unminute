# workspace/watched_ws.py
# Managing the Workspace's watched folders (#23): the list, adding one,
# turning one on or off, its authority to delete originals (#111 A6) or fetch
# cloud files, removing one, and Check now, moved unchanged out of
# workspace/app.py (#414). Workspace inherits WatchedFoldersMixin, so the
# bridge (workspace/api.py) calls the same methods on the Workspace as before.
# The mixin keeps no state of its own: every attribute it uses is the
# Workspace's, set in Workspace.__init__. What stays in app.py: the
# watched-folder thread and its scan (_watch_loop, _scan_folders,
# _scan_watched), which use the import limits (AUDIO_EXTENSIONS,
# MAX_IMPORT_BYTES) that tests read from app.py's source.

from typing import TYPE_CHECKING, Any

from workspace import audit, intake
from workspace.app_common import MANAGED_OFF, WorkspaceError, _hold_id

if TYPE_CHECKING:                      # the type check only (tools/type_check.py)
    import threading

    from workspace.store import Store


class WatchedFoldersMixin:
    """Managing watched folders, for Workspace (workspace/app.py) only."""

    if TYPE_CHECKING:
        # For the type check only: what the methods below use of the
        # Workspace, typed as Workspace.__init__ sets it. Nothing here exists
        # when the app runs.
        _scan_wake: threading.Event
        audit: audit.AuditLog
        last_scan: Any
        store: Store

        def _allowed(self, name): ...
        def _not_finish_only(self): ...
        def _not_locked(self): ...

    # -- watched folders (#23; the scan thread is in workspace/app.py) ------------------

    def watched_folders(self):
        return {"folders": intake.summary(self.store), "last_scan": self.last_scan,
                "interval_minutes": intake.SCAN_INTERVAL_S // 60}

    def add_watched_folder(self, path, include_existing):
        if not self._allowed("AllowWatchedFolders"):
            raise WorkspaceError(MANAGED_OFF.format(what="Watched folders"))
        if not isinstance(include_existing, bool):
            raise WorkspaceError("Expected yes or no.")
        try:
            fid = intake.add_folder(self.store, path, include_existing)
        except intake.IntakeError as exc:
            raise WorkspaceError(str(exc)) from exc
        self.audit.record("folder_added", folder=fid, existing=include_existing)
        self._scan_wake.set()   # not scan_now: finish-only still saves it (B5b)
        return fid

    def set_folder_active(self, fid, active):
        if not isinstance(active, bool):
            raise WorkspaceError("Expected on or off.")
        intake.set_active(self.store, _hold_id(fid), active)
        if active:
            self._scan_wake.set()

    def set_folder_authority(self, fid, delete_originals=None, fetch_cloud=None,
                             confirmed=False):
        """confirmed: the person said Yes in the Windows question (the bridge
        asks); granting deletion is refused without it (#111 A6)."""
        fid = _hold_id(fid)
        for v in (delete_originals, fetch_cloud):
            if v is not None and not isinstance(v, bool):
                raise WorkspaceError("Expected on or off.")
        if delete_originals and not self._allowed("AllowDeleteOriginals"):
            raise WorkspaceError(MANAGED_OFF.format(what="Deleting original recordings"))
        if delete_originals and confirmed is not True:
            raise WorkspaceError("Allowing the app to delete originals needs your confirmation "
                                 "in Windows.")
        if delete_originals is not None:
            before = intake.authority_for(self.store, fid)
            intake.set_delete_originals(self.store, fid, delete_originals)
            after = intake.authority_for(self.store, fid)
            if after != before:                   # changed: granted (confirmed) or withdrawn
                self.audit.record("delete_authority", folder=fid, granted=after)
        if fetch_cloud is not None:
            intake.set_fetch_cloud(self.store, fid, fetch_cloud)
            self._scan_wake.set()

    def remove_folder(self, fid):
        fid = _hold_id(fid)
        row = self.store._one("SELECT delete_originals FROM watched_folders WHERE id = ?",
                              (fid,))
        intake.remove_folder(self.store, fid)
        if row is not None and audit.HEX32.fullmatch(fid):
            self.audit.record("folder_removed", folder=fid,
                              had_authority=bool(row["delete_originals"]))

    def scan_now(self):
        self._not_locked()
        self._not_finish_only()
        self._scan_wake.set()
