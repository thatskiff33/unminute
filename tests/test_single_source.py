"""Facts that have one source (R12, #133): the minimum NVIDIA driver, speech
model names and what the app calls them, and download progress as a number.
The page shows what Python sends and reads nothing out of a title or message.
"""
import ast
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asr_engines  # noqa: E402
from workspace import gpu, setup  # noqa: E402

# Junctions are made with cmd.exe from the system folder, never by name, and
# with the minimal environment (#253). The helper is loaded from its file so
# tools/ stays off this module's import path.
_wps_spec = importlib.util.spec_from_file_location("windows_powershell",
                                                   ROOT / "tools" / "windows_powershell.py")
windows_powershell = importlib.util.module_from_spec(_wps_spec)
_wps_spec.loader.exec_module(windows_powershell)


def mklink_junction(link, target, **kwargs):
    """cmd /c mklink /J link target, with cmd.exe from the system folder and
    the minimal environment, never cmd by name (#253)."""
    return subprocess.run([windows_powershell.system_program("cmd.exe"), "/c", "mklink", "/J",
                           str(link), str(target)],
                          env=windows_powershell.environment(), **kwargs)

APP_JS = (ROOT / "workspace" / "ui" / "app.js").read_text(encoding="utf-8")
# Shipped code a person could edit a fact into (tests and tools are not shipped).
SHIPPED = [ROOT / "asr_engines.py", *sorted((ROOT / "workspace").glob("*.py")),
           *sorted((ROOT / "workspace" / "ui").glob("*.js")),
           *sorted((ROOT / "workspace" / "ui").glob("*.html"))]


def strings(path):
    """The string literals in a Python or JavaScript file (not its comments)."""
    return re.findall(r"""("[^"\n]*"|'[^'\n]*'|`[^`\n]*`)""", path.read_text(encoding="utf-8"))


