# workspace/media.py
# Import a recording into a meeting's media folder with bounded memory:
#   - the original file is hashed (its identity) but never modified;
#   - audio is decoded once, in a contained decoder process (see below), to
#     16 kHz mono float32, and written block by block, encrypted under the
#     meeting's audio key (workspace/audiocrypt.py, #111 C3a), to
#     <media>/audio.uma; the whole file is authenticated once, at import;
#   - engines then read slices of it (AudioReader), each block authenticated
#     as it is read. No plain copy of the decoded audio is ever written.
# Nothing in a media path comes from the source file's name. audiocrypt is
# imported inside the functions that use it (window-process module budget).

import hashlib
import os
import sys
from pathlib import Path

import numpy as np

from workspace.decode_child import (E_MEMORY, E_NO_AUDIO, E_SETUP, E_TOO_DENSE, E_TOO_LONG,
                                    LOW_INTEGRITY, MAX_DECODED_S, max_samples_for)

SAMPLE_RATE = 16000
AUDIO_FILE = "audio.uma"
SAVE_FAILED = "The recording could not be saved correctly. Try again."


def audio_context(mid):
    """The context a meeting's decoded audio file is sealed under (with its
    random file id, docs/ENCRYPTION.md "Audio files"). Pinned by a test:
    changing it makes every stored meeting's audio unreadable."""
    return ("decoded", mid)


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
#     process writes (encrypts) the audio itself and enforces the length and
#     disk limits.
# FFmpeg in the decoder may only use the supported recording types' demuxers,
# their audio decoders and the local file protocol (decode_child.OPEN_OPTIONS).
# Input sample rates outside 4 kHz to 384 kHz are refused, and a file may
# decode to at most one second of audio per 16 bytes of its size
# (decode_child.max_samples_for), enforced here as well as in the decoder.

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
    E_TOO_DENSE: "The recording holds far more audio than a file of its size can. It may be "
                 "damaged.",
}
last_decoder_integrity = None      # what the last decoder reported (tests, diagnostics)


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
             folder=None, dense_limit=None):
    """Write the decoder's audio frames to `out` (anything with write(bytes))
    until it reports done. Returns the number of samples. Every limit is
    enforced here, whatever the decoder does: `dense_limit` is the most
    samples the source file's size allows (decode_child.max_samples_for)."""
    samples = since_check = 0
    while True:
        kind, payload = next_frame()
        if kind == b"D" and not payload:
            return samples
        if kind != b"A" or len(payload) % 4:
            raise _error(kind, payload)
        samples += len(payload) // 4
        if samples > max_samples:
            raise DecodeError(MESSAGES[E_TOO_LONG])
        if dense_limit is not None and samples > dense_limit:
            raise DecodeError(MESSAGES[E_TOO_DENSE])
        out.write(payload)
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


def decode(src, out, folder=None):
    """Decode `src` in the contained decoder process, writing raw float32
    16 kHz mono to `out` (anything with write(bytes)). Returns the number of
    samples. folder: where the output goes, for the free-space checks.
    Raises DecodeError with a safe message. The app writes only through
    decode_to_file (encrypted); tools that compare engines on public clips
    pass their own plain writer."""
    global last_decoder_integrity
    import json
    import struct
    import subprocess

    from workspace import decode_child, jobs
    try:
        dense_limit = max_samples_for(os.path.getsize(src))
    except OSError as exc:
        raise DecodeError(_GENERIC) from exc
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    # -X disable-remote-debug: no other process can make the decoder run code
    # through Python 3.14's debugger interface (PEP 768; older Pythons ignore it).
    proc = subprocess.Popen([_python(), "-I", "-B", "-X", "disable-remote-debug", "-c",
                             decode_child.BOOT],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, creationflags=flags, env=env)
    import time
    job, frames = None, None
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
        samples = _collect(lambda: frames.next(IDLE_TIMEOUT_S), out, folder=folder,
                           dense_limit=dense_limit)
        if samples == 0:
            raise DecodeError("The file contains no audio.")
        return samples
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


def decode_to_file(src, dest, key, context):
    """Decode `src` in the contained decoder process to `dest`, encrypted
    (audiocrypt, float32, mono, 16 kHz) under `key` with `context`. Written
    as `dest`'s ".partial" file, finished (last block sealed, flushed to
    disk), then renamed. Returns (samples, the file's random id). Raises
    DecodeError with a safe message; a failed decode leaves no file."""
    from workspace import audiocrypt
    tmp = Path(dest).with_suffix(".partial")
    if _free_bytes(tmp.parent) < DISK_RESERVE:
        raise DecodeError("There is not enough free disk space to import this recording.")
    # A stale file from an interrupted import: the writer only ever creates a
    # new file (a new file id and key, so no nonce is reused) and refuses one
    # that exists.
    tmp.unlink(missing_ok=True)
    out = audiocrypt.AudioWriter(tmp, key, context, "float32", 1, SAMPLE_RATE)
    ok = False
    try:
        samples = decode(src, out, folder=tmp.parent)
        out.close()                              # seals the last block, fsyncs
        ok = True
    finally:
        if not ok:
            out.abort()
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass                 # e.g. held by antivirus: goes with the folder
    os.replace(tmp, dest)
    return samples, out.file_id


