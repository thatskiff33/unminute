# workspace/gpu.py
# Optional NVIDIA acceleration: two separate components, each in its own
# folder and loaded only by its own worker process.
#
# Speaker identification (pyannote) runs on PyTorch. The package ships the CPU
# build; the CUDA build is an optional component the user downloads in Setup:
# the official PyTorch CUDA 13 wheels for the SAME versions the app pins
# (torch 2.14.1, torchaudio 2.11.0), pinned by SHA-256 and checked again file
# by file against each wheel's RECORD. It is unpacked into its own folder and
# put first on the import path of a separate worker process used only for
# speaker identification, so transcription engines never share a process with
# it (their CUDA libraries are a different version).
#
# Whisper (CTranslate2 4.8, already in the package) runs on the graphics card
# with NVIDIA's CUDA 12 cuBLAS, the one CUDA library CTranslate2's Windows
# build loads at run time (#248; it links the CUDA runtime statically and is
# built without cuDNN). The second component is NVIDIA's own cuBLAS wheel from
# PyPI, pinned and checked the same way, in its own folder. Only the Whisper
# GPU worker loads it, by full path, and that worker never sees the PyTorch
# CUDA 13 folder: the two components' libraries clash in one process.
#
# The wheels bundle the CUDA libraries; nothing is installed system-wide and
# graphics drivers are never touched. A detected NVIDIA card does not by
# itself enable anything: a component must be installed and the setting
# turned on (Faster mode), and the actual device used is recorded.

import base64
import csv
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from workspace import asr_engines

COMPONENT = "gpu-speakers"
VERSION = "torch-2.14.1-cu130"
LICENCE = ("PyTorch BSD-3-Clause; NVIDIA CUDA runtime and cuDNN under NVIDIA's licences",
           "PyTorch CUDA 13.0 build from download-r2.pytorch.org")

# Pinned on 17 October 2026 from https://download.pytorch.org/whl/cu130/ (the
# index publishes these SHA-256 values with each file), for CPython 3.14. The
# index gives no SHA-256 for torch 2.13.0's CPython 3.14 wheel, so the app moved
# to 2.14.1, whose wheel has one (#111 B4). record_sha256 pins each
# wheel's own file list (RECORD), so an installed file cannot be changed
# together with its RECORD line and still pass.
WHEELS = (
    {"name": "torch", "version": "2.14.1+cu130", "size": 1998854405,
     "url": "https://download-r2.pytorch.org/whl/cu130/"
            "torch-2.14.1%2Bcu130-cp314-cp314-win_amd64.whl",
     "sha256": "e25757e423be3c107a36d7495fa5aeb021b9ab506457cd7c7e068505f0283ae0",
     "record_sha256": "18309d15de951a0d2291a0fcb262df5427824106346cabeb032dc87b5ab92397"},
    {"name": "torchaudio", "version": "2.11.0+cu130", "size": 1722736,
     "url": "https://download-r2.pytorch.org/whl/cu130/"
            "torchaudio-2.11.0%2Bcu130-cp314-cp314-win_amd64.whl",
     "sha256": "b345401d76a371031b2fe965bd9579b9621c9d87902ee3e586be665df04c179e",
     "record_sha256": "3990f10471f16b97f09f44fd2a5e55aad4b3418ca5103efffe238a0ef0d91757"},
)
DOWNLOAD_HOST = "https://download-r2.pytorch.org/whl/cu130/"
DOWNLOAD_BYTES = sum(w["size"] for w in WHEELS)
# CUDA 13.0 needs an NVIDIA driver from the R580 series or later. The Whisper
# component's CUDA 12.8 libraries run on any driver new enough for CUDA 13
# (newer drivers run older CUDA libraries), so one minimum covers the setting.
MIN_DRIVER = (580, 0)


def version_text(version):
    """A driver version as people write it: (591, 0) is "591", (591, 4) is "591.04"."""
    return f"{version[0]}" if not version[1] else f"{version[0]}.{version[1]:02d}"


MIN_DRIVER_TEXT = version_text(MIN_DRIVER)      # every message and the page use this (R12)
# C++ headers, needed only to compile extensions, not to run: not unpacked
# (9,379 files; they also pass Windows' 260-character path limit).
SKIP_PREFIXES = ("torch/include/",)

# -- the Whisper component (#248) ------------------------------------------------------

