# workspace/speaker_load.py
# Build the pyannote speaker pipeline from hash-checked bytes (#111 B9, #218).
#
# Left to itself, pyannote 4.0.4 reopens each model file by path after the
# app has hashed it, unpickles both checkpoints in full
# (torch.load(weights_only=False)) and imports whatever module a checkpoint
# names. Here, instead:
#   - the bytes come from asr_engines.read_verified, which read each pinned
#     file once and hashed exactly those bytes: no file is opened again;
#   - each checkpoint is checked before torch sees it: one pickle inside a
#     torch zip archive, importing only ALLOWED_GLOBALS, by no other route;
#   - torch.load runs with weights_only=True and a scoped allowlist of three
#     pyannote classes (torch.serialization.safe_globals, never the
#     process-wide add_safe_globals);
#   - the pipeline and model classes are fixed in code, never named by a file,
#     and the config, checkpoint metadata, specifications, hyperparameters and
#     PLDA arrays must be exactly the pinned revision's (EXPECTED_*); a config
#     with a leftover "$model/" entry, preprocessors or another pipeline name
#     is refused, because pyannote would resolve those against the working
#     folder or import them;
#   - PLDA arrays are read with np.load(allow_pickle=False).
# Any failed check or load is a ModelLoadError with fixed wording (P3): no
# path, no content. The libraries are imported first, outside that wrapper, so
# an import failure (a broken install, a CUDA library in the GPU worker) or a
# MemoryError keeps its own type: those are not safety failures.
#
# The SHA-256 pins (asr_engines.SPEAKER_MODEL) stay the main control. The
# weights-only allowlist cannot run code, but a hostile pickle could still
# set unusual Enum or Specifications values, which the exact checks here catch.
# A new model revision needs the EXPECTED_* values below updated with its pins.
#
# Imports torch, pyannote, numpy and yaml only inside functions: the module is
# imported by the worker, and by --self-test, never at window start-up.

import copy
import io
import pickle
import pickletools
import zipfile
from dataclasses import dataclass

from asr_engines import ModelLoadError

LOAD_FAILED = ("The speaker identification model did not pass its safety checks and was "
               "not loaded. Repair it in Setup.")

PIPELINE_NAME = "pyannote.audio.pipelines.SpeakerDiarization"

# The only globals a pinned checkpoint's pickle may name ("module name", as
# pickletools reports a GLOBAL opcode). The torch entries are what torch.save
# writes for tensors; torch's weights-only unpickler allows them by default.
ALLOWED_GLOBALS = frozenset({
    "collections OrderedDict",
    "torch._utils _rebuild_tensor_v2",
    "torch FloatStorage",
    "torch LongStorage",
    "pyannote.audio.core.task Problem",
    "pyannote.audio.core.task Resolution",
    "pyannote.audio.core.task Specifications",
})
# Opcodes that import or call by a route other than an allowlisted GLOBAL.
REFUSED_OPCODES = frozenset({"STACK_GLOBAL", "INST", "OBJ", "EXT1", "EXT2", "EXT4", "PERSID"})
MAX_PICKLE_BYTES = 1024 * 1024     # the pinned pickles are 7 KB and 29 KB

# config.yaml at the pinned revision, exactly. "$model/..." entries are
# replaced by the models built here, never resolved by pyannote.
EXPECTED_CONFIG = {
    "dependencies": {"pyannote.audio": "4.0.0"},
    "pipeline": {
        "name": PIPELINE_NAME,
        "params": {"clustering": "VBxClustering",
                   "segmentation": "$model/segmentation", "segmentation_batch_size": 32,
                   "embedding": "$model/embedding", "embedding_batch_size": 32,
                   "embedding_exclude_overlap": True,
                   "plda": "$model/plda"},
    },
    "params": {"clustering": {"threshold": 0.6, "Fa": 0.07, "Fb": 0.8},
               "segmentation": {"min_duration_off": 0.0}},
}
MODEL_SLOTS = {"segmentation": "$model/segmentation", "embedding": "$model/embedding",
               "plda": "$model/plda"}


@dataclass(frozen=True)
class ExpectedModel:
    file: str
    module: str              # imported here by this fixed name, never the checkpoint's
    cls: str
    metadata: dict           # every top-level entry except state_dict and pyannote.audio
    specifications: dict     # Specifications fields (and cached values), Enums by name


