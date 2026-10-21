# A key protector for tests (#111 C2b-2): Linux CI has no Windows Data
# Protection, and the fast tests should not call it thousands of times. It
# keeps the root key in effect in clear text, so three guards keep it away
# from a real workspace: it refuses any path under the real workspace folder
# (allows, store.under_workspace_root: passes only when LOCALAPPDATA is not
# set, as on Linux CI), the store refuses any protector not named "dpapi"
# (keystore.APP_PROTECTOR) there as well, and tests/test_content_access.py
# checks that nothing in workspace/ or tools/ names this module, and that
# every Store and Workspace in tests/ has it but the named few. Not a test
# module itself (no test_ prefix), and tests/ is not in the package.

from workspace import store
from workspace.dpapi import DpapiError

PREFIX = b"UNMINUTE-TEST-KEY"


class TestProtector:
    """protect(root) = PREFIX + root. fail=True: every call raises
    DpapiError with code (13 unless given, as Windows does for data it cannot
    unlock; #111 C4a tests pass others, such as 0x8009000B as a signed number)."""
    __test__ = False            # not a test case, whatever a runner thinks of its name
    name = "test"

    def __init__(self, fail=False, code=13):
        self.fail = fail
        self.code = code

    def allows(self, db_path):
        return not store.under_workspace_root(db_path)

    def protect(self, root):
        if self.fail:
            raise DpapiError(f"Windows could not protect the workspace key (error {self.code}).",
                             self.code)
        return PREFIX + bytes(root)

    def unprotect(self, blob):
        if self.fail or not isinstance(blob, bytes) or not blob.startswith(PREFIX) \
                or len(blob) != len(PREFIX) + 32:
            raise DpapiError(f"Windows could not unlock the workspace key (error {self.code}).",
                             self.code)
        return blob[len(PREFIX):]


def protector(fail=False):
    return TestProtector(fail=fail)


def open_store(path, **kwargs):
    """A Store with the test protector."""
    kwargs.setdefault("protector", TestProtector())
    return store.Store(path, **kwargs)