WHISPER_COMPONENT = "gpu-whisper"
WHISPER_VERSION = "cublas-12.8.4.1-cu12"
WHISPER_LICENCE = ("NVIDIA CUDA Toolkit licence (cuBLAS), published by NVIDIA on PyPI",
                   "NVIDIA cuBLAS 12.8.4.1 for CUDA 12 from files.pythonhosted.org")
# Pinned on 20 October 2026 from PyPI's JSON API
# (https://pypi.org/pypi/nvidia-cublas-cu12/12.8.4.1/json, "digests.sha256" of
# the win_amd64 file), and checked against the downloaded file. 12.8.4.1 is
# the cuBLAS of the CUDA 12.8.1 toolkit CTranslate2 4.8.0's Windows wheels are
# built with (python/tools/prepare_build_environment_windows.sh at v4.8.0).
# record_sha256 is the SHA-256 of the wheel's own RECORD, read from that file.
# A CTranslate2 upgrade must check which cuBLAS it loads before this changes
# (tests/test_consistency.py ties the two).
WHISPER_CTRANSLATE2 = "4.8.0"
WHISPER_WHEELS = (
    {"name": "nvidia_cublas_cu12", "version": "12.8.4.1", "size": 567544208,
     "url": "https://files.pythonhosted.org/packages/70/61/"
            "7d7b3c70186fb651d0fbd35b01dbfc8e755f69fd58f817f3d0f642df20c3/"
            "nvidia_cublas_cu12-12.8.4.1-py3-none-win_amd64.whl",
     "sha256": "47e9b82132fa8d2b4944e708049229601448aaad7e6f296f630f2d1a32de35af",
     "record_sha256": "e0059cd74d01a9bfed5eff1c032187472423fb8914d25bae41b8d0d9138c8f6c"},
)
# Only this component's download process may reach this host (setup.py).
WHISPER_DOWNLOAD_HOST = "https://files.pythonhosted.org/packages/"
WHISPER_DOWNLOAD_BYTES = sum(w["size"] for w in WHISPER_WHEELS)
# Where the DLLs are, and the ones loaded by full path, in load order:
# cublas64_12.dll imports cublasLt64_12.dll.
WHISPER_BIN = "nvidia/cublas/bin"
WHISPER_DLLS = ("cublasLt64_12.dll", "cublas64_12.dll")

# The Whisper models offered on the graphics card, and the precision each
# runs at there (on the processor they run at int8). Measured on a 4 GB RTX 500
# Ada, 20 October 2026 (docs/RELEASE_CHECKLIST.md, "Whisper on the graphics
# card"): large-v3 at float16 filled the card and spilled 1.6 GB into shared
# system memory, running 4.5 times slower; at int8_float16 (int8 weights, as on
# the processor) it stayed within the card's memory (3.8 GB at peak). A model
# not listed stays on the processor.
WHISPER_PRECISION = {
    asr_engines.WHISPER_TURBO: "float16",
    asr_engines.WHISPER_LARGE: "int8_float16",
}


def _long(path):
    """An extended-length path on Windows, so deep files (third-party
    licence texts) can be written and checked. Python imports use normal paths;
    the files it imports stay well inside the limit."""
    p = str(Path(path).resolve())
    if os.name != "nt" or p.startswith("\\\\?\\"):
        return Path(p)
    if p.startswith("\\\\"):                       # a network (UNC) folder
        return Path("\\\\?\\UNC\\" + p[2:])
    return Path("\\\\?\\" + p)


class GpuError(RuntimeError):
    """A refused or failed GPU step (message is safe to show)."""


def _nvidia_smi():
    """nvidia-smi from where the NVIDIA driver installs it, by absolute path
    only: never found through PATH or the current folder, either of which
    could make the app run a planted program at every start."""
    import ctypes
    try:
        buf = ctypes.create_unicode_buffer(260)
        n = ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260)
        system32 = buf.value if 0 < n < 260 else None
    except (AttributeError, OSError):
        system32 = None
    candidates = [os.path.join(system32, "nvidia-smi.exe")] if system32 else []
    program_files = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles")
    if program_files:          # older drivers
        candidates.append(os.path.join(program_files, "NVIDIA Corporation", "NVSMI",
                                       "nvidia-smi.exe"))
    return next((c for c in candidates if os.path.isfile(c)), None)