SEGMENTATION = ExpectedModel(
    file="segmentation/pytorch_model.bin",
    module="pyannote.audio.models.segmentation.PyanNet", cls="PyanNet",
    metadata={"pytorch-lightning_version": "2.0.2", "hparams_name": "kwargs",
              "hyper_parameters": {
                  "sample_rate": 16000, "num_channels": 1,
                  "sincnet": {"stride": 10, "sample_rate": 16000},
                  "lstm": {"hidden_size": 128, "num_layers": 4, "bidirectional": True,
                           "monolithic": True, "dropout": 0.5, "batch_first": True},
                  "linear": {"hidden_size": 128, "num_layers": 2}}},
    specifications={"problem": ("Problem", "MONO_LABEL_CLASSIFICATION"),
                    "resolution": ("Resolution", "FRAME"), "duration": 10.0,
                    "min_duration": None, "warm_up": (0.0, 0.0),
                    "classes": ["speaker#1", "speaker#2", "speaker#3"],
                    "powerset_max_classes": 2, "permutation_invariant": True,
                    "powerset": True, "num_powerset_classes": 7},
)
EMBEDDING = ExpectedModel(
    file="embedding/pytorch_model.bin",
    module="pyannote.audio.models.embedding.wespeaker", cls="WeSpeakerResNet34",
    metadata={"pytorch-lightning_version": "2.0.1.post0"},
    specifications={"problem": ("Problem", "REPRESENTATION"),
                    "resolution": ("Resolution", "CHUNK"), "duration": 5.0,
                    "min_duration": None, "warm_up": (0.0, 0.0), "classes": None,
                    "powerset_max_classes": None, "permutation_invariant": False,
                    "powerset": False},
)
PYANNOTE_VERSIONS = {"pyannote.audio": "4.0.0"}
# Specifications values pyannote caches on the instance (functools.cached_property).
CACHED_SPECIFICATIONS = frozenset({"powerset", "num_powerset_classes"})

# PLDA arrays: (file, {name: (dtype, shape)}).
EXPECTED_PLDA = (
    ("plda/xvec_transform.npz", {"mean1": ("<f8", (256,)), "mean2": ("<f4", (128,)),
                                 "lda": ("<f4", (256, 128))}),
    ("plda/plda.npz", {"mu": ("<f8", (128,)), "tr": ("<f8", (128, 128)),
                       "psi": ("<f8", (128,))}),
)


class Refused(ValueError):
    """A check failed. Its text is a fixed reason (never content or a path);
    callers turn it into ModelLoadError(LOAD_FAILED)."""


# --- Checks that need no machine-learning library ------------------------------

def _same(value, expected):
    """Equal, with the same types throughout (True is not 1, a list is not a tuple)."""
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return (value.keys() == expected.keys()
                and all(_same(value[k], expected[k]) for k in expected))
    if isinstance(expected, (list, tuple)):
        return len(value) == len(expected) and all(map(_same, value, expected, strict=True))
    return value == expected


def pickle_globals(data):
    """Every global the checkpoint's pickle names, without unpickling it.
    Refuses anything but a torch zip archive holding one small pickle, and a
    pickle that imports or calls by any route other than GLOBAL."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        raise Refused("not a zip checkpoint") from None
    pickles = [e for e in entries if e.filename.endswith("/data.pkl")]
    if len(pickles) != 1 or pickles[0].filename.count("/") != 1:
        raise Refused("not exactly one pickle in the checkpoint")
    if pickles[0].file_size > MAX_PICKLE_BYTES:
        raise Refused("checkpoint pickle too large")
    try:
        body = archive.read(pickles[0])
        found = set()
        for op, arg, _ in pickletools.genops(body):
            if op.name in REFUSED_OPCODES:
                raise Refused("checkpoint pickle imports by a refused route")
            if op.name == "GLOBAL":
                found.add(arg)
    except Refused:
        raise
    except Exception:          # truncated, malformed or a bad archive member
        raise Refused("checkpoint pickle is malformed") from None
    return found


def check_pickle(data):
    """Refuse a checkpoint whose pickle names a global outside ALLOWED_GLOBALS."""
    if not pickle_globals(data) <= ALLOWED_GLOBALS:
        raise Refused("checkpoint pickle names a global that is not allowed")


def check_config(config):
    """The pipeline config must be the pinned revision's, exactly."""
    if not isinstance(config, dict):
        raise Refused("config is not a mapping")
    if "preprocessors" in config:
        raise Refused("config has preprocessors")
    pipeline = config.get("pipeline")
    if not isinstance(pipeline, dict) or pipeline.get("name") != PIPELINE_NAME:
        raise Refused("unexpected pipeline name")
    slots = {("pipeline", "params", k): v for k, v in MODEL_SLOTS.items()}
    if any(text.startswith("$model") and slots.get(path) != text
           for path, text in _strings(config)):
        raise Refused("config has an unexpected $model entry")
    if not _same(config, EXPECTED_CONFIG):
        raise Refused("config is not the pinned one")


