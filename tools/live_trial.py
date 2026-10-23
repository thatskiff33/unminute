# tools/live_trial.py
# Live-draft trial harness (#104): the app's live-draft path, offline, on a
# public recording fed at real time, measured per phrase cut length.
#
#   run:       feed a recording to the app's own DraftSession and draft worker
#              process (workspace.drafts, worker.draft_main) in the recorder's
#              0.1 s blocks at real time, once per cut length (default 6, 8 and
#              10 s), then run the full pass (Parakeet v2, as compare_engines
#              does) on the same audio. Reports, per cut:
#                - delay: when the phrase holding each correctly recognised
#                  word reached the draft session (checked every POLL_S; the
#                  page's own polling is not counted), minus when it finished
#                  being spoken (the AMI word times with --ami-words, otherwise
#                  the full pass's word times), median and 90th percentile, in
#                  seconds;
#                - agreement: 100% minus the draft's word error rate measured
#                  against the full pass (how close the draft is to the text
#                  that replaces it);
#                - the draft's word error rate against the AMI reference (with
#                  --ami-words), and the full pass's;
#                - the draft worker's processor time while the audio was fed,
#                  as % of one core, its peak memory, and phrases, skipped and
#                  lost seconds.
#   manifest:  a compare_engines manifest for AMI recordings, with each
#              meeting's reference built from its word files (every speaker's
#              words in time order), for compare_engines.py run --draft-cut.
#
#   python tools/live_trial.py run --cache <models> --ami-words <words> \
#       --start 0 --seconds 450 --out trial.json ES2004a.Mix-Headset.wav
#
# The cut length is the only thing changed from the app: each draft worker
# runs worker.draft_main with drafts.MAX_PHRASE_S set to the cut
# (compare_engines.draft_cut). Everything else is the shipped code: the tap and
# feeder, the worker's start-up (offline, below-normal priority, 2 threads),
# the resampler, Silero scoring, the phrase splitter and Parakeet v2.
#
# Public or synthetic audio only (AGENTS.md P1, P2): the AMI Meeting Corpus is
# CC BY 4.0. The results file and the printed table hold counts, times and
# rates only, never transcript text; the text stays in this process's memory.
# --cache is required, so the installed app's model folder is never used by
# default. The tool sets itself to below-normal priority. Run heavy trials one
# at a time on an otherwise idle computer; --speed above 1 is for tests only
# (its delays and processor figures mean nothing).

import argparse
import functools
import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_engines import draft_cut, normalise  # noqa: E402

BLOCK_S = 0.1           # the recorder's block (workspace.recorder.BLOCK_S)
POLL_S = 0.02           # how often new phrases are looked for (their times are this close)
DEFAULT_CUTS = (6.0, 8.0, 10.0)


# --- reference words -------------------------------------------------------------

def ami_words(folder, meeting):
    """Every speaker's words of one AMI meeting, from its
    <meeting>.<speaker>.words.xml files, in time order: [(start, end, word)].
    Punctuation, untimed words and non-word events are left out."""
    import xml.etree.ElementTree as ET
    files = sorted(Path(folder).glob(f"{meeting}.*.words.xml"))
    if not files:
        sys.exit(f"No AMI word files for {meeting}.")
    words = []
    for path in files:
        for el in ET.parse(path).getroot():
            if el.tag != "w" or el.get("punc") == "true":
                continue
            try:
                start, end = float(el.get("starttime")), float(el.get("endtime"))
            except (TypeError, ValueError):
                continue
            text = (el.text or "").strip()
            if text:
                words.append((start, end, text))
    words.sort(key=lambda w: (w[0], w[1]))
    return words


def in_window(words, start, seconds):
    """The words that start within [start, start + seconds), timed from start.
    seconds None: to the end."""
    end = math.inf if seconds is None else start + seconds
    return [(round(s - start, 3), round(e - start, 3), w) for s, e, w in words
            if start <= s < end]


# --- scoring ---------------------------------------------------------------------

