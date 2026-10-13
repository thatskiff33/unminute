# asr_engines.py
# Speech-recognition engines behind one timed-segment interface (issue #13).
#
# Every engine yields Segment(start, end, text) in seconds from the start of
# the recording, so the queue worker, speaker labelling and transcript output
# do not care which engine produced the text.
#
# Parakeet runs through onnx-asr on ONNX Runtime (CPU, INT8). Its model files
# are pinned to an exact Hugging Face revision and SHA-256 manifest: a folder
# only counts as installed when every expected file is present with the right
# size, and is fully hashed before first use. A damaged installation is moved
# aside for diagnosis (never deleted) and replaced with a verified download.
#
# This module never touches the network on its own. Downloads happen only in
# install_parakeet(), which callers wrap in their own network permission
# (transcriber_core.allow_network() in the app; the installer runs it online).
#
# CLI (used by install.ps1):
#   python asr_engines.py ensure [--model NAME ...] [--present] [--cache DIR]

import hashlib
import json
import os
import re
import shutil
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 16000

# Silero VAD splits long recordings into bounded pieces. Parakeet's ONNX export
# fails outright on single inputs of roughly 10 minutes (onnx-asr issue #131),
# so no piece may exceed this length. 20 s is onnx-asr's own default.
MAX_SEGMENT_SECONDS = 20.0

VERIFIED_MARKER = ".verified.json"


# --- Shared interface ---------------------------------------------------------

@dataclass(frozen=True)
class Segment:
    """One timed piece of transcript text, in seconds from the recording start."""
    start: float
    end: float
    text: str


class ModelLoadError(RuntimeError):
    """A model is missing, damaged or failed to load. The message says what to do."""


# --- Parakeet model manifests -------------------------------------------------

@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ModelSpec:
    """A pinned, hash-verified set of files from one Hugging Face revision."""
    name: str          # name shown in the app
    folder: str        # folder under <cache>/parakeet
    onnx_asr_name: str  # model name understood by onnx_asr.load_model
    repo: str
    revision: str
    files: tuple
    quantization: str | None = None

    @property
    def download_bytes(self):
        return sum(f.size for f in self.files)


