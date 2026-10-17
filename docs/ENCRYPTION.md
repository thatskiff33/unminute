# Encryption at rest

How Unminute encrypts meeting content on disk, what that protects against and what it does not. This is the design the code follows; threat IDs (T6 and so on) are in [SECURITY_MODEL.md](SECURITY_MODEL.md).

**Status (#111 phase C).** C1 adds the building blocks: `workspace/dpapi.py`, `workspace/vault.py` and `workspace/audiocrypt.py`, with their tests. The app does not use them yet. C2 encrypts the database's content columns (schema 2), C3 the audio files and recorder chunks with crypto-erase at cleanup, and C4 adds the locked screen for when Windows cannot unlock the key. Until C3 ships, content on disk is protected by BitLocker only.

## What it protects against

* **T6, offline access to the disk.** A laptop that is off or hibernated, a removed disk or a disk image holds only ciphertext. Opening it needs Windows to unlock the user's key, which normally means this user signed in on this computer (see the DPAPI limits below).
* **T7, deleted content left behind.** Each meeting's content is sealed under that meeting's own keys, and cleanup destroys them (C3). A leftover copy of the content (database free pages, the WAL, SSD remnants) cannot be read once every copy of that meeting's wrapped key is gone too. The live database removes it with the controls it already has: `secure_delete`, WAL truncation and compaction. Copies of the whole workspace taken before cleanup (a backup, a disk image) still hold the wrapped key, so this Windows user can open those meetings from them.
* **T8, other accounts and backup agents** reading the workspace files see ciphertext. An administrator who can act as the user, or who holds the domain's DPAPI backup key, can unlock it (below).

## What it does not do

* **Code running as the signed-in user can read everything.** Windows unlocks the root key for any process running as the user (that is how the app itself unlocks it). Malware in the session, or a person using the unlocked session, reads content as the app does. Endpoint protection is the control (T9).
* **DPAPI is not tied to one computer in every set-up.** Microsoft documents that a user with a roaming profile can unlock the data from another computer on the network, and that on an Active Directory domain anyone holding the domain's DPAPI backup keys can unlock any domain user's data. Microsoft describes the user's keys as derived from their password, so a disk image plus that password may be enough. What applies to Unlimit's laptops (Entra ID joined, hybrid or domain joined, roaming profiles or not) is to confirm on a pilot laptop (validation tracker #189).
* **Keys live in memory while the app runs.** Python cannot reliably wipe memory, so keys can appear in a crash dump, the page file or the hibernation file. BitLocker covers the page and hibernation files; crash-dump settings are part of the IT deployment guide (#111 slice D1). `workspace/dpapi.py` wipes its copy of the key and Windows's output buffer, but Python's own copies remain until the memory is reused.
* **Some facts stay readable.** Timestamps, states, counts, durations and receipts stay in plain text, so the update gate and `--status` work without a key. The length of each sealed value shows the approximate length of its text.
* **A sealed value is bound to its place, not to its version.** An older copy of the same value (for example from a backup) opens where it was stored. C2 decides whether any value needs protection against that.
* **Content written before C3** by v0.4.x may remain in free disk space in plain text, protected by BitLocker only.
* **If Windows loses the user's protection keys, the content is lost.** This can happen when an administrator resets the password without the old one, or a profile is damaged. The app then shows the locked screen (C4) and never falls back to plain text. Whether an Entra ID password reset keeps these keys is to confirm (#189).

## Key hierarchy

| Key | What it is | Where it is kept | What it protects |
| --- | --- | --- | --- |
| Root key | 32 random bytes per workspace | Only protected by Windows for this user (DPAPI, user scope, no prompt, Unminute's own entropy) | Everything, through the keys below |
| Wrap key | Derived from the root key (HKDF-SHA256, label `wrap`) | Never stored | Meeting keys, and nothing else (sealed with the label `key`) |
| Workspace key | Derived (label `workspace`) | Never stored | Workspace-wide values such as watched-folder paths and the templates file |
| Fingerprint key | Derived (label `fingerprint`), then one key per purpose | Never stored | HMAC-SHA256 fingerprints that replace plain hashes (of a file's SHA-256 digest, so large files are still hashed as they stream) |
| Check value | Derived (label `check`) | Stored | Tells a wrong or damaged root key from a good one, so the app can say which happened |
| Meeting text key, meeting audio key | 32 random bytes each, per meeting | Only wrapped under the wrap key, bound to their purpose and the meeting's random reference | That meeting's text and audio. Destroying the wrapped key is the erase. The text key seals values; the audio key is only used to derive per-file keys |

Every random value comes from the operating system's secure generator (`secrets`). HKDF is used without a salt, which RFC 5869 allows for a key that is already uniformly random.

**Labels and contexts** (`vault.encode`) are encoded as `unminute/v1/`, the label (lower-case letters, digits and hyphens), a zero byte, then each part of the context as a type byte (`s` text as UTF-8, `i` a signed 64-bit big-endian integer, `b` bytes), a 4-byte big-endian length and the part itself. Two different labels or contexts therefore never give the same bytes. The same bytes are the HKDF info of a derived key and the associated data of a sealed value.

## Sealed values

A sealed value (a "cell") is: format version (1 byte), a random 12-byte nonce, the AES-256-GCM ciphertext, and its 16-byte tag. The associated data is the version byte plus the encoded label `cell` and the value's context, for example table, column and row. A value copied to another row, column or meeting therefore fails to open, as does any changed byte. Errors from opening never carry the value or chain an exception that does.

Random 96-bit nonces are safe for at most 2^32 values per key (NIST SP 800-38D, section 8.3). A meeting key seals one meeting's values: even one value per word with ten edits each, a 12-hour meeting is about 2^20. The workspace and wrap keys seal a few values per folder, template or meeting.

Wrapped meeting keys use the same format under the wrap key with the label `key` and the context `(purpose, meeting reference)`. A text key cannot be unwrapped as an audio key or as another meeting's key, and a wrapped key never opens as a stored value.

## Audio files

Decoded audio and recorder chunks are long and read in pieces, so they have their own format (`workspace/audiocrypt.py`):

| Part | Size | Content |
| --- | --- | --- |
| Header | 64 bytes, little-endian | `UMA1`; format version (1 byte); sample format (1 byte: 1 float32, 2 int16); channels (2 bytes); sample rate (4); frames per block (4, always 16,384); the file's random id (32); 16 reserved zero bytes |
| Header tag | 16 bytes | AES-256-GCM over nothing, nonce `ff ff ff ff` then 8 zero bytes, the header as associated data |
| Blocks | 16,384 frames each, plus a 16-byte tag | AES-256-GCM. Nonce: 4 zero bytes then the block's index (8 bytes, big-endian). Associated data: the header, the index (8 bytes, big-endian) and 1 byte that is 1 for the last block. The last block holds fewer than 16,384 frames, possibly none |

* **Key.** Each file has its own key: HKDF-SHA256 of the meeting's audio key, with the label `audio` and the context `(file id, then the caller's context)`, for example the meeting and which file it is. A block moved from one file, chunk or meeting to another does not open. The file id is in the info rather than the salt, because it is read from the file (RFC 5869, section 3.4).
* **Nonces.** Unique because every file has its own key, and a writer only ever creates a new file: it never appends to or rewrites one. It takes each block's index before writing the block, and after a failed write it refuses to write more or to finish the file, so no nonce is ever used twice under one key.
* **Opening.** The header tag is checked first, so a wrong key or context, or a changed header, fails at once. Then the last block is checked. Reordering blocks, cutting the file or adding to it is detected.
* **Finished and interrupted files.** A finished file ends with a block marked last. A file without one was interrupted (the app stopped while writing it). It opens only when the caller allows that, and then only up to its last whole block. Anyone who can change the file can also cut it to look interrupted, so only recovery may allow it, and it should call `verify()`. C3 decides how the recorder's recovery uses this.
* **Reads.** Any range can be read, into a buffer the caller provides (`readinto`, for example a numpy array) or as a new one; each block is checked when it is read, and `verify()` checks every block. The overhead is 16 bytes per 64 KB block of mono float32 audio (0.02%).

## Choices

* **AES-256-GCM and HKDF-SHA256** from pyca/cryptography, which uses OpenSSL; **HMAC-SHA256** from Python's standard library. No primitive is written here.
* **Random nonces for cells, counters for audio.** Cells are written from many places, so a random nonce needs no shared state; audio blocks are many and written in order under a per-file key.
* **Key wrapping with AES-GCM rather than AES key wrap,** so a wrapped key is bound to its purpose and meeting.
* **DPAPI in user scope,** never machine scope, which any account on the computer could unlock.
* **Per-meeting keys rather than one key for the whole database or folder** (for example Windows EFS or an encrypted SQLite build): only per-meeting keys allow one meeting to be erased while the others stay.
* **No wrap-key rotation at cleanup** (decided 18 Oct 2026, the recommended option while the user was away; recorded on #111 and the C1 pull request). Rotating the wrap key would not help against a whole-workspace backup, which holds the root key's protected copy too, and BitLocker already covers leftover disk blocks while the laptop is off. The limit is stated under T7 above instead.
* **Versioned formats.** Each format has a version and every label has `v1`, so a later algorithm can sit beside the current one.

## Tests

`tests/test_vault.py`, `tests/test_audiocrypt.py` and `tests/test_dpapi.py`. Known answers built from the raw primitives pin every stored format: sealed values, wrapped keys, fingerprints, the root key's DPAPI entropy and audio files. A change that would make stored data unreadable fails a test.

## Sources

* [NIST SP 800-38D](https://csrc.nist.gov/pubs/sp/800/38/d/final): AES-GCM, and the 2^32 limit for random nonces under one key (section 8.3).
* [RFC 5869](https://www.rfc-editor.org/rfc/rfc5869): HKDF; a salt is optional for a uniformly random key (section 3.3), and a salt should not be one an attacker can choose (section 3.4).
* [Microsoft: CryptProtectData](https://learn.microsoft.com/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata): DPAPI's user and machine scope, the no-prompt flag, and unlocking from another computer with a roaming profile.
* [Microsoft: DPAPI backup keys on AD domain controllers](https://learn.microsoft.com/windows/win32/seccng/cng-dpapi-backup-keys-on-ad-domain-controllers): keys protected by the user's password, and the domain backup keys that can unlock any domain user's data.
* [pyca/cryptography: authenticated encryption](https://cryptography.io/en/latest/hazmat/primitives/aead/): the AES-GCM interface used.