class DriverMinimumTests(unittest.TestCase):
    def test_one_definition_in_code(self):
        minimum = re.escape(gpu.MIN_DRIVER_TEXT)
        found = []
        for path in SHIPPED:
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if path.name == "gpu.py" and line.startswith("MIN_DRIVER = "):
                    continue                            # the definition
                if re.search(rf"\b{minimum}\b", line) and re.search(r"(?i)driver|or later", line):
                    found.append(f"{path.name}:{n}")
        self.assertEqual(found, [])

    def test_the_text_follows_the_tuple(self):
        self.assertEqual(gpu.version_text((580, 0)), "580")
        self.assertEqual(gpu.version_text((580, 65)), "580.65")
        self.assertEqual(gpu.version_text((581, 7)), "581.07")
        self.assertEqual(gpu.MIN_DRIVER_TEXT, gpu.version_text(gpu.MIN_DRIVER))

    def test_the_docs_name_the_same_minimum(self):
        for doc in ("README.md", "docs/WORKSPACE_GUIDE.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            numbers = set(re.findall(r"(?i)driver[^.\n]{0,30}?\b(\d{3}(?:\.\d+)?)\b", text))
            self.assertEqual(numbers, {gpu.MIN_DRIVER_TEXT}, doc)

    def test_the_page_gets_it_from_settings(self):
        self.assertIn("${s.gpu.min_driver}", APP_JS)


class ModelNameTests(unittest.TestCase):
    def test_every_speech_model_has_one_label(self):
        speech = set(asr_engines.PARAKEET_MODELS) | set(asr_engines.WHISPER_MODELS)
        self.assertEqual(set(asr_engines.LABELS), speech)
        self.assertEqual(asr_engines.label("test:x:y"), "test:x:y")      # unknown: as it is

    def test_no_model_name_or_label_is_written_out_in_shipped_code(self):
        labels = "|".join(re.escape(v) for v in asr_engines.LABELS.values())
        found = []
        for path in SHIPPED:
            if path.name == "asr_engines.py":
                continue                                # the source itself
            for s in strings(path):
                if re.search(rf"parakeet-tdt|large-v3|{labels}", s):
                    found.append(f"{path.name}: {s[:60]}")
        self.assertEqual(found, [])

    def test_setup_takes_names_and_labels_from_asr_engines(self):
        names = [c["name"] for c in setup.CARDS]
        self.assertEqual(len(names), len(set(names)))
        speech = [c for c in setup.CARDS if c["role"] == "speech"]
        self.assertEqual([c["name"] for c in speech if c["recommended"]],
                         [asr_engines.RECOMMENDED_PARAKEET])
        for card in setup.CARDS:
            self.assertIn(card["name"], asr_engines.MANAGED_MODELS)
            self.assertTrue(card["label"])
        for card in speech:
            self.assertEqual(card["label"], asr_engines.label(card["name"]))
            self.assertIn(card["label"], card["title"])
        self.assertEqual(set(asr_engines.LICENCES),
                         set(asr_engines.MANAGED_MODELS) | {asr_engines.SILERO_VAD.name})

    def test_a_meeting_and_its_earlier_versions_carry_labels(self):
        from workspace.app import Workspace
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(Path(tmp) / "ws", Path(tmp) / "models", start=False, battery=False,
                           policy_reader=lambda: ({}, ()))
            try:
                mid = ws.store.create_meeting("Synthetic meeting")
                sid = ws.store.add_source(mid, original_path=None, original_sha256="0" * 64,
                                          original_size=1, original_mtime_ns=1,
                                          audio_sha256="0" * 64, samples=16000,
                                          sample_rate=16000)
                for model in (asr_engines.PARAKEET_V3, asr_engines.PARAKEET_V2):
                    ws.store.ensure_version(mid, sid, engine="parakeet", model=model,
                                            model_revision="r", precision="int8",
                                            config={"chunking": "v1"}, chunk_plan=[])
                d = ws.detail(mid)
                self.assertEqual(d["model_label"], "Parakeet v2")
                self.assertEqual([v["model_label"] for v in d["older_versions"]],
                                 ["Parakeet v3"])
                self.assertIsNone(ws._facts(None)["model_label"])
            finally:
                ws.close()

    def test_download_messages_name_the_model_as_the_page_does(self):
        said = []
        spec = asr_engines.MANAGED_MODELS[asr_engines.PARAKEET_V3]

        def refuse(spec, dest):
            raise OSError("no network in tests")
        with tempfile.TemporaryDirectory() as tmp, \
                self.assertRaises(asr_engines.ModelLoadError):
            asr_engines.install_model(spec, tmp, log=said.append, download=refuse)
        self.assertTrue(any(m.startswith("Downloading Parakeet v3 (") for m in said), said)

    def test_every_model_message_names_the_model_by_its_label(self):
        # No message in asr_engines or engines puts a model's raw name in
        # front of the user: each goes through label(). An engine's own
        # .label stays the model id (tools store it as a version's model).
        for path in (ROOT / "asr_engines.py", ROOT / "workspace" / "engines.py"):
            source = path.read_text(encoding="utf-8")
            raw = re.findall(r"\{(?:spec\.name|name)\}", source)
            self.assertEqual(raw, [], path.name)
            errors = [node for node in ast.walk(ast.parse(source))
                      if isinstance(node, ast.Call)
                      and getattr(node.func, "id", None) == "ModelLoadError"]
            self.assertGreater(len(errors), 2, path.name)
            raw = [f"{path.name}:{node.lineno}" for node in errors for part in ast.walk(node)
                   if isinstance(part, ast.FormattedValue) and _raw_model_name(part.value)]
            self.assertEqual(raw, [])
        from workspace import engines
        with mock.patch.object(asr_engines, "parakeet_status", return_value=("missing", "x")), \
                self.assertRaisesRegex(asr_engines.ModelLoadError, "^Parakeet v3 is not ready"):
            engines.load_engine(asr_engines.PARAKEET_V3, ".", 1)

    def test_a_parakeet_engine_names_the_model_by_its_label(self):
        engine = asr_engines.ParakeetEngine(asr_engines.PARAKEET_V2, ".", 1)
        self.assertEqual(engine.label, asr_engines.PARAKEET_V2)      # the id, as stored
        with self.assertRaisesRegex(asr_engines.ModelLoadError, "^Parakeet v2 is not loaded"):
            engine.transcribe("synthetic.wav")

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic")
        onnx_asr = types.ModuleType("onnx_asr")
        onnx_asr.load_model = onnx_asr.load_vad = fail
        onnxruntime = types.ModuleType("onnxruntime")
        onnxruntime.disable_telemetry_events = lambda: None
        onnxruntime.SessionOptions = mock.MagicMock
        with mock.patch.dict(sys.modules, onnx_asr=onnx_asr, onnxruntime=onnxruntime), \
                mock.patch.object(asr_engines, "check_files", return_value=[]), \
                self.assertRaisesRegex(asr_engines.ModelLoadError,
                                       r"^Parakeet v2 failed to load \(RuntimeError"):
            engine.load()

    def test_the_page_reads_no_names_out_of_titles_or_ids(self):
        self.assertNotIn("c.title", APP_JS)
        self.assertNotIn("modelName", APP_JS)
        self.assertIn("d.model_label", APP_JS)
        self.assertIn("v.model_label", APP_JS)


def _raw_model_name(expr):
    """True for a formatted value that is a model's raw name: `name`,
    `spec.name`, `self.spec.name` or `self.label`. label(...) is what is wanted."""
    if isinstance(expr, ast.Name):
        return expr.id == "name"
    return isinstance(expr, ast.Attribute) and expr.attr in ("name", "label")


# What a junction's outside folder holds: a file, and a .cache folder of its own.
_OUTSIDE = [".cache", ".cache/app.txt", "keep", "keep/file.txt"]


@contextlib.contextmanager
def _outside_folder(base, at):
    """A junction at `at` to a fresh folder outside the cache holding _OUTSIDE.
    Afterwards only the junction is removed, never what it points to."""
    outside = base / "elsewhere"
    for rel in ("keep/file.txt", ".cache/app.txt"):
        (outside / rel).parent.mkdir(parents=True, exist_ok=True)
        (outside / rel).write_text("synthetic", encoding="utf-8")
    at.parent.mkdir(parents=True, exist_ok=True)
    mklink_junction(at, outside, check=True, capture_output=True)
    try:
        yield outside
    finally:
        # Checked here, not with the is_link() under test.
        if os.path.lexists(at) and os.lstat(at).st_file_attributes & 0x400:
            os.rmdir(at)                    # removes the junction itself
        shutil.rmtree(outside)


def _synthetic_model():
    """A small pinned model (spec, {file name: bytes}) for check-record tests."""
    contents = {"config.json": b"{}", "weights.onnx": b"\x01\x02" * 500}
    files = tuple(asr_engines.ModelFile(n, len(d), hashlib.sha256(d).hexdigest())
                  for n, d in contents.items())
    return asr_engines.ModelSpec(name="synthetic", folder="synthetic", repo="example/x",
                                 revision="0" * 40, files=files), contents


def _tree(folder):
    """Every entry under `folder`, relative, sorted."""
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))