def detect():
    """The NVIDIA card and driver, from nvidia-smi (installed with the driver):
    {"name", "driver", "memory_mb", "driver_ok"} or None if there is none."""
    exe = _nvidia_smi()
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=name,driver_version,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True,
                             text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() \
        else ""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != 3:
        return None
    try:
        driver = tuple(int(x) for x in parts[1].split(".")[:2])
        memory = int(float(parts[2]))
    except ValueError:
        return None
    return {"name": parts[0][:80], "driver": parts[1], "memory_mb": memory,
            "driver_ok": driver >= MIN_DRIVER}


_detected = []        # cached result of detect() for this process


def detected():
    """detect(), asked once per app session (nvidia-smi takes a moment). A
    driver update is seen after the app restarts."""
    if not _detected:
        _detected.append(detect())
    return _detected[0]


def _hash_ok(path, digest):
    algo, _, want = digest.partition("=")
    if algo != "sha256":
        return False
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode() == want


# Full checks in this app session, by installation: True passed (later GPU
# runs use the quick check), False failed (no GPU run until Check every file
# passes or a repair replaces the component; it is not hashed again before each
# run). A repair writes a new installed.json, so it starts afresh. Kept in
# memory only: after a restart, the check before the first GPU run finds a
# damaged component again. Shared by both components (keyed by marker path).
_full_checks = {}