def _strings(value, path=()):
    """(path, text) for every string in a nested config, keys included (a
    key's path ends in None, so it never matches a model slot)."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield (*path, k, None), k
            yield from _strings(v, (*path, k))
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, (*path, None))


def check_checkpoint(checkpoint, expected, classes, tensor_type):
    """The unpickled checkpoint must hold tensors, the pinned metadata, the
    pinned architecture and the pinned specifications, and nothing else."""
    if not isinstance(checkpoint, dict):
        raise Refused("checkpoint is not a mapping")
    if set(checkpoint) != {"state_dict", "pyannote.audio", *expected.metadata}:
        raise Refused("checkpoint has unexpected entries")
    for key, value in expected.metadata.items():
        if not _same(checkpoint[key], value):
            raise Refused("checkpoint metadata or hyperparameters are not the pinned ones")
    state = checkpoint["state_dict"]
    if not isinstance(state, dict) or not state or not all(
            type(k) is str and isinstance(v, tensor_type) for k, v in state.items()):
        raise Refused("checkpoint weights are not plain tensors")
    meta = checkpoint["pyannote.audio"]
    if not isinstance(meta, dict) or set(meta) != {"versions", "architecture", "specifications"}:
        raise Refused("checkpoint has unexpected pyannote entries")
    if not _same(meta["versions"], PYANNOTE_VERSIONS):
        raise Refused("checkpoint version is not the pinned one")
    if not _same(meta["architecture"], {"module": expected.module, "class": expected.cls}):
        raise Refused("checkpoint names another architecture")
    check_specifications(meta["specifications"], expected, classes)


def check_specifications(spec, expected, classes):
    """Exact type, fields and values; Enums must be the real members."""
    problem, resolution, specifications = classes
    if type(spec) is not specifications:
        raise Refused("specifications have the wrong type")
    stored = vars(spec)
    if not set(stored) <= set(expected.specifications):
        raise Refused("specifications have unexpected attributes")
    enums = {"Problem": problem, "Resolution": resolution}
    for name, want in expected.specifications.items():
        if name not in stored:
            if name in CACHED_SPECIFICATIONS:
                continue                       # computed later from the checked fields
            value = getattr(type(spec), name, Refused)   # a field left at its default
        else:
            value = stored[name]
        if isinstance(want, tuple) and want and want[0] in enums:
            ok = value is enums[want[0]][want[1]]
        else:
            ok = _same(value, want)
        if not ok:
            raise Refused("specifications are not the pinned ones")


def read_plda_arrays(data, expected):
    """Check one PLDA file's arrays with np.load(allow_pickle=False)."""
    import numpy as np
    try:
        with np.load(io.BytesIO(data), allow_pickle=False) as arrays:
            if set(arrays.files) != set(expected):
                raise Refused("PLDA file has unexpected arrays")
            for name, (dtype, shape) in expected.items():
                array = arrays[name]
                if array.dtype.str != dtype or array.shape != shape:
                    raise Refused("PLDA array is not the pinned shape")
    except Refused:
        raise
    except Exception:          # not an npz, an object array, a damaged member
        raise Refused("PLDA file is malformed") from None


# --- Loading (torch and pyannote, in the worker) -------------------------------

def _classes():
    from pyannote.audio.core.task import Problem, Resolution, Specifications
    return Problem, Resolution, Specifications


def weights_only_load(data, classes):
    """torch.load of checked bytes, weights only, the three classes allowed
    only for this call."""
    import torch
    with torch.serialization.safe_globals(list(classes)):
        return torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)


def load_model(data, expected, classes):
    """One sub-model from its checked bytes, as the fixed class."""
    import importlib

    import torch
    check_pickle(data)
    check_checkpoint(weights_only_load(data, classes), expected, classes, torch.Tensor)
    klass = getattr(importlib.import_module(expected.module), expected.cls)
    with torch.serialization.safe_globals(list(classes)):
        model = klass.load_from_checkpoint(io.BytesIO(data), map_location="cpu",
                                           strict=True, weights_only=True)
    if type(model) is not klass:
        raise Refused("model has the wrong type")
    check_specifications(model.specifications, expected, classes)
    model.eval()
    return model