# Hashes were taken from the pinned revisions. Large files match the LFS
# SHA-256 Hugging Face publishes; small files were checked against Hugging
# Face's git blob IDs before their SHA-256 was recorded.
PARAKEET_MODELS = {
    "parakeet-tdt-0.6b-v2": ModelSpec(
        name="parakeet-tdt-0.6b-v2",
        folder="parakeet-tdt-0.6b-v2-int8",
        onnx_asr_name="nemo-parakeet-tdt-0.6b-v2",
        repo="istupakov/parakeet-tdt-0.6b-v2-onnx",
        revision="0bbb45a3365852604aef28b538a8f066f4ccaa85",
        quantization="int8",
        files=(
            ModelFile("config.json", 97,
                      "666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466"),
            ModelFile("decoder_joint-model.int8.onnx", 8998286,
                      "a449f49acd68979d418651dd2dcb737cc0f1bf0225e009e29ee326354edbf7d3"),
            ModelFile("encoder-model.int8.onnx", 652184014,
                      "3e0581fda6ab843888b51e56d7ee78b6d5bc3237ec113af1f732d1d5286aa155"),
            ModelFile("vocab.txt", 9384,
                      "ec182b70dd42113aff6c5372c75cac58c952443eb22322f57bbd7f53977d497d"),
        ),
    ),
    "parakeet-tdt-0.6b-v3": ModelSpec(
        name="parakeet-tdt-0.6b-v3",
        folder="parakeet-tdt-0.6b-v3-int8",
        onnx_asr_name="nemo-parakeet-tdt-0.6b-v3",
        repo="istupakov/parakeet-tdt-0.6b-v3-onnx",
        revision="8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce",
        quantization="int8",
        files=(
            ModelFile("config.json", 97,
                      "666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466"),
            ModelFile("decoder_joint-model.int8.onnx", 18202004,
                      "eea7483ee3d1a30375daedc8ed83e3960c91b098812127a0d99d1c8977667a70"),
            ModelFile("encoder-model.int8.onnx", 652183999,
                      "6139d2fa7e1b086097b277c7149725edbab89cc7c7ae64b23c741be4055aff09"),
            ModelFile("vocab.txt", 93939,
                      "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
        ),
    ),
}

SILERO_VAD = ModelSpec(
    name="silero-vad",
    folder="silero-vad",
    onnx_asr_name="silero",
    repo="istupakov/silero-vad-onnx",
    revision="b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6",
    files=(
        ModelFile("config.json", 30,
                  "1094039d370c82889582ba739a3d1caac5754c8b3a17a66a534200c9f72086e2"),
        ModelFile("silero_vad.onnx", 2327524,
                  "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"),
    ),
)

RECOMMENDED_PARAKEET = "parakeet-tdt-0.6b-v2"


def is_parakeet(name):
    return name in PARAKEET_MODELS


def model_dir(spec, cache_dir):
    return Path(cache_dir) / "parakeet" / spec.folder


def staging_dir(spec, cache_dir):
    """Short, fixed download folder for one model. huggingface_hub writes
    temporary files under <dir>\\.cache\\huggingface\\download\\ with names of
    ~113 characters, so a long folder name here breaks Windows' 260-character
    path limit."""
    return Path(cache_dir) / "parakeet" / f"~dl-{zlib.crc32(spec.folder.encode()):08x}"


# --- Integrity checks ---------------------------------------------------------

def _sha256(path, chunk=8 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _fingerprint(folder, spec):
    """Cheap identity of the files on disk: size and modification time."""
    out = {}
    for f in spec.files:
        st = (folder / f.name).stat()
        out[f.name] = [st.st_size, st.st_mtime_ns]
    return out


def _read_marker(folder):
    try:
        return json.loads((folder / VERIFIED_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_files(spec, folder, full=False):
    """Return a list of problems with `folder` (empty when it is usable).

    Quick mode checks presence and exact sizes, and trusts a verification
    marker only if it names this revision and the files are unchanged since
    hashing. full=True (or a missing/stale marker) hashes every file and
    records a fresh marker on success."""
    folder = Path(folder)
    if not folder.is_dir():
        return ["not installed"]
    problems = _quick_problems(spec, folder)
    if problems:
        return problems

    marker = _read_marker(folder)
    trusted = (not full and marker is not None and marker.get("ok", True)
               and marker.get("revision") == spec.revision
               and marker.get("files") == _fingerprint(folder, spec))
    if trusted:
        return []

    for f in spec.files:
        if _sha256(folder / f.name) != f.sha256:
            problems.append(f"{f.name} does not match the expected checksum")
    try:
        # Record the outcome either way: a failure makes the quick status show
        # "needs repair" until these exact files change.
        (folder / VERIFIED_MARKER).write_text(json.dumps({
            "repo": spec.repo, "revision": spec.revision, "ok": not problems,
            "problems": problems, "files": _fingerprint(folder, spec),
        }), encoding="utf-8")
    except OSError:
        pass  # read-only cache: still usable, it just re-hashes next time
    return problems


def _recorded_failure(spec, folder):
    """Problems from an earlier full check of these exact files, if it failed."""
    marker = _read_marker(folder)
    try:
        if (marker and marker.get("ok") is False and marker.get("revision") == spec.revision
                and marker.get("files") == _fingerprint(folder, spec)):
            return marker.get("problems") or ["checksum mismatch"]
    except OSError:
        pass
    return []


def parakeet_status(name, cache_dir):
    """('ready' | 'missing' | 'invalid', detail) without hashing large files."""
    spec = PARAKEET_MODELS[name]
    folder = model_dir(spec, cache_dir)
    vad_folder = model_dir(SILERO_VAD, cache_dir)
    if not folder.exists():
        return "missing", "not installed"
    problems = _quick_problems(spec, folder) or _recorded_failure(spec, folder)
    if vad_folder.exists():
        problems += [f"speech detector: {p}" for p in
                     _quick_problems(SILERO_VAD, vad_folder)
                     or _recorded_failure(SILERO_VAD, vad_folder)]
    else:
        problems.append("speech detector: not installed")
    return ("invalid", "; ".join(problems)) if problems else ("ready", "")


def _quick_problems(spec, folder):
    """Presence and size checks only (no hashing)."""
    problems = []
    for f in spec.files:
        p = folder / f.name
        if not p.is_file():
            problems.append(f"{f.name} is missing")
        elif p.stat().st_size != f.size:
            problems.append(f"{f.name} has the wrong size "
                            f"({p.stat().st_size:,} bytes, expected {f.size:,})")
    return problems


# --- Download and repair ------------------------------------------------------

def _download(spec, dest):
    """Fetch exactly the pinned files of one revision into `dest`."""
    # Deferred import: honours the offline flags the caller has set.
    from huggingface_hub import snapshot_download
    # Public repos: never send a stored token with these requests.
    snapshot_download(spec.repo, revision=spec.revision, local_dir=str(dest),
                      allow_patterns=[f.name for f in spec.files], token=False)


def install_model(spec, cache_dir, log=print, download=_download):
    """Make `spec` present and verified. Returns 'ok', 'installed' or 'repaired'.

    An existing folder that fails verification is renamed to
    '<folder>.invalid-<timestamp>' so it can be inspected, then replaced by a
    fresh download that is verified BEFORE it is moved into place. Re-running
    on a good installation only re-checks it."""
    target = model_dir(spec, cache_dir)
    had_folder = target.exists()
    if had_folder:
        problems = check_files(spec, target, full=True)
        if not problems:
            return "ok"
        log(f"{spec.name}: installed files are not usable ({'; '.join(problems)}).")

    staging = staging_dir(spec, cache_dir)
    if staging.is_dir() and not check_files(spec, staging, full=True):
        # A verified download from an attempt whose final move failed.
        log(f"Reusing the verified {spec.name} download from the last attempt.")
        return _move_into_place(spec, target, staging, had_folder, log)
    shutil.rmtree(staging, ignore_errors=True)   # leftover of an interrupted run
    staging.parent.mkdir(parents=True, exist_ok=True)
    log(f"Downloading {spec.name} ({spec.download_bytes / 1e9:.2f} GB, "
        f"revision {spec.revision[:7]})...")
    try:
        try:
            download(spec, staging)
        except Exception as exc:
            raise ModelLoadError(
                f"Could not download {spec.name} ({type(exc).__name__}: {exc}). "
                f"Nothing was changed. Check the internet connection and try again, "
                f"or run Update / repair.") from exc
        # Download bookkeeping only; dropping it keeps the model folder clean.
        shutil.rmtree(staging / ".cache", ignore_errors=True)
        problems = check_files(spec, staging, full=True)
        if problems:
            raise ModelLoadError(
                f"The downloaded {spec.name} files failed verification "
                f"({'; '.join(problems)}). Nothing was changed; try Update / repair again.")
    except BaseException:
        # Only our own fresh, unverified download is discarded here.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return _move_into_place(spec, target, staging, had_folder, log)


def _move_into_place(spec, target, staging, had_folder, log):
    """Swap a verified staging folder in, keeping any damaged copy aside."""
    kept = None
    if had_folder:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        kept = target.with_name(f"{target.name}.invalid-{stamp}")
        target.rename(kept)
        log(f"Kept the damaged copy for diagnosis: {kept}")
    try:
        staging.rename(target)
    except OSError as exc:
        # e.g. antivirus still scanning the new files. Put the old folder back
        # so the model stays visible (and repairable); keep the verified
        # download in place for the next attempt to pick up.
        if kept is not None and not target.exists():
            kept.rename(target)
        raise ModelLoadError(
            f"Could not move the verified {spec.name} files into place ({exc}). "
            f"Close the app, wait a minute and run Update / repair again.") from exc
    log(f"{spec.name} verified and ready.")
    return "repaired" if had_folder else "installed"


def install_parakeet(name, cache_dir, log=print, download=_download):
    """Install or repair a Parakeet model together with its speech detector."""
    results = {SILERO_VAD.name: install_model(SILERO_VAD, cache_dir, log, download)}
    spec = PARAKEET_MODELS[name]
    results[spec.name] = install_model(spec, cache_dir, log, download)
    return results


# --- Engines ------------------------------------------------------------------

_SPACE_BEFORE_CLOSER = re.compile(r"\s+(?=[.,!?;:%)\]}])")


def join_tokens(tokens):
    """Token pieces -> text. The Parakeet vocabularies contain a bare-space
    token, so drop spaces before closing punctuation ("Hello , world" ->
    "Hello, world") but keep them before "$" and other openers."""
    return re.sub(r"\s{2,}", " ", _SPACE_BEFORE_CLOSER.sub("", "".join(tokens))).strip()


def split_sentences(start, end, tokens, timestamps, text):
    """Split one recognised piece into sentence Segments using token times.

    `timestamps` are token start times relative to `start`. A sentence ends at
    '.', '?' or '!' when the next token begins a new word, so decimals such as
    '3.5' stay intact. Falls back to one Segment when timings are unavailable."""
    text = text.strip()
    if not text:
        return []
    if not tokens or len(tokens) != len(timestamps):
        return [Segment(start, end, text)]
    out, first, buf = [], 0, []
    for i, tok in enumerate(tokens):
        buf.append(tok)
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok.rstrip().endswith((".", "?", "!")) and (nxt is None or nxt.startswith(" ")):
            seg_end = start + timestamps[i + 1] if nxt is not None else end
            sentence = join_tokens(buf)
            if sentence:
                out.append(Segment(start + timestamps[first], min(seg_end, end), sentence))
            first, buf = i + 1, []
    tail = join_tokens(buf)
    if tail:
        out.append(Segment(start + timestamps[first], end, tail))
    return out


class WhisperEngine:
    """faster-whisper behind the shared interface; settings are unchanged."""

    device = "cpu"

    def __init__(self, model, label, precision, language, beam_size=5):
        self.model = model
        self.label = label
        self.precision = precision
        self.language = language
        self.beam_size = beam_size

    @property
    def description(self):
        return f"{self.label} (faster-whisper, {self.precision}, {self.device})"

    def transcribe(self, audio):
        """`audio` is a path or a 16 kHz float32 waveform. Returns (segments, duration)."""
        segments, info = self.model.transcribe(
            audio, language=self.language, vad_filter=True, beam_size=self.beam_size)
        return (Segment(s.start, s.end, s.text.strip()) for s in segments), info.duration

    def close(self):
        self.model = None


class ParakeetEngine:
    """NVIDIA Parakeet TDT (ONNX INT8) on the CPU via onnx-asr."""

    device = "cpu"

    def __init__(self, name, cache_dir, threads, decode_audio):
        self.spec = PARAKEET_MODELS[name]
        self.cache_dir = cache_dir
        self.threads = threads
        self.decode_audio = decode_audio
        self.label = name
        self.precision = self.spec.quantization or "float32"
        self._asr = None

    @property
    def description(self):
        return (f"{self.label} (ONNX {self.precision}, {self.device}, "
                f"revision {self.spec.revision[:7]})")

    def load(self):
        """Verify then load, entirely from local files. Raises ModelLoadError."""
        for spec in (self.spec, SILERO_VAD):
            problems = check_files(spec, model_dir(spec, self.cache_dir))
            if problems:
                raise ModelLoadError(
                    f"{spec.name} cannot be used: {'; '.join(problems)}. It now shows "
                    f"'needs repair': start the queue again and accept the repair, "
                    f"or run Update / repair.")
        try:
            import onnx_asr
            import onnxruntime as rt
            opts = rt.SessionOptions()
            opts.intra_op_num_threads = self.threads
            opts.inter_op_num_threads = 1
            cpu = ["CPUExecutionProvider"]
            # An existing local folder makes onnx-asr load offline, with no downloads.
            asr = onnx_asr.load_model(
                self.spec.onnx_asr_name, model_dir(self.spec, self.cache_dir),
                quantization=self.spec.quantization, sess_options=opts, providers=cpu)
            vad = onnx_asr.load_vad(SILERO_VAD.onnx_asr_name,
                                    model_dir(SILERO_VAD, self.cache_dir),
                                    sess_options=opts, providers=cpu)
            self._asr = asr.with_vad(vad, batch_size=1,
                                     max_speech_duration_s=MAX_SEGMENT_SECONDS).with_timestamps()
        except Exception as exc:
            raise ModelLoadError(
                f"{self.label} failed to load ({type(exc).__name__}: {exc}). "
                f"Run Update / repair; if it keeps failing, choose a Whisper model.") from exc
        return self

    def transcribe(self, audio):
        """`audio` is a path or a 16 kHz float32 waveform. Returns (segments, duration).

        Segments are produced lazily, one speech piece at a time, so callers
        can pause or stop between them."""
        if self._asr is None:
            raise ModelLoadError(f"{self.label} is not loaded.")
        wav = audio
        if isinstance(audio, (str, Path)):
            wav = self.decode_audio(str(audio), sampling_rate=SAMPLE_RATE)
        duration = len(wav) / SAMPLE_RATE

        def generate():
            for res in self._asr.recognize(wav, sample_rate=SAMPLE_RATE):
                yield from split_sentences(res.start, res.end, res.tokens, res.timestamps, res.text)

        return generate(), duration

    def close(self):
        self._asr = None


# --- Installer entry point ----------------------------------------------------

def _main(argv):
    import argparse
    parser = argparse.ArgumentParser(description="Install or repair Parakeet models.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ensure = sub.add_parser("ensure", help="download missing / repair damaged models")
    ensure.add_argument("--model", action="append", default=[], choices=sorted(PARAKEET_MODELS))
    ensure.add_argument("--present", action="store_true",
                        help="also verify and repair every Parakeet model already on disk")
    ensure.add_argument("--default-parakeet", action="store_true",
                        help=f"install {RECOMMENDED_PARAKEET} only if no Parakeet model is on disk")
    ensure.add_argument("--cache", default=os.path.join(
        os.environ.get("LOCALAPPDATA", os.getcwd()), "whisper-models"))
    args = parser.parse_args(argv)

    def on_disk(spec):
        # A verified download whose final move failed counts as present too.
        return model_dir(spec, args.cache).exists() or staging_dir(spec, args.cache).exists()

    names = list(dict.fromkeys(args.model))
    if args.present:
        names += [n for n, s in PARAKEET_MODELS.items() if on_disk(s) and n not in names]
    if args.default_parakeet and not any(on_disk(s) for s in PARAKEET_MODELS.values()):
        names.append(RECOMMENDED_PARAKEET)
    if not names:
        print("  No Parakeet models selected or installed.")
        return 0
    failed = False
    for name in names:
        try:
            results = install_parakeet(name, args.cache, log=lambda m: print("  " + m))
            print(f"  {name}: {results[name]}")
        except Exception as exc:
            failed = True
            print(f"  {name}: FAILED - {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    # The installer's download step: telemetry off, but no network guard,
    # because downloading is the point.
    import offline_guard
    offline_guard.disable_telemetry()
    sys.exit(_main(sys.argv[1:]))
