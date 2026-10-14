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
# Whisper models get the same treatment. An existing Hugging Face cache copy is
# reused when its files verify; otherwise a verified copy is downloaded into
# <cache>/whisper/<model> and the old cache is left untouched for diagnosis.
# After loading, feature_width_problem() confirms the preprocessing produces
# the mel-band count the model expects before anything is transcribed.
#
# This module never touches the network on its own. Downloads happen only in
# install()/install_model(), which callers wrap in their own network permission
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
from dataclasses import dataclass, field
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
    """One timed piece of transcript text, in seconds from the recording start.

    `words` (optional) holds (start, end, text) per word, with each word's text
    keeping its leading space. The workspace uses word times to divide a
    sentence that crosses a chunk boundary (docs/WORKSPACE_DESIGN.md)."""
    start: float
    end: float
    text: str
    words: tuple = field(default=(), compare=False)


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
    folder: str        # folder under <cache>/<family>
    repo: str
    revision: str
    files: tuple
    onnx_asr_name: str | None = None  # model name understood by onnx_asr.load_model
    quantization: str | None = None
    family: str = "parakeet"          # "parakeet" or "whisper"
    n_mels: int | None = None         # Whisper: mel bands the model expects
    hub_repos: tuple = ()             # repo ids an older install may have in the HF cache
    gated: bool = False               # download needs the user's Hugging Face token

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

# faster-whisper (CTranslate2) models. File hashes match Hugging Face's LFS
# SHA-256 and git blob IDs; the model files are identical across turbo
# revisions 0c94664 and 0a363e9 (only README.md changed), so any cached copy
# with these exact files is accepted whatever revision it came from. Both
# models need 128 mel bands: faster-whisper 1.2.1 silently falls back to 80
# when preprocessor_config.json is missing, malformed or lacks feature_size.
_WHISPER_SHARED = (
    ModelFile("preprocessor_config.json", 340,
              "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711"),
    ModelFile("vocabulary.json", 1068114,
              "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1"),
)

