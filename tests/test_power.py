# Keeping the computer awake while recording (workspace/power.py).
#   python -m unittest discover -s tests -v

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace import power  # noqa: E402  (stdlib only)


class FakeKernel32:
    """Counts power requests the way Windows would."""

    def __init__(self, create=42, set_ok=1):
        self.handle, self.set_ok = create, set_ok
        self.active, self.open, self.reasons = 0, 0, []

    def create(self, reason):
        self.reasons.append(reason._obj.Reason.SimpleReasonString)
        if self.handle and self.handle != power.INVALID_HANDLE_VALUE:
            self.open += 1
        return self.handle

    def set(self, handle, kind):
        assert kind == power.POWER_REQUEST_SYSTEM_REQUIRED
        self.active += bool(self.set_ok)
        return self.set_ok

    def clear(self, handle, kind):
        self.active -= 1
        return 1

    def close(self, handle):
        self.open -= 1
        return 1


class KeepAwakeTests(unittest.TestCase):
    def test_held_once_and_released_once(self):
        api = FakeKernel32()
        k = power.KeepAwake(api)
        self.assertTrue(k.hold())
        self.assertTrue(k.hold())                       # already held: no second request
        self.assertEqual((k.held, api.active, api.open), (True, 1, 1))
        self.assertEqual(api.reasons, [power.REASON])
        k.release()
        k.release()                                     # repeat is harmless
        self.assertEqual((k.held, api.active, api.open), (False, 0, 0))

    def test_a_refused_request_leaves_nothing_open(self):
        for api in (FakeKernel32(create=None), FakeKernel32(create=power.INVALID_HANDLE_VALUE),
                    FakeKernel32(set_ok=0)):
            k = power.KeepAwake(api)
            self.assertFalse(k.hold())
            self.assertEqual((k.held, api.active, api.open), (False, 0, 0))
            k.release()

    def test_an_unavailable_api_is_not_fatal(self):
        class Broken:
            def create(self, reason):
                raise OSError("no power API")
        k = power.KeepAwake(Broken())
        self.assertFalse(k.hold())
        self.assertFalse(k.held)

    def test_any_error_is_contained_and_no_handle_is_left_open(self):
        # A recording must start and end whatever the power calls do.
        def failing(real, close_first):
            def boom(*args):
                if close_first:
                    real(*args)              # closed, then reports an error
                raise ValueError("unexpected")
            return boom

        for step in ("create", "set", "clear", "close"):
            api = FakeKernel32()
            setattr(api, step, failing(getattr(api, step), step == "close"))
            k = power.KeepAwake(api)
            with self.subTest(step):
                held = k.hold()
                self.assertEqual(held, step in ("clear", "close"))
                k.release()
                self.assertFalse(k.held)
                self.assertEqual(api.open, 0)

    @unittest.skipUnless(os.name == "nt", "Windows power requests")
    def test_windows_accepts_the_request(self):
        # The real calls: checks the ctypes signatures and REASON_CONTEXT layout.
        # The request lasts only for this test (Windows drops it at exit anyway).
        k = power.KeepAwake()
        try:
            self.assertTrue(k.hold())
        finally:
            k.release()
        self.assertFalse(k.held)


if __name__ == "__main__":
    unittest.main()
