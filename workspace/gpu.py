# workspace/gpu.py
# Optional NVIDIA acceleration for speaker identification only.
#
# Speaker identification (pyannote) runs on PyTorch. The package ships the CPU
# build; the CUDA build is an optional component the user downloads in Setup:
# the official PyTorch CUDA 13 wheels for the SAME versions the app pins
# (torch 2.13.0, torchaudio 2.11.0), pinned by SHA-256 and checked again file
# by file against each wheel's RECORD. It is unpacked into its own folder and
# put first on the import path of a separate worker process used only for
# speaker identification, so transcription engines never share a process with
# it (their CUDA libraries are a different version).
#
# The wheels bundle the CUDA runtime and cuDNN libraries; nothing is installed
# system-wide and graphics drivers are never touched. A detected NVIDIA card
# does not by itself enable anything: the component must be installed and the
# setting turned on (Faster mode), and the actual device used is recorded.

import base64
import csv
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

COMPONENT = "gpu-speakers"
VERSION = "torch-2.13.0-cu130"
LICENCE = ("PyTorch BSD-3-Clause; NVIDIA CUDA runtime and cuDNN under NVIDIA's licences",
           "PyTorch CUDA 13.0 build from download-r2.pytorch.org")

# Pinned on 14 October 2026 from https://download.pytorch.org/whl/cu130/ (the
# index publishes these SHA-256 values with each file). record_sha256 pins each
# wheel's own file list (RECORD), so an installed file cannot be changed
# together with its RECORD line and still pass.
WHEELS = (
    {"name": "torch", "version": "2.13.0+cu130", "size": 1915519202,
     "url": "https://download-r2.pytorch.org/whl/cu130/"
            "torch-2.13.0%2Bcu130-cp312-cp312-win_amd64.whl",
     "sha256": "2efab1e83604ca628c6d85b9e188c153690980498d1297081a9dad704919303c",
     "record_sha256": "e415d943930b06b803c218451db82748e7cdfe57c514e5d51de9a175ee5a2c16"},
    {"name": "torchaudio", "version": "2.11.0+cu130", "size": 1722734,
     "url": "https://download-r2.pytorch.org/whl/cu130/"
            "torchaudio-2.11.0%2Bcu130-cp312-cp312-win_amd64.whl",
     "sha256": "f74949f9ace1e4a6cf9468bdb3211b9cfa0af6ea348125471ac71c8621d6c77d",
     "record_sha256": "10cdec950a2b1ea0d80c6967ab49a380c030ebdec3f10f402d4f986efc94d62f"},
)
DOWNLOAD_HOST = "https://download-r2.pytorch.org/whl/cu130/"
DOWNLOAD_BYTES = sum(w["size"] for w in WHEELS)
# CUDA 13.0 needs an NVIDIA driver from the R580 series or later.
MIN_DRIVER = (580, 0)
# C++ headers, needed only to compile extensions, not to run: not unpacked
# (9,379 files; they also pass Windows' 260-character path limit).
SKIP_PREFIXES = ("torch/include/",)


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


def component_dir(cache_dir):
    return Path(cache_dir) / "gpu" / VERSION


def site_dir(cache_dir):
    return component_dir(cache_dir) / "site"


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


def _record_entries(site):
    """(relative path, digest, size) for every file the two wheels installed,
    from their RECORD files, each of which must match its pinned SHA-256."""
    entries = []
    for w in WHEELS:
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


def _hash_ok(path, digest):
    algo, _, want = digest.partition("=")
    if algo != "sha256":
        return False
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode() == want


def _unlisted(site, entries, full):
    """Files that are not in the wheels' RECORD. Any file under the import
    root could be loaded, so none may be added. Quick: the import root and
    torch/lib (where DLLs load from); full: everything. __pycache__ is
    Python's own byte-code cache and cannot be pinned. The folder is in the
    user's own profile, so this check finds damage and stray files; anyone able
    to write there could equally change the app's own installed files."""
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
    lib = Path(site) / "torch" / "lib"
    if lib.is_dir():
        folders = {rel.split("/")[2] for rel in listed
                   if rel.startswith("torch/lib/") and rel.count("/") > 2}
        out += [f"torch/lib/{p.name}" for p in lib.iterdir()
                if f"torch/lib/{p.name}" not in listed
                and not (p.is_dir() and p.name in folders)]
    return out