WHISPER_MODELS = {
    "large-v3-turbo": ModelSpec(
        name="large-v3-turbo",
        folder="faster-whisper-large-v3-turbo",
        family="whisper",
        repo="dropbox-dash/faster-whisper-large-v3-turbo",
        revision="0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
        hub_repos=("mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                   "dropbox-dash/faster-whisper-large-v3-turbo"),
        n_mels=128,
        files=(
            ModelFile("config.json", 2263,
                      "b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e"),
            ModelFile("model.bin", 1617884929,
                      "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"),
            ModelFile("tokenizer.json", 2710337,
                      "297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd"),
            *_WHISPER_SHARED,
        ),
    ),
    "large-v3": ModelSpec(
        name="large-v3",
        folder="faster-whisper-large-v3",
        family="whisper",
        repo="Systran/faster-whisper-large-v3",
        revision="edaa852ec7e145841d8ffdb056a99866b5f0a478",
        hub_repos=("Systran/faster-whisper-large-v3",),
        n_mels=128,
        files=(
            ModelFile("config.json", 2394,
                      "a9306624f5ec14270a014b647e5c316b6e03a662c369758d1b90697a7b0655b9"),
            ModelFile("model.bin", 3087284237,
                      "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1"),
            ModelFile("tokenizer.json", 2480617,
                      "6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca"),
            *_WHISPER_SHARED,
        ),
    ),
}

# pyannote speaker identification. Gated on Hugging Face (needs the user's
# token to download, never to load). pyannote 4.0.4 loads both sub-models with
# torch.load(weights_only=False), which can execute code from a tampered file,
# so these hashes are what stands between the cache and code execution.
SPEAKER_MODEL = ModelSpec(
    name="speaker-diarization-community-1",
    folder="speaker-diarization-community-1",
    family="pyannote",
    repo="pyannote/speaker-diarization-community-1",
    revision="3533c8cf8e369892e6b79ff1bf80f7b0286a54ee",
    hub_repos=("pyannote/speaker-diarization-community-1",),
    gated=True,
    files=(
        ModelFile("config.yaml", 444,
                  "5ce2bfa9a938dc132cec1172592d65173cbb8f444ea1e4133f10f9391de155be"),
        ModelFile("embedding/pytorch_model.bin", 26646242,
                  "6f10ff60898a1d185fa22e1d11e0bfa8a92efec811f11bca48cb8cafebefd929"),
        ModelFile("plda/plda.npz", 133852,
                  "9b77bcd840692710dd3496f62ecfeed8d8e5f002fd991b785079b244eab7d255"),
        ModelFile("plda/xvec_transform.npz", 134376,
                  "325f1ce8e48f7e55e9c8aa47e05d2766b7c48c4b25b8de8dd751e7a4cc5fbe8f"),
        ModelFile("segmentation/pytorch_model.bin", 5906507,
                  "7ad24338d844fb95985486eb1a464e32d229f6d7a03c9abe60f978bacf3f816e"),
    ),
)

MANAGED_MODELS = {**PARAKEET_MODELS, **WHISPER_MODELS, SPEAKER_MODEL.name: SPEAKER_MODEL}


def is_parakeet(name):
    return name in PARAKEET_MODELS


def is_whisper(name):
    return name in WHISPER_MODELS


def model_dir(spec, cache_dir):
    return Path(cache_dir) / spec.family / spec.folder


def staging_dir(spec, cache_dir):
    """Short, fixed download folder for one model. huggingface_hub writes
    temporary files under <dir>\\.cache\\huggingface\\download\\ with names of
    ~113 characters, so a long folder name here breaks Windows' 260-character
    path limit."""
    return Path(cache_dir) / spec.family / f"~dl-{zlib.crc32(spec.folder.encode()):08x}"


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


def _read_marker(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_files(spec, folder, full=False, marker=None):
    """Return a list of problems with `folder` (empty when it is usable).

    Quick mode checks presence, exact sizes and (Whisper) the preprocessing
    config, and trusts a verification marker only if it names this revision
    and the files are unchanged since hashing. full=True (or a missing/stale
    marker) hashes every file and records a fresh marker on success. `marker`
    defaults to a file inside `folder`; pass another path for folders we must
    not write to (an existing Hugging Face cache)."""
    folder = Path(folder)
    marker = Path(marker) if marker else folder / VERIFIED_MARKER
    if not folder.is_dir():
        return ["not installed"]
    problems = _quick_problems(spec, folder)
    if problems:
        return problems

    recorded = _read_marker(marker)
    trusted = (not full and recorded is not None and recorded.get("ok", True)
               and recorded.get("revision") == spec.revision
               and recorded.get("files") == _fingerprint(folder, spec))
    if trusted:
        return []

    for f in spec.files:
        if _sha256(folder / f.name) != f.sha256:
            problems.append(f"{f.name} does not match the expected checksum")
    try:
        # Record the outcome either way: a failure makes the quick status show
        # "needs repair" until these exact files change.
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "repo": spec.repo, "revision": spec.revision, "folder": str(folder),
            "ok": not problems, "problems": problems, "files": _fingerprint(folder, spec),
        }), encoding="utf-8")
    except OSError:
        pass  # read-only cache: still usable, it just re-hashes next time
    return problems


def _recorded_failure(spec, folder, marker=None):
    """Problems from an earlier full check of these exact files, if it failed."""
    folder = Path(folder)
    marker = _read_marker(marker or folder / VERIFIED_MARKER)
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
    """Presence, size and preprocessing-config checks (no hashing)."""
    problems = []
    for f in spec.files:
        p = folder / f.name
        if not p.is_file():
            problems.append(f"{f.name} is missing")
        elif p.stat().st_size != f.size:
            problems.append(f"{f.name} has the wrong size "
                            f"({p.stat().st_size:,} bytes, expected {f.size:,})")
    if spec.n_mels:
        problems += _preprocessor_problems(spec, folder)
    return problems


def _preprocessor_problems(spec, folder):
    """Explain a preprocessing config that would make faster-whisper fall back
    to 80 mel bands (the 'expected (1, 128, 3000), got (1, 80, 3000)' failure)."""
    p = Path(folder) / "preprocessor_config.json"
    if not p.is_file():
        return []   # already reported as missing
    try:
        config = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["preprocessor_config.json is not valid JSON"]
    if not isinstance(config, dict) or "feature_size" not in config:
        return ["preprocessor_config.json has no feature_size"]
    if config["feature_size"] != spec.n_mels:
        return [f"preprocessor_config.json sets feature_size {config['feature_size']}, "
                f"but {spec.name} needs {spec.n_mels}"]
    return []


# --- Whisper: verified folders and feature-width check ------------------------

def _hub_snapshots(spec, cache_dir):
    """Snapshot folders an older install left in the Hugging Face cache."""
    out = []
    for repo in spec.hub_repos:
        snaps = Path(cache_dir) / ("models--" + repo.replace("/", "--")) / "snapshots"
        if snaps.is_dir():
            out += sorted(p for p in snaps.iterdir() if p.is_dir())
    return out


