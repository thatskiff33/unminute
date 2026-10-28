# workspace/media.py
# Import a recording into a meeting's media folder with bounded memory:
#   - the original file is hashed (its identity) but never modified;
#   - audio is decoded once, in a contained decoder process (see below), to
#     16 kHz mono float32, and written block by block, encrypted under the
#     meeting's audio key (workspace/audiocrypt.py, #111 C3a), to
#     <media>/audio.uma; the whole file is authenticated once, at import;
#   - engines then read slices of it (AudioReader), each block authenticated
#     as it is read. No plain copy of the decoded audio is ever written.
# A microphone recording (#30) has no original file: its encrypted chunks
# are opened and checked in this process (workspace/recorder.py) and streamed
# to the decoder over its stdin as one WAV (import_stream, #111 C3b), so no
# plain recording.wav is ever written. A two-track recording (#108) also
# leaves its sealed track-activity file there (workspace/activity.py).
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
#   - starts in a Windows AppContainer with no capabilities (#299,
#     workspace/appcontainer.py), suspended, so Windows gives it no access to
#     the user's files, the workspace or its key, and no network, loopback
#     included; it gets no handle to the app;
#   - puts in a Windows job before it runs (workspace/jobs.py: memory limit,
#     no child processes, no clipboard or desktop access, killed with the app),
#     and checks its token from outside (that container, no capabilities, Low
#     integrity) before resuming it;
#   - gives the recording as one read-only handle opened here (the container
#     cannot open it by its path), only after it reports READY at Low
#     integrity, checked here too;
#   - never trusts: replies are fixed binary frames, checked here; this
#     process writes (encrypts) the audio itself and enforces the length and
#     disk limits.
# If the container cannot be set up the import is refused (SandboxUnavailable,
# with a message pointing to IT); it never falls back to a decoder outside the
# container (the owner's decision of 24 October 2026, #299).
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


# Shown when the decoder's container cannot be set up on this computer (#299).
# The IT guide's section is docs/IT_DEPLOYMENT.md, "Protected decoder".
SANDBOX_REFUSED = ("This recording can't be opened safely on this computer: Unminute's "
                   "protected decoder could not start. Ask your IT team to check "
                   "\"Protected decoder\" in Unminute's IT guide.")


class SandboxUnavailable(DecodeError):
    """The decoder's AppContainer could not be set up, so the recording was
    not given to any decoder. step (appcontainer.STEPS) and winerror (a
    number) go to the log for IT; neither holds a path or text (P3)."""

    def __init__(self, step, winerror=0):
        super().__init__(SANDBOX_REFUSED)
        self.step, self.winerror = step, int(winerror or 0)


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


def _deadline_s(src=None, size=None):
    """The longest a whole decode may take: 15 minutes plus 1 second per MB.
    Decoding runs many times faster than real time, so only a stuck or
    hostile decoder gets near this."""
    if size is None:
        try:
            size = os.path.getsize(src)
        except OSError:
            size = 0
    return 900 + size / (1 << 20)


WAV_HEADER_BYTES = 44
MAX_WAV_DATA = 0xFFFFFFFF - 36          # a WAV header's sizes are 32-bit


class WavFeed:
    """A recording streamed to the decoder over its stdin as one WAV (#111
    C3b): a 44-byte header (PCM, mono, 16-bit, exact sizes), then the audio's
    blocks. audio: anything with rate, frames and blocks() yielding
    little-endian int16 bytes (recorder.RecordingAudio). The feeder thread
    hashes the PCM as it goes (the recording's identity, sha256) and counts
    it, so a stream that does not match its header is refused."""

    def __init__(self, audio):
        import struct
        self.audio = audio
        self.rate = int(audio.rate)
        self.data_bytes = int(audio.frames) * 2
        if self.data_bytes > MAX_WAV_DATA:
            raise DecodeError(MESSAGES[E_TOO_LONG])
        self.size = WAV_HEADER_BYTES + self.data_bytes
        self.header = (b"RIFF" + struct.pack("<I", 36 + self.data_bytes) + b"WAVE"
                       + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, self.rate,
                                                self.rate * 2, 2, 16)
                       + b"data" + struct.pack("<I", self.data_bytes))
        self.sha256 = None
        self.sent = 0
        self.error = None

    def write_to(self, pipe):
        """Write the whole WAV to pipe and close it (the decoder sees the
        end). Runs on the feeder thread; any exception is kept in error."""
        h = hashlib.sha256()
        try:
            pipe.write(self.header)
            for block in self.audio.blocks():
                if self.sent + len(block) > self.data_bytes:
                    raise DecodeError(SAVE_FAILED)        # more audio than the header says
                h.update(block)
                pipe.write(block)
                self.sent += len(block)
            if self.sent != self.data_bytes:
                raise DecodeError(SAVE_FAILED)
            pipe.flush()
            self.sha256 = h.hexdigest()
        except BaseException as exc:          # raised again by decode(), on its thread
            self.error = exc
        finally:
            try:
                pipe.close()
            except OSError:
                pass


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


