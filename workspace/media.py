# workspace/media.py
# Import a recording into a meeting's media folder with bounded memory:
#   - the original file is hashed (its identity) but never modified;
#   - audio is decoded once, in a contained decoder process (see below), to
#     16 kHz mono float32, and written block by block to <media>/audio.f32
#     while this process hashes it;
#   - engines then read slices of that file through a memory map.
# Nothing in a media path comes from the source file's name.

import hashlib
import os
import sys
from pathlib import Path

import numpy as np

from workspace.decode_child import (DEMUXERS, E_MEMORY, E_NO_AUDIO, E_SETUP, E_TOO_LONG,
                                    LOW_INTEGRITY, MAX_DECODED_S, OPEN_OPTIONS)

SAMPLE_RATE = 16000
AUDIO_FILE = "audio.f32"


def sha256_file(path, chunk=8 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


# Recordings can come from clients or shared folders, so the file is treated
# as hostile (#111 slice A3, threats T2 to T4 in docs/SECURITY_MODEL.md). It is
# decoded by workspace/decode_child.py in a separate process that this app:
#   - starts as a plain child process (it gets no handle to the app);
#   - puts in a Windows job before giving it the file (workspace/jobs.py:
#     memory limit, no child processes, no clipboard or desktop access,
#     killed with the app);
#   - requires to report Low integrity first (it lowers its own level), so
#     Windows stops it writing the user's files or opening this process;
#   - never trusts: replies are fixed binary frames, checked here; this
#     process writes and hashes the audio itself and enforces the length and
#     disk limits.
# FFmpeg in the decoder may only use the supported recording types' demuxers
# and the local file protocol (decode_child.OPEN_OPTIONS).

DISK_RESERVE = 1 << 30             # never fill the disk: keep 1 GB free
DECODER_MEMORY = 2 << 30           # job memory limit for the decode process
READY_TIMEOUT_S = 180.0            # start-up, imports included (antivirus can be slow)
IDLE_TIMEOUT_S = 300.0             # longest pause between audio frames
MAX_FRAME_BYTES = 4 << 20          # a frame larger than this is a broken decoder
_CHECK_EVERY = 64 << 20            # bytes written between free-space checks
_GENERIC = "That recording could not be imported (unsupported or damaged file)."
MESSAGES = {
    E_NO_AUDIO: "The file has no audio track.",
    E_TOO_LONG: "The recording is longer than 12 hours.",
    E_MEMORY: "The recording needs more memory to decode than this app allows. It may be "
              "damaged.",
    E_SETUP: "The recording could not be decoded safely on this computer. Ask your IT team.",
}
last_decoder_integrity = None      # what the last decoder reported (tests, diagnostics)
SUPPORTED_DEMUXERS, DECODER_OPTIONS = DEMUXERS, OPEN_OPTIONS    # for tests and docs


class DecodeError(ValueError):
    """The recording could not be decoded (message is safe to show)."""


def _free_bytes(folder):
    import shutil
    return shutil.disk_usage(folder).free


class _Frames:
    """Reads the decoder's frames on a thread, so waits can time out. The
    thread ends when the stream ends or stop() is called; it never blocks
    for ever on a full queue. `deadline` (time.monotonic) bounds the whole
    decode, however steadily a decoder trickles out frames."""

    def __init__(self, stream, deadline=None):
        import queue
        import threading
        self._stream, self._queue = stream, queue.Queue(maxsize=8)
        self._stop, self._deadline = threading.Event(), deadline
        self._thread = threading.Thread(target=self._run, daemon=True, name="decoder-frames")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, timeout):
        self._thread.join(timeout)

    def _put(self, item):
        import queue
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self._stream.read(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _run(self):
        import struct
        try:
            while not self._stop.is_set():
                head = self._read(5)
                if head is None:
                    self._put((None, None))
                    return
                kind, size = head[:1], struct.unpack("<I", head[1:])[0]
                if kind not in (b"R", b"A", b"D", b"E") or size > MAX_FRAME_BYTES:
                    self._put((b"?", None))             # broken: stop reading
                    return
                payload = self._read(size) if size else b""
                if payload is None:
                    self._put((None, None))
                    return
                if not self._put((kind, payload)) or kind in (b"D", b"E"):
                    return
        except (OSError, ValueError):                   # the stream was closed under us
            self._put((None, None))

    def next(self, timeout):
        import queue
        import time
        if self._deadline is not None:
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise DecodeError(TOO_SLOW)
            timeout = min(timeout, left)
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise DecodeError(TOO_SLOW) from None
            raise DecodeError("Decoding the recording stopped making progress. It may be "
                              "damaged.") from None


TOO_SLOW = "Decoding the recording took far longer than it should. It may be damaged."


def _deadline_s(src):
    """The longest a whole decode may take: 15 minutes plus 1 second per MB.
    Decoding runs many times faster than real time, so only a stuck or
    hostile decoder gets near this."""
    try:
        size = os.path.getsize(src)
    except OSError:
        size = 0
    return 900 + size / (1 << 20)


def _error(kind, payload):
    if kind == b"E" and len(payload) == 1:
        return DecodeError(MESSAGES.get(payload[0], _GENERIC))
    if kind is None:
        return DecodeError("The recording could not be decoded (the decoder stopped). It may "
                           "be damaged.")
    return DecodeError(_GENERIC)


def _collect(next_frame, out, max_samples=MAX_DECODED_S * SAMPLE_RATE, reserve=DISK_RESERVE,
             folder=None):
    """Write the decoder's audio frames to `out` until it reports done.
    Returns (samples, sha256 of the bytes written). Every limit is enforced
    here, whatever the decoder does."""
    h = hashlib.sha256()
    samples = since_check = 0
    while True:
        kind, payload = next_frame()
        if kind == b"D" and not payload:
            return samples, h.hexdigest()
        if kind != b"A" or len(payload) % 4:
            raise _error(kind, payload)
        samples += len(payload) // 4
        if samples > max_samples:
            raise DecodeError(MESSAGES[E_TOO_LONG])
        out.write(payload)
        h.update(payload)
        since_check += len(payload)
        if folder is not None and since_check >= _CHECK_EVERY:
            since_check = 0
            if _free_bytes(folder) < reserve:
                raise DecodeError("There is not enough free disk space to import this "
                                  "recording.")


def _python():
    """The interpreter itself, not a virtual environment's launcher (which
    would start the interpreter as a second process outside the job's
    one-process limit). The child gets this process's sys.path instead."""
    return getattr(sys, "_base_executable", None) or sys.executable


def decode_to_file(src, dest):
    """Decode `src` to raw float32 16 kHz mono at `dest` in the contained
    decoder process. Returns (samples, sha256 of the written bytes). Raises
    DecodeError with a safe message."""
    global last_decoder_integrity
    import json
    import struct
    import subprocess

    from workspace import decode_child, jobs
    tmp = Path(dest).with_suffix(".partial")
    if _free_bytes(tmp.parent) < DISK_RESERVE:
        raise DecodeError("There is not enough free disk space to import this recording.")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    proc = subprocess.Popen([_python(), "-I", "-B", "-c", decode_child.BOOT],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, creationflags=flags, env=env)
    import time
    job, ok, frames = None, False, None
    try:
        try:
            job = jobs.contain(proc.pid, DECODER_MEMORY)
        except jobs.JobError as exc:
            raise DecodeError(MESSAGES[E_SETUP]) from exc
        frames = _Frames(proc.stdout, deadline=time.monotonic() + _deadline_s(src))
        proc.stdin.write((json.dumps(sys.path) + "\n").encode("utf-8"))
        proc.stdin.flush()
        kind, payload = frames.next(READY_TIMEOUT_S)
        if kind != b"R" or len(payload) != 4:
            raise _error(kind, payload)
        last_decoder_integrity = struct.unpack("<I", payload)[0]
        if sys.platform == "win32":
            # Checked here too, not only taken from the decoder's own report.
            actual = decode_child.process_integrity(proc.pid)
            last_decoder_integrity = max(last_decoder_integrity, actual)
            if last_decoder_integrity > LOW_INTEGRITY:
                raise DecodeError(MESSAGES[E_SETUP])    # not sandboxed: never give it the file
        proc.stdin.write((json.dumps({"src": str(src)}) + "\n").encode("utf-8"))
        proc.stdin.flush()
        with open(tmp, "wb") as out:
            samples, digest = _collect(lambda: frames.next(IDLE_TIMEOUT_S), out,
                                       folder=tmp.parent)
            out.flush()
            os.fsync(out.fileno())
        if samples == 0:
            raise DecodeError("The file contains no audio.")
        ok = True
    except OSError as exc:          # broken pipe and the like: the decoder went away
        raise DecodeError("The recording could not be decoded (the decoder stopped). It may "
                          "be damaged.") from exc
    finally:
        # Stop the decoder first: the reader thread may be blocked reading its
        # output, and closing the pipe would wait for that read. Once the
        # process is gone the read ends and the pipes close at once.
        if frames is not None:
            frames.stop()
        if job is not None:
            job.close()                          # ends the decoder if it is still running
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            pass
        if frames is not None:
            frames.join(5)                       # its read ends once the process is gone
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        if not ok:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass                 # e.g. held by antivirus: goes with the folder
    os.replace(tmp, dest)
    return samples, digest


class AudioReader:
    """Read-only memory-mapped access to a meeting's decoded audio."""

    def __init__(self, path):
        self.path = Path(path)
        self._mm = np.memmap(self.path, dtype="<f4", mode="r")

    def __len__(self):
        return len(self._mm)

    def read(self, start, end):
        """A copy of samples [start, end) - only that slice is paged in."""
        start, end = max(0, int(start)), min(len(self._mm), int(end))
        return np.array(self._mm[start:end], dtype=np.float32)

    def close(self):
        mm = self._mm
        self._mm = None
        if mm is not None and getattr(mm, "_mmap", None) is not None:
            mm._mmap.close()      # release the file handle (needed before cleanup, #17)


class SourceChanged(ValueError):
    """The original file changed while it was being imported."""


def import_recording(store, media_root, src_path, title=None):
    """Create a meeting from a local recording. Returns (meeting_id, source_id).
    The caller validates src_path first (workspace.safe_paths).

    A failed import leaves nothing behind: the half-created meeting and its
    media folder (our own fresh content) are removed. If the original changes
    while it is hashed and decoded, the import is refused, because the
    recorded identity would not match the decoded audio."""
    import shutil
    src = Path(src_path)
    before = src.stat()
    original_sha = sha256_file(src)
    mid = store.create_meeting(title or "Untitled meeting")
    folder = Path(media_root) / mid
    try:
        folder.mkdir(parents=True, exist_ok=False)
        samples, audio_sha = decode_to_file(src, folder / AUDIO_FILE)
        after = src.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise SourceChanged("The recording changed while it was being imported. "
                                "Wait until it has finished copying, then try again.")
        sid = store.add_source(mid, original_path=str(src), original_sha256=original_sha,
                               original_size=before.st_size,
                               original_mtime_ns=before.st_mtime_ns,
                               audio_sha256=audio_sha, samples=samples,
                               sample_rate=SAMPLE_RATE)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        try:
            store.delete_new_meeting(mid)
        except Exception:
            pass
        raise
    return mid, sid


def audio_path(media_root, mid):
    return Path(media_root) / mid / AUDIO_FILE