def whisper_candidates(spec, cache_dir):
    """Existing folders that may hold this model: our managed copy first."""
    managed = model_dir(spec, cache_dir)
    return ([managed] if managed.is_dir() else []) + _hub_snapshots(spec, cache_dir)


def _marker_for(spec, cache_dir, folder):
    """Markers for Hugging Face cache folders live outside that cache."""
    if Path(folder) == model_dir(spec, cache_dir):
        return None
    key = hashlib.sha1(str(Path(folder).resolve()).lower().encode()).hexdigest()[:16]
    return Path(cache_dir) / spec.family / ".verified" / f"{spec.folder}-{key}.json"


def whisper_status(name, cache_dir):
    """('ready' | 'missing' | 'invalid', detail) without hashing large files.
    Works for any Hugging Face-cached model (Whisper, speaker identification)."""
    spec = MANAGED_MODELS[name]
    candidates = whisper_candidates(spec, cache_dir)
    if not candidates:
        return "missing", "not installed"
    reports = []
    for folder in candidates:
        problems = (_quick_problems(spec, folder)
                    or _recorded_failure(spec, folder, _marker_for(spec, cache_dir, folder)))
        if not problems:
            return "ready", ""
        reports.append("; ".join(problems))
    return "invalid", " | ".join(dict.fromkeys(reports))


def find_verified_whisper(spec, cache_dir, full=False):
    """First candidate folder whose files all verify, or None."""
    for folder in whisper_candidates(spec, cache_dir):
        marker = _marker_for(spec, cache_dir, folder)
        # A copy already found bad (and unchanged since) is skipped rather than
        # re-hashed on every load; a full check (repair) re-examines it.
        if not full and _recorded_failure(spec, folder, marker):
            continue
        if not check_files(spec, folder, full=full, marker=marker):
            return folder
    return None


def feature_width_problem(model, spec):
    """Compare the loaded feature extractor with what the model expects.

    For faster-whisper 1.2.1 / CTranslate2 4.8.0, mel_filters has shape
    (n_mels, n_fft // 2 + 1) and model.model.n_mels is the model's input width."""
    produced = int(model.feature_extractor.mel_filters.shape[0])
    expected = int(model.model.n_mels)
    if produced != expected:
        return (f"{spec.name} expects {expected} mel bands but its preprocessing "
                f"settings produce {produced}")
    if spec.n_mels and expected != spec.n_mels:
        return f"{spec.name} reports {expected} mel bands; this model should use {spec.n_mels}"
    return None


# --- Download and repair ------------------------------------------------------

def _download(spec, dest):
    """Fetch exactly the pinned files of one revision into `dest`."""
    # Deferred import: honours the offline flags the caller has set.
    from huggingface_hub import snapshot_download
    # Public repos: never send a stored token. Gated repos: let huggingface_hub
    # pick up the user's token implicitly (HF_TOKEN), as the app always has.
    snapshot_download(spec.repo, revision=spec.revision, local_dir=str(dest),
                      allow_patterns=[f.name for f in spec.files],
                      token=None if spec.gated else False)


def install_model(spec, cache_dir, log=print, download=_download, has_token=None):
    """Make `spec` present and verified. Returns 'ok', 'installed' or 'repaired'.

    An existing folder that fails verification is renamed to
    '<folder>.invalid-<timestamp>' so it can be inspected, then replaced by a
    fresh download that is verified BEFORE it is moved into place. Re-running
    on a good installation only re-checks it."""
    target = model_dir(spec, cache_dir)
    hub_cached = bool(spec.hub_repos)
    if hub_cached:
        # Reuse any copy that verifies, including an older Hugging Face cache.
        if find_verified_whisper(spec, cache_dir, full=True) is not None:
            return "ok"
        for snap in _hub_snapshots(spec, cache_dir):
            log(f"{spec.name}: the cached copy in {snap} is not usable "
                f"({'; '.join(_quick_problems(spec, snap)) or 'checksum mismatch'}). "
                f"It is left untouched for diagnosis.")
    had_folder = target.exists()
    if had_folder:
        # A hub-cached model's managed copy was just fully checked above.
        problems = (_quick_problems(spec, target) or _recorded_failure(spec, target)
                    if hub_cached else check_files(spec, target, full=True))
        if not problems and not hub_cached:
            return "ok"
        log(f"{spec.name}: installed files are not usable "
            f"({'; '.join(problems) or 'checksum mismatch'}).")

    # has_token: the caller passes the token to `download` itself (workspace
    # setup, #19); None keeps the legacy behaviour of reading HF_TOKEN.
    token_ok = os.environ.get("HF_TOKEN") if has_token is None else has_token
    if spec.gated and not token_ok and not staging_dir(spec, cache_dir).is_dir():
        raise ModelLoadError(
            f"{spec.name} needs downloading again, which needs your Hugging Face token. "
            f"Run install.cmd and follow the token step.")
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