class Component:
    """One optional graphics card component: pinned wheels, unpacked into
    their own folder under <cache>/gpu/<version>, and checked file by file
    against each wheel's pinned RECORD.

    wheels: the pinned wheels, or a function returning them (read at each
    use). lib_dirs: folders native libraries load from, which the quick check
    also searches for added files. skip: path prefixes not unpacked (headers)."""

    def __init__(self, key, version, wheels, host, *, lib_dirs, skip=()):
        self.key, self.version, self._wheels, self.host = key, version, wheels, host
        self.lib_dirs, self.skip = tuple(lib_dirs), tuple(skip)

    @property
    def wheels(self):
        return self._wheels() if callable(self._wheels) else self._wheels

    @property
    def download_bytes(self):
        return sum(w["size"] for w in self.wheels)

    def component_dir(self, cache_dir):
        return Path(cache_dir) / "gpu" / self.version

    def staging_dir(self, cache_dir):
        """Where install() builds the component before it replaces anything;
        its wheels folder is what Setup measures for progress."""
        return self.component_dir(cache_dir).parent / (self.version + ".partial")

    def site_dir(self, cache_dir):
        return self.component_dir(cache_dir) / "site"

    def _record_entries(self, site):
        """(relative path, digest, size) for every file the wheels installed,
        from their RECORD files, each of which must match its pinned SHA-256."""
        entries = []
        for w in self.wheels:
            record = Path(site) / f"{w['name']}-{w['version']}.dist-info" / "RECORD"
            try:
                data = record.read_bytes()
            except OSError as exc:
                raise GpuError("The graphics card component is incomplete.") from exc
            if hashlib.sha256(data).hexdigest() != w["record_sha256"]:
                raise GpuError("The graphics card component's file list has been changed.")
            try:
                for row in csv.reader(data.decode("utf-8").splitlines()):
                    if row and row[0]:
                        entries.append((row[0], row[1] if len(row) > 1 else "",
                                        int(row[2]) if len(row) > 2 and row[2] else None))
            except (UnicodeDecodeError, ValueError) as exc:
                raise GpuError("The graphics card component's file list is damaged.") from exc
        return entries

    def _unlisted(self, site, entries, full):
        """Files that are not in the wheels' RECORD. Any file under the import
        root could be loaded, so none may be added. Quick: the import root and
        the library folders (where DLLs load from); full: everything.
        __pycache__ is Python's own byte-code cache and cannot be pinned. The
        folder is in the user's own profile, so this check finds damage and
        stray files; anyone able to write there could equally change the app's
        own installed files."""
        listed = {rel.replace("\\", "/") for rel, _, _ in entries}
        tops = {rel.split("/")[0] for rel in listed}
        out = []
        if full:
            for root, dirs, files in os.walk(site):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in files:
                    rel = os.path.relpath(os.path.join(root, f), site).replace("\\", "/")
                    if rel not in listed:
                        out.append(rel)
            return out
        for p in Path(site).iterdir():
            if p.name not in tops:
                out.append(p.name)
        for lib_dir in self.lib_dirs:
            lib, depth = Path(site) / lib_dir, lib_dir.count("/") + 1
            if lib.is_dir():
                folders = {rel.split("/")[depth] for rel in listed
                           if rel.startswith(lib_dir + "/") and rel.count("/") > depth}
                out += [f"{lib_dir}/{p.name}" for p in lib.iterdir()
                        if f"{lib_dir}/{p.name}" not in listed
                        and not (p.is_dir() and p.name in folders)]
        return out

    def check(self, cache_dir, full=False):
        """Problems with the installed component ([] = fine). Quick: pinned file
        lists, every file present with the right size, no added files at the
        import root or in the library folders. Full: every file's SHA-256, and
        no added file anywhere. Never raises: anything unreadable is reported
        as a problem."""
        if not (self.component_dir(cache_dir) / "installed.json").is_file():
            return ["not installed"]
        try:
            site = _long(self.site_dir(cache_dir))
            entries = self._record_entries(site)
            problems = [f"{rel}: not part of the component"
                        for rel in self._unlisted(site, entries, full)]
            for rel, digest, size in entries:
                if not digest or rel.startswith(self.skip):   # RECORD itself; headers
                    continue
                p = site / rel
                if not p.is_file() or (size is not None and p.stat().st_size != size):
                    problems.append(f"{rel}: missing or wrong size")
                elif full and not _hash_ok(p, digest):
                    problems.append(f"{rel}: checksum does not match")
                if len(problems) > 20:
                    break
            return problems
        except GpuError as exc:
            return [str(exc)]
        except (OSError, ValueError) as exc:
            return [f"The graphics card component could not be read ({type(exc).__name__})."]

    def status(self, cache_dir):
        """('ready' | 'missing' | 'invalid', plain detail). A full check that
        failed this session counts, though the quick check may pass (#153)."""
        problems = self.check(cache_dir)
        if problems == ["not installed"]:
            return "missing", "Not downloaded yet."
        if problems or _full_checks.get(self._install_key(cache_dir)) is False:
            return "invalid", ("Damaged or incomplete. Repair downloads a fresh, verified copy; "
                               "the damaged copy is kept aside (only the most recent one).")
        return "ready", "Downloaded and verified."

    def install(self, cache_dir, log, fetch):
        """Download (with `fetch(url, dest, expected_size)`), verify and unpack
        the component. Runs in the download process. Nothing replaces a working
        copy until the new one is complete and verified."""
        # A link or junction at the gpu, component or download folder is refused
        # before anything is deleted or written. Following one would put the
        # download, the component and the clean-up of older damaged copies outside
        # the cache, and rmtree would delete whatever the download folder points
        # to (T5). remove() refuses the same.
        if any(asr_engines.is_link(p) for p in (Path(cache_dir) / "gpu",
                                                self.component_dir(cache_dir),
                                                self.staging_dir(cache_dir))):
            raise GpuError("A graphics card component folder is a link to somewhere else, so "
                           "it was not used. Ask your IT team.")
        base = _long(self.component_dir(cache_dir))
        staging = base.parent / self.staging_dir(cache_dir).name   # never resolved
        shutil.rmtree(staging, ignore_errors=True)
        (staging / "wheels").mkdir(parents=True)
        try:
            site = staging / "site"
            for w in self.wheels:
                dest = staging / "wheels" / (w["name"] + ".whl")
                log(f"Downloading {w['name']} ({w['size'] / 1e9:.2f} GB)…")
                fetch(w["url"], dest, w["size"])
                log(f"Checking {w['name']}…")
                h = hashlib.sha256()
                with open(dest, "rb") as f:
                    for block in iter(lambda: f.read(8 << 20), b""):
                        h.update(block)
                if h.hexdigest() != w["sha256"]:
                    raise GpuError(f"The {w['name']} download does not match its published "
                                   "checksum. Nothing was installed; try again.")
                log(f"Unpacking {w['name']}…")
                with zipfile.ZipFile(dest) as z:
                    members = [m for m in z.namelist() if not m.startswith(self.skip)]
                    for member in members:
                        parts = member.replace("\\", "/").split("/")
                        if member.startswith(("/", "\\")) or ":" in member or ".." in parts:
                            raise GpuError("The download contains an unsafe path.")
                    z.extractall(site, members=members)
                dest.unlink()
            shutil.rmtree(staging / "wheels", ignore_errors=True)
            log("Checking every file…")
            entries = self._record_entries(site)
            if self._unlisted(site, entries, full=True) or any(
                    digest and not rel.startswith(self.skip)
                    and (not (site / rel).is_file() or not _hash_ok(site / rel, digest))
                    for rel, digest, _ in entries):
                raise GpuError("The unpacked files do not match their checksums. Try again.")
            (staging / "installed.json").write_text(json.dumps(
                {"version": self.version,
                 "wheels": [{k: w[k] for k in ("name", "version", "sha256")}
                            for w in self.wheels]}, indent=1), encoding="utf-8")
            older = list(base.parent.glob(self.version + ".invalid-*"))
            if base.exists():
                # The damaged copy is set aside, never deleted silently.
                _rename(base, base.parent / (self.version + ".invalid-" + os.urandom(3).hex()))
            _rename(staging, base)
            for old in older:
                # Only the most recent damaged copy is kept: each is up to several GB.
                shutil.rmtree(old, ignore_errors=True)
            return {"version": self.version}
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _install_key(self, cache_dir):
        marker = self.component_dir(cache_dir) / "installed.json"
        try:
            return str(marker), marker.stat().st_mtime_ns
        except OSError:
            return str(marker), None

    def full_check(self, cache_dir):
        """The full check (every file's SHA-256 and no added file anywhere, a
        few seconds), remembered for this installation in this app session."""
        key = self._install_key(cache_dir)
        _full_checks.pop(key, None)          # no earlier pass stands while this runs
        problems = self.check(cache_dir, full=True)
        _full_checks[key] = not problems
        return problems

    def verified_for_use(self, cache_dir):
        """The full check before the first GPU run of an app session. Once one
        has passed, later runs use the quick check; once one has failed (here or
        in Check every file), the component is not used until a full check
        passes again or it is repaired."""
        recorded = _full_checks.get(self._install_key(cache_dir))
        if recorded is False:
            return False
        if recorded:
            return not self.check(cache_dir)
        return not self.full_check(cache_dir)


