# Encryption at rest

How Unminute encrypts meeting content on disk, what that protects against and what it does not. This is the design the code follows; threat IDs (T6 and so on) are in [SECURITY_MODEL.md](SECURITY_MODEL.md).

**Status (#111 phase C).** C1 adds the building blocks: `workspace/dpapi.py`, `workspace/vault.py` and `workspace/audiocrypt.py`, with their tests. The app does not use them yet. C2 encrypts the database's content columns (schema 2), C3 the audio files and recorder chunks with crypto-erase at cleanup, and C4 adds the locked screen for when Windows cannot unlock the key. Until C3 ships, content on disk is protected by BitLocker only.

## What it protects against

* **T6, offline access to the disk.** A laptop that is off or hibernated, a removed disk, a disk image or a backup copy of the workspace holds only ciphertext. Opening it needs this Windows user signed in on this computer.
* **T7, deleted content left behind.** Each meeting's content is sealed under that meeting's own keys. Cleanup destroys the keys (C3), so copies left in database free pages, the WAL, SSD remnants or old backups cannot be read, wherever they are.
* **T8, other accounts, administrators and backup agents** reading the workspace files see ciphertext.

## What it does not do

* **Code running as the signed-in user can read everything.** Windows unlocks the root key for any process running as the user (that is how the app itself unlocks it). Malware in the session, or a person using the unlocked session, reads content as the app does. Endpoint protection is the control (T9).
* **Keys live in memory while the app runs.** Python cannot reliably wipe memory, so keys can appear in a crash dump, the page file or the hibernation file. BitLocker covers the page and hibernation files; crash-dump settings are part of the IT deployment guide (D1). `workspace/dpapi.py` wipes Windows's output buffer, but Python's own copies remain until the memory is reused.
* **Some facts stay readable.** Timestamps, states, counts, durations and receipts stay in plain text, so the update gate and `--status` work without a key. The length of each sealed value shows the approximate length of its text.
* **Content written before C3** by v0.4.x may remain in free disk space in plain text, protected by BitLocker only.
* **If Windows loses the user's protection keys, the content is lost.** This can happen when an administrator resets the password without the old one, or a profile is damaged. The app then shows the locked screen (C4) and never falls back to plain text. Whether an Entra ID password reset keeps these keys is to be confirmed on a pilot laptop (validation tracker #189).

## Key hierarchy

| Key | What it is | Where it is kept | What it protects |
| --- | --- | --- | --- |
| Root key | 32 random bytes per workspace | Only protected by Windows for this user (DPAPI, user scope, no prompt, Unminute's own entropy) | Everything, through the keys below |
| Wrap key | Derived from the root key (HKDF-SHA256, label `wrap`) | Never stored | Meeting keys, and nothing else |
| Workspace key | Derived (label `workspace`) | Never stored | Workspace-wide values such as watched-folder paths and the templates file |
| Fingerprint key | Derived (label `fingerprint`), then one key per purpose | Never stored | HMAC-SHA256 fingerprints that replace plain hashes |
| Check value | Derived (label `check`) | Stored | Tells a wrong or damaged root key from a good one, so the app can say which happened |
| Meeting text key, meeting audio key | 32 random bytes each, per meeting | Only wrapped under the wrap key, bound to their purpose and the meeting's random reference | That meeting's text and audio. Destroying the wrapped key is the erase |

Every random value comes from the operating system's secure generator (`secrets`). Labels and contexts are encoded with a type and a length for each part (`vault.encode`), so two different contexts never give the same bytes.

## Sealed values

A sealed value (a "cell") is: format version (1 byte), a random 12-byte nonce, the AES-256-GCM ciphertext, and its 16-byte tag. The associated data is the version byte plus the value's context, for example table, column and row. A value copied to another row, column or meeting therefore fails to open, as does any changed byte.

Random 96-bit nonces are safe for at most 2^32 values per key (NIST SP 800-38D, section 8.3). A meeting key seals one meeting's values, and the workspace and wrap keys seal a few values per folder, template or meeting, so real use stays many orders of magnitude below that.

Wrapped meeting keys use the same format under the wrap key, with the context `("key", purpose, meeting reference)`. A text key cannot be unwrapped as an audio key or as another meeting's key.

## Audio files

Decoded audio and recorder chunks are long and read in pieces, so they have their own format (`workspace/audiocrypt.py`):

| Part | Size | Content |
| --- | --- | --- |
| Header | 64 bytes | `UMA1`, format version, sample format (1 float32, 2 int16), channels, sample rate, frames per block (16,384), the file's random 32-byte id, 16 reserved zero bytes; all little-endian |
| Blocks | 16,384 frames each, plus a 16-byte tag | AES-256-GCM. The last block holds fewer than 16,384 frames, possibly none |

* **Key.** Each file has its own key: HKDF-SHA256 of the meeting's audio key, salted with the file's id and labelled with the caller's context (for example the meeting and which file it is). A block moved from one file, chunk or meeting to another does not open.
* **Nonce.** The block's index. It is unique because every file has its own key, and a writer only ever creates a new file: it never appends to or rewrites one, so a nonce is never used twice under one key.
* **Associated data.** The whole header, the block's index, and whether it is the last block. Changing the header, reordering blocks, cutting the file or adding to it is detected.
* **Finished and interrupted files.** A finished file ends with a block marked last. A file without one was interrupted (the app stopped while writing it). It opens only when the caller allows that, and then only up to its last whole block; the recorder's recovery uses this.
* **Reads.** Any range can be read; each block is checked when it is read, and the last block when the file is opened. `verify()` checks every block. The overhead is 16 bytes per 64 KB block of mono float32 audio (0.02%).

## Choices

* **AES-256-GCM, HKDF-SHA256 and HMAC-SHA256** from pyca/cryptography, which uses OpenSSL. No primitive is written here.
* **Random nonces for cells, counters for audio.** Cells are written from many places, so a random nonce needs no shared state; audio blocks are many and written in order under a per-file key.
* **Key wrapping with AES-GCM rather than AES key wrap,** so a wrapped key is bound to its purpose and meeting.
* **DPAPI in user scope,** never machine scope, which any account on the computer could unlock.
* **Per-meeting keys rather than one key for the whole database or folder** (for example Windows EFS or an encrypted SQLite build): only per-meeting keys allow one meeting to be erased while the others stay.
* **Versioned formats.** Each format has a version and every label has `v1`, so a later algorithm can sit beside the current one.

## Tests

`tests/test_vault.py`, `tests/test_audiocrypt.py` and `tests/test_dpapi.py`. Known answers built from the raw primitives pin the formats, so a change that would make stored data unreadable fails a test.

## Sources

* [NIST SP 800-38D](https://csrc.nist.gov/pubs/sp/800/38/d/final): AES-GCM, and the 2^32 limit for random nonces under one key (section 8.3).
* [RFC 5869](https://www.rfc-editor.org/rfc/rfc5869): HKDF.
* [Microsoft: CryptProtectData](https://learn.microsoft.com/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata): DPAPI's user and machine scope, and the no-prompt flag.
* [pyca/cryptography: authenticated encryption](https://cryptography.io/en/latest/hazmat/primitives/aead/): the AES-GCM interface used.
