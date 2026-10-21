# workspace/keystore.py
# The workspace's key protector, the codec that seals content columns, and
# the schema 1 to 2 transforms (#111 C2b-2). The SQL that stores what these
# produce is in workspace/store.py; nothing here names a table in SQL.
# docs/ENCRYPTION.md has the design, docs/WORKSPACE_DESIGN.md "Content
# columns" the columns and contexts.
#
#   - DpapiProtector keeps the root key protected by Windows for this user
#     (vault.seal_root). It is the only protector the app uses: the store
#     refuses any other for a workspace under the real data folder.
#   - VaultCodec seals each content value under its meeting's text key (the
#     workspace key for workspace values), bound to its table, column and
#     row: ("meeting", meeting id, table, physical column, *row) or
#     ("workspace", table, physical column, *row).
#   - LockedCodec stands in while the key cannot be used (the store's lock
#     mode): every seal and unseal raises the store's KeyUnavailable.
#   - transform_from_1 turns the configuration a schema-1 workspace carries
#     over (watched folders, intake history, the intake salt) into schema 2's
#     sealed and fingerprinted rows.
#
# Stdlib at import; cryptography is loaded by vault.py on first use.

import ntpath
import re

from workspace import vault

# Phase C (#111) ships as one release. C7 sets this True when every Phase C slice
# has merged (the owner's decision, 21 Oct 2026; it was C4b).
PHASE_C_COMPLETE = False
# VERSION when C2b-2 merged; no release can be prepared until PHASE_C_COMPLETE.
PHASE_C_FROZEN_VERSION = "0.5.0-preview.3"

_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")

# The only protector name the store accepts for the real workspace. A
# constant, so replacing DpapiProtector (as a test may) cannot change it.
APP_PROTECTOR = "dpapi"


class DpapiProtector:
    """The root key protected by Windows Data Protection for this user
    (workspace/dpapi.py, with vault.ROOT_ENTROPY)."""
    name = APP_PROTECTOR

    def allows(self, db_path):
        return True

    def protect(self, root):
        return vault.seal_root(root)

    def unprotect(self, blob):
        return vault.open_root(blob)


def fingerprint(keyring, purpose, sha256_hex):
    """The HMAC fingerprint of a SHA-256 given as 64 lower-case hex
    characters (a file's or a text's digest): compares like the hash, but
    cannot be checked against a guessed file without the workspace's key."""
    if not isinstance(sha256_hex, str) or not _SHA256_HEX.match(sha256_hex):
        raise ValueError("a fingerprint takes a SHA-256 as 64 lower-case hex characters")
    return keyring.fingerprint(purpose, bytes.fromhex(sha256_hex))


def folder_fingerprint(keyring, identity):
    """The fingerprint of a watched folder's identity (intake.folder_identity)."""
    return keyring.fingerprint("folder", identity.encode("utf-8"))


def context(meeting, table, column, row):
    """The context a value is sealed for: where it is stored. column is the
    physical (sealed) column name, row the row tuple of WORKSPACE_DESIGN.md."""
    if meeting is None:
        return ("workspace", table, column, *row)
    return ("meeting", meeting, table, column, *row)


class VaultCodec:
    """Seals and opens content values (str) for the store, which calls it
    only while holding its own lock (vault.Sealer's thread rule).
    text_sealer(meeting) returns the vault.Sealer of that meeting's text key
    or raises the store's KeyMissing or DamagedContent. vault.VaultError
    from a value that does not open is passed to the store, which turns it
    into DamagedContent."""

    def __init__(self, keyring, text_sealer):
        self.keyring = keyring
        self._text_sealer = text_sealer
        self._workspace = None

    def __repr__(self):
        return "<VaultCodec>"                  # never the keys

    def _sealer(self, meeting):
        if meeting is None:
            if self._workspace is None:
                self._workspace = vault.Sealer(self.keyring.workspace_key)
            return self._workspace
        return self._text_sealer(meeting)

    def seal(self, meeting, table, column, row, value):
        return self._sealer(meeting).seal_text(value, context(meeting, table, column, row))

    def unseal(self, meeting, table, column, row, value):
        return self._sealer(meeting).unseal_text(value, context(meeting, table, column, row))

    def fingerprint(self, purpose, sha256_hex):
        return fingerprint(self.keyring, purpose, sha256_hex)