# The speaker component's wheels are read at each use, so tests can pin
# their own (gpu.WHEELS).
SPEAKERS = Component(COMPONENT, VERSION, lambda: WHEELS, DOWNLOAD_HOST,
                     lib_dirs=("torch/lib",), skip=SKIP_PREFIXES)
WHISPER = Component(WHISPER_COMPONENT, WHISPER_VERSION, lambda: WHISPER_WHEELS,
                    WHISPER_DOWNLOAD_HOST, lib_dirs=(WHISPER_BIN,),
                    skip=("nvidia/cublas/include/",))
COMPONENTS = {COMPONENT: SPEAKERS, WHISPER_COMPONENT: WHISPER}


def component(name):
    """The component with this name, or None for anything else."""
    return COMPONENTS.get(name) if isinstance(name, str) else None


# The speaker component, by the names the app and its tests have always used.
def staging_dir(cache_dir):
    return SPEAKERS.staging_dir(cache_dir)


def component_dir(cache_dir):
    return SPEAKERS.component_dir(cache_dir)


def site_dir(cache_dir):
    return SPEAKERS.site_dir(cache_dir)


def check(cache_dir, full=False):
    return SPEAKERS.check(cache_dir, full)


def status(cache_dir):
    return SPEAKERS.status(cache_dir)


def install(cache_dir, log, fetch):
    return SPEAKERS.install(cache_dir, log, fetch)


def full_check(cache_dir):
    return SPEAKERS.full_check(cache_dir)


def verified_for_use(cache_dir):
    return SPEAKERS.verified_for_use(cache_dir)



def _rename(src, dst):
    """Rename a folder of freshly written files. Antivirus or the search
    indexer may hold them for a moment, so a rename refused as access denied or
    in use is retried as a model's is (asr_engines.rename_with_retry, #249)."""
    return asr_engines.rename_with_retry(src, dst)


