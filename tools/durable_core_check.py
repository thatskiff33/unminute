# tools/durable_core_check.py
# Real-engine check for the durable core (#14): import a non-sensitive
# recording into a temporary workspace, transcribe it in chunks with Parakeet,
# crash deliberately before one chunk's commit, resume from a new database
# connection, and compare with an uninterrupted run. Exits non-zero on any
# difference.
#
#   python tools/durable_core_check.py <recording> <model cache dir> [crash chunk]

import ctypes
import ctypes.wintypes as wt
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asr_engines  # noqa: E402
from workspace import chunking, media, runner  # noqa: E402
from workspace.store import Store  # noqa: E402


class Crash(Exception):
    pass


def peak_gb():
    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t)] + \
                   [(f"f{i}", ctypes.c_size_t) for i in range(7)]
    c = PMC()
    c.cb = ctypes.sizeof(c)
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
    k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return round(c.PeakWorkingSetSize / 1e9, 2)


def transcribe(root, src, engine, crash_at=None):
    store = Store(root / "workspace.db")
    mid, sid = media.import_recording(store, root / "media", src, "Check")
    reader = media.AudioReader(media.audio_path(root / "media", mid))
    plan = chunking.plan_chunks(reader, len(reader))
    vid = store.ensure_version(mid, sid, engine="parakeet", model=engine.label,
                               model_revision=engine.spec.revision, precision="int8",
                               config={"vad_max_s": 20}, chunk_plan=plan)
    calls = {"n": 0}
    real = engine.transcribe

    def counting(audio):
        calls["n"] += 1
        return real(audio)
    engine.transcribe = counting
    try:
        if crash_at is not None:
            def boom(idx):
                if idx == crash_at:
                    raise Crash()
            try:
                runner.run_version(store, mid, vid, reader, engine,
                                   hooks={"before_commit": boom, "reraise": True})
            except Crash:
                pass
            states = [c["status"] for c in store.chunks(vid)]
            print(f"  crashed before committing chunk {crash_at}: "
                  f"{states.count('done')} done, {states.count('pending')} pending")
            store.close()
            store = Store(root / "workspace.db")         # restart
        result = runner.run_version(store, mid, vid, reader, engine)
        text = [r["text"] for r in store.transcript(vid)]
        return result, text, len(plan), calls["n"]
    finally:
        engine.transcribe = real
        reader.close()
        store.close()


def main():
    src, cache = Path(sys.argv[1]), sys.argv[2]
    crash_at = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    from faster_whisper.audio import decode_audio
    engine = asr_engines.ParakeetEngine("parakeet-tdt-0.6b-v2", cache, 4, decode_audio).load()
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        t = time.perf_counter()
        r1, straight, n_chunks, calls1 = transcribe(Path(a), src, engine)
        print(f"uninterrupted: {r1}, {n_chunks} chunks, {calls1} engine calls, "
              f"{len(straight)} segments, {time.perf_counter() - t:.0f}s")
        t = time.perf_counter()
        r2, resumed, _, calls2 = transcribe(Path(b), src, engine, crash_at)
        print(f"crash + resume: {r2}, {calls2} engine calls (expected {n_chunks + 1}), "
              f"{len(resumed)} segments, {time.perf_counter() - t:.0f}s")
    same = straight == resumed
    print(f"identical transcript: {same}; peak RAM {peak_gb()} GB")
    if not same:
        import difflib
        print("\n".join(list(difflib.unified_diff(straight, resumed, lineterm=""))[:40]))
    ok = same and r1 == r2 == "done" and calls2 == n_chunks + 1
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
