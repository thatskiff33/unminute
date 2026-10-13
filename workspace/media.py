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


def decode_to_file(src, dest):
    """Stream-decode `src` to raw float32 16 kHz mono at `dest`.
    Returns (samples, sha256 of the written bytes)."""
    import av
    h = hashlib.sha256()
    samples = 0
    tmp = Path(dest).with_suffix(".partial")
    with av.open(str(src)) as container, open(tmp, "wb") as out:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("The file has no audio track.")
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)

        def write(frames):
            nonlocal samples
            for fr in frames:
                data = np.ascontiguousarray(fr.to_ndarray().reshape(-1), dtype="<f4").tobytes()
                out.write(data)
                h.update(data)
                samples += len(data) // 4

        for frame in container.decode(stream):
            write(resampler.resample(frame))
        write(resampler.resample(None))       # flush
        out.flush()
        os.fsync(out.fileno())
    if samples == 0:
        tmp.unlink(missing_ok=True)
        raise ValueError("The file contains no audio.")
    os.replace(tmp, dest)
    return samples, h.hexdigest()


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