def _cache_bytecode_under(site):
    """Cache the bytecode of modules loaded from under site, and only those,
    even when sys.dont_write_bytecode (-B) is set. A path hook gives the
    folders under site their own finder, whose source loader writes the cache
    itself after compiling the module's own file; every other folder, and any
    zip or egg under site, keeps Python's own finder."""
    import importlib.machinery as machinery
    import importlib.util
    import marshal
    import sys
    root = os.path.normcase(os.path.abspath(site))

    class ComponentLoader(machinery.SourceFileLoader):
        # Python compiles a module only when it found no valid cached bytecode;
        # source_to_code notes what get_code compiled, and get_code caches it.
        _compiled = None

        def source_to_code(self, data, path, *, _optimize=-1):
            code = super().source_to_code(data, path, _optimize=_optimize)
            self._compiled = (path, data, code)
            return code

        def get_code(self, fullname):
            self._compiled = None
            code = super().get_code(fullname)
            compiled, self._compiled = self._compiled, None
            if compiled is None or not sys.dont_write_bytecode:
                return code                      # a valid cache, or Python caches it itself
            path, data, compiled_code = compiled
            if compiled_code is not code or path != self.path:
                return code
            try:
                # Stat first, then read: the cache is written only when the file
                # still holds exactly the bytes compiled, so it always comes from
                # the file, and an edit after the stat leaves it out of date.
                mtime = int(self.path_stats(path)["mtime"])
                if self.get_data(path) != data:
                    return code
                # A timestamp .pyc (PEP 552): magic, flags 0, source mtime and size.
                pyc = (importlib.util.MAGIC_NUMBER + (0).to_bytes(4, "little")
                       + (mtime & 0xFFFFFFFF).to_bytes(4, "little")
                       + (len(data) & 0xFFFFFFFF).to_bytes(4, "little")
                       + marshal.dumps(code))
                self.set_data(importlib.util.cache_from_source(path), pyc)
            except (OSError, ValueError, NotImplementedError):
                pass                             # no cache is slower, never wrong
            return code

    def under(path):
        folder = os.path.normcase(os.path.abspath(path))
        return folder == root or folder.startswith(root + os.sep)

    def hook(path):
        if not under(path):
            raise ImportError("not the GPU component")        # Python's own finder
        if not os.path.isdir(path):
            raise ImportError("not a folder")                 # a zip or egg: zipimporter
        # Python's own loaders, in its own order, except for source files.
        return machinery.FileFinder(
            path, (machinery.ExtensionFileLoader, machinery.EXTENSION_SUFFIXES),
            (ComponentLoader, machinery.SOURCE_SUFFIXES),
            (machinery.SourcelessFileLoader, machinery.BYTECODE_SUFFIXES))

    sys.path_hooks.insert(0, hook)
    for path in [p for p in sys.path_importer_cache if under(p)]:   # found before the hook
        del sys.path_importer_cache[path]


def activate(site):
    """In a fresh worker process, BEFORE anything imports torch: put the CUDA
    build first on the import path and make its DLLs loadable."""
    import sys
    site = str(site)
    # The app starts with -B (its own folder ships precompiled), and this
    # worker inherits it. The component is unpacked from wheels without .pyc:
    # Python caches its bytecode beside it, as before #111 B2, rather than
    # compile torch again at every worker start. Only the component's: -B
    # stays in force for every other module, so the worker writes no bytecode
    # into the app's folder or a checkout (#206).
    _cache_bytecode_under(site)
    sys.path.insert(0, site)
    lib = os.path.join(site, "torch", "lib")
    if os.path.isdir(lib):
        os.add_dll_directory(lib)


# SetDefaultDllDirectories: the application's folder, System32 and folders
# added with os.add_dll_directory only; never the current folder or PATH.
LOAD_LIBRARY_SEARCH_DEFAULT_DIRS = 0x00001000


def restrict_dll_search():
    """Make every later load by bare name in this process search only the safe
    folders. cuBLAS, cuDNN and CTranslate2 load some libraries by name at run
    time (cuBLASLt names NVRTC), and a copy on PATH or in the current folder
    must never be picked up (T25). Called only in the Whisper GPU worker: it
    changes the whole process. Libraries loaded by full path are unaffected."""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetDefaultDllDirectories.argtypes = [ctypes.c_uint32]
    if not k32.SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS):
        raise OSError(ctypes.get_last_error(), "could not restrict the DLL search order")


def activate_whisper(site):
    """In a fresh Whisper GPU worker, BEFORE CTranslate2 first uses the card:
    restrict the DLL search order, then load the component's cuBLAS by full
    path. CTranslate2 asks Windows for cublas64_12.dll by name, and Windows
    then uses the copy already loaded, so a cuBLAS elsewhere on the computer
    (a CUDA toolkit on PATH) is never picked up. Nothing goes on the import
    path: the component is libraries only, and the PyTorch CUDA 13 component
    is never added here."""
    import ctypes
    restrict_dll_search()
    folder = os.path.join(str(site), *WHISPER_BIN.split("/"))
    os.add_dll_directory(folder)
    for name in WHISPER_DLLS:
        ctypes.WinDLL(os.path.join(folder, name))