class LockedCodec:
    """The codec while the workspace's key cannot be used (Store lock mode):
    nothing can be sealed or opened, and each try raises the same
    KeyUnavailable the store holds."""

    def __init__(self, error):
        self.error = error

    def seal(self, *args):
        raise self.error

    def unseal(self, *args):
        raise self.error

    def fingerprint(self, *args):
        raise self.error


# -- schema 1 to 2 (the empty-workspace migration, #26) --------------------------------

# A watched folder listed twice in schema 1 (paths that differ only in case or
# a trailing separator) is merged into the older row, with this note.
MERGED_NOTE = ("This folder was listed twice (with different capitals) and has been merged. "
               "Check its settings.")


def stored_folder_identity(path):
    """The identity of a folder path already resolved with os.path.realpath,
    compared as Windows does, with no trailing separator. The one
    definition: intake.folder_identity resolves a path and then uses this,
    and the migration uses it as is (a schema-1 path was resolved when it
    was added, and the folder may now be offline)."""
    return ntpath.normcase(path).rstrip("\\/")


def transform_from_1(carried, keyring):
    """Schema 2's rows for the configuration a schema-1 workspace carries
    over. carried: {table: (column names, rows as tuples)}. Returns {table:
    [row dict]} whose keys are schema 2's column names. Sealed and
    fingerprinted values come only from here, and before writing anything
    the store refuses rows that hold anything else in those columns: a
    plain value, or a fingerprint equal to a schema-1 value
    (store._plain_in_sealed)."""
    codec = VaultCodec(keyring, None)
    rows = {t: [dict(zip(cols, r, strict=True)) for r in data]
            for t, (cols, data) in carried.items()}
    out = {}
    if "receipts" in rows:
        out["receipts"] = rows["receipts"]
    # Watched folders: sealed path, fingerprinted identity; duplicates merged
    # into the older row with the least authority and a note.
    kept, dropped = {}, set()
    for f in sorted(rows.get("watched_folders", []),
                    key=lambda f: (f.get("created_at") or 0, f["id"])):
        identity = stored_folder_identity(f["path"])
        first = kept.get(identity)
        if first is None:
            kept[identity] = dict(f)
            continue
        dropped.add(f["id"])
        for flag in ("delete_originals", "fetch_cloud"):        # least authority
            first[flag] = min(first.get(flag) or 0, f.get(flag) or 0)
        first["active"] = max(first.get("active") or 0, f.get("active") or 0)
        first["last_error"] = MERGED_NOTE
    folders = []
    for identity, f in kept.items():
        path = f.pop("path")
        f["path_sealed"] = codec.seal(None, "watched_folders", "path_sealed", (f["id"],), path)
        f["path_fp"] = folder_fingerprint(keyring, identity)
        folders.append(f)
    if folders:
        out["watched_folders"] = folders
    # The intake salt, sealed; nothing else was ever kept in intake_meta.
    meta = [{"k": "salt", "v_sealed": codec.seal(None, "intake_meta", "v_sealed", ("salt",),
                                                 r["v"])}
            for r in rows.get("intake_meta", []) if r["k"] == "salt" and r["v"]]
    if meta:
        out["intake_meta"] = meta
    # Imported content: its SHA-256 becomes a fingerprint. A value that is not
    # a SHA-256 could not have come from media.sha256_file: dropped.
    content = [{"fp": fingerprint(keyring, "original", r["sha256"]),
                "imported_at": r["imported_at"]}
               for r in rows.get("intake_content", [])
               if isinstance(r["sha256"], str) and _SHA256_HEX.match(r["sha256"])]
    if content:
        out["intake_content"] = content
    # Files seen in each folder keep their keys (the salt is kept), and an
    # import's content hash in progress becomes a fingerprint. A merged
    # folder's rows go with it.
    seen = []
    for r in rows.get("intake_seen", []):
        if r["folder_id"] in dropped:
            continue
        r = dict(r)
        value = r.pop("content", None)
        r["content_fp"] = (fingerprint(keyring, "original", value)
                           if isinstance(value, str) and _SHA256_HEX.match(value) else None)
        seen.append(r)
    if seen:
        out["intake_seen"] = seen
    return out
