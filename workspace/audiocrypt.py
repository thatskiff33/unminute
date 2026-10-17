# workspace/audiocrypt.py
# Encrypted audio files with random-access reads (#111 C1; C3 uses it for a
# meeting's decoded audio and the recorder's chunks). Design, byte layout and
# limits: docs/ENCRYPTION.md, "Audio files".
#
# File: a 64-byte header, a 16-byte header tag, then blocks. Each block holds
# FRAMES_PER_BLOCK frames (the last one fewer) sealed with AES-256-GCM:
#   - key: HKDF-SHA256 of the meeting's audio key, labelled with the file's
#     random 32-byte id and the caller's context (for example the meeting and
#     which file it is), so every file has its own key;
#   - the header tag seals nothing under its own nonce, with the header as
#     associated data: a wrong key or context, or a changed header, fails on
#     opening, even for a file that was interrupted;
#   - block nonce: the block's index, unique under that file's key;
#   - block associated data: the whole header, the block's index and whether
#     it is the last block. Reordered, swapped, cut or extended files fail.
# A finished file always ends with a block marked last, holding fewer than a
# full block of frames (possibly none). A file without one was interrupted:
# it opens only when the caller allows that (allow_incomplete), and then only
# up to its last whole block.
#
# A writer only ever creates a new file (never appends or rewrites), takes
# each block's index before writing it, and refuses to finish after a failed
# write, so no nonce is used twice under one key. AudioCryptError messages
# never carry content or paths (P3); an OSError from opening a file names the
# path, as elsewhere in the app, and is the caller's to report.

import os
import secrets
import struct
import threading

from workspace import vault

MAGIC = b"UMA1"
VERSION = 1
HEADER = struct.Struct("<4sBBHII32s16s")       # 64 bytes
TAG_BYTES = 16
DATA_START = HEADER.size + TAG_BYTES          # the header tag comes first
FRAMES_PER_BLOCK = 16384
FILE_ID_BYTES = 32
SAMPLE_FORMATS = {"float32": (1, 4), "int16": (2, 2)}  # name: (code, bytes per sample)
_BY_CODE = {code: (name, size) for name, (code, size) in SAMPLE_FORMATS.items()}
_ITEM_FORMATS = {"float32": ("f", "<f", "=f"), "int16": ("h", "<h", "=h")}
_RAW_FORMATS = ("B", "b", "c")
MAX_CHANNELS = 8
RATES = range(8000, 192001)
_NONCE = struct.Struct(">4xQ")                 # 4 zero bytes, then the block index
_HEADER_NONCE = b"\xff" * 4 + bytes(8)         # never a block's nonce
_BLOCK = struct.Struct(">QB")


class AudioCryptError(Exception):
    """An encrypted audio file could not be read. Safe to show: it never names
    the file or its content."""


_WRONG = ("The audio file could not be opened: the key is wrong, or the file is "
          "damaged or not where it was stored.")


def _file_aead(key, file_id, context):
    # The file id goes into HKDF's info, not its salt: it comes from the
    # file's header, and RFC 5869 (3.4) advises against a salt an attacker
    # can choose. The audio key is random, so no salt is needed (3.3).
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(vault.derive(key, "audio", (file_id,) + context))


class _Layout:
    def __init__(self, header):
        if len(header) != HEADER.size:
            raise AudioCryptError("The audio file is damaged (too short).")
        magic, version, fmt, channels, rate, per_block, file_id, reserved = \
            HEADER.unpack(header)
        if magic != MAGIC:
            raise AudioCryptError("This is not an encrypted Unminute audio file.")
        if version != VERSION:
            raise AudioCryptError("The audio file is from a newer version of Unminute.")
        if (fmt not in _BY_CODE or not 1 <= channels <= MAX_CHANNELS or rate not in RATES
                or per_block != FRAMES_PER_BLOCK or reserved != bytes(16)):
            raise AudioCryptError("The audio file is damaged (header).")
        self.header = header
        self.sample_format, sample_bytes = _BY_CODE[fmt]
        self.channels = channels
        self.rate = rate
        self.file_id = file_id
        self.frame_bytes = sample_bytes * channels
        self.block_bytes = FRAMES_PER_BLOCK * self.frame_bytes
        self.record_bytes = self.block_bytes + TAG_BYTES

    def aad(self, index, last):
        return self.header + _BLOCK.pack(index, 1 if last else 0)

    @staticmethod
    def nonce(index):
        return _NONCE.pack(index)


