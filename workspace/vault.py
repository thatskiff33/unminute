# workspace/vault.py
# Keys and sealed values for encryption at rest (#111 C1; C2 and C3 wire it
# in). The design, its limits and the reasons are in docs/ENCRYPTION.md.
#
# Keys:
#   - The root key: 32 random bytes per workspace, stored only protected by
#     Windows for this user (seal_root, workspace/dpapi.py).
#   - Keys derived from it with HKDF-SHA256 and fixed labels (Keyring): the
#     wrap key (wraps meeting keys and nothing else), the workspace key
#     (workspace-wide values such as watched-folder paths), the fingerprint
#     key (HMAC-SHA256 instead of plain hashes) and a check value that tells
#     a wrong or damaged root key apart from a good one.
#   - Meeting keys: 32 random bytes each (text and audio), stored only
#     wrapped. Destroying the wrapped key makes everything sealed under it
#     unreadable, wherever copies of the ciphertext remain (crypto-erase, C3).
#
# A sealed value is: format version (1 byte) | random nonce (12) |
# AES-256-GCM ciphertext | tag (16). Its associated data names where it
# belongs (the context: for example table, column and row), so a value copied
# to another row, column or meeting does not open. Contexts are encoded with
# a type and length per part, so no two different contexts encode the same.
#
# Errors never carry content, keys or contexts (P3). cryptography is imported
# on first use: the window process starts without it until C2 needs it.

import hashlib
import hmac
import re
import secrets
import struct

from workspace import dpapi

VERSION = 1
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
OVERHEAD = 1 + NONCE_BYTES + TAG_BYTES
# Separates Unminute's DPAPI blobs from any other use of the user's DPAPI keys.
ROOT_ENTROPY = b"Unminute workspace root key v1"
_LABEL = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_INT64 = struct.Struct(">q")
_LEN = struct.Struct(">I")


class VaultError(Exception):
    """A key or value could not be opened: the wrong key, the wrong place, or
    damaged. The message is safe to show and never names what it was."""


def new_key():
    """32 bytes from the operating system's secure random generator."""
    return secrets.token_bytes(KEY_BYTES)


def _key(key):
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise ValueError("a key is 32 bytes")
    return key


def _label(label):
    if not isinstance(label, str) or not _LABEL.match(label):
        raise ValueError("a label is lower-case letters, digits and hyphens")
    return label.encode("ascii")


def encode(kind, context):
    """Unambiguous bytes for (kind, context): the associated data of a
    sealed value, or the HKDF info of a derived key. context is a tuple of
    str, int (not bool) and bytes parts."""
    if not isinstance(context, tuple):
        raise TypeError("a context is a tuple")
    out = [b"unminute/v1/", _label(kind)]
    for part in context:
        if isinstance(part, bool):
            raise TypeError("a context part is str, int or bytes")
        if isinstance(part, str):
            data, tag = part.encode("utf-8"), b"s"
        elif isinstance(part, int):
            data, tag = _INT64.pack(part), b"i"
        elif isinstance(part, bytes):
            data, tag = part, b"b"
        else:
            raise TypeError("a context part is str, int or bytes")
        out += [tag, _LEN.pack(len(data)), data]
    return b"".join(out)


def derive(key, kind, context=(), salt=None):
    """A 32-byte key derived from key with HKDF-SHA256 (RFC 5869), separated
    by kind and context."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt,
                info=encode(kind, context)).derive(_key(key))


def _aead(key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(_key(key))


def seal(key, data, context):
    """data (bytes) sealed under key for the place context names."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("seal takes bytes; use seal_text for text")
    head = bytes([VERSION])
    nonce = secrets.token_bytes(NONCE_BYTES)
    return head + nonce + _aead(key).encrypt(nonce, bytes(data), head + encode("cell", context))


def unseal(key, blob, context):
    """The bytes seal() was given, if key and context are the ones it used
    and the blob is unchanged."""
    if not isinstance(blob, bytes) or len(blob) < OVERHEAD or blob[0] != VERSION:
        raise VaultError("A stored value is damaged or from a newer version of Unminute.")
    from cryptography.exceptions import InvalidTag
    nonce = blob[1:1 + NONCE_BYTES]
    try:
        return _aead(key).decrypt(nonce, blob[1 + NONCE_BYTES:],
                                  blob[:1] + encode("cell", context))
    except InvalidTag:
        raise VaultError("A stored value could not be opened: the key is wrong, or the "
                         "value is damaged or not where it was stored.") from None


def seal_text(key, text, context):
    if not isinstance(text, str):
        raise TypeError("seal_text takes str")
    return seal(key, text.encode("utf-8"), context)


def unseal_text(key, blob, context):
    data = unseal(key, blob, context)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise VaultError("A stored value is not text.") from None


def seal_root(root):
    """The root key protected by Windows for this user (DPAPI)."""
    return dpapi.protect(_key(root), ROOT_ENTROPY)


def open_root(blob):
    """The root key from seal_root's blob. Raises dpapi.DpapiError when
    Windows cannot unlock it (the locked screen, C4) and VaultError when what
    it unlocks is not a key."""
    root = dpapi.unprotect(blob, ROOT_ENTROPY)
    if len(root) != KEY_BYTES:
        raise VaultError("The workspace key is damaged.")
    return root


class Keyring:
    """The keys derived from a workspace's root key."""

    def __init__(self, root):
        _key(root)
        self._wrap = derive(root, "wrap")
        self.workspace_key = derive(root, "workspace")
        self._fingerprint = derive(root, "fingerprint")
        self.check_value = derive(root, "check")

    def __repr__(self):
        return "<Keyring>"                     # never the keys

    def matches(self, check_value):
        """Whether a stored check value came from this root key."""
        return isinstance(check_value, bytes) and hmac.compare_digest(check_value,
                                                                      self.check_value)

    def wrap(self, key, purpose, ref):
        """A meeting key sealed for storage, bound to its purpose (for example
        "text" or "audio") and its meeting reference, so it cannot be used in
        another meeting's or purpose's place."""
        return seal(self._wrap, _key(key), ("key", _label(purpose).decode(), ref))

    def unwrap(self, blob, purpose, ref):
        key = unseal(self._wrap, blob, ("key", _label(purpose).decode(), ref))
        if len(key) != KEY_BYTES:
            raise VaultError("A stored key is damaged.")
        return key

    def fingerprint(self, purpose, data):
        """HMAC-SHA256 of data under a key for this purpose, as hex: compares
        like a hash, but cannot be checked against a guessed file without the
        workspace's key."""
        key = derive(self._fingerprint, "fingerprint", (_label(purpose).decode(),))
        return hmac.new(key, bytes(data), hashlib.sha256).hexdigest()