def _runtime():
    """The decoder's interpreter and import path: this process's own (the
    installed runtime and app folders, which the container can read). Tests
    point it at a scratch copy the container can read (tools/decoder_runtime.py)."""
    return _python(), list(sys.path)


# -X disable-remote-debug: no other process can make the decoder run code
# through Python 3.14's debugger interface (PEP 768; older Pythons ignore it).
DECODER_ARGS = ("-I", "-B", "-X", "disable-remote-debug", "-c")
_THREADS = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


def _launch(exe, source):
    """Start the decoder: (process, job, request line 2 for a file or None).
    source: the open recording (a binary file), or None for the stdin form.
    On Windows it starts in the AppContainer, in its job and with its token
    checked before it runs; any failure raises SandboxUnavailable and nothing
    else is started. Elsewhere (only the Linux test job: the app runs on
    Windows) it is a plain child process given the file descriptor."""
    from workspace import decode_child
    argv = [exe, *DECODER_ARGS, decode_child.BOOT]
    if sys.platform != "win32":
        import subprocess
        fds = (source.fileno(),) if source is not None else ()
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=dict(os.environ, **_THREADS),
                                pass_fds=fds)
        return proc, None, ({"handle": fds[0]} if fds else None)
    import msvcrt

    from workspace import appcontainer, jobs
    held = []

    def contain(pid):
        try:
            held.append(jobs.contain(pid, DECODER_MEMORY))
        except jobs.JobError as exc:
            raise appcontainer.SandboxError("contain", exc.errno or 0) from exc
    handles = [msvcrt.get_osfhandle(source.fileno())] if source is not None else []
    try:
        proc = appcontainer.start(argv, contain, handles=handles,
                                  env=appcontainer.environment(_THREADS))
    except (appcontainer.SandboxError, OSError) as exc:
        for job in held:
            job.close()
        if isinstance(exc, appcontainer.SandboxError):
            raise SandboxUnavailable(exc.step, exc.code) from exc
        raise SandboxUnavailable("start", getattr(exc, "winerror", None) or 0) from exc
    return proc, held[0], ({"handle": handles[0]} if handles else None)


def _await_ready(proc, frames, path):
    """Send the import path, then wait for READY at Low integrity (checked
    from outside on Windows too). Anything else means the container could not
    run the decoder: SandboxUnavailable, before the recording is given."""
    import json
    import struct

    from workspace import decode_child
    global last_decoder_integrity
    try:
        proc.stdin.write((json.dumps(path) + "\n").encode("utf-8"))
        proc.stdin.flush()
        kind, payload = frames.next(READY_TIMEOUT_S)
    except (OSError, DecodeError) as exc:          # it ended, or never answered
        raise SandboxUnavailable("ready") from exc
    if kind != b"R" or len(payload) != 4:
        raise SandboxUnavailable("ready")
    last_decoder_integrity = struct.unpack("<I", payload)[0]
    if sys.platform == "win32":
        # Checked here too, not only taken from the decoder's own report.
        try:
            actual = decode_child.process_integrity(proc.pid)
        except OSError as exc:
            raise SandboxUnavailable("check", getattr(exc, "errno", 0) or 0) from exc
        last_decoder_integrity = max(last_decoder_integrity, actual)
        if last_decoder_integrity > LOW_INTEGRITY:
            raise SandboxUnavailable("check")      # not sandboxed: never give it the file


