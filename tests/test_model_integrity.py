# The speaker model loads only from hash-checked bytes, weights only (#111
# A4, B9, #218). pyannote's own loader reopens the files by path after they
# are hashed and unpickles them in full, so the workspace reads each pinned
# file once, hashes it and builds the pipeline from those bytes in memory.
#
# CI installs no torch or pyannote: these tests use stand-ins for them that
# record how they are called (the real libraries are exercised by --self-test
# and the evidence runs recorded on #218). The byte, config, checkpoint and
# PLDA checks are pure Python and run for real.
#   python -m unittest discover -s tests -v

import contextlib
import copy
import dataclasses
import enum
import hashlib
import importlib.abc
import importlib.util
import io
import json
import os
import pickle
import struct
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asr_engines  # noqa: E402
from workspace import speaker_load  # noqa: E402
from workspace.speaker_load import Refused  # noqa: E402

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None

# --- Which files are opened, from an audit hook (it cannot be removed, so it
# records only while a test switches it on) -----------------------------------

OPENED = []
_RECORDING = []


def _audit(event, args):
    if _RECORDING and event == "open" and args and isinstance(args[0], (str, bytes, Path)):
        OPENED.append(str(Path(args[0] if not isinstance(args[0], bytes)
                               else args[0].decode(errors="replace"))))


sys.addaudithook(_audit)


@contextlib.contextmanager
def recording_opens():
    OPENED.clear()
    _RECORDING.append(True)
    try:
        yield OPENED
    finally:
        _RECORDING.clear()


# --- Synthetic checkpoints (never unpickled here) -----------------------------