def _raised(call):
    """What `call` raised, or None (checked after the outside folder)."""
    try:
        call()
    except Exception as exc:
        return exc
    return None


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.downloads = setup.Downloads(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_page_reads_no_numbers_out_of_messages(self):
        regexes = re.findall(r"/[^/\n]*(?:GB|%)[^/\n]*/[gimsuy]*\.(?:exec|test)\(", APP_JS)
        self.assertEqual(regexes, [])
        self.assertIn("c.download.percent", APP_JS)
        # The bar is set in place; the lists' signatures leave the number out,
        # so a moving bar never rebuilds the list (focus and open Details stay).
        self.assertIn("percent: null", APP_JS)
        self.assertEqual(APP_JS.count("cardsSig(s.cards)"), 2)
        self.assertEqual(APP_JS.count("updateProgress($("), 3)   # Setup's two lists, Downloads

    def test_each_attempt_records_when_it_started(self):
        with mock.patch("threading.Thread"):            # the download itself never starts
            before = time.time()
            self.downloads.start(asr_engines.RECOMMENDED_PARAKEET)
        started = self.downloads.state[asr_engines.RECOMMENDED_PARAKEET]["started"]
        self.assertGreaterEqual(started, before)

    def test_a_model_download_is_measured_by_what_it_has_written(self):
        name = asr_engines.RECOMMENDED_PARAKEET
        spec = asr_engines.MANAGED_MODELS[name]
        self.assertIsNone(self.downloads.percent(name))                 # nothing running
        self.downloads.state[name] = {"state": "running", "message": "Starting", "log": []}
        self.assertIsNone(self.downloads.percent(name))                 # nothing written yet
        asr_engines.staging_dir(spec, self.tmp.name).mkdir(parents=True)
        with mock.patch.object(setup, "_size_of", return_value=spec.download_bytes // 4):
            self.assertEqual(self.downloads.percent(name), 25)
        with mock.patch.object(setup, "_size_of", return_value=spec.download_bytes * 2):
            self.assertEqual(self.downloads.percent(name), 100)          # never past 100
        with mock.patch.object(setup, "_size_of", return_value=0):
            self.assertIsNone(self.downloads.percent(name))   # nothing of this attempt's yet
        self.downloads.state[name] = {"state": "running", "message": "Checking", "log": [],
                                      "verify": True}
        self.assertIsNone(self.downloads.percent(name))                 # a check: no number
        self.downloads.state[name] = {"state": "ready", "message": "Done", "log": []}
        self.assertIsNone(self.downloads.percent(name))

    def test_a_cancelled_attempts_leftovers_do_not_count(self):
        name = asr_engines.RECOMMENDED_PARAKEET
        spec = asr_engines.MANAGED_MODELS[name]
        staging = asr_engines.staging_dir(spec, self.tmp.name)
        staging.mkdir(parents=True)
        old = staging / "left-from-last-time.onnx"
        old.write_bytes(b"x" * 4000)
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        (staging / "this-attempt.onnx").write_bytes(b"x" * 1000)
        self.assertEqual(setup._size_of(staging, since=time.time() - 60), 1000)
        self.assertEqual(setup._size_of(staging), 5000)
        self.downloads.state[name] = {"state": "running", "message": "x", "log": [],
                                      "started": time.time() - 60}
        with mock.patch.object(setup, "_size_of", return_value=0) as size:
            self.downloads.percent(name)
        self.assertEqual(size.call_args.args[1], self.downloads.state[name]["started"])

    def test_the_graphics_component_counts_finished_wheels(self):
        self.downloads.state[gpu.COMPONENT] = {"state": "running", "message": "x", "log": []}
        wheels = gpu.staging_dir(Path(self.tmp.name)) / "wheels"
        wheels.mkdir(parents=True)
        self.assertIsNone(self.downloads.percent(gpu.COMPONENT))        # none started yet
        first, second = gpu.WHEELS[0], gpu.WHEELS[1]
        (wheels / (first["name"] + ".whl")).write_bytes(b"x")
        with mock.patch.object(setup, "_file_size", return_value=first["size"] // 2):
            self.assertEqual(self.downloads.percent(gpu.COMPONENT),
                             round(100 * (first["size"] // 2) / gpu.DOWNLOAD_BYTES))
        (wheels / (first["name"] + ".whl")).unlink()                    # unpacked, deleted
        (wheels / (second["name"] + ".whl")).write_bytes(b"x")
        with mock.patch.object(setup, "_file_size", return_value=0):
            # The first counts at its pinned size: the bar never drops back.
            self.assertEqual(self.downloads.percent(gpu.COMPONENT),
                             round(100 * first["size"] / gpu.DOWNLOAD_BYTES))

    def test_a_wheel_left_by_a_cancelled_attempt_shows_no_number(self):
        wheels = gpu.staging_dir(Path(self.tmp.name)) / "wheels"
        wheels.mkdir(parents=True)
        stale = wheels / (gpu.WHEELS[1]["name"] + ".whl")      # cancelled during the second
        stale.write_bytes(b"x" * 1000)
        os.utime(stale, (time.time() - 3600, time.time() - 3600))
        self.downloads.state[gpu.COMPONENT] = {"state": "running", "message": "x", "log": [],
                                               "started": time.time()}
        self.assertIsNone(self.downloads.percent(gpu.COMPONENT))      # not 100% from leftovers

    def test_install_downloads_where_progress_looks(self):
        cache = Path(self.tmp.name) / "models"
        seen = []

        def fetch(url, dest, size):
            seen.append(os.path.samefile(Path(dest).parent, gpu.staging_dir(cache) / "wheels"))
            raise OSError("no network in tests")
        with self.assertRaises(OSError):
            gpu.install(cache, log=lambda m: None, fetch=fetch)
        self.assertEqual(seen, [True])
        self.assertFalse(gpu.staging_dir(cache).exists())          # cleaned up after a failure

    @unittest.skipUnless(sys.platform == "win32", "junctions are a Windows feature")
    def test_install_never_follows_a_linked_download_folder(self):
        # A junction at the gpu, component or staging folder must not be
        # followed: deleting or writing through it would reach whatever it
        # points to.
        base = Path(self.tmp.name)
        cache = base / "models"
        for at in (cache / "gpu", gpu.component_dir(cache), gpu.staging_dir(cache)):
            with self.subTest(at=at.name):
                fetched = []

                # Writes its file, so without the refusal install would carry on.
                def fetch(url, dest, size, fetched=fetched):
                    fetched.append(url)
                    Path(dest).write_bytes(b"x")
                with _outside_folder(base, at) as outside:
                    error = _raised(lambda: gpu.install(cache, log=lambda m: None, fetch=fetch))
                    self.assertEqual(_tree(outside), _OUTSIDE)    # nothing deleted or written
                self.assertEqual(fetched, [])
                self.assertIsInstance(error, gpu.GpuError)
                self.assertIn("is a link", str(error))

    @unittest.skipUnless(sys.platform == "win32", "junctions are a Windows feature")
    def test_a_model_install_never_follows_a_linked_folder(self):
        # The same for a speech model's family, model and download folders.
        # Without the refusal the download is written through the link, and
        # its .cache clean-up deletes the linked folder's own .cache.
        base = Path(self.tmp.name)
        cache = base / "models"
        spec = asr_engines.MANAGED_MODELS[asr_engines.PARAKEET_V3]
        target = asr_engines.model_dir(spec, cache)
        for at in (target.parent, target, asr_engines.staging_dir(spec, cache),
                   target.parent / ".verified"):
            with self.subTest(at=at.name):
                downloaded = []

                def download(spec, dest, downloaded=downloaded):
                    downloaded.append(spec.name)
                    (Path(dest) / ".cache").mkdir(parents=True, exist_ok=True)
                    (Path(dest) / "a.bin").write_bytes(b"x")
                with _outside_folder(base, at) as outside:
                    error = _raised(lambda: asr_engines.install_model(
                        spec, cache, log=lambda m: None, download=download))
                    self.assertEqual(_tree(outside), _OUTSIDE)
                self.assertEqual(downloaded, [])
                self.assertIsInstance(error, asr_engines.ModelLoadError)
                self.assertRegex(str(error), "^A Parakeet v3 folder .* is a link")
                # The page shows it whole: no "..", no "Try again" after "ask IT".
                self.assertEqual(setup._friendly(error), str(error))
                self.assertTrue(str(error).endswith("Ask your IT team."))

    def test_a_message_with_a_technical_part_is_still_cut_there(self):
        error = asr_engines.ModelLoadError("Could not download Parakeet v3 (OSError: x). "
                                           "Nothing was changed.")
        self.assertEqual(setup._friendly(error), "Could not download Parakeet v3. Try again.")

    @unittest.skipUnless(sys.platform == "win32", "junctions are a Windows feature")
    def test_removal_never_deletes_check_records_through_a_link(self):
        base = Path(self.tmp.name)
        cache = base / "models"
        spec = asr_engines.MANAGED_MODELS[asr_engines.WHISPER_LARGE]
        family = cache / spec.family
        record = f"{spec.folder}-{'b' * 16}.json"          # the pattern remove() deletes
        # A Hugging Face cache copy lies outside the family folder, so its own
        # parents never pass through a link there.
        hub = cache / ("models--" + spec.hub_repos[0].replace("/", "--"))
        hub.mkdir(parents=True)
        for at, rel in ((family / ".verified", record), (family, f".verified/{record}")):
            with self.subTest(at=at.name):
                if at.exists():
                    at.rmdir()                          # empty, left by the earlier case
                with _outside_folder(base, at) as outside:
                    (outside / rel).parent.mkdir(exist_ok=True)
                    (outside / rel).write_text("{}", encoding="utf-8")
                    with self.assertRaisesRegex(setup.RemovalRefused, "is a link"):
                        setup.remove(asr_engines.WHISPER_LARGE, cache)
                    self.assertTrue(hub.is_dir())
                    extra = [rel] if at.name == ".verified" else [".verified", rel]
                    self.assertEqual(_tree(outside), sorted([*_OUTSIDE, *extra]))

    @unittest.skipUnless(sys.platform == "win32", "junctions are a Windows feature")
    def test_a_check_record_is_never_written_through_a_link(self):
        base = Path(self.tmp.name)
        spec, contents = _synthetic_model()
        family = base / "models" / "parakeet"
        hub_copy = base / "models" / "synthetic"        # a copy outside the family folder
        record = family / ".verified" / "synthetic-x.json"
        # Where the junction is, the folder checked, and its record (None: its own).
        cases = {"model folder": (family / "synthetic", family / "synthetic", None),
                 "record folder": (family / ".verified", hub_copy, record),
                 "family folder": (family, hub_copy, record)}
        for case, (at, folder, marker) in cases.items():
            with self.subTest(case=case):
                if at.exists():
                    at.rmdir()                          # empty, left by an earlier case
                with _outside_folder(base, at) as outside:
                    folder.mkdir(parents=True, exist_ok=True)
                    for n, d in contents.items():
                        (folder / n).write_bytes(d)
                    self.assertEqual(asr_engines.check_files(spec, folder, full=True,
                                                             marker=marker), [])
                    # Only what the test put there: no check record.
                    expected = [*_OUTSIDE, *contents] if folder == at else _OUTSIDE
                    self.assertEqual(_tree(outside), sorted(expected))

    @unittest.skipUnless(sys.platform == "win32", "junctions are a Windows feature")
    def test_a_check_record_is_not_trusted_through_a_link(self):
        # A redirected folder that carries an old "ok" record: a file changed
        # without changing its size or time is still found, whether the link
        # is at the model, family or record folder.
        base = Path(self.tmp.name)
        spec, contents = _synthetic_model()
        for case in ("model folder", "family folder", "record folder"):
            with self.subTest(case=case):
                root = base / case.replace(" ", "-")      # a fresh layout per case
                real = root / "real"                       # where the link points
                family = root / "models" / "parakeet"
                # The files, their record (None: their own), the link, and the
                # folder and record as reached through the link.
                if case == "model folder":
                    files, record, link = real, None, family / "synthetic"
                    via, via_record = link, None
                elif case == "family folder":
                    files, record, link = real / "synthetic", None, family
                    via, via_record = family / "synthetic", None
                else:
                    files, record = root / "models" / "hub", real / "synthetic-x.json"
                    link = family / ".verified"
                    via, via_record = files, link / record.name
                files.mkdir(parents=True)
                for n, d in contents.items():
                    (files / n).write_bytes(d)
                self.assertEqual(asr_engines.check_files(spec, files, full=True,
                                                         marker=record), [])    # records "ok"
                weights = files / "weights.onnx"
                st = weights.stat()
                weights.write_bytes(b"\x02\x01" * 500)                   # same size
                os.utime(weights, ns=(st.st_atime_ns, st.st_mtime_ns))  # same time
                self.assertEqual(asr_engines.check_files(spec, files, marker=record), [])
                link.parent.mkdir(parents=True, exist_ok=True)
                mklink_junction(link, real, check=True, capture_output=True)
                try:
                    self.assertEqual(asr_engines.check_files(spec, via, marker=via_record),
                                     ["weights.onnx does not match the expected checksum"])
                finally:
                    os.rmdir(link)                                     # the junction only

    @unittest.skipUnless(sys.platform == "win32", "junctions are a Windows feature")
    def test_links_in_a_download_folder_are_never_followed(self):
        base = Path(self.tmp.name)
        elsewhere = base / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "big.bin").write_bytes(b"x" * 50000)
        staging = base / "staging"
        staging.mkdir()
        (staging / "own.bin").write_bytes(b"x" * 1000)

        def junction(link, target):
            mklink_junction(link, target, check=True, capture_output=True)
        junction(staging / "to-elsewhere", elsewhere)
        junction(staging / "loop-a", staging)                # a loop back to the folder
        junction(staging / "loop-b", staging)
        self.assertEqual(setup._size_of(staging), 1000)     # only its own file, and it ends
        self.assertEqual(setup._size_of(staging / "to-elsewhere"), 0)   # a link at the top
        self.assertEqual(setup._size_of(base / "missing"), 0)

    def test_a_file_link_in_a_download_folder_counts_nothing(self):
        base = Path(self.tmp.name)
        (base / "elsewhere.bin").write_bytes(b"x" * 50000)
        staging = base / "staging"
        staging.mkdir()
        (staging / "own.bin").write_bytes(b"x" * 1000)
        try:
            os.symlink(base / "elsewhere.bin", staging / "linked.bin")
        except OSError:
            self.skipTest("creating symlinks needs Developer Mode or admin on Windows")
        self.assertEqual(setup._size_of(staging), 1000)

    def test_setup_state_sends_the_number(self):
        from workspace.app import Workspace
        ws = Workspace(Path(self.tmp.name) / "ws", Path(self.tmp.name) / "models", start=False,
                       battery=False, policy_reader=lambda: ({}, ()))
        try:
            name = asr_engines.RECOMMENDED_PARAKEET
            ws.downloads.state[name] = {"state": "running", "message": "Starting", "log": []}
            with mock.patch.object(ws.downloads, "percent", return_value=40), \
                    mock.patch.object(gpu, "detected", return_value=None):
                card = next(c for c in ws.setup_state()["cards"] if c["name"] == name)
            self.assertEqual((card["download"]["percent"], card["label"]), (40, "Parakeet v2"))
            self.assertNotIn("percent", ws.downloads.state[name])      # a copy, not the state
            with mock.patch.object(gpu, "detected", return_value=None):
                settings = ws.get_settings()
            self.assertEqual(settings["gpu"]["min_driver"], gpu.MIN_DRIVER_TEXT)
            choices = settings["choices"]
            self.assertEqual([(c["name"], c["label"], c["recommended"]) for c in choices],
                             [(n, asr_engines.label(n), n == asr_engines.RECOMMENDED_PARAKEET)
                              for n in (*asr_engines.PARAKEET_MODELS, *asr_engines.WHISPER_MODELS)])
        finally:
            ws.close()


PROBE = r"""
import json, sys, types
from unittest import mock
attempted = set()

class Recorder:
    def find_spec(self, name, path=None, target=None):
        attempted.add(name.partition(".")[0])
        return None

sys.meta_path.insert(0, Recorder())
# The native libraries are stubbed; everything else in the load is real.
class Model:
    def with_vad(self, *a, **k): return self
    def with_timestamps(self): return self
onnx_asr = types.ModuleType("onnx_asr")
onnx_asr.load_model = onnx_asr.load_vad = lambda *a, **k: Model()
class Options:
    def add_session_config_entry(self, *a): pass
onnxruntime = types.ModuleType("onnxruntime")
onnxruntime.disable_telemetry_events = lambda: None
onnxruntime.SessionOptions = Options
sys.modules.update(onnx_asr=onnx_asr, onnxruntime=onnxruntime)
import asr_engines
from workspace import engines, worker
with mock.patch.object(asr_engines, "parakeet_status", return_value=("ready", "")), \
        mock.patch.object(asr_engines, "check_files", return_value=[]):
    engine = engines.load_engine(asr_engines.RECOMMENDED_PARAKEET, ".", 1)
print(json.dumps({"attempted": sorted(attempted), "decoder": engine.decode_audio is None,
                  "loaded": engine._asr is not None}))
"""


class ParakeetWorkerTests(unittest.TestCase):
    def test_a_parakeet_engine_never_imports_faster_whisper(self):
        # A recorder sees every import attempt, even where faster_whisper and
        # CTranslate2 are not installed (CI's test install). The worker module
        # and the engine's own load run for real; only ONNX is stubbed.
        result = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        seen = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(seen["decoder"] and seen["loaded"])
        self.assertNotIn("faster_whisper", seen["attempted"])
        self.assertNotIn("ctranslate2", seen["attempted"])

    def test_a_file_path_needs_a_decoder(self):
        engine = asr_engines.ParakeetEngine(asr_engines.RECOMMENDED_PARAKEET, ".", 1)
        engine._asr = object()                          # as if loaded
        with self.assertRaises(TypeError):
            engine.transcribe("synthetic.wav")


if __name__ == "__main__":
    unittest.main()