def decode(src, out, folder=None, feed=None):
    """Decode `src` in the contained decoder process, writing raw float32
    16 kHz mono to `out` (anything with write(bytes)). Returns the number of
    samples. folder: where the output goes, for the free-space checks.
    feed: a WavFeed instead of src (#111 C3b): the decoder reads the WAV
    from its stdin, written by a feeder thread (never this thread: it reads
    the decoder's output, and the two pipes would deadlock). Raises
    DecodeError with a safe message (SandboxUnavailable when the decoder's
    container could not be set up: no decoder then got the recording), or
    the feed's own error (for example AudioCryptError when a chunk changed
    after it was checked). The app writes only through decode_to_file
    (encrypted); tools that compare engines on public clips pass their own
    plain writer."""
    import json
    import subprocess
    import threading
    import time
    source = None
    if feed is None:
        try:
            source = open(src, "rb")           # this process opens it; the decoder cannot
            size = os.fstat(source.fileno()).st_size
        except OSError as exc:
            if source is not None:
                source.close()
            raise DecodeError(_GENERIC) from exc
    else:
        size = feed.size
    dense_limit = max_samples_for(size)
    proc, job, frames, feeder = None, None, None, None
    try:
        exe, path = _runtime()
        proc, job, request = _launch(exe, source)
        frames = _Frames(proc.stdout, deadline=time.monotonic() + _deadline_s(size=size))
        _await_ready(proc, frames, path)
        if feed is not None:
            request = {"stdin": "wav", "bytes": feed.size}
        proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        proc.stdin.flush()
        if feed is not None:
            feeder = threading.Thread(target=feed.write_to, args=(proc.stdin,), daemon=True,
                                      name="decoder-feed")
            feeder.start()
        samples = _collect(lambda: frames.next(IDLE_TIMEOUT_S), out, folder=folder,
                           dense_limit=dense_limit)
        if feed is not None:
            # The decoder said it is done, so it read to the end of the stream.
            feeder.join(IDLE_TIMEOUT_S)
            if feeder.is_alive():
                raise DecodeError(TOO_SLOW)
            if feed.error is not None:
                raise feed.error
        if samples == 0:
            raise DecodeError("The file contains no audio.")
        return samples
    except OSError as exc:          # broken pipe and the like: the decoder went away
        raise DecodeError("The recording could not be decoded (the decoder stopped). It may "
                          "be damaged.") from exc
    except DecodeError as exc:
        # The feed's own failure (a chunk that changed after it was checked)
        # explains a decoder that stopped early better than the decoder can.
        if feed is not None and feed.error is not None and not isinstance(exc, SandboxUnavailable) \
                and not isinstance(feed.error, (OSError, DecodeError)):
            raise feed.error from None
        raise
    finally:
        # Stop the decoder first: the reader thread may be blocked reading its
        # output, and closing the pipe would wait for that read. Once the
        # process is gone the read ends and the pipes close at once.
        if frames is not None:
            frames.stop()
        if job is not None:
            job.close()                          # ends the decoder if it is still running
        if proc is not None:
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
        if feeder is not None:
            feeder.join(10)                      # its write fails once the process is gone
        if proc is not None:
            for pipe in (proc.stdin, proc.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
            if hasattr(proc, "close"):
                proc.close()                     # the container process's handle
        if source is not None:
            source.close()


def decode_to_file(src, dest, key, context, feed=None):
    """Decode `src` (or a WavFeed, feed) in the contained decoder process to
    `dest`, encrypted (audiocrypt, float32, mono, 16 kHz) under `key` with
    `context`. Written as `dest`'s ".partial" file, finished (last block
    sealed, flushed to disk), then renamed. Returns (samples, the file's
    random id). Raises DecodeError with a safe message; a failed decode
    leaves no file."""
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
        samples = decode(src, out, folder=tmp.parent, feed=feed)
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


# Playback level (#108, the owner's decision 2(b), 25 Oct 2026): the app
# plays a meeting at a standard level for listening, whatever level it was
# recorded or imported at. The level is measured from a sample of the
# decoded audio (one encrypted block every PLAYBACK_SAMPLE_EVERY blocks, all
# of it for a short meeting), in windows of PLAYBACK_WINDOW frames, gated
# like EBU R128 (windows below -70 dB are left out, then those more than
# 10 dB below the mean of the rest), without its frequency weighting. The
# gain to PLAYBACK_TARGET_DB is capped (raised at most PLAYBACK_MAX_GAIN_DB,
# lowered at most PLAYBACK_MIN_GAIN_DB) and each clip goes through the mix's
# soft limiter (workspace/mixdown.soft_limit) so no sample goes past full
# scale. Applied to clips as they are played (Workspace.clip): the saved
# audio never changes.
PLAYBACK_TARGET_DB = -18.0           # the level meetings are played at (gated, dB of full scale)
PLAYBACK_MAX_GAIN_DB = 15.0
PLAYBACK_MIN_GAIN_DB = -20.0
PLAYBACK_PIECE = 16384               # one encrypted block (audiocrypt.FRAMES_PER_BLOCK)
PLAYBACK_SAMPLE_EVERY = 30           # blocks: about 1 s read in every 30 s
PLAYBACK_WHOLE_BLOCKS = 64           # a meeting this short (about a minute) is read whole
PLAYBACK_WINDOW = 4096               # frames per loudness window (0.256 s)
_ABSOLUTE_GATE_DB = -70.0           # then windows 10 dB below the mean of the rest (mixdown)


def playback_gain(reader):
    """The linear gain that plays this audio at PLAYBACK_TARGET_DB (1.0 for
    silence). reader: a meeting's decoded audio (AudioReader: len, read)."""
    frames = len(reader)
    blocks = -(-frames // PLAYBACK_PIECE)
    step = 1 if blocks <= PLAYBACK_WHOLE_BLOCKS else PLAYBACK_SAMPLE_EVERY
    powers = []
    for b in range(0, blocks, step):
        x = reader.read(b * PLAYBACK_PIECE, (b + 1) * PLAYBACK_PIECE)
        n = len(x) - len(x) % PLAYBACK_WINDOW
        if n:
            w = x[:n].astype(np.float64).reshape(-1, PLAYBACK_WINDOW)
            powers.append(np.mean(w * w, axis=1))
    if not powers:
        return 1.0
    from workspace.mixdown import gated_level_db    # the mix's measure; loaded when first played
    level = gated_level_db(np.concatenate(powers), _ABSOLUTE_GATE_DB)
    if level is None:
        return 1.0
    gain_db = min(PLAYBACK_MAX_GAIN_DB, max(PLAYBACK_MIN_GAIN_DB, PLAYBACK_TARGET_DB - level))
    return float(10 ** (gain_db / 20))


def apply_playback_gain(samples, gain):
    """A clip (float32, full scale 1.0) at the meeting's playback gain,
    through the soft limiter. A new array; the same one when gain is 1."""
    if gain == 1.0:
        return samples
    from workspace.mixdown import soft_limit       # numpy only; loaded when first played
    return soft_limit(samples * np.float64(gain), full=1.0).astype(np.float32)


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
        _forget_new_meeting(store, mid, folder)
        raise
    return mid, sid


def _forget_new_meeting(store, mid, folder):
    """A failed import leaves nothing behind. Its keys go first (with the
    write-ahead log emptied, store.delete_new_meeting), then its folder, so
    a crash in between leaves only audio that can never be opened (#111
    C3c). A meeting kept by a hold (#193) keeps its keys; its folder is
    still removed, as before."""
    import shutil
    try:
        store.delete_new_meeting(mid)
    except Exception:
        pass
    shutil.rmtree(folder, ignore_errors=True)


def import_stream(store, media_root, audio, title, recording):
    """Create a meeting from a microphone recording (#30, #111 C3b) with no
    original file: audio (recorder.RecordingAudio) is streamed to the
    contained decoder over its stdin as one WAV, decoded and saved encrypted
    as for any import, and authenticated in full once. recording:
    (recording_id, info JSON text) for add_source, whose transaction also
    stores the sealed recording_sources row and deletes the recording's key
    (the chunks left behind can then never be opened). The PCM's SHA-256 is
    the source's identity (fingerprinted by the store). A failed import
    leaves nothing behind, as import_recording. Returns (meeting_id, source_id)."""
    feed = WavFeed(audio)
    mid = store.create_meeting(title or "Untitled meeting")
    folder = meeting_folder(media_root, mid)
    try:
        folder.mkdir(parents=True, exist_ok=False)
        samples, file_id = decode_to_file(None, folder / AUDIO_FILE,
                                          store.meeting_key(mid, "audio"), audio_context(mid),
                                          feed=feed)
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
        _write_activity(store, folder, mid, audio)
        sid = store.add_source(mid, original_path=None, original_sha256=feed.sha256,
                               original_size=feed.data_bytes, original_mtime_ns=0,
                               audio_id=file_id.hex(), samples=samples,
                               sample_rate=SAMPLE_RATE, recording=recording)
    except BaseException:
        _forget_new_meeting(store, mid, folder)
        raise
    return mid, sid


def _write_activity(store, folder, mid, audio):
    """A two-track recording's track-activity file (workspace/activity.py,
    #108), sealed under the meeting's audio key in its folder, before the
    import commits: a failed import removes it with the folder. It only
    feeds an optional suggestion, so it never fails the import: without it
    no suggestion is offered. A two-track recording left without the file
    is recorded in the app log as `activity_unwritten` (#461), with the
    stage ("tracking": the mix gave no levels, or asking it for them
    raised; "write": they could not be sealed or written) and the
    exception's type name only (P3), so support can tell it from a call
    where no one qualified."""
    get = getattr(audio, "activity", None)
    if get is None:
        return
    try:
        result = get()
    except Exception as exc:
        _activity_unwritten(store, "tracking", exc)
        return
    if result is None:
        if getattr(audio, "two_track", False):
            _activity_unwritten(store, "tracking", None)
        return
    try:
        from workspace import activity
        activity.write(folder, store.meeting_key(mid, "audio"), mid, result)
    except Exception as exc:
        _activity_unwritten(store, "write", exc)


def _activity_unwritten(store, stage, exc):
    """The content-free record of a track-activity file not written: the
    stage and the exception's type name, never its message (P3)."""
    from workspace import diag
    log = getattr(store, "log", None) or diag.current()
    if log is None:
        return
    fields = {"stage": stage}
    if exc is not None:
        fields["error"] = type(exc).__qualname__
    try:
        log.write("activity_unwritten", **fields)
    except Exception:
        pass                                   # diagnostics never fail the import


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