def align(ref, hyp):
    """Word-level Levenshtein alignment: (errors, [(ref index, hyp index)] for
    each word that matches on the best path)."""
    n, m = len(ref), len(hyp)
    prev = list(range(m + 1))
    back = []                        # per ref word: 0 diagonal, 1 deletion, 2 insertion
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        steps = bytearray(m + 1)
        steps[0] = 1
        r = ref[i - 1]
        for j in range(1, m + 1):
            same = r == hyp[j - 1]
            diag = prev[j - 1] + (not same)
            up, left = prev[j] + 1, cur[j - 1] + 1
            best = cur[j] = min(diag, up, left)
            # On a tie: a match, then a deletion or insertion, then a
            # substitution, so the path keeps as many matched words as it can.
            if not (same and diag == best):
                steps[j] = 1 if up == best else 2 if left == best else 0
        back.append(steps)
        prev = cur
    pairs, i, j = [], n, m
    while i > 0 and j > 0:
        step = back[i - 1][j]
        if step == 0:
            if ref[i - 1] == hyp[j - 1]:
                pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif step == 1:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return prev[m], pairs


def percentile(values, q):
    """Nearest-rank percentile, or None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q / 100 * len(ordered))) - 1]


def score(phrases, spoken):
    """Draft phrases ({start, end, text, at}: at is when the phrase appeared,
    in seconds of the recording) against spoken words [(start, end, word)]:
    word error rate, and each matched word's delay (appeared minus spoken
    end). Phrases are scored in the order they were spoken."""
    ref = [(tok, end) for _, end, text in spoken for tok in normalise(text)]
    ordered = sorted(phrases, key=lambda p: (p["start"], p["end"]))
    hyp = [(tok, p["at"]) for p in ordered for tok in normalise(p["text"])]
    errors, pairs = align([t for t, _ in ref], [t for t, _ in hyp])
    delays = [round(hyp[j][1] - ref[i][1], 3) for i, j in pairs]
    n = len(ref)
    return {"words": n, "draft_words": len(hyp), "errors": errors, "matched": len(pairs),
            "wer_pct": round(100 * errors / n, 2) if n else None, "delays": delays,
            "delay_median_s": percentile(delays, 50), "delay_p90_s": percentile(delays, 90)}


def report(runs, final_words, ref_words, config, final=None):
    """Counts, times and rates per cut (no text): the results file."""
    timing = ref_words if ref_words else final_words
    rows = []
    for run in runs:
        phrases = run["phrases"]
        row = {"cut_s": run["cut_s"], "state": run["state"], "phrases": len(phrases),
               "phrase_median_s": percentile([round(p["end"] - p["start"], 2)
                                              for p in phrases], 50),
               "load_s": run["load_s"], "skipped_s": run["skipped_s"],
               "dropped_s": run["dropped_s"], "peak_gb": run["peak_gb"],
               "cpu_s": run["cpu_s"], "wall_s": run["wall_s"],
               "cpu_pct_one_core": (round(100 * run["cpu_s"] / run["wall_s"], 1)
                                    if run["cpu_s"] is not None and run["wall_s"] else None)}
        if timing:
            got = score(phrases, timing)
            row.update(delay_median_s=got["delay_median_s"], delay_p90_s=got["delay_p90_s"],
                       delay_words=got["matched"])
        if final_words:
            got = score(phrases, final_words)
            row["agreement_pct"] = (round(100 - got["wer_pct"], 2)
                                    if got["wer_pct"] is not None else None)
        if ref_words:
            row["draft_wer_pct"] = score(phrases, ref_words)["wer_pct"]
        rows.append(row)
    out = {"config": dict(config), "timing": ("AMI word times" if ref_words else
                                              "full-pass word times" if final_words else None),
           "runs": rows}
    if final is not None or final_words:
        out["final"] = dict(final or {})
        out["final"]["words"] = sum(len(normalise(w)) for _, _, w in final_words or ())
        if ref_words and final_words:
            spoken = [{"start": s, "end": e, "text": w, "at": e} for s, e, w in final_words]
            out["final"]["wer_pct"] = score(spoken, ref_words)["wer_pct"]
    if ref_words:
        out["reference_words"] = sum(len(normalise(w)) for _, _, w in ref_words)
    return out


def table(rep):
    """The results as a Markdown table (numbers only)."""
    def cell(v, fmt="{}", suffix=""):
        return "-" if v is None else fmt.format(v) + suffix
    lines = ["| Cut | Phrases (median length) | Delay median / 90% | Agreement with full pass "
             "| Draft WER | CPU (% of one core) | Peak memory | Skipped / lost |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rep["runs"]:
        lines.append(
            f"| {r['cut_s']:g} s | {r['phrases']} ({cell(r['phrase_median_s'], '{:.1f}', ' s')})"
            f" | {cell(r.get('delay_median_s'), '{:.1f}', ' s')} / "
            f"{cell(r.get('delay_p90_s'), '{:.1f}', ' s')}"
            f" | {cell(r.get('agreement_pct'), '{:.1f}', '%')}"
            f" | {cell(r.get('draft_wer_pct'), '{:.1f}', '%')}"
            f" | {cell(r['cpu_pct_one_core'], '{:.1f}', '%')}"
            f" | {cell(r['peak_gb'], '{:.2f}', ' GB')}"
            f" | {r['skipped_s']:.1f} s / {r['dropped_s']:.1f} s |")
    final = rep.get("final")
    if final:
        lines.append("")
        lines.append(f"Full pass: {final.get('words', 0)} words, word error rate "
                     f"{cell(final.get('wer_pct'), '{:.1f}', '%')}, "
                     f"{cell(final.get('proc_s'), '{:.0f}', ' s')} to transcribe.")
    if rep.get("timing"):
        lines.append(f"Delays timed against {rep['timing']}.")
    return "\n".join(lines)


# --- one draft run ---------------------------------------------------------------

def _draft_main(cut_s, *args, **kwargs):
    """The draft worker process with this trial's cut (its own process: the
    app's constant is changed there only)."""
    from workspace import worker
    with draft_cut(cut_s):
        worker.draft_main(*args, **kwargs)


