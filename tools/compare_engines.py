# tools/compare_engines.py
# Compare speech engines on the same recordings (issue #12).
#
#   run:   transcribe every clip in a manifest with ONE engine, in its own
#          process, recording load time, processing time and peak memory.
#   score: word error rate per engine and per set against reference text,
#          plus speed, and a side-by-side file for listening checks.
#
# A manifest is JSON Lines: {"set": "...", "id": "...", "wav": "path", "ref": "..."}
# ("ref" optional: without it you still get timings and side-by-side text).
#
# `run` loads the engine exactly as the app's worker does (workspace.engines,
# offline, the verified model cache in %LOCALAPPDATA%\whisper-models, Faster
# mode's thread count unless --threads is given).
#
#   python tools/compare_engines.py run   --engine parakeet-tdt-0.6b-v2 --out v2.jsonl m.jsonl
#   python tools/compare_engines.py score v2.jsonl v3.jsonl turbo.jsonl --side-by-side sbs.md
#
# Keep real recordings, transcripts and per-clip results private; publish only
# aggregate figures (see issue #12). Run heavy engines one at a time.

import argparse
import ctypes
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# --- Text normalisation (applied identically to references and hypotheses) ---

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
         "fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = ((10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (1000, "thousand"))
_ORDINAL = {"one": "first", "two": "second", "three": "third", "five": "fifth",
            "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
# UK/NZ spellings mapped to the US forms the engines tend to write, so a
# spelling convention is not scored as a recognition error. Not exhaustive.
_SPELLING = {w: w.replace("ise", "ize").replace("isa", "iza") for w in (
    "organise organised organises organising organisation organisations recognise "
    "recognised realise realised realising prioritise prioritised finalise finalised "
    "summarise summarised apologise categorise authorise authorised").split()}
_SPELLING.update({"colour": "color", "colours": "colors", "favour": "favor",
                  "favourite": "favorite", "behaviour": "behavior", "centre": "center",
                  "centres": "centers", "labour": "labor", "analyse": "analyze",
                  "analysed": "analyzed", "travelling": "traveling", "cheque": "check",
                  "programme": "program", "defence": "defense", "neighbour": "neighbor"})
# Known limits: years are spelled as plain numbers ("two thousand twenty four",
# not "twenty twenty-four"), so compare against references written with digits
# or spoken the same way; British/US variants beyond _SPELLING still count.

FILLERS = {"uh", "um", "umm", "uhm", "hmm", "hm", "mm", "mmm", "mhm", "ah", "er", "erm",
           "eh", "huh", "uhhuh", "mmhmm"}


def int_words(n):
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + (" " + int_words(rest) if rest else "")
    for value, word in _SCALES:
        if n >= value:
            rest = n % value
            return int_words(n // value) + " " + word + (" " + int_words(rest) if rest else "")
    return str(n)


def number_words(text):
    whole, _, frac = text.partition(".")
    out = int_words(int(whole)) if len(whole) <= 15 else " ".join(_ONES[int(d)] for d in whole)
    if frac:
        out += " point " + " ".join(_ONES[int(d)] for d in frac)
    return out


def ordinal_words(n):
    words = int_words(n).split()
    last = words[-1]
    if last in _ORDINAL:
        words[-1] = _ORDINAL[last]
    elif last.endswith("y"):
        words[-1] = last[:-1] + "ieth"
    else:
        words[-1] = last + "th"
    return " ".join(words)


def normalise(text):
    t = text.lower().replace("…", " ").replace("’", "'")
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)                       # 109,000 -> 109000
    t = re.sub(r"\$\s?(\d+(?:\.\d+)?)(\s?(?:thousand|million|billion|trillion))?",
               lambda m: f"{m.group(1)}{m.group(2) or ''} dollars", t)
    t = t.replace("%", " percent").replace("&", " and ")
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", lambda m: ordinal_words(int(m.group(1))), t)
    t = re.sub(r"\d+(?:\.\d+)?", lambda m: " " + number_words(m.group(0)) + " ", t)
    t = re.sub(r"[-/]", " ", t)
    t = re.sub(r"[^a-z0-9' ]", " ", t)
    words = []
    for w in t.split():
        w = {"'cause": "because", "'cos": "because"}.get(w, w).strip("'")
        w = {"ok": "okay", "cos": "because"}.get(w, w)
        w = _SPELLING.get(w, w)
        if w and w not in FILLERS:
            words.append(w)
    return words


def edit_distance(ref, hyp):
    """Word-level Levenshtein distance."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1]


# --- run ----------------------------------------------------------------------

def peak_memory_gb():
    import ctypes.wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t),
                    ("d", ctypes.c_size_t), ("e", ctypes.c_size_t), ("f", ctypes.c_size_t)]
    c = PMC()
    c.cb = ctypes.sizeof(c)
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
    ok = k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return round(c.PeakWorkingSetSize / 1e9, 2) if ok else None


def cmd_run(args):
    sys.path.insert(0, str(Path(args.repo).resolve()))
    # As the worker does: no telemetry and no network before any model library.
    import offline_guard
    offline_guard.disable_telemetry()
    offline_guard.install()
    offline_guard.disable_onnxruntime_telemetry()
    from faster_whisper.audio import decode_audio

    from workspace import resources
    from workspace.engines import load_engine
    from workspace.store import local_appdata

    cache = Path(args.cache) if args.cache else local_appdata() / "whisper-models"
    threads = args.threads or resources.worker_threads(resources.FASTER)
    clips = [json.loads(line) for m in args.manifest
             for line in Path(m).read_text(encoding="utf-8").splitlines() if line.strip()]
    t0 = time.perf_counter()
    engine = load_engine(args.engine, str(cache), threads)
    load_s = time.perf_counter() - t0
    with open(args.out, "w", encoding="utf-8") as out:
        out.write(json.dumps({"engine": args.engine, "description": engine.description,
                              "threads": threads, "load_s": round(load_s, 2)}) + "\n")
        for i, clip in enumerate(clips, 1):
            wav = decode_audio(clip["wav"], sampling_rate=16000)
            t = time.perf_counter()
            segments, duration = engine.transcribe(wav)
            text = " ".join(s.text for s in segments)
            out.write(json.dumps({**clip, "hyp": text, "audio_s": round(duration, 3),
                                  "proc_s": round(time.perf_counter() - t, 3)}) + "\n")
            print(f"{i}/{len(clips)} {clip['set']}", file=sys.stderr, flush=True)
        out.write(json.dumps({"peak_ram_gb": peak_memory_gb()}) + "\n")


# --- score --------------------------------------------------------------------

def load_results(path):
    """(header, clips, footer). An interrupted run has no footer line."""
    lines = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x]
    head, rest = lines[0], lines[1:]
    tail = rest.pop() if rest and "peak_ram_gb" in rest[-1] else {}
    return head, rest, tail


def cmd_score(args):
    runs = [load_results(p) for p in args.results]
    sets = sorted({c["set"] for _, clips, _ in runs for c in clips})
    print("| Engine | " + " | ".join(f"WER {s}" for s in sets)
          + " | WER all | Audio min | Proc s | x real time | Load s | Peak RAM GB |")
    print("| --- " * (len(sets) + 7) + "|")
    for head, clips, tail in runs:
        cells, tot_e, tot_n = [], 0, 0
        for s in sets:
            e = n = 0
            for c in clips:
                if c["set"] == s and c.get("ref"):
                    ref = normalise(c["ref"])
                    e += edit_distance(ref, normalise(c["hyp"]))
                    n += len(ref)
            cells.append(f"{100 * e / n:.1f}%" if n else "-")
            tot_e, tot_n = tot_e + e, tot_n + n
        audio = sum(c["audio_s"] for c in clips)
        proc = sum(c["proc_s"] for c in clips)
        wer_all = f"{100 * tot_e / tot_n:.1f}%" if tot_n else "-"
        speed = f"{audio / proc:.1f}" if proc else "-"
        print(f"| {head['engine']} | " + " | ".join(cells)
              + f" | {wer_all} | {audio / 60:.1f} | {proc:.0f}"
              + f" | {speed} | {head['load_s']} | {tail.get('peak_ram_gb', '-')} |")
    if args.side_by_side:
        by_id = {}
        for head, clips, _ in runs:
            for c in clips:
                by_id.setdefault((c["set"], str(c["id"])), {"ref": c.get("ref", "")})[
                    head["engine"]] = c["hyp"]
        with open(args.side_by_side, "w", encoding="utf-8") as f:
            for (s, i), texts in sorted(by_id.items()):
                f.write(f"## {s} {i}\n\n")
                for k, v in texts.items():
                    f.write(f"- **{k}**: {v}\n")
                f.write("\n")


def main():
    p = argparse.ArgumentParser(description="Compare speech engines on the same recordings.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--engine", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--repo", default=str(REPO))
    r.add_argument("--cache", help="model cache (default %%LOCALAPPDATA%%\\whisper-models)")
    r.add_argument("--threads", type=int, help="processor threads (default: as Faster mode)")
    r.add_argument("manifest", nargs="+")
    s = sub.add_parser("score")
    s.add_argument("results", nargs="+")
    s.add_argument("--side-by-side")
    args = p.parse_args()
    {"run": cmd_run, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    main()
