# Windows Data Protection for the workspace root key (#111 C1,
# workspace/dpapi.py): user scope, no prompts, nothing readable in the blob or
# in errors.
#   python -m unittest discover -s tests -v

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace import dpapi  # noqa: E402

SECRET = b"synthetic key material 0123456789"
ENTROPY = b"Unminute test entropy"


@unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
class DpapiTests(unittest.TestCase):

    def test_round_trip(self):
        for data in (b"", b"\x00", SECRET, bytes(range(256)) * 16):
            with self.subTest(n=len(data)):
                blob = dpapi.protect(data, ENTROPY)
                self.assertEqual(data, dpapi.unprotect(blob, ENTROPY))
        self.assertEqual(SECRET, dpapi.unprotect(dpapi.protect(bytearray(SECRET), b""), b""))

    def test_the_blob_holds_nothing_readable(self):
        blob = dpapi.protect(SECRET, ENTROPY)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(SECRET[:8], blob)
        self.assertNotIn(ENTROPY, blob)
        self.assertNotEqual(blob, dpapi.protect(SECRET, ENTROPY))      # salted each time

    def test_wrong_entropy_or_a_changed_blob_is_refused(self):
        blob = dpapi.protect(SECRET, ENTROPY)
        with self.assertRaises(dpapi.DpapiError) as cm:
            dpapi.unprotect(blob, b"other")
        self.assertNotEqual(0, cm.exception.code)
        with self.assertRaises(dpapi.DpapiError):
            dpapi.unprotect(blob, b"")
        for i in (len(blob) // 2, len(blob) - 5, len(blob) - 1):
            changed = bytearray(blob)
            changed[i] ^= 0x01
            with self.subTest(byte=i), self.assertRaises(dpapi.DpapiError):
                dpapi.unprotect(bytes(changed), ENTROPY)
        for bad in (b"", blob[:20], b"x" * (dpapi.MAX_BLOB + 1)):
            with self.subTest(n=len(bad)), self.assertRaises(dpapi.DpapiError):
                dpapi.unprotect(bad, ENTROPY)
        # Empty or oversized blobs are refused before Windows sees them.
        for bad in (b"", b"x" * (dpapi.MAX_BLOB + 1)):
            with self.assertRaises(dpapi.DpapiError) as cm:
                dpapi.unprotect(bad, ENTROPY)
            self.assertEqual(0, cm.exception.code)

    def test_errors_carry_only_the_windows_code(self):
        blob = dpapi.protect(SECRET, ENTROPY)
        with self.assertRaises(dpapi.DpapiError) as cm:
            dpapi.unprotect(blob, b"other")
        message = str(cm.exception)
        self.assertRegex(message, r"^Windows could not unlock the workspace key \(error \d+\)\.$")
        for text in (SECRET.decode(), ENTROPY.decode(), blob.hex()[:16]):
            self.assertNotIn(text, message)

    def test_user_scope_and_no_prompt(self):
        # The flags are UI_FORBIDDEN only: never LOCAL_MACHINE (0x4), which
        # any account on the computer could unprotect.
        seen = []
        real = dpapi._crypt

        class Spy:
            def __getattr__(self, name):
                fn = getattr(real, name)

                def call(*args):
                    seen.append((name, args[1] if name == "CryptProtectData" else None,
                                 args[5]))
                    return fn(*args)
                return call

        with mock.patch.object(dpapi, "_crypt", Spy()):
            dpapi.unprotect(dpapi.protect(SECRET, ENTROPY), ENTROPY)
        self.assertEqual([("CryptProtectData", "Unminute", 1), ("CryptUnprotectData", None, 1)],
                         seen)

    def test_only_bytes(self):
        for bad in ("text", None, 5, memoryview(SECRET)):
            with self.subTest(bad=type(bad).__name__), self.assertRaises(TypeError):
                dpapi.protect(bad, ENTROPY)
        with self.assertRaises(TypeError):
            dpapi.protect(SECRET, "entropy")
        with self.assertRaises(TypeError):
            dpapi.unprotect("blob", ENTROPY)


class OtherSystemsTests(unittest.TestCase):

    def test_without_windows_it_refuses(self):
        with mock.patch.object(dpapi.sys, "platform", "linux"):
            for call in (lambda: dpapi.protect(SECRET, ENTROPY),
                         lambda: dpapi.unprotect(b"x" * 100, ENTROPY)):
                with self.assertRaises(dpapi.DpapiError) as cm:
                    call()
                self.assertEqual(0, cm.exception.code)


if __name__ == "__main__":
    unittest.main()