def install(name, cache_dir, log=print, download=_download, has_token=None):
    """Install or repair any managed model. Returns {component: result}."""
    if is_parakeet(name):
        return install_parakeet(name, cache_dir, log, download)
    return {name: install_model(MANAGED_MODELS[name], cache_dir, log, download, has_token)}


def is_present(name, cache_dir):
    """True if any copy of the model exists on disk, usable or not."""
    if is_parakeet(name):
        return model_dir(PARAKEET_MODELS[name], cache_dir).exists()
    return bool(whisper_candidates(MANAGED_MODELS[name], cache_dir))


# Family-neutral names: these work for any model cached by Hugging Face.
hub_model_status = whisper_status
find_verified = find_verified_whisper

# Licences as published on each model card (checked 13 October 2026).
LICENCES = {
    "parakeet-tdt-0.6b-v2": ("CC BY 4.0", "NVIDIA parakeet-tdt-0.6b-v2, ONNX export by istupakov"),
    "parakeet-tdt-0.6b-v3": ("CC BY 4.0", "NVIDIA parakeet-tdt-0.6b-v3, ONNX export by istupakov"),
    "silero-vad": ("MIT", "Silero VAD, ONNX packaging by istupakov"),
    "large-v3-turbo": ("MIT", "OpenAI whisper-large-v3-turbo, CTranslate2 conversion"),
    "large-v3": ("MIT", "OpenAI whisper-large-v3, CTranslate2 conversion by Systran"),
    "speaker-diarization-community-1": (
        "CC BY 4.0 (gated)",
        "pyannoteAI speaker-diarization-community-1; accept terms to download"),
}


def manifest_markdown():
    """The model manifest as Markdown (docs/MODEL_MANIFEST.md is generated from this)."""
    lines = [
        "# Model integrity manifest",
        "",
        "Generated from the manifests in `asr_engines.py` by",
        "`python asr_engines.py manifest --out docs/MODEL_MANIFEST.md`;",
        "a unit test fails if this file and the code disagree.",
        "Every file must match its size and SHA-256 before a model counts as",
        "installed. Large-file hashes match Hugging Face's published LFS SHA-256;",
        "small files were checked against Hugging Face's git blob IDs.",
        "",
        "| Model | Licence | Source | Repository | Revision |",
        "| --- | --- | --- | --- | --- |",
    ]
    specs = [*PARAKEET_MODELS.values(), SILERO_VAD, *WHISPER_MODELS.values(), SPEAKER_MODEL]
    for s in specs:
        lic, src = LICENCES[s.name]
        lines.append(f"| {s.name} | {lic} | {src} | `{s.repo}` | `{s.revision}` |")
    for s in specs:
        lines += ["", f"## {s.name}", "", "| File | Bytes | SHA-256 |", "| --- | --- | --- |"]
        lines += [f"| `{f.name}` | {f.size} | `{f.sha256}` |" for f in s.files]
    return "\n".join(lines) + "\n"


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
    words = _token_words(start, end, tokens, timestamps)   # [(first_token_idx, word)]
    out, first, buf = [], 0, []

    def words_in(lo, hi):
        # By token position, not time: TDT can give several tokens one timestamp,
        # so a time filter could move a word into the neighbouring sentence.
        return tuple(w for i, w in words if lo <= i < hi)

    for i, tok in enumerate(tokens):
        buf.append(tok)
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok.rstrip().endswith((".", "?", "!")) and (nxt is None or nxt.startswith(" ")):
            seg_end = start + timestamps[i + 1] if nxt is not None else end
            sentence = join_tokens(buf)
            if sentence:
                out.append(Segment(start + timestamps[first], min(seg_end, end), sentence,
                                   words_in(first, i + 1)))
            first, buf = i + 1, []
    tail = join_tokens(buf)
    if tail:
        out.append(Segment(start + timestamps[first], end, tail, words_in(first, len(tokens))))
    return out