def process_cpu_s(pid):
    """A process's processor time so far (user and kernel), in seconds."""
    if pid is None:
        return None
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
        k32.CloseHandle.argtypes = [wt.HANDLE]
        h = k32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            times = [wt.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(h, *[ctypes.byref(t) for t in times]):
                return None
            kernel, user = ((t.dwHighDateTime << 32 | t.dwLowDateTime) for t in times[2:])
            return (kernel + user) / 1e7
        finally:
            k32.CloseHandle(h)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return None


def peak_memory_gb(pid):
    """A process's peak working set (Windows) or peak resident size, in GB."""
    if pid is None:
        return None
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as wt

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t)] + \
                       [(f"x{i}", ctypes.c_size_t) for i in range(6)]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(Counters), wt.DWORD]
        k32.CloseHandle.argtypes = [wt.HANDLE]
        h = k32.OpenProcess(0x1000 | 0x0010, False, pid)   # query limited, read memory
        if not h:
            return None
        try:
            c = Counters()
            c.cb = ctypes.sizeof(c)
            ok = k32.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
            return round(c.PeakWorkingSetSize / 1e9, 2) if ok else None
        finally:
            k32.CloseHandle(h)
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return round(int(line.split()[1]) * 1024 / 1e9, 2)
    except (OSError, ValueError):
        pass
    return None