def check(cache_dir, full=False):
    """Problems with the installed component ([] = fine). Quick: pinned file
    lists, every file present with the right size, no added files at the
    import root or in torch/lib. Full: every file's SHA-256, and no added file
    anywhere. Never raises: anything unreadable is reported as a problem."""
    if not (component_dir(cache_dir) / "installed.json").is_file():
        return ["not installed"]
    try:
        site = _long(site_dir(cache_dir))
        entries = _record_entries(site)
        problems = [f"{rel}: not part of the component"
                    for rel in _unlisted(site, entries, full)]
        for rel, digest, size in entries:
            if not digest or rel.startswith(SKIP_PREFIXES):   # RECORD itself; headers
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


def status(cache_dir):
    """('ready' | 'missing' | 'invalid', plain detail)."""
    problems = check(cache_dir)
    if problems == ["not installed"]:
        return "missing", "Not downloaded yet."
    if problems:
        return "invalid", ("Damaged or incomplete. Repair downloads a fresh, verified copy; "
                           "the damaged copy is kept aside (only the most recent one).")
    return "ready", "Downloaded and verified."


def install(cache_dir, log, fetch):
    """Download (with `fetch(url, dest, expected_size)`), verify and unpack the
    component. Runs in the download process. Nothing replaces a working copy
    until the new one is complete and verified."""
    base = _long(component_dir(cache_dir))
    staging = base.parent / (VERSION + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "wheels").mkdir(parents=True)
    try:
        site = staging / "site"
        for w in WHEELS:
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
                members = [m for m in z.namelist() if not m.startswith(SKIP_PREFIXES)]
                for member in members:
                    parts = member.replace("\\", "/").split("/")
                    if member.startswith(("/", "\\")) or ":" in member or ".." in parts:
                        raise GpuError("The download contains an unsafe path.")
                z.extractall(site, members=members)
            dest.unlink()
        shutil.rmtree(staging / "wheels", ignore_errors=True)
        log("Checking every file…")
        entries = _record_entries(site)
        if _unlisted(site, entries, full=True) or any(
                digest and not rel.startswith(SKIP_PREFIXES)
                and (not (site / rel).is_file() or not _hash_ok(site / rel, digest))
                for rel, digest, _ in entries):
            raise GpuError("The unpacked files do not match their checksums. Try again.")
        (staging / "installed.json").write_text(json.dumps(
            {"version": VERSION, "wheels": [{k: w[k] for k in ("name", "version", "sha256")}
                                            for w in WHEELS]}, indent=1), encoding="utf-8")
        older = list(base.parent.glob(VERSION + ".invalid-*"))
        if base.exists():
            # The damaged copy is set aside, never deleted silently.
            _rename(base, base.parent / (VERSION + ".invalid-" + os.urandom(3).hex()))
        _rename(staging, base)
        for old in older:
            # Only the most recent damaged copy is kept: each is several GB.
            shutil.rmtree(old, ignore_errors=True)
        return {"version": VERSION}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


_verified = set()     # component folders fully checked in this app session


def verified_for_use(cache_dir):
    """Before the first GPU run of an app session: the full check (every
    file's SHA-256 and no added file anywhere, a few seconds). Later runs in
    the same session use the quick check."""
    key = str(component_dir(cache_dir))
    if key in _verified:
        return not check(cache_dir)
    if check(cache_dir, full=True):
        return False
    _verified.add(key)
    return True


def _rename(src, dst, attempts=20, wait=0.5):
    """Rename a folder of freshly written files. Antivirus or the search
    indexer may hold them for a moment, so a refused rename is retried briefly."""
    import time
    for i in range(attempts):
        try:
            return src.rename(dst)
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(wait)


def activate(site):
    """In a fresh worker process, BEFORE anything imports torch: put the CUDA
    build first on the import path and make its DLLs loadable."""
    import sys
    site = str(site)
    sys.path.insert(0, site)
    lib = os.path.join(site, "torch", "lib")
    if os.path.isdir(lib):
        os.add_dll_directory(lib)