def archive(pickle_bytes, extra=(), zip64=True):
    """A torch-style zip checkpoint holding one pickle. Like torch.save, it
    ends with zip64 end records even though it is small (zip64=False keeps
    the plain layout Python's zipfile writes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("archive/data.pkl", pickle_bytes)
        z.writestr("archive/version", "3\n")
        for name, data in extra:
            z.writestr(name, data)
    return with_zip64_records(buf.getvalue()) if zip64 else buf.getvalue()


END = struct.Struct("<4sHHHHIIH")
ZIP64_LOCATOR = struct.Struct("<4sIQI")
ZIP64_END = struct.Struct("<4sQHHIIQQQQ")


def with_zip64_records(data):
    """Insert a zip64 end record and its locator before the end record, as
    torch.save writes them (the central directory does not move)."""
    *_, total, cd_size, cd_offset, _ = END.unpack_from(data, len(data) - END.size)
    record = ZIP64_END.pack(b"PK\x06\x06", ZIP64_END.size - 12, 45, 45, 0, 0,
                            total, total, cd_size, cd_offset)
    locator = ZIP64_LOCATOR.pack(b"PK\x06\x07", 0, len(data) - END.size, 1)
    return data[:-END.size] + record + locator + data[-END.size:]


def patched(data, at, fmt, value):
    """data with the field at offset at (negative: from the end) replaced."""
    at %= len(data)
    return data[:at] + struct.pack(fmt, value) + data[at + struct.calcsize(fmt):]


def count_one(data, record):
    """data with every entry count in its end records (this disk's and the
    total, in the end record and the zip64 one at record) set to 1."""
    for at, fmt in ((-END.size + 8, "<H"), (-END.size + 10, "<H"),
                    (record + 24, "<Q"), (record + 32, "<Q")):
        data = patched(data, at, fmt, 1)
    return data


def plain_pickle(tag):
    """Names only an allowed global (collections OrderedDict)."""
    import collections
    return pickle.dumps(collections.OrderedDict(tag=tag), protocol=2)


# PROTO 2, GLOBAL 'builtins eval', '0', TUPLE1, REDUCE, STOP: a code-running
# pickle, built from opcodes so nothing here could ever run it.
CODE_RUNNING = b"\x80\x02cbuiltins\neval\nX\x01\x00\x00\x000\x85R."


# --- Stand-ins for torch and pyannote -----------------------------------------

class Problem(enum.Enum):
    BINARY_CLASSIFICATION = 0
    MONO_LABEL_CLASSIFICATION = 1
    MULTI_LABEL_CLASSIFICATION = 2
    REPRESENTATION = 3
    REGRESSION = 4


class Resolution(enum.Enum):
    FRAME = 1
    CHUNK = 2


@dataclasses.dataclass
class Specifications:
    problem: Problem
    resolution: Resolution
    duration: float
    min_duration: float | None = None
    warm_up: tuple | None = (0.0, 0.0)
    classes: list | None = None
    powerset_max_classes: int | None = None
    permutation_invariant: bool = False


CLASSES = (Problem, Resolution, Specifications)


class Tensor:
    pass


def segmentation_spec():
    spec = Specifications(Problem.MONO_LABEL_CLASSIFICATION, Resolution.FRAME, 10.0,
                          classes=["speaker#1", "speaker#2", "speaker#3"],
                          powerset_max_classes=2, permutation_invariant=True)
    del spec.min_duration                 # as pickled: left at the class default
    spec.__dict__.update(powerset=True, num_powerset_classes=7)    # cached values
    return spec


def embedding_spec():
    return Specifications(Problem.REPRESENTATION, Resolution.CHUNK, 5.0)


def checkpoint(expected, spec):
    return {"state_dict": {"w": Tensor()},
            "pyannote.audio": {"versions": dict(speaker_load.PYANNOTE_VERSIONS),
                               "architecture": {"module": expected.module,
                                                "class": expected.cls},
                               "specifications": spec},
            **copy.deepcopy(expected.metadata)}


class FakeLibraries:
    """torch, pyannote and yaml stand-ins. torch.load returns the checkpoint
    registered for the exact bytes it is given."""

    def __init__(self):
        self.loads, self.from_checkpoint, self.plda, self.pipelines = [], [], [], []
        self.from_pretrained, self.add_safe_globals = [], []
        self.allowlist = None
        self.checkpoints = {}            # bytes -> checkpoint dict
        self.load_error = None
        self.on_load = None              # called with the load's number (from 1)
        fake = self

        torch = types.ModuleType("torch")
        torch.__version__ = "2.14.1+cpu"
        torch.Tensor = Tensor
        torch.set_num_threads = lambda n: None
        torch.from_numpy = lambda a: types.SimpleNamespace(unsqueeze=lambda dim: a)
        torch.device = lambda name: name
        torch.cuda = types.SimpleNamespace(is_available=lambda: False,
                                           empty_cache=lambda: None)

        @contextlib.contextmanager
        def safe_globals(classes):
            if fake.allowlist is not None:
                raise AssertionError("nested allowlist")
            fake.allowlist = tuple(classes)
            try:
                yield
            finally:
                fake.allowlist = None

        def load(f, map_location=None, weights_only=None, **kwargs):
            data = f.getvalue() if isinstance(f, io.BytesIO) else None
            fake.loads.append({"source": type(f).__name__, "data": data,
                               "weights_only": weights_only, "allowlist": fake.allowlist})
            if fake.on_load is not None:
                fake.on_load(len(fake.loads))
            if fake.load_error is not None:
                raise fake.load_error
            return copy.deepcopy(fake.checkpoints[data])

        torch.load = load
        torch.serialization = types.SimpleNamespace(
            safe_globals=safe_globals,
            add_safe_globals=lambda classes: fake.add_safe_globals.append(classes))

        class Model:
            @classmethod
            def load_from_checkpoint(cls, f, map_location=None, strict=None,
                                     weights_only=None, **kwargs):
                # Lightning unpickles the checkpoint again, through torch.load.
                fake.from_checkpoint.append({"cls": cls.__name__, "source": type(f).__name__,
                                             "strict": strict, "weights_only": weights_only})
                ckpt = torch.load(f, map_location=map_location, weights_only=weights_only)
                model = cls()
                model.specifications = ckpt["pyannote.audio"]["specifications"]
                return model

            def eval(self):
                self.evaluated = True
                return self

        class PyanNet(Model):
            pass

        class WeSpeakerResNet34(Model):
            pass

        self.PyanNet, self.WeSpeakerResNet34 = PyanNet, WeSpeakerResNet34

        class PLDA:
            def __init__(self, transform_npz, plda_npz):
                fake.plda.append((transform_npz, plda_npz))

        class Annotation:
            def itertracks(self, yield_label=False):
                yield types.SimpleNamespace(start=0.5, end=1.5), None, "SPEAKER_00"

        class SpeakerDiarization:
            def __init__(self, **params):
                fake.pipelines.append(self)
                self.params, self.instantiated, self.ran = params, None, []

            def instantiate(self, params):
                self.instantiated = params

            def to(self, device):
                return self

            def __call__(self, file, **kwargs):
                self.ran.append(kwargs)
                return types.SimpleNamespace(speaker_diarization=Annotation(),
                                             exclusive_speaker_diarization=Annotation())

        class Pipeline:
            @classmethod
            def from_pretrained(cls, checkpoint, **kwargs):
                fake.from_pretrained.append(checkpoint)
                return SpeakerDiarization()

        def module(name, **attrs):
            m = types.ModuleType(name)
            m.__dict__.update(attrs)
            return m

        yaml = module("yaml", safe_load=json.loads)      # the synthetic config is JSON
        self.modules = {
            "torch": torch, "yaml": yaml,
            "pyannote": module("pyannote"),
            "pyannote.audio": module("pyannote.audio", Pipeline=Pipeline),
            "pyannote.audio.core": module("pyannote.audio.core"),
            "pyannote.audio.core.task": module("pyannote.audio.core.task", Problem=Problem,
                                               Resolution=Resolution,
                                               Specifications=Specifications),
            "pyannote.audio.core.plda": module("pyannote.audio.core.plda", PLDA=PLDA),
            "pyannote.audio.models": module("pyannote.audio.models"),
            "pyannote.audio.models.segmentation": module("pyannote.audio.models.segmentation"),
            SEG.module: module(SEG.module, PyanNet=PyanNet),
            "pyannote.audio.models.embedding": module("pyannote.audio.models.embedding"),
            EMB.module: module(EMB.module, WeSpeakerResNet34=WeSpeakerResNet34),
            "pyannote.audio.pipelines": module("pyannote.audio.pipelines",
                                               SpeakerDiarization=SpeakerDiarization),
        }

    def installed(self):
        return mock.patch.dict(sys.modules, self.modules)


SEG, EMB = speaker_load.SEGMENTATION, speaker_load.EMBEDDING


def npz(arrays):
    import numpy as np
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    return buf.getvalue()


def plda_files():
    import numpy as np
    out = {}
    for name, expected in speaker_load.EXPECTED_PLDA:
        out[name] = npz({k: np.zeros(shape, dtype=dtype) for k, (dtype, shape) in expected.items()})
    return out


class SyntheticModel:
    """A cache holding a synthetic speaker model, pinned like the real one."""

    def __init__(self, root, config=None, segmentation=None):
        self.cache = Path(root)
        self.files = {
            "config.yaml": json.dumps(config or speaker_load.EXPECTED_CONFIG).encode(),
            SEG.file: segmentation or archive(plain_pickle("segmentation")),
            EMB.file: archive(plain_pickle("embedding")),
            **plda_files(),
        }
        self.folder = asr_engines.model_dir(asr_engines.SPEAKER_MODEL, self.cache)
        for name, data in self.files.items():
            (self.folder / name).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / name).write_bytes(data)
        real = asr_engines.SPEAKER_MODEL
        self.spec = dataclasses.replace(real, files=tuple(
            asr_engines.ModelFile(name, len(data), hashlib.sha256(data).hexdigest())
            for name, data in self.files.items()))
        self.paths = {str(self.folder / name) for name in self.files}

    def pinned(self):
        return mock.patch.object(asr_engines, "SPEAKER_MODEL", self.spec)

    def libraries(self):
        fake = FakeLibraries()
        fake.checkpoints = {self.files[SEG.file]: checkpoint(SEG, segmentation_spec()),
                            self.files[EMB.file]: checkpoint(EMB, embedding_spec())}
        return fake


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class SpeakerModelLoadTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = Path(self.tmp.name) / "audio.f32"
        np.zeros(16000, dtype="<f4").tofile(self.audio)

    def diarize(self, model, fake):
        from workspace import engines
        with model.pinned(), fake.installed():
            return engines.diarize_file("pyannote", str(model.cache), str(self.audio), None, 1)

    def test_speaker_model_loads_only_the_hash_checked_bytes(self):
        # The I1 guard (AGENTS.md): every pinned file is read once and hashed,
        # and the pipeline is built from exactly those bytes, weights only.
        model = SyntheticModel(Path(self.tmp.name) / "cache")
        fake = model.libraries()
        checked = []
        real_read = asr_engines.read_verified

        def read_verified(spec, cache_dir):
            blobs = real_read(spec, cache_dir)
            checked.append(len(OPENED))          # opens up to the end of the check
            return blobs

        with recording_opens() as opened, \
                mock.patch.object(asr_engines, "read_verified", side_effect=read_verified):
            turns, exclusive, device = self.diarize(model, fake)
        self.assertEqual((turns, device), ([(0.5, 1.5, "SPEAKER_00")], "cpu"))

        model_opens = [p for p in opened if p in model.paths]
        self.assertEqual(sorted(model_opens), sorted(model.paths))     # each file once
        self.assertEqual(len(checked), 1)
        self.assertFalse([p for p in opened[checked[0]:] if p in model.paths],
                         "a model file was opened again after its check")

        # pyannote's own loader, which reads by path, is never used.
        self.assertEqual(fake.from_pretrained, [])
        # Every unpickling: the checked bytes, from memory, weights only, with
        # exactly the three pyannote classes allowed.
        self.assertEqual(len(fake.loads), 4)           # check, then Lightning, per model
        for call in fake.loads:
            self.assertEqual(call["source"], "BytesIO")
            self.assertIs(call["weights_only"], True)
            self.assertEqual(call["allowlist"], CLASSES)
            self.assertIn(call["data"], (model.files[SEG.file], model.files[EMB.file]))
        self.assertEqual([(c["cls"], c["source"], c["strict"], c["weights_only"])
                          for c in fake.from_checkpoint],
                         [("PyanNet", "BytesIO", True, True),
                          ("WeSpeakerResNet34", "BytesIO", True, True)])
        # PLDA arrays from the checked bytes too.
        (transform, plda), = fake.plda
        self.assertEqual((transform.getvalue(), plda.getvalue()),
                         (model.files["plda/xvec_transform.npz"], model.files["plda/plda.npz"]))
        # The pipeline class and its models are fixed in code.
        pipeline, = fake.pipelines
        self.assertIs(type(pipeline.params["segmentation"]), fake.PyanNet)
        self.assertIs(type(pipeline.params["embedding"]), fake.WeSpeakerResNet34)
        self.assertEqual({k: v for k, v in pipeline.params.items()
                          if k not in ("segmentation", "embedding", "plda")},
                         {k: v for k, v in speaker_load.EXPECTED_CONFIG["pipeline"]["params"]
                          .items() if not str(v).startswith("$model")})
        self.assertEqual(pipeline.instantiated, speaker_load.EXPECTED_CONFIG["params"])

    def test_the_allowlist_is_scoped(self):
        model = SyntheticModel(Path(self.tmp.name) / "cache")
        fake = model.libraries()
        self.diarize(model, fake)
        self.assertEqual(fake.add_safe_globals, [])     # never process-wide
        self.assertIsNone(fake.allowlist)                # ended with its scope
        self.assertTrue(fake.loads)
        self.assertTrue(all(c["allowlist"] == CLASSES for c in fake.loads))

    def test_speaker_model_is_not_loaded_without_a_full_check(self):
        model = SyntheticModel(Path(self.tmp.name) / "cache")
        fake = model.libraries()
        with mock.patch.object(asr_engines, "read_verified", return_value=None) as check:
            with self.assertRaises(asr_engines.ModelLoadError):
                self.diarize(model, fake)
        check.assert_called_once_with(model.spec, str(model.cache))
        self.assertEqual((fake.loads, fake.pipelines, fake.from_pretrained), ([], [], []))

    def test_a_changed_file_is_refused_before_anything_loads(self):
        model = SyntheticModel(Path(self.tmp.name) / "cache")
        fake = model.libraries()
        path = model.folder / SEG.file
        data = bytearray(path.read_bytes())
        data[-1] ^= 1                                    # same size, different bytes
        path.write_bytes(bytes(data))
        with self.assertRaises(asr_engines.ModelLoadError):
            self.diarize(model, fake)
        self.assertEqual((fake.loads, fake.pipelines), ([], []))

    def test_a_code_running_checkpoint_is_refused(self):
        # Even with pins that match it (a pin changed by mistake), a checkpoint
        # whose pickle would run code never reaches torch.load.
        model = SyntheticModel(Path(self.tmp.name) / "cache",
                               segmentation=archive(CODE_RUNNING))
        fake = model.libraries()
        with self.assertRaises(asr_engines.ModelLoadError) as caught:
            self.diarize(model, fake)
        self.assertEqual(str(caught.exception), speaker_load.LOAD_FAILED)
        self.assertEqual((fake.loads, fake.pipelines), ([], []))

    def test_a_checkpoint_that_changes_a_shared_enum_is_refused(self):
        # torch's weights-only unpickler may call and BUILD an allowed class:
        # Problem(1) is the module's own member, and BUILD can then set its
        # attributes for the whole worker. The specifications check compares
        # members by identity, so it would not notice. Such a pickle passes
        # the pickle check; the stand-in torch.load applies its BUILD.
        # PROTO 2, GLOBAL Problem, (1,), REDUCE, {"_value_": 99}, BUILD, STOP.
        edit = (b"\x80\x02cpyannote.audio.core.task\nProblem\nK\x01\x85R"
                b"}X\x07\x00\x00\x00_value_Kcsb.")
        speaker_load.check_pickle(archive(edit))
        member = Problem.MONO_LABEL_CLASSIFICATION
        original = dict(vars(member))

        def restore():
            object.__setattr__(member, "__class__", Problem)
            vars(member).clear()
            vars(member).update(original)
        self.addCleanup(restore)

        def swap_class():
            # BUILD's slotstate is applied with setattr, so a pickle can set
            # __class__ too; that change is outside the member's __dict__.
            object.__setattr__(member, "__class__", Resolution)

        for label, at in (("the check's unpickling", 1), ("Lightning's unpickling", 2),
                          ("an attribute added", 1), ("its class swapped", 1)):
            with self.subTest(label):
                model = SyntheticModel(Path(self.tmp.name) / label, segmentation=archive(edit))
                fake = model.libraries()
                if label == "its class swapped":
                    fake.on_load = lambda n, at=at: swap_class() if n == at else None
                else:
                    change = ({"added": True} if label == "an attribute added"
                              else {"_value_": 99})
                    fake.on_load = lambda n, at=at, change=change: (
                        vars(member).update(change) if n == at else None)
                try:
                    with self.assertRaises(asr_engines.ModelLoadError) as caught:
                        self.diarize(model, fake)
                    self.assertEqual(str(caught.exception), speaker_load.LOAD_FAILED)
                    self.assertEqual(fake.pipelines, [])
                    # The member is put back as it was, for the rest of the worker.
                    self.assertIs(type(member), Problem)
                    self.assertEqual(vars(member), original)
                finally:
                    restore()

    def test_failures_have_fixed_wording(self):
        # P3: an unpickling error's text (which can hold anything) is not shown.
        model = SyntheticModel(Path(self.tmp.name) / "cache")
        fake = model.libraries()
        fake.load_error = pickle.UnpicklingError(r"Unsupported global at C:\Users\someone\x")
        with self.assertRaises(asr_engines.ModelLoadError) as caught:
            self.diarize(model, fake)
        self.assertEqual(str(caught.exception), speaker_load.LOAD_FAILED)
        self.assertEqual(fake.pipelines, [])

    def test_a_checkpoint_naming_another_architecture_is_refused(self):
        model = SyntheticModel(Path(self.tmp.name) / "cache")
        fake = model.libraries()
        ckpt = fake.checkpoints[model.files[SEG.file]]
        ckpt["pyannote.audio"]["architecture"] = {"module": "os", "class": "system"}
        with self.assertRaises(asr_engines.ModelLoadError):
            self.diarize(model, fake)
        self.assertEqual((fake.from_checkpoint, fake.pipelines), ([], []))

    def test_an_unexpected_config_is_refused(self):
        config = copy.deepcopy(speaker_load.EXPECTED_CONFIG)
        config["preprocessors"] = {"audio": "./database.yml"}
        model = SyntheticModel(Path(self.tmp.name) / "cache", config=config)
        fake = model.libraries()
        with self.assertRaises(asr_engines.ModelLoadError):
            self.diarize(model, fake)
        self.assertEqual((fake.loads, fake.pipelines), ([], []))

    def test_a_gpu_library_failing_to_import_still_falls_back(self):
        # D4: in the GPU worker a CUDA library that fails to load while
        # pyannote is imported keeps its own words, so speakers._gpu_failure
        # sees a graphics card failure and the pass reruns on the processor.
        # It is not reported as a failed safety check.
        from workspace import speakers
        from workspace.worker import WorkerError, safe_message
        model = SyntheticModel(Path(self.tmp.name) / "cache")
        fake = model.libraries()
        name = "pyannote.audio.pipelines"
        del fake.modules[name]
        fake.modules["pyannote.audio"].__path__ = []        # a package, as the real one is
        error = OSError("[WinError 126] Error loading cudnn64_9.dll or one of its dependencies.")
        with mock.patch.object(sys, "meta_path", [FailingImport(name, error), *sys.meta_path]):
            with self.assertRaises(OSError) as caught:
                self.diarize(model, fake)
        exc = caught.exception
        self.assertNotIsInstance(exc, asr_engines.ModelLoadError)
        # As the worker reports it to the app (worker.WorkerHandle._request).
        reported = WorkerError(f"{type(exc).__name__}: {safe_message(exc)}")
        self.assertTrue(speakers._gpu_failure(reported))
        self.assertEqual((fake.loads, fake.pipelines), ([], []))    # before any check

    def test_running_out_of_memory_is_not_called_a_safety_failure(self):
        model = SyntheticModel(Path(self.tmp.name) / "cache")
        fake = model.libraries()
        fake.load_error = MemoryError()
        with self.assertRaises(MemoryError) as caught:
            self.diarize(model, fake)
        self.assertNotIsInstance(caught.exception, asr_engines.ModelLoadError)

    def test_the_worker_log_says_which_check_failed(self):
        # The message is fixed (P3), but the worker's incident record keeps
        # the cause's type name and the frames down to the failed check.
        from workspace import diag
        model = SyntheticModel(Path(self.tmp.name) / "cache")
        fake = model.libraries()
        ckpt = fake.checkpoints[model.files[SEG.file]]
        ckpt["pyannote.audio"]["architecture"] = {"module": "os", "class": "system"}
        logs = Path(self.tmp.name) / "logs"
        try:                        # assertRaises would drop the traceback
            self.diarize(model, fake)
        except asr_engines.ModelLoadError as exc:
            diag.Log(logs, "worker").incident("worker", exc, op="diarize")
        [record] = [json.loads(line) for path in diag.files(logs)
                    for line in path.read_text(encoding="ascii").splitlines()]
        self.assertEqual((record["error"], record["type"]), ("ModelLoadError", "Refused"))
        self.assertEqual(record["frames"][-1][::2],
                         ["workspace/speaker_load.py", "check_checkpoint"])
        self.assertNotIn("system", json.dumps(record))

    def test_the_gpu_trial_uses_the_apps_loader(self):
        # tools/gpu_trial.py is the CPU against GPU agreement check
        # (docs/RELEASE_CHECKLIST.md): it must load the speaker model as the
        # app does, or its evidence does not cover the app's path (E1).
        import numpy as np
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        self.addCleanup(sys.path.remove, sys.path[0])
        import gpu_trial
        model = SyntheticModel(Path(self.tmp.name) / "cache")
        fake = model.libraries()
        env = {"TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD": "1"}
        with model.pinned(), fake.installed(), mock.patch.dict(os.environ, env):
            turns, _ = gpu_trial.pyannote(str(model.cache), np.zeros(16000, "<f4"), "cpu")
            self.assertEqual(os.environ.get("TORCH_FORCE_WEIGHTS_ONLY_LOAD"), "1")
            self.assertNotIn("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", os.environ)
        self.assertEqual(turns, [(0.5, 1.5, "SPEAKER_00")])
        self.assertEqual(fake.from_pretrained, [])
        self.assertEqual(len(fake.loads), 4)
        self.assertTrue(all(c["source"] == "BytesIO" and c["weights_only"] is True
                            for c in fake.loads))


# --- --self-test, against a weights-only torch stand-in -----------------------

class WeightsOnlyTorch:
    """A torch stand-in for speaker_load.self_check (the packaged build runs it
    against the real torch). save writes a torch-style archive with the
    standard pickler, protocol 2 as torch.save uses. load reads it back the
    way torch's weights-only unpickler does: it imports only
    collections.OrderedDict and the classes allowed for that call
    (safe_globals), and BUILD puts a dict state into the object's __dict__ and
    applies slot state with setattr. weights_only=False is weights-only when
    TORCH_FORCE_WEIGHTS_ONLY_LOAD is set, and is never unpickled in full here."""

    def __init__(self):
        import collections
        self.allowed = ()
        fake = self

        @contextlib.contextmanager
        def safe_globals(classes):
            saved, fake.allowed = fake.allowed, tuple(classes)
            try:
                yield
            finally:
                fake.allowed = saved

        class Unpickler(pickle.Unpickler):
            def find_class(self, module, name):
                for klass in (collections.OrderedDict, *fake.allowed):
                    if (klass.__module__, klass.__qualname__) == (module, name):
                        return klass
                raise pickle.UnpicklingError("Unsupported global")

        def save(obj, f):
            f.write(archive(pickle.dumps(obj, protocol=2)))

        def load(f, map_location=None, weights_only=None):
            if not (weights_only or os.environ.get("TORCH_FORCE_WEIGHTS_ONLY_LOAD") == "1"):
                raise AssertionError("a full unpickling in a test")
            with zipfile.ZipFile(f) as z:
                return Unpickler(io.BytesIO(z.read("archive/data.pkl"))).load()

        self.module = types.ModuleType("torch")
        self.module.__dict__.update(
            Tensor=Tensor, zeros=lambda n: [0.0] * n, save=save, load=load,
            serialization=types.SimpleNamespace(safe_globals=safe_globals))


def attributes_only(enums, load):
    """enums_unchanged without its class comparison: the bug --self-test must
    show (#231)."""
    saved = {member: dict(vars(member)) for enum_ in enums for member in enum_}
    try:
        return load()
    finally:
        changed = False
        for member, attrs in saved.items():
            if vars(member) != attrs:
                vars(member).clear()
                vars(member).update(attrs)
                changed = True
        if changed:
            raise Refused(speaker_load.ENUM_CHANGED)


class SelfTestTests(unittest.TestCase):
    ATTRIBUTES = "changes an Enum member's attributes"
    CLASS = "changes an Enum member's class"

    def self_check(self):
        """speaker_load.self_check's [(ok, text)], run against the stand-ins
        (named as pyannote's classes, so the pickle check accepts them). Every
        run must leave the shared member as it was, whatever it reports."""
        member = Problem.MONO_LABEL_CLASSIFICATION
        original = dict(vars(member))
        modules = {**FakeLibraries().modules, "torch": WeightsOnlyTorch().module}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(sys.modules, modules))
            stack.enter_context(mock.patch.dict(os.environ))
            for klass in CLASSES:
                stack.enter_context(mock.patch.object(klass, "__module__",
                                                      "pyannote.audio.core.task"))
            results = speaker_load.self_check()
        self.assertIs(type(member), Problem)
        self.assertEqual(vars(member), original)
        return results

    def test_the_self_test_passes(self):
        results = self.self_check()
        self.assertEqual([text for ok, text in results if not ok], [])
        for change in (self.ATTRIBUTES, self.CLASS):
            self.assertEqual(sum(change in text for _, text in results), 1, change)

    def test_the_self_test_shows_a_broken_enum_check(self):
        # A packaged build's --self-test must catch a loader that lets a
        # checkpoint change a shared member: each broken check fails its line,
        # and the self-test still puts the member back (#231).
        for label, broken, failing in (
                ("class not compared", attributes_only, [self.CLASS]),
                ("nothing compared", lambda enums, load: load(),
                 [self.ATTRIBUTES, self.CLASS])):
            with self.subTest(label), mock.patch.object(speaker_load, "enums_unchanged", broken):
                results = self.self_check()
                failed = [text for ok, text in results if not ok]
                self.assertEqual(len(failed), len(failing), failed)
                for change, text in zip(failing, failed, strict=True):
                    self.assertIn(change, text)