def run_cut(pcm, rate, cut_s, *, cache, model=None, allow_test=False, threads=None,
            speed=1.0, quiet_s=10.0, progress=None):
    """Feed 16-bit audio at `rate` to a fresh DraftSession whose worker cuts
    at cut_s, in the recorder's blocks at real time (times speed), as the
    recorder's writer thread does, ending with a pause's flush. Returns the
    phrases (with the text, in memory only, and `at`: when each appeared, in
    seconds of the recording) and the session's figures."""
    import numpy as np

    from workspace import drafts
    with draft_cut(cut_s):                   # refuses a bad cut here, in this process
        pass
    session = drafts.DraftSession(
        "trial", rate, str(cache), model=model or drafts.DRAFT_MODEL,
        threads=threads or drafts.DRAFT_THREADS, allow_test_engines=allow_test,
        target=functools.partial(_draft_main, float(cut_s)))
    phrases, seen = [], 0
    figures = {"cut_s": float(cut_s), "load_s": None, "cpu_s": None, "wall_s": None,
               "peak_gb": None, "skipped_s": 0.0, "dropped_s": 0.0}
    try:
        session.start()
        loading = time.monotonic()
        while session.state == drafts.STARTING and \
                time.monotonic() - loading < drafts.LOAD_TIMEOUT_S:
            time.sleep(0.05)
        if session.state != drafts.RUNNING:
            figures.update(state=session.state, phrases=[])
            return figures
        pid = session.pid
        block = max(1, round(rate * BLOCK_S))
        pcm = np.asarray(pcm, dtype=np.int16)
        fed = threading.Event()
        cpu_start = process_cpu_s(pid)
        t0 = time.perf_counter()
        fed_at = {}

        def feed():
            # As the writer thread: each block once it has been "captured".
            for i in range(0, len(pcm), block):
                chunk = pcm[i:i + block]
                wait = t0 + (i + len(chunk)) / rate / speed - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
                session.tap(i, chunk)
            session.tap(len(pcm), None)          # a pause: the last phrase ends
            fed_at["wall"] = time.perf_counter() - t0
            fed_at["cpu"] = process_cpu_s(pid)
            fed.set()

        feeder = threading.Thread(target=feed, name="trial-feed", daemon=True)
        feeder.start()
        last_new = time.perf_counter()
        total_s, next_note = len(pcm) / rate, 60.0
        while True:
            snap = session.snapshot(seen, limit=200)
            now = time.perf_counter()
            for item in snap["phrases"]:
                if item.get("kind") == "phrase":
                    phrases.append({"start": item["start"], "end": item["end"],
                                    "text": item["text"], "at": round((now - t0) * speed, 3)})
            if snap["phrases"]:
                seen = snap["since"] + len(snap["phrases"])
                last_new = now
            if snap["state"] != drafts.RUNNING:
                break
            if fed.is_set() and now - max(last_new, t0 + fed_at["wall"]) > quiet_s:
                break
            if progress and now - t0 >= next_note:
                next_note += 60.0
                progress(f"  {min(total_s, (now - t0) * speed):.0f} of {total_s:.0f} s fed, "
                         f"{len(phrases)} phrases")
            time.sleep(POLL_S)
        feeder.join(5)
        figures["peak_gb"] = peak_memory_gb(pid)
        if fed_at.get("cpu") is not None and cpu_start is not None:
            figures["cpu_s"] = round(fed_at["cpu"] - cpu_start, 2)
            figures["wall_s"] = round(fed_at["wall"], 2)
        figures.update(state=snap["state"], load_s=session.load_s,
                       skipped_s=round(session.skipped_s, 1),
                       dropped_s=round(session.dropped_s, 1))
    finally:
        session.stop()
    figures["phrases"] = phrases
    return figures


# --- run -------------------------------------------------------------------------

def _decode(wav):
    """16 kHz mono float32, through the app's contained decoder."""
    import io
    import tempfile

    import numpy as np

    from workspace import media
    raw = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="live-trial-") as tmp:
        samples = media.decode(str(wav), raw, folder=Path(tmp))
    out = np.frombuffer(raw.getbuffer(), dtype="<f4").copy()
    if len(out) != samples:
        sys.exit(f"The decoded audio is {len(out)} samples, not {samples}.")
    return out


def to_device_rate(audio, rate, piece=10_000_000):
    """16 kHz float32 audio as 16-bit audio at a microphone's rate (linear
    interpolation, in pieces to bound memory): the draft resamples it back,
    as it does a real microphone's."""
    import numpy as np
    if rate == 16000:
        scaled = np.round(audio * 32768.0)
        return np.clip(scaled, -32768, 32767).astype(np.int16)
    count = int(len(audio) * rate / 16000)
    xp = np.arange(len(audio))
    out = np.empty(count, dtype=np.int16)
    for a in range(0, count, piece):
        at = np.arange(a, min(count, a + piece)) * (16000 / rate)
        part = np.round(np.interp(at, xp, audio) * 32768.0)
        out[a:a + len(at)] = np.clip(part, -32768, 32767)
    return out


