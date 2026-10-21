# tools/worker_check.py
# Real-engine check for isolated workers (#15): transcribe a non-sensitive
# recording through a worker process in Quiet and Faster modes, and report
# priority, threads, timing, idle unloading and agreement with an in-process
# whole-recording transcription. Measurements, not promises.
#
#   python tools/worker_check.py <recording> <model cache dir> [model]

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from workspace import chunking, media, resources, runner  # noqa: E402
from workspace.store import Store  # noqa: E402
from workspace.worker import WorkerEngine, WorkerHandle  # noqa: E402


def run(root, src, handle, model, mode):
    store = Store(root / f"{mode}.db")
    mid, sid = media.import_recording(store, root / f"media-{mode}", src, "Check")
    reader = media.open_audio(store, root / f"media-{mode}", mid)
    plan = chunking.plan_chunks(reader, len(reader))
    vid = store.ensure_version(mid, sid, engine="worker", model=model, model_revision="pinned",
                               precision="int8", config={"mode": mode}, chunk_plan=plan)
    threads = resources.worker_threads(mode)
    eng = WorkerEngine(handle, model, threads)
    t = time.perf_counter()
    result = runner.run_version(store, mid, vid, reader, eng)
    elapsed = time.perf_counter() - t
    rows = store.transcript(vid)
    words_ok = all(r["words_json"] for r in rows)
    att = store.attempts(vid, 0)[-1]
    text = " ".join(r["text"] for r in rows)
    reader.close()
    store.close()
    return result, elapsed, threads, words_ok, (att["backend"], att["precision"]), text


def main():
    src, cache = Path(sys.argv[1]), sys.argv[2]
    model = sys.argv[3] if len(sys.argv) > 3 else "parakeet-tdt-0.6b-v2"
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        handle = WorkerHandle(cache, quiet=True, idle_unload_s=3)
        try:
            results = {}
            for mode in (resources.QUIET, resources.FASTER):
                r = run(root, src, handle, model, mode)
                results[mode] = r
                pid = handle.pid
                prio = resources.get_priority(pid) if pid else None
                print(f"{mode}: {r[0]}, {r[1]:.0f}s, threads={r[2]}, worker pid {pid} "
                      f"priority=0x{prio or 0:x} (below normal=0x4000), words kept={r[3]}, "
                      f"attempt backend/precision={r[4]}")
                ok &= r[0] == "done" and r[3] and prio == resources.BELOW_NORMAL_PRIORITY_CLASS
            time.sleep(6)
            print(f"after idle: worker pid {handle.pid} (None = unloaded)")
            ok &= handle.pid is None
        finally:
            handle.close()

    # Agreement with an unchunked, in-process transcription.
    from compare_engines import edit_distance, normalise
    from faster_whisper.audio import decode_audio

    from workspace import asr_engines
    eng = asr_engines.ParakeetEngine(model, cache, resources.worker_threads(), decode_audio).load()
    segs, dur = eng.transcribe(decode_audio(str(src), sampling_rate=16000))
    whole = normalise(" ".join(s.text for s in segs))
    for mode, r in results.items():
        d = edit_distance(whole, normalise(r[5]))
        print(f"{mode} vs whole recording: {d} word edits of {len(whole)}")
        ok &= d <= max(2, len(whole) // 200)
    print(f"audio {dur:.0f}s; cpu_count={os.cpu_count()}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