def _token_words(start, end, tokens, timestamps):
    """Group tokens into words: a token beginning with a space starts a new
    word, which ends where the next one starts. Returns
    [(index of the word's first token, (start, end, text))]."""
    starts = [i for i, t in enumerate(tokens) if i == 0 or t.startswith(" ")]
    words = []
    for n, i in enumerate(starts):
        j = starts[n + 1] if n + 1 < len(starts) else len(tokens)
        w_end = start + timestamps[j] if j < len(tokens) else end
        words.append((i, (start + timestamps[i], min(w_end, end), "".join(tokens[i:j]))))
    return words


class WhisperEngine:
    """faster-whisper behind the shared interface; settings are unchanged."""

    device = "cpu"

    def __init__(self, model, label, precision, language, beam_size=5, word_timestamps=False):
        self.model = model
        self.label = label
        self.precision = precision
        self.language = language
        self.beam_size = beam_size
        self.word_timestamps = word_timestamps   # the workspace needs word times

    @property
    def description(self):
        return f"{self.label} (faster-whisper, {self.precision}, {self.device})"

    def transcribe(self, audio):
        """`audio` is a path or a 16 kHz float32 waveform. Returns (segments, duration)."""
        if not self.word_timestamps:     # legacy app: settings unchanged
            segments, info = self.model.transcribe(
                audio, language=self.language, vad_filter=True, beam_size=self.beam_size)
            return (Segment(s.start, s.end, s.text.strip()) for s in segments), info.duration
        segments, info = self.model.transcribe(
            audio, language=self.language, vad_filter=True, beam_size=self.beam_size,
            word_timestamps=True)
        return (Segment(s.start, s.end, s.text.strip(),
                        tuple((w.start, w.end, w.word) for w in (s.words or ())))
                for s in segments), info.duration

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
            # Idle ONNX Runtime threads busy-wait by default. On hybrid CPUs
            # (performance and efficiency cores) that made more threads much
            # slower: 14 threads took 4x as long as 4 on a Core Ultra 7 255H.
            # Without spinning, extra threads no longer hurt, and idle CPU drops.
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
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
    parser = argparse.ArgumentParser(description="Install or repair speech models.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ensure = sub.add_parser("ensure", help="download missing / repair damaged models")
    ensure.add_argument("--model", action="append", default=[], choices=sorted(MANAGED_MODELS))
    ensure.add_argument("--present", action="store_true",
                        help="also verify and repair every model already on disk")
    ensure.add_argument("--default-parakeet", action="store_true",
                        help=f"install {RECOMMENDED_PARAKEET} only if no Parakeet model is on disk")
    ensure.add_argument("--cache", default=os.path.join(
        os.environ.get("LOCALAPPDATA", os.getcwd()), "whisper-models"))
    man = sub.add_parser("manifest", help="write the model integrity manifest as Markdown")
    man.add_argument("--out", help="file to write (UTF-8, LF); default prints it")
    args = parser.parse_args(argv)
    if args.cmd == "manifest":
        if args.out:   # written directly: PowerShell 5.1 '>' would produce UTF-16
            Path(args.out).write_text(manifest_markdown(), encoding="utf-8", newline="\n")
        else:
            sys.stdout.write(manifest_markdown())
        return 0

    def on_disk(spec):
        # A verified download whose final move failed counts as present too.
        return model_dir(spec, args.cache).exists() or staging_dir(spec, args.cache).exists()

    selected = list(dict.fromkeys(args.model))
    if args.default_parakeet and not any(on_disk(s) for s in PARAKEET_MODELS.values()):
        selected.append(RECOMMENDED_PARAKEET)
    extra = []
    if args.present:
        extra = [n for n, s in MANAGED_MODELS.items() if n not in selected
                 and (is_present(n, args.cache) or staging_dir(s, args.cache).exists())]
    if not selected and not extra:
        print("  No speech models selected or installed.")
        return 0
    failed_selected = failed_extra = False
    for name in selected + extra:
        try:
            results = install(name, args.cache, log=lambda m: print("  " + m))
            print(f"  {name}: {results[name]}")
        except Exception as exc:
            print(f"  {name}: FAILED - {exc}")
            if name in selected:
                failed_selected = True
            else:
                failed_extra = True
    # 1 = a model the user chose failed; 2 = only an already-present extra
    # model could not be repaired (the installer warns and carries on).
    return 1 if failed_selected else (2 if failed_extra else 0)


if __name__ == "__main__":
    # The installer's download step: telemetry off, but no network guard,
    # because downloading is the point.
    import offline_guard
    offline_guard.disable_telemetry()
    sys.exit(_main(sys.argv[1:]))