def _git_head():
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def cmd_run(args):
    from workspace import offline_guard, resources
    # As the worker does: no telemetry and no network before any model library.
    offline_guard.disable_telemetry()
    offline_guard.install()
    offline_guard.disable_onnxruntime_telemetry()
    resources.set_priority()                 # below normal; the draft workers set theirs
    from workspace import asr_engines, drafts, engines

    cuts = [float(c) for c in args.cuts.split(",") if c.strip()]
    for cut in cuts:
        with draft_cut(cut):                 # refuse a bad cut before anything loads
            pass
    if not Path(args.cache).is_dir():
        sys.exit("The model cache folder does not exist.")
    meeting = args.meeting or Path(args.wav).name.split(".")[0]
    ref = None
    if args.ami_words:
        ref = in_window(ami_words(args.ami_words, meeting), args.start, args.seconds or None)
    audio = _decode(args.wav)
    first = int(args.start * 16000)
    audio = audio[first: first + int(args.seconds * 16000) if args.seconds else None]
    seconds = len(audio) / 16000
    pcm = to_device_rate(audio, args.device_rate)
    say = functools.partial(print, file=sys.stderr, flush=True)
    say(f"{seconds:.0f} s of audio at {args.device_rate} Hz; cuts {', '.join(map(str, cuts))}")

    runs, descriptions = [], set()
    for n, cut in enumerate(cuts):
        if n:
            time.sleep(args.rest)            # let the previous worker's memory go
        say(f"cut {cut:g} s")
        run = run_cut(pcm, args.device_rate, cut, cache=args.cache, speed=args.speed,
                      progress=say)
        say(f"cut {cut:g} s: {run['state']}, {len(run['phrases'])} phrases")
        runs.append(run)

    final_words, final = None, None
    if not args.no_final:
        threads = args.threads or resources.worker_threads(resources.QUIET)
        t = time.perf_counter()
        engine = engines.load_engine(asr_engines.PARAKEET_V2, args.cache, threads)
        load_s = time.perf_counter() - t
        t = time.perf_counter()
        segments, _ = engine.transcribe(audio)
        final_words = []
        for seg in segments:
            if seg.words:
                final_words += [(float(s), float(e), str(w)) for s, e, w in seg.words]
            else:
                final_words.append((seg.start, seg.end, seg.text))
        final = {"engine": engine.description, "threads": threads, "load_s": round(load_s, 2),
                 "proc_s": round(time.perf_counter() - t, 2)}
        descriptions.add(engine.description)
        del engine

    config = {"recording": Path(args.wav).name, "start_s": args.start,
              "seconds": round(seconds, 1), "device_rate": args.device_rate,
              "block_s": BLOCK_S, "speed": args.speed, "cuts": cuts,
              "draft_model": drafts.DRAFT_MODEL, "draft_threads": drafts.DRAFT_THREADS,
              "commit": _git_head(), "processor": platform.processor(),
              "logical_processors": os.cpu_count(),
              "end_silence_s": drafts.END_SILENCE_S, "pre_roll_s": drafts.PRE_ROLL_S}
    rep = report(runs, final_words, ref, config, final)
    Path(args.out).write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(table(rep))


def cmd_manifest(args):
    """compare_engines manifest lines with AMI references (public corpus)."""
    with open(args.out, "w", encoding="utf-8") as out:
        for wav in args.wav:
            meeting = Path(wav).name.split(".")[0]
            words = ami_words(args.ami_words, meeting)
            out.write(json.dumps({"set": args.set, "id": meeting, "wav": str(wav),
                                  "ref": " ".join(w for _, _, w in words)}) + "\n")
            print(f"{meeting}: {len(words)} reference words", file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(description="Live-draft trial on public audio (#104).")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="measure the live-draft path per cut length")
    r.add_argument("wav", help="a public recording (for example AMI ES2004a)")
    r.add_argument("--cache", required=True,
                   help="model cache with Parakeet v2 and Silero (never the app's own)")
    r.add_argument("--out", required=True, help="results file (JSON, numbers only)")
    r.add_argument("--cuts", default=",".join(f"{c:g}" for c in DEFAULT_CUTS),
                   help="cut lengths in seconds, comma separated (default 6,8,10)")
    r.add_argument("--start", type=float, default=0.0, help="seconds into the recording")
    r.add_argument("--seconds", type=float, default=450.0,
                   help="seconds of audio to feed (default 450; 0 for all)")
    r.add_argument("--device-rate", type=int, default=48000,
                   help="microphone rate the audio is fed at (default 48000)")
    r.add_argument("--ami-words", help="folder of AMI word files, for the reference")
    r.add_argument("--meeting", help="AMI meeting id (default: the file name's first part)")
    r.add_argument("--threads", type=int, help="full-pass threads (default: Quiet mode's)")
    r.add_argument("--no-final", action="store_true", help="skip the full pass")
    r.add_argument("--speed", type=float, default=1.0,
                   help="feed this many times real time (tests only; default 1)")
    r.add_argument("--rest", type=float, default=10.0, help="seconds between runs")
    m = sub.add_parser("manifest", help="compare_engines manifest with AMI references")
    m.add_argument("wav", nargs="+")
    m.add_argument("--ami-words", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--set", default="ami")
    args = p.parse_args(argv)
    {"run": cmd_run, "manifest": cmd_manifest}[args.cmd](args)


if __name__ == "__main__":
    main()
