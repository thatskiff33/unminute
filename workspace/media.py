# workspace/media.py
# Import a recording into a meeting's media folder with bounded memory:
#   - the original file is hashed (its identity) but never modified;
#   - audio is decoded once, streamed through PyAV and a resampler to 16 kHz
#     mono float32, and written block by block to <media>/audio.f32 while
#     hashing it;
#   - engines then read slices of that file through a memory map.
# Nothing in a media path comes from the source file's name.

import hashlib
import os
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
AUDIO_FILE = "audio.f32"


def sha256_file(path, chunk=8 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


# Recordings can come from clients or shared folders, so the file is treated
# as hostile (#111 slice A3, threats T2 to T4 in docs/SECURITY_MODEL.md):
#   - it is decoded in a short-lived child process inside a Windows job
#     (workspace/jobs.py: memory cap, no child processes, no clipboard, killed
#     with the app), never in the process that owns the database;
#   - FFmpeg reads it through a Python file object and may only use its
#     demuxers for the supported recording types and the local file protocol,
#     whatever the content claims to be (no playlists, streams or scripts);
#   - decoding stops at MAX_DECODED_S of audio and keeps DISK_RESERVE free.
# Container demuxer names (FFmpeg); covers AUDIO_EXTENSIONS in workspace/app.py.
DEMUXERS = "wav,mp3,mov,mp4,m4a,matroska,webm,flac,ogg,aac,asf,avi"
OPEN_OPTIONS = {"protocol_whitelist": "file", "format_whitelist": DEMUXERS}
MAX_DECODED_S = 12 * 3600          # longer than any meeting; a "bomb" stops here
DISK_RESERVE = 1 << 30             # never fill the disk: keep 1 GB free
DECODER_MEMORY = 2 << 30           # job memory limit for the decode process
_CHECK_EVERY = 64 << 20            # bytes written between free-space checks


class DecodeError(ValueError):
    """The recording could not be decoded (message is safe to show)."""


def _free_bytes(folder):
    import shutil
    return shutil.disk_usage(folder).free


def _decode(src, tmp, max_samples=MAX_DECODED_S * SAMPLE_RATE, reserve=DISK_RESERVE):
    """The decode itself: runs inside the contained child process.
    Returns (samples, sha256 of the written bytes)."""
    import av
    h = hashlib.sha256()
    samples = 0
    since_check = 0
    tmp = Path(tmp)
    try:
        with open(src, "rb") as fh, av.open(fh, options=OPEN_OPTIONS) as container, \
                open(tmp, "wb") as out:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise DecodeError("The file has no audio track.")
            if container.duration and container.duration / 1e6 > MAX_DECODED_S + 60:
                raise DecodeError("The recording is longer than 12 hours.")
            expected = (container.duration or 0) / 1e6 * SAMPLE_RATE * 4
            if _free_bytes(tmp.parent) < min(expected, max_samples * 4) + reserve:
                raise DecodeError("There is not enough free disk space to import this "
                                  "recording.")
            resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)

            def write(frames):
                nonlocal samples, since_check
                for fr in frames:
                    data = np.ascontiguousarray(fr.to_ndarray().reshape(-1),
                                                dtype="<f4").tobytes()
                    samples += len(data) // 4
                    if samples > max_samples:
                        raise DecodeError("The recording is longer than 12 hours.")
                    out.write(data)
                    h.update(data)
                    since_check += len(data)
                    if since_check >= _CHECK_EVERY:
                        since_check = 0
                        if _free_bytes(tmp.parent) < reserve:
                            raise DecodeError("There is not enough free disk space to "
                                              "import this recording.")

            for frame in container.decode(stream):
                write(resampler.resample(frame))
            write(resampler.resample(None))       # flush
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if samples == 0:
        tmp.unlink(missing_ok=True)
        raise DecodeError("The file contains no audio.")
    return samples, h.hexdigest()


def _decoder_main(conn):
    """Entry point of the decode process. It installs the offline guard and
    waits for its one job only after the parent has put it in a Windows job,
    so it never touches the recording unconstrained."""
    import sys
    if sys.stdout is None or sys.stderr is None:    # pythonw: no console
        sys.stdout = sys.stderr = open(os.devnull, "w")
    import offline_guard
    offline_guard.disable_telemetry()
    offline_guard.install()
    try:
        src, tmp = conn.recv()
    except (EOFError, OSError):
        return
    try:
        samples, digest = _decode(src, tmp)
        conn.send(("ok", samples, digest))
    except DecodeError as exc:
        conn.send(("error", str(exc)))
    except MemoryError:
        conn.send(("error", "The recording needs more memory to decode than this app "
                            "allows. It may be damaged."))
    except Exception:            # PyAV/FFmpeg errors carry paths: never sent back
        conn.send(("error", "That recording could not be imported (unsupported or "
                            "damaged file)."))


def _decode_timeout(src):
    """Generous: decoding is far faster than real time. Only a hung decoder
    reaches this (10 minutes plus 1 second per 2 MB of input)."""
    try:
        size = os.path.getsize(src)
    except OSError:
        size = 0
    return 600 + size / (2 << 20)


def decode_to_file(src, dest):
    """Decode `src` to raw float32 16 kHz mono at `dest` in a contained child
    process. Returns (samples, sha256 of the written bytes). Raises
    DecodeError with a safe message."""
    import multiprocessing as mp

    from workspace import jobs
    tmp = Path(dest).with_suffix(".partial")
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_decoder_main, args=(child,), daemon=True,
                       name="unminute-decode")
    proc.start()
    child.close()
    job = None
    reply = ("error", "")
    try:
        try:
            job = jobs.contain(proc.pid, DECODER_MEMORY)
        except jobs.JobError as exc:
            raise DecodeError("The recording could not be decoded safely on this computer "
                              "(the decoder could not be contained).") from exc
        parent.send((str(src), str(tmp)))
        if not parent.poll(_decode_timeout(src)):
            raise DecodeError("Decoding the recording took too long. It may be damaged.")
        try:
            reply = parent.recv()
        except (EOFError, OSError) as exc:
            raise DecodeError("The recording could not be decoded (the decoder stopped). "
                              "It may be damaged.") from exc
    finally:
        parent.close()
        if job is not None:
            job.close()                      # kills the process if it is still running
        proc.join(10)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        if reply[0] != "ok":
            tmp.unlink(missing_ok=True)      # whatever a stopped decoder left
    if reply[0] != "ok":
        tmp.unlink(missing_ok=True)
        raise DecodeError(reply[1])
    _, samples, digest = reply
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