class AudioReader:
    """A meeting's decoded audio (16 kHz mono float32, encrypted): random
    reads with bounded memory. Opening checks the header tag and the last
    block; every other block is checked when it is read (AudioCryptError if
    it does not open). One reader per thread; close() releases the file
    (needed before cleanup on Windows)."""

    def __init__(self, path, key, context):
        from workspace import audiocrypt
        self.path = Path(path)
        self._r = audiocrypt.AudioReader(self.path, key, context)
        if (self._r.sample_format, self._r.channels, self._r.rate) != ("float32", 1,
                                                                        SAMPLE_RATE):
            self._r.close()
            raise audiocrypt.AudioCryptError("The audio file is damaged (header).")
        self.file_id = self._r.file_id

    def __len__(self):
        return self._r.frames

    def read(self, start, end):
        """A new float32 array of samples [start, end), clamped to the audio
        (empty when nothing is left): only the blocks it covers are read."""
        start, end = max(0, int(start)), min(self._r.frames, int(end))
        out = np.empty(max(0, end - start), dtype=np.float32)
        if len(out):
            self._r.readinto(out, start)
        return out

    def verify(self):
        """Authenticate every block. Returns the number of frames."""
        return self._r.verify()

    def close(self):
        self._r.close()


def open_audio(store, media_root, mid):
    """The meeting's decoded audio: the one way the app opens it. KeyMissing
    or DamagedContent (the store) when its audio key is gone or will not
    open, OSError when the file is missing, AudioCryptError when it is
    damaged or not this meeting's."""
    key = store.meeting_key(mid, "audio")
    return AudioReader(audio_path(media_root, mid), key, audio_context(mid))


def damaged_audio(exc):
    """Whether exc says an encrypted audio file did not open or read."""
    from workspace.audiocrypt import AudioCryptError
    return isinstance(exc, AudioCryptError)


class AudioRef:
    """What the speaker pass hands the worker: the file, its key and
    context, and its length. The key goes to the worker only inside the
    diarize request, over its pipe; it is never shown (repr) or logged."""

    __slots__ = ("path", "key", "context", "frames")

    def __init__(self, path, key, context, frames):
        self.path, self.key, self.context, self.frames = Path(path), key, context, frames

    def __repr__(self):
        return f"AudioRef(frames={self.frames})"


def audio_ref(store, media_root, mid):
    """An AudioRef for the meeting's decoded audio, its length read from the
    file (its header and last block are checked). Raises as open_audio."""
    key = store.meeting_key(mid, "audio")
    reader = AudioReader(audio_path(media_root, mid), key, audio_context(mid))
    try:
        return AudioRef(reader.path, key, audio_context(mid), len(reader))
    finally:
        reader.close()


class SourceChanged(ValueError):
    """The original file changed while it was being imported."""


def import_recording(store, media_root, src_path, title=None):
    """Create a meeting from a local recording. Returns (meeting_id, source_id).
    The caller validates src_path first (workspace.safe_paths).

    A failed import leaves nothing behind: the half-created meeting, its
    keys and its media folder (our own fresh content) are removed. If the
    original changes while it is hashed and decoded, the import is refused,
    because the recorded identity would not match the decoded audio. The
    saved file is read back and authenticated in full once, here; runs then
    check its length and file id only (runner._audio_matches)."""
    import shutil
    src = Path(src_path)
    before = src.stat()
    original_sha = sha256_file(src)
    mid = store.create_meeting(title or "Untitled meeting")
    folder = meeting_folder(media_root, mid)
    try:
        folder.mkdir(parents=True, exist_ok=False)
        samples, file_id = decode_to_file(src, folder / AUDIO_FILE,
                                          store.meeting_key(mid, "audio"), audio_context(mid))
        after = src.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise SourceChanged("The recording changed while it was being imported. "
                                "Wait until it has finished copying, then try again.")
        try:
            reader = open_audio(store, media_root, mid)
            try:
                saved = reader.verify()
            finally:
                reader.close()
        except Exception as exc:
            if not damaged_audio(exc):
                raise
            saved = None
        if saved != samples:
            raise DecodeError(SAVE_FAILED)
        # audio_id is the decoded file's random id (in its header), not a hash
        # of the audio: it ties the source to this exact file.
        sid = store.add_source(mid, original_path=str(src), original_sha256=original_sha,
                               original_size=before.st_size,
                               original_mtime_ns=before.st_mtime_ns,
                               audio_id=file_id.hex(), samples=samples,
                               sample_rate=SAMPLE_RATE)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        try:
            store.delete_new_meeting(mid)
        except Exception:
            pass
        raise
    return mid, sid


class BadMeetingId(ValueError):
    """A meeting id that is not the app's own format (#194): never joined to a
    path. The message is safe to show."""


def meeting_folder(media_root, mid):
    """The meeting's folder in the media store. Ids are the app's own
    (store.new_id: 32 lowercase hex characters); anything else, such as an id
    from a damaged or edited database, is refused before any path is built,
    so it can never point outside the media store."""
    if not (isinstance(mid, str) and len(mid) == 32
            and all(c in "0123456789abcdef" for c in mid)):
        raise BadMeetingId("Meeting not found.")
    return Path(media_root) / mid


def audio_path(media_root, mid):
    return meeting_folder(media_root, mid) / AUDIO_FILE