class AudioWriter:
    """Writes a new encrypted audio file. Use as a context manager: leaving
    normally finishes the file; leaving with an exception leaves it
    unfinished (it then opens only with allow_incomplete)."""

    def __init__(self, path, key, context, sample_format="float32", channels=1,
                 rate=16000):
        if sample_format not in SAMPLE_FORMATS:
            raise ValueError("unsupported sample format")
        if not 1 <= channels <= MAX_CHANNELS or rate not in RATES:
            raise ValueError("unsupported channels or rate")
        file_id = secrets.token_bytes(FILE_ID_BYTES)
        header = HEADER.pack(MAGIC, VERSION, SAMPLE_FORMATS[sample_format][0], channels,
                             rate, FRAMES_PER_BLOCK, file_id, bytes(16))
        self._layout = _Layout(header)
        self._sample_format = sample_format
        self._aead = _file_aead(key, file_id, context)
        self._failed = False
        self._f = open(path, "xb")              # never over an existing file
        try:
            self._f.write(header + self._aead.encrypt(_HEADER_NONCE, b"", header))
        except BaseException:
            self._f.close()
            raise
        self._buf = bytearray()
        self._index = 0
        self.frames = 0

    def _seal(self, data, last):
        L = self._layout
        index = self._index
        self._index += 1                        # taken before writing: never reused
        try:
            self._f.write(self._aead.encrypt(L.nonce(index), bytes(data), L.aad(index, last)))
        except BaseException:
            self._failed = True
            raise

    def _view(self, data):
        view = memoryview(data)
        if view.format not in _RAW_FORMATS and view.format not in _ITEM_FORMATS[
                self._sample_format]:
            raise TypeError("write bytes or samples of the file's format")
        return view.cast("B")

    def write(self, data):
        """Append whole frames: bytes (little-endian samples, interleaved
        channels) or an array of the file's sample format."""
        if self._f is None or self._failed:
            raise ValueError("the file is closed")
        view = self._view(data)
        L = self._layout
        if len(view) % L.frame_bytes:
            raise ValueError("write whole frames")
        self._buf += view
        while len(self._buf) >= L.block_bytes:
            self._seal(self._buf[:L.block_bytes], last=False)
            del self._buf[:L.block_bytes]
        self.frames += len(view) // L.frame_bytes

    def flush(self, durable=True):
        """Write the whole blocks so far to disk (the part block waits)."""
        if self._f is None:
            raise ValueError("the file is closed")
        try:
            self._f.flush()
            if durable:
                os.fsync(self._f.fileno())
        except BaseException:
            self._failed = True
            raise

    def close(self, durable=True):
        """Finish the file: seal the last block, marked as last. After a
        failed write the file cannot be finished: it is left unfinished and
        this raises."""
        if self._f is None:
            return
        if self._failed:
            self.abort()
            raise ValueError("an earlier write failed; the file was left unfinished")
        try:
            self._seal(self._buf, last=True)
            self._buf.clear()
            self.flush(durable)
        except BaseException:
            self.abort()
            raise
        self._f.close()
        self._f = None

    def abort(self):
        """Stop without finishing the file (it stays incomplete)."""
        if self._f is not None:
            self._buf.clear()
            try:
                self._f.close()
            finally:
                self._f = None

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if kind is None:
            self.close()
        else:
            self.abort()


