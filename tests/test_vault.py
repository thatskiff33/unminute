# Keys and sealed values for encryption at rest (#111 C1, workspace/vault.py;
# design in docs/ENCRYPTION.md). The formats are pinned by known answers built
# from the raw primitives, so a refactor cannot silently make stored data
# unreadable.
#   python -m unittest discover -s tests -v

import importlib.util
import os
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace import dpapi, vault  # noqa: E402

HAVE_CRYPTOGRAPHY = importlib.util.find_spec("cryptography") is not None
needs_cryptography = unittest.skipUnless(HAVE_CRYPTOGRAPHY, "needs cryptography")

KEY = bytes(range(32))
TEXT = "Kia ora, synthetic text"
# Sealed under KEY with nonce 100..111 for ("passages", "text", 7), built from
# AESGCM directly (scratch script in the C1 pull request).
PINNED_CELL = bytes.fromhex(
    "016465666768696a6b6c6d6e6f0372bf46169b37b21e112686ae0d0f892ba1267eee148757f178"
    "16894473958557570ea9d1872f")
PINNED_DERIVED = {
    "wrap": "92574d50198b3d8ea2633c21f178972923dea185de0cd91c59d340b020d9a8cc",
    "workspace": "41e12bad1f50c9112ec58c3e562a286329f4adc3fe5da517afba546f04c5b20f",
    "fingerprint": "ba8fb2b9603e1d76e50b30a915e84e5492667c21486a84ae63c2dff905739965",
    "check": "a318cbe0da4ac81a2e4af70a50cf810933e742cf292df741af90d178a18c301d",
}


def part(tag, data):
    return tag + struct.pack(">I", len(data)) + data