def import_libraries():
    """Import every library the build uses, before any check runs. Not
    wrapped: a broken install or, in the GPU worker, a missing CUDA, cuDNN or
    cuBLAS library keeps its own type and text, so the GPU worker's failure
    still falls back to the processor (D4, speakers._gpu_failure) and the
    worker log names it. The build's own imports then find these loaded."""
    import importlib
    for name in ("yaml", "numpy", "torch", "pyannote.audio.core.task",
                 "pyannote.audio.core.plda", "pyannote.audio.pipelines",
                 SEGMENTATION.module, EMBEDDING.module):
        importlib.import_module(name)


def build_pipeline(blobs):
    """The speaker pipeline from read_verified's bytes. A failed check, a
    refused unpickling or a model that does not build raises ModelLoadError
    with fixed wording (P3). Import failures and MemoryError are not safety
    failures and keep their own type."""
    import_libraries()
    try:
        return _build(blobs)
    except (ModelLoadError, MemoryError):
        raise
    except Exception as exc:
        # The text of the cause is never shown (P3). The worker log's incident
        # gets the cause's type name (diag: "type") and, through its
        # traceback, the frames down to the check or library call that failed.
        raise ModelLoadError(LOAD_FAILED).with_traceback(exc.__traceback__) from exc


def _build(blobs):
    import yaml
    config = yaml.safe_load(blobs["config.yaml"].decode("utf-8"))
    check_config(config)
    for name, expected in EXPECTED_PLDA:
        read_plda_arrays(blobs[name], expected)
    classes = _classes()
    from pyannote.audio.core.plda import PLDA
    from pyannote.audio.pipelines import SpeakerDiarization
    params = copy.deepcopy(EXPECTED_CONFIG["pipeline"]["params"])
    params["segmentation"] = load_model(blobs[SEGMENTATION.file], SEGMENTATION, classes)
    params["embedding"] = load_model(blobs[EMBEDDING.file], EMBEDDING, classes)
    params["plda"] = PLDA(io.BytesIO(blobs[EXPECTED_PLDA[0][0]]),
                          io.BytesIO(blobs[EXPECTED_PLDA[1][0]]))
    pipeline = SpeakerDiarization(**params)
    pipeline.instantiate(copy.deepcopy(EXPECTED_CONFIG["params"]))
    return pipeline


# --- --self-test ---------------------------------------------------------------

class _Payload:
    """Pickles as a call to eval: what a code-running checkpoint does. The
    expression is harmless if a check ever failed and it ran."""
    def __reduce__(self):
        return eval, ("0",)


def self_check():
    """[(ok, text)]: a synthetic code-running checkpoint is refused by the
    loader and by torch's weights-only unpickler, the allowlist ends with its
    scope, and TORCH_FORCE_WEIGHTS_ONLY_LOAD overrides weights_only=False.
    Needs torch and pyannote; reads and writes no file."""
    import os

    import torch
    classes = problem, resolution, specifications = _classes()
    spec = specifications(problem.MONO_LABEL_CLASSIFICATION, resolution.FRAME, 10.0)

    def checkpoint(extra):
        buf = io.BytesIO()
        torch.save({"state_dict": {"w": torch.zeros(2)},
                    "pyannote.audio": {"specifications": spec, "extra": extra}}, buf)
        return buf.getvalue()

    hostile, plain = checkpoint(_Payload()), checkpoint("plain")
    results = []

    def refused(label, attempt):
        try:
            attempt()
        except (Refused, ModelLoadError, pickle.UnpicklingError):
            results.append((True, label))
        except Exception as exc:
            results.append((False, f"{label} ({type(exc).__name__})"))
        else:
            results.append((False, label))

    refused("speaker model loader refuses a code-running checkpoint",
            lambda: load_model(hostile, SEGMENTATION, classes))
    refused("weights-only loading refuses a code-running checkpoint",
            lambda: weights_only_load(hostile, classes))
    try:
        loaded = weights_only_load(plain, classes)
        ok = type(loaded["pyannote.audio"]["specifications"]) is specifications
        results.append((ok, "weights-only loading reads a plain checkpoint"))
    except Exception as exc:
        results.append((False, f"weights-only loading reads a plain checkpoint "
                               f"({type(exc).__name__})"))
    refused("the pyannote allowlist ends with its scope",
            lambda: torch.load(io.BytesIO(plain), map_location="cpu", weights_only=True))
    saved = {k: os.environ.get(k) for k in ("TORCH_FORCE_WEIGHTS_ONLY_LOAD",
                                            "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD")}
    try:
        os.environ["TORCH_FORCE_WEIGHTS_ONLY_LOAD"] = "1"
        os.environ.pop("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", None)
        refused("TORCH_FORCE_WEIGHTS_ONLY_LOAD overrides weights_only=False",
                lambda: torch.load(io.BytesIO(hostile), map_location="cpu",
                                   weights_only=False))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return results