class AudioReader:
    """Random-access reads from an encrypted audio file. The header tag and
    the last block are checked on opening; every other block when it is
    read."""

    def __init__(self, path, key, context, allow_incomplete=False):
        self._f = open(path, "rb")
        try:
            self._open(key, context, allow_incomplete)
        except BaseException:
            self._f.close()
            raise
        self._lock = threading.Lock()
        self._cached = (-1, b"")

    def _open(self, key, context, allow_incomplete):
        from cryptography.exceptions import InvalidTag
        L = self._layout = _Layout(self._f.read(HEADER.size))
        self.sample_format, self.channels, self.rate = L.sample_format, L.channels, L.rate
        self._aead = _file_aead(key, L.file_id, context)
        tag = self._f.read(TAG_BYTES)
        if len(tag) != TAG_BYTES:
            raise AudioCryptError("The audio file is damaged (too short).")
        try:
            self._aead.decrypt(_HEADER_NONCE, tag, L.header)
        except InvalidTag:
            raise AudioCryptError(_WRONG) from None
        body = os.fstat(self._f.fileno()).st_size - DATA_START
        whole, rest = divmod(body, L.record_bytes)
        self._whole = whole
        self._last = None
        if rest >= TAG_BYTES and (rest - TAG_BYTES) % L.frame_bytes == 0:
            try:
                self._last = self._decrypt(whole, rest, last=True)
            except AudioCryptError:
                if not allow_incomplete:
                    raise
        if self._last is None and not allow_incomplete:
            raise AudioCryptError("The audio file is incomplete or damaged.")
        self.complete = self._last is not None
        self.frames = whole * FRAMES_PER_BLOCK + (len(self._last or b"") // L.frame_bytes)

    def _decrypt(self, index, size, last):
        from cryptography.exceptions import InvalidTag
        L = self._layout
        self._f.seek(DATA_START + index * L.record_bytes)
        record = self._f.read(size)
        if len(record) != size:
            raise AudioCryptError("The audio file is damaged (cut short).")
        try:
            return self._aead.decrypt(L.nonce(index), record, L.aad(index, last))
        except InvalidTag:
            raise AudioCryptError(_WRONG) from None

    def _block(self, index):
        if index == self._whole:
            return self._last
        if self._cached[0] != index:
            self._cached = (index, self._decrypt(index, self._layout.record_bytes, last=False))
        return self._cached[1]

    def __len__(self):
        return self.frames

    def readinto(self, buffer, start):
        """Fill buffer (a writable bytes-like object, for example a numpy
        array of the file's sample format) with frames from frame start.
        Returns the number of frames copied: fewer than the buffer holds at
        the end of the audio."""
        out = memoryview(buffer).cast("B")
        fb = self._layout.frame_bytes
        if not 0 <= start <= self.frames or len(out) % fb:
            raise ValueError("outside the audio, or not whole frames")
        end = min(self.frames, start + len(out) // fb)
        with self._lock:
            if self._f.closed:
                raise ValueError("the file is closed")
            pos = start
            while pos < end:
                index, offset = divmod(pos, FRAMES_PER_BLOCK)
                block = self._block(index)
                take = min(end - pos, FRAMES_PER_BLOCK - offset)
                at = (pos - start) * fb
                out[at:at + take * fb] = block[offset * fb:(offset + take) * fb]
                pos += take
        return end - start

    def read(self, start, count):
        """Up to count frames from frame start, as a bytearray."""
        if not 0 <= start <= self.frames or count < 0:
            raise ValueError("outside the audio")
        out = bytearray((min(self.frames, start + count) - start) * self._layout.frame_bytes)
        self.readinto(out, start)
        return out

    def verify(self):
        """Authenticate every block. Returns the number of frames."""
        with self._lock:
            for index in range(self._whole):
                self._decrypt(index, self._layout.record_bytes, last=False)
        return self.frames

    def close(self):
        with self._lock:
            self._f.close()
            self._cached = (-1, b"")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