class EncodeTests(unittest.TestCase):
    """Contexts encode with a type and a length per part."""

    def test_the_format_as_documented(self):
        self.assertEqual(
            b"unminute/v1/cell" + part(b"s", b"passages") + part(b"i", struct.pack(">q", 5))
            + part(b"b", b"\x01"),
            vault.encode("cell", ("passages", 5, b"\x01")))
        self.assertEqual(b"unminute/v1/wrap", vault.encode("wrap", ()))

    def test_different_contexts_never_encode_the_same(self):
        pairs = [(("ab", "c"), ("a", "bc")), (("1",), (1,)), ((b"x",), ("x",)),
                 ((), ("",)), (("a", ""), ("a",)), ((-1,), (1,)), ((0,), (bytes(8),))]
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                self.assertNotEqual(vault.encode("cell", a), vault.encode("cell", b))
        self.assertNotEqual(vault.encode("cell", ()), vault.encode("key", ()))

    def test_only_str_int_and_bytes_parts(self):
        for bad in ((True,), (1.5,), (None,), (bytearray(b"x"),), (["a"],)):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                vault.encode("cell", bad)
        with self.assertRaises(TypeError):
            vault.encode("cell", ["passages"])
        for kind in ("Cell", "", "a b", "x" * 33, "1cell"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                vault.encode(kind, ())
        with self.assertRaises(struct.error):
            vault.encode("cell", (2 ** 63,))


@needs_cryptography
class SealTests(unittest.TestCase):

    def test_round_trip(self):
        for text in ("", TEXT, "Māori macrons, emoji 🎙, 中文", "x" * 100_000):
            with self.subTest(n=len(text)):
                blob = vault.seal_text(KEY, text, ("passages", "text", 1))
                self.assertEqual(text, vault.unseal_text(KEY, blob, ("passages", "text", 1)))
        self.assertEqual(b"\x00\xff", vault.unseal(KEY, vault.seal(KEY, b"\x00\xff", ("a",)),
                                                   ("a",)))

    def test_layout_and_a_fresh_nonce_each_time(self):
        a = vault.seal_text(KEY, TEXT, ("t",))
        b = vault.seal_text(KEY, TEXT, ("t",))
        self.assertEqual(len(a), 1 + 12 + len(TEXT.encode()) + 16)
        self.assertEqual(1, a[0])
        self.assertNotEqual(a[1:13], b[1:13])
        self.assertNotEqual(a, b)
        self.assertNotIn(TEXT.encode(), a)

    def test_the_pinned_value_opens(self):
        self.assertEqual(TEXT, vault.unseal_text(KEY, PINNED_CELL, ("passages", "text", 7)))

    def test_the_format_is_aes_gcm_with_the_documented_associated_data(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        blob = vault.seal_text(KEY, TEXT, ("passages", "text", 7))
        aad = b"\x01" + vault.encode("cell", ("passages", "text", 7))
        self.assertEqual(TEXT.encode(), AESGCM(KEY).decrypt(blob[1:13], blob[13:], aad))

    def test_the_wrong_key_or_place_does_not_open(self):
        blob = vault.seal_text(KEY, TEXT, ("passages", "text", 7))
        wrong = [(bytes(32), ("passages", "text", 7)), (KEY, ("passages", "text", 8)),
                 (KEY, ("passages", "title", 7)), (KEY, ("edits", "text", 7)),
                 (KEY, ("passages", "text", "7")), (KEY, ("passages", "text", 7, ""))]
        for key, context in wrong:
            with self.subTest(context=context), self.assertRaises(vault.VaultError):
                vault.unseal(key, blob, context)

    def test_any_changed_byte_is_refused(self):
        blob = vault.seal(KEY, b"synthetic", ("a",))
        for i in range(len(blob)):
            changed = bytearray(blob)
            changed[i] ^= 0x01
            with self.subTest(byte=i), self.assertRaises(vault.VaultError):
                vault.unseal(KEY, bytes(changed), ("a",))
        for bad in (blob[:-1], blob[:28], b"", b"\x02" + blob[1:], bytearray(blob), None):
            with self.subTest(bad=bad if bad is None else len(bad)), \
                    self.assertRaises(vault.VaultError):
                vault.unseal(KEY, bad, ("a",))

    def test_errors_name_nothing(self):
        blob = vault.seal_text(KEY, "Client Kowhai Ltd", ("passages", "row-77"))
        for key, context in ((bytes(32), ("passages", "row-77")), (KEY, ("passages", "x"))):
            with self.assertRaises(vault.VaultError) as cm:
                vault.unseal(key, blob, context)
            message = str(cm.exception)
            for secret in ("Kowhai", "row-77", "passages", KEY.hex(), blob.hex()[:16]):
                self.assertNotIn(secret, message)
            self.assertIsNone(cm.exception.__cause__)

    def test_bad_arguments(self):
        with self.assertRaises(TypeError):
            vault.seal(KEY, "text", ("a",))
        with self.assertRaises(TypeError):
            vault.seal_text(KEY, b"bytes", ("a",))
        for key in (b"", KEY[:16], KEY + b"x", bytearray(KEY), KEY.hex()):
            with self.subTest(key=type(key).__name__ + str(len(key))), \
                    self.assertRaises(ValueError):
                vault.seal(key, b"x", ("a",))
        not_text = vault.seal(KEY, b"\xff\xfe", ("a",))
        with self.assertRaises(vault.VaultError):
            vault.unseal_text(KEY, not_text, ("a",))

    def test_new_keys_are_random_32_bytes(self):
        keys = {vault.new_key() for _ in range(64)}
        self.assertEqual(64, len(keys))
        self.assertTrue(all(isinstance(k, bytes) and len(k) == 32 for k in keys))


@needs_cryptography
class KeyringTests(unittest.TestCase):

    def test_derived_keys_are_pinned_hkdf_sha256(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        for label, expected in PINNED_DERIVED.items():
            with self.subTest(label=label):
                self.assertEqual(expected, vault.derive(KEY, label).hex())
                raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                           info=b"unminute/v1/" + label.encode()).derive(KEY)
                self.assertEqual(expected, raw.hex())
        ring = vault.Keyring(KEY)
        self.assertEqual(PINNED_DERIVED["check"], ring.check_value.hex())
        self.assertEqual(PINNED_DERIVED["workspace"], ring.workspace_key.hex())
        self.assertEqual(4, len(set(PINNED_DERIVED.values())))

    def test_the_check_value_tells_root_keys_apart(self):
        ring = vault.Keyring(KEY)
        self.assertTrue(ring.matches(vault.Keyring(KEY).check_value))
        self.assertFalse(ring.matches(vault.Keyring(bytes(32)).check_value))
        for bad in (None, "", ring.check_value.hex(), ring.check_value[:16]):
            self.assertFalse(ring.matches(bad))

    def test_wrapped_keys_are_bound_to_purpose_and_meeting(self):
        ring = vault.Keyring(KEY)
        meeting_key = vault.new_key()
        blob = ring.wrap(meeting_key, "text", b"meeting-ref-1")
        self.assertNotIn(meeting_key, blob)
        self.assertEqual(meeting_key, ring.unwrap(blob, "text", b"meeting-ref-1"))
        for other, purpose, ref in ((ring, "audio", b"meeting-ref-1"),
                                    (ring, "text", b"meeting-ref-2"),
                                    (vault.Keyring(bytes(32)), "text", b"meeting-ref-1")):
            with self.subTest(purpose=purpose, ref=ref), self.assertRaises(vault.VaultError):
                other.unwrap(blob, purpose, ref)
        # Keys are wrapped under their own key, not the workspace key.
        with self.assertRaises(vault.VaultError):
            vault.unseal(ring.workspace_key, blob, ("key", "text", b"meeting-ref-1"))
        with self.assertRaises(ValueError):
            ring.wrap(meeting_key, "Text", b"m")
        with self.assertRaises(ValueError):
            ring.wrap(meeting_key[:31], "text", b"m")

    def test_a_wrapped_value_that_is_not_a_key_is_refused(self):
        ring = vault.Keyring(KEY)
        not_a_key = vault.seal(ring._wrap, b"short", ("key", "text", b"m"))
        with self.assertRaises(vault.VaultError):
            ring.unwrap(not_a_key, "text", b"m")

    def test_fingerprints_need_the_workspace_key(self):
        import hashlib
        ring, other = vault.Keyring(KEY), vault.Keyring(bytes(32))
        digest = hashlib.sha256(b"synthetic original").digest()
        a = ring.fingerprint("original", digest)
        self.assertEqual(a, ring.fingerprint("original", digest))
        self.assertEqual(64, len(a))
        self.assertNotEqual(a, ring.fingerprint("folder", digest))
        self.assertNotEqual(a, other.fingerprint("original", digest))
        self.assertNotEqual(a, hashlib.sha256(digest).hexdigest())
        self.assertNotEqual(a, digest.hex())

    def test_the_keyring_never_shows_its_keys(self):
        ring = vault.Keyring(KEY)
        for text in (repr(ring), str(ring)):
            for key in (KEY, ring.workspace_key, ring.check_value):
                self.assertNotIn(key.hex(), text)
                self.assertNotIn(repr(key), text)


@needs_cryptography
class RootKeyTests(unittest.TestCase):

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
    def test_round_trip_through_windows(self):
        root = vault.new_key()
        blob = vault.seal_root(root)
        self.assertNotIn(root, blob)
        self.assertEqual(root, vault.open_root(blob))
        self.assertEqual(vault.Keyring(root).check_value,
                         vault.Keyring(vault.open_root(blob)).check_value)

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
    def test_a_blob_for_another_purpose_or_not_a_key_is_refused(self):
        with self.assertRaises(dpapi.DpapiError):
            vault.open_root(dpapi.protect(vault.new_key(), b"another use"))
        with self.assertRaises(vault.VaultError):
            vault.open_root(dpapi.protect(b"short", vault.ROOT_ENTROPY))
        with self.assertRaises(ValueError):
            vault.seal_root(b"short")

    def test_without_windows_it_refuses(self):
        with mock.patch.object(dpapi.sys, "platform", "linux"):
            with self.assertRaises(dpapi.DpapiError):
                vault.seal_root(vault.new_key())
            with self.assertRaises(dpapi.DpapiError):
                vault.open_root(b"x" * 200)


class ImportTests(unittest.TestCase):

    def test_importing_does_not_load_cryptography(self):
        # The window process imports store modules at start; cryptography
        # (about 70 ms) loads on first use instead.
        code = ("import sys; import workspace.vault, workspace.audiocrypt, workspace.dpapi; "
                "print(sorted(m for m in sys.modules if m.split('.')[0] == 'cryptography'))")
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, check=True,
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual("[]", out)


if __name__ == "__main__":
    unittest.main()