class FailingImport(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Importing `name` raises `error`, as a native library failing to load does."""

    def __init__(self, name, error):
        self.name, self.error = name, error

    def find_spec(self, fullname, path=None, target=None):
        return importlib.util.spec_from_loader(fullname, self) if fullname == self.name else None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        raise self.error


class PickleCheckTests(unittest.TestCase):
    def test_a_checkpoint_naming_only_allowed_globals_passes(self):
        speaker_load.check_pickle(archive(plain_pickle("x")))

    def test_a_code_running_pickle_is_refused(self):
        with self.assertRaises(Refused):
            speaker_load.check_pickle(archive(CODE_RUNNING))

    def test_other_import_routes_are_refused(self):
        import collections
        stack_global = pickle.dumps(collections.OrderedDict(), protocol=4)   # STACK_GLOBAL
        inst = b"(ibuiltins\neval\n."                                        # INST
        ext = b"\x80\x02\x82\x01."                                           # EXT1
        for body in (stack_global, inst, ext):
            with self.subTest(body=body[:12]), self.assertRaises(Refused):
                speaker_load.check_pickle(archive(body))

    def test_anything_but_one_small_pickle_in_a_zip_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a/b/data.pkl", plain_pickle("b"))
        nested = buf.getvalue()
        cases = {
            "not a zip": CODE_RUNNING,
            "legacy format": b"\x80\x02}q\x00.",
            "two pickles": archive(plain_pickle("a"), [("other/data.pkl", plain_pickle("b"))]),
            "nested pickle": nested,
            "malformed": archive(b"\x80\x02cbuiltins\n"),
            "too large": archive(b"\x80\x02" + b"N0" * (speaker_load.MAX_PICKLE_BYTES // 2)
                                 + b"."),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(Refused):
                speaker_load.check_pickle(data)

    def test_entry_names_torch_could_resolve_differently_are_refused(self):
        # torch finds "<root>/data.pkl" case-insensitively under the first
        # entry's root, and Python's ZipInfo.filename cuts a name at a NUL and
        # turns a backslash into "/". Each archive below holds an allowed
        # pickle where Python looks and a code-running one torch could read.
        def renamed(placeholder, raw):
            # Entry names are not covered by the CRC, so the raw bytes of a
            # same-length placeholder can be swapped in both headers.
            data = archive(plain_pickle("decoy"), [(placeholder, CODE_RUNNING)])
            self.assertEqual(data.count(placeholder.encode()), 2)
            return data.replace(placeholder.encode(), raw)

        cases = {
            "upper-case duplicate": archive(plain_pickle("decoy"),
                                            [("archive/DATA.PKL", CODE_RUNNING)]),
            "duplicate name": renamed("archive/data.pkX", b"archive/data.pkl"),
            "NUL in a name": renamed("archive/data.pkl_x", b"archive/data.pkl\x00x"),
            "backslash in a name": renamed("archive/zz.pkl", b"archive\\zz.pkl"),
            "non-ASCII name": renamed("archive/zz.pkl", b"archive/\xc3\xa9.pkl"),
            "two roots": archive(plain_pickle("decoy"), [("other/version", "3\n")]),
            "entry outside any root": archive(plain_pickle("decoy"), [("data.pkl", CODE_RUNNING)]),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(Refused):
                speaker_load.check_pickle(data)
        # The torch layout itself passes.
        speaker_load.check_pickle(archive(plain_pickle("x"), [
            ("archive/byteorder", "little"), ("archive/data/0", b"\0" * 8),
            ("archive/.format_version", "1"), ("archive/.storage_alignment", "64"),
            ("archive/.data/serialization_id", "0123456789")]))


    def test_an_archive_python_and_torch_could_read_differently_is_refused(self):
        # Python's zip reader adjusts every offset for data placed before the
        # archive; torch's reader uses the offsets as written. So of a code-
        # running archive followed by an allowed one, Python reads the allowed
        # one and torch the other. In each case Python finds an allowed pickle.
        decoy = archive(plain_pickle("decoy"))
        record = -END.size - ZIP64_LOCATOR.size - ZIP64_END.size
        locator = -END.size - ZIP64_LOCATOR.size
        with_comment = io.BytesIO()
        with zipfile.ZipFile(with_comment, "w") as z:
            z.writestr("archive/data.pkl", plain_pickle("decoy"))
            z.comment = b"x"
        cases = {
            "an archive prepended": archive(CODE_RUNNING) + decoy,
            "an archive prepended, plain layout": (
                archive(CODE_RUNNING, zip64=False)
                + archive(plain_pickle("decoy"), zip64=False)),
            "data before the archive": b"\0" * 64 + decoy,
            "data after the end record": decoy + b"\0" * 64,
            "an archive comment": with_comment.getvalue(),
            "zip64 locator points elsewhere": patched(decoy, locator + 8, "<Q", 0),
            "zip64 record disagrees with the end record":
                patched(decoy, record + 48, "<Q", 0),
            "zip64 record claims extra data": patched(decoy, record + 4, "<Q", 52),
            "end record disagrees with the zip64 record":
                patched(decoy, -END.size + 16, "<I", 0),
            # Every count says one entry (so the records agree with each
            # other); only the central directory, which lists two, disagrees.
            "an entry the end records do not count": count_one(decoy, record),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(Refused):
                speaker_load.check_pickle(data)
        # Both layouts pass with nothing outside the archive.
        speaker_load.check_pickle(decoy)
        speaker_load.check_pickle(archive(plain_pickle("x"), zip64=False))


class ConfigCheckTests(unittest.TestCase):
    def changed(self, change):
        config = copy.deepcopy(speaker_load.EXPECTED_CONFIG)
        change(config)
        return config

    def test_the_pinned_config_passes(self):
        speaker_load.check_config(copy.deepcopy(speaker_load.EXPECTED_CONFIG))

    def test_unexpected_configs_are_refused(self):
        def params(config):
            return config["pipeline"]["params"]
        cases = {
            "leftover $model entry": lambda c: params(c).update(
                der_variant="$model/elsewhere"),
            "$model entry moved": lambda c: c["params"]["segmentation"].update(
                min_duration_off="$model/segmentation"),
            "$model slot pointing elsewhere": lambda c: params(c).update(
                segmentation="$model/embedding"),
            "preprocessors": lambda c: c.update(preprocessors={"audio": "db.yml"}),
            "another pipeline": lambda c: c["pipeline"].update(name="os.system"),
            "freeze": lambda c: c.update(freeze={"clustering": {}}),
            "extra parameter": lambda c: params(c).update(token="x"),
            "changed value": lambda c: c["params"]["clustering"].update(threshold=0.5),
            "changed type": lambda c: params(c).update(embedding_batch_size=32.0),
            "bool for int": lambda c: params(c).update(segmentation_batch_size=True),
        }
        for label, change in cases.items():
            with self.subTest(label), self.assertRaises(Refused):
                speaker_load.check_config(self.changed(change))
        for config in (None, [], "pipeline"):
            with self.subTest(config=config), self.assertRaises(Refused):
                speaker_load.check_config(config)


class CheckpointCheckTests(unittest.TestCase):
    def check(self, ckpt, expected=SEG):
        speaker_load.check_checkpoint(ckpt, expected, CLASSES, Tensor)

    def test_the_pinned_structure_passes(self):
        self.check(checkpoint(SEG, segmentation_spec()))
        self.check(checkpoint(EMB, embedding_spec()), EMB)

    def test_unexpected_checkpoints_are_refused(self):
        def meta(c):
            return c["pyannote.audio"]

        def hparams(c):
            return c["hyper_parameters"]
        cases = {
            "another architecture": lambda c: meta(c)["architecture"].update(module="os"),
            "another class": lambda c: meta(c)["architecture"].update({"class": "SSeRiouSS"}),
            "extra top-level entry": lambda c: c.update(callbacks={}),
            "extra pyannote entry": lambda c: meta(c).update(extra=1),
            "another version": lambda c: meta(c).update(versions={"pyannote.audio": "9"}),
            "changed hyperparameter": lambda c: hparams(c)["lstm"].update(num_layers=5),
            "extra hyperparameter": lambda c: hparams(c).update(task="x"),
            "missing hyperparameter": lambda c: hparams(c).pop("linear"),
            "weights not tensors": lambda c: c["state_dict"].update(w="text"),
            "no weights": lambda c: c.update(state_dict={}),
            "specifications another type": lambda c: meta(c).update(
                specifications=types.SimpleNamespace(**vars(segmentation_spec()))),
            "another problem": lambda c: setattr(meta(c)["specifications"], "problem",
                                                 Problem.REGRESSION),
            "an Enum value, not the member": lambda c: setattr(
                meta(c)["specifications"], "resolution", 1),
            "extra attribute": lambda c: setattr(meta(c)["specifications"], "hook", "x"),
            "changed cached value": lambda c: setattr(meta(c)["specifications"],
                                                      "num_powerset_classes", 8),
            "changed classes": lambda c: meta(c)["specifications"].classes.append("x"),
            "list for tuple": lambda c: setattr(meta(c)["specifications"], "warm_up",
                                                [0.0, 0.0]),
        }
        for label, change in cases.items():
            ckpt = checkpoint(SEG, segmentation_spec())
            change(ckpt)
            with self.subTest(label), self.assertRaises(Refused):
                self.check(ckpt)
        for ckpt in (None, [], "x"):
            with self.subTest(ckpt=ckpt), self.assertRaises(Refused):
                self.check(ckpt)

    def test_a_missing_required_field_is_refused(self):
        ckpt = checkpoint(EMB, embedding_spec())
        del ckpt["pyannote.audio"]["specifications"].duration
        with self.assertRaises(Refused):
            self.check(ckpt, EMB)


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class PldaCheckTests(unittest.TestCase):
    def test_the_pinned_arrays_pass(self):
        for name, expected in speaker_load.EXPECTED_PLDA:
            speaker_load.read_plda_arrays(plda_files()[name], expected)

    def test_unexpected_arrays_are_refused(self):
        import numpy as np
        name, expected = speaker_load.EXPECTED_PLDA[1]
        good = {k: np.zeros(shape, dtype=dtype) for k, (dtype, shape) in expected.items()}
        cases = {
            "object array (needs pickle)": {**good, "mu": np.array([{"a": 1}], dtype=object)},
            "another dtype": {**good, "psi": good["psi"].astype("<f4")},
            "another shape": {**good, "tr": np.zeros((2, 2))},
            "extra array": {**good, "x": np.zeros(1)},
            "missing array": {k: v for k, v in good.items() if k != "tr"},
        }
        for label, arrays in cases.items():
            with self.subTest(label), self.assertRaises(Refused):
                speaker_load.read_plda_arrays(npz(arrays), expected)
        with self.assertRaises(Refused):
            speaker_load.read_plda_arrays(b"not an npz", expected)

    def test_arrays_are_read_without_pickle(self):
        import numpy as np
        name, expected = speaker_load.EXPECTED_PLDA[0]
        with mock.patch.object(np, "load", wraps=np.load) as load:
            speaker_load.read_plda_arrays(plda_files()[name], expected)
        (source,), kwargs = load.call_args
        self.assertIsInstance(source, io.BytesIO)
        self.assertIs(kwargs.get("allow_pickle"), False)


class ReadVerifiedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.files = {"a.bin": b"alpha", "b/c.bin": b"bravo"}
        self.spec = asr_engines.ModelSpec(
            name="m", folder="m", repo="r/m", revision="1", family="pyannote",
            files=tuple(asr_engines.ModelFile(n, len(d), hashlib.sha256(d).hexdigest())
                        for n, d in self.files.items()))
        self.folder = asr_engines.model_dir(self.spec, self.cache)
        for name, data in self.files.items():
            (self.folder / name).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / name).write_bytes(data)

    def test_each_file_is_read_once_and_its_bytes_returned(self):
        with recording_opens() as opened:
            blobs = asr_engines.read_verified(self.spec, self.cache)
        self.assertEqual(blobs, self.files)
        paths = [str(self.folder / n) for n in self.files]
        self.assertEqual(sorted(p for p in opened if p in paths), sorted(paths))

    def test_a_marker_does_not_replace_reading_the_bytes(self):
        asr_engines.check_files(self.spec, self.folder)          # writes a good marker
        self.assertEqual(asr_engines.read_verified(self.spec, self.cache), self.files)

    def test_a_changed_or_grown_file_is_refused(self):
        for data in (b"alphA", b"alpha!"):
            with self.subTest(data=data):
                path = self.folder / "a.bin"
                path.write_bytes(data)
                with mock.patch.object(asr_engines, "_quick_problems", return_value=[]):
                    self.assertIsNone(asr_engines.read_verified(self.spec, self.cache))
                marker = json.loads((self.folder / asr_engines.VERIFIED_MARKER)
                                    .read_text(encoding="utf-8"))
                self.assertFalse(marker["ok"])
                path.write_bytes(b"alpha")


if __name__ == "__main__":
    unittest.main()
