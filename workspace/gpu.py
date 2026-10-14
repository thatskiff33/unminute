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
           "PyTorch CUDA 13.0 build from download.pytorch.org")

# Pinned on 14 October 2026 from https://download.pytorch.org/whl/cu130/ (the
# index publishes these SHA-256 values with each file).
WHEELS = (
    {"name": "torch", "version": "2.13.0+cu130", "size": 1915519202,
     "url": "https://download-r2.pytorch.org/whl/cu130/"
            "torch-2.13.0%2Bcu130-cp312-cp312-win_amd64.whl",
     "sha256": "2efab1e83604ca628c6d85b9e188c153690980498d1297081a9dad704919303c"},
    {"name": "torchaudio", "version": "2.11.0+cu130", "size": 1722734,
     "url": "https://download-r2.pytorch.org/whl/cu130/"
            "torchaudio-2.11.0%2Bcu130-cp312-cp312-win_amd64.whl",
     "sha256": "f74949f9ace1e4a6cf9468bdb3211b9cfa0af6ea348125471ac71c8621d6c77d"},
)
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
    p = Path(path).resolve()
    return Path("\\\\?\\" + str(p)) if os.name == "nt" and not str(p).startswith("\\\\?\\") \
        else p


class GpuError(RuntimeError):
    """A refused or failed GPU step (message is safe to show)."""


def component_dir(cache_dir):
    return Path(cache_dir) / "gpu" / VERSION


def site_dir(cache_dir):
    return component_dir(cache_dir) / "site"


def detect():
    """The NVIDIA card and driver, from nvidia-smi (installed with the driver):
    {"name", "driver", "memory_mb", "driver_ok"} or None if there is none."""
    exe = shutil.which("nvidia-smi") or os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
    try:
        out = subprocess.run([exe, "--query-gpu=name,driver_version,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True,
                             text=True, timeout=10,
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
    """detect(), asked once per app session (nvidia-smi takes a moment)."""
    if not _detected:
        _detected.append(detect())
    return _detected[0]


def _record_entries(site):
    """(relative path, sha256 b64 digest or '', size) for every file the two
    wheels installed, from their RECORD files."""
    entries = []
    for w in WHEELS:
        record = site / f"{w['name']}-{w['version']}.dist-info" / "RECORD"
        if not record.is_file():
            raise GpuError("The graphics card component is incomplete. Repair it in Setup.")
        with open(record, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if row and row[0]:
                    entries.append((row[0], row[1] if len(row) > 1 else "",
                                    int(row[2]) if len(row) > 2 and row[2] else None))
    return entries


def check(cache_dir, full=False):
    """Problems with the installed component ([] = fine). Quick: every file is
    present with the right size. Full: every file's SHA-256 too."""
    site = site_dir(cache_dir)
    if not (component_dir(cache_dir) / "installed.json").is_file():
        return ["not installed"]
    problems = []
    try:
        entries = _record_entries(site)
    except GpuError as exc:
        return [str(exc)]
    site = _long(site)
    for rel, digest, size in entries:
        p = site / rel
        if not digest or rel.startswith(SKIP_PREFIXES):   # RECORD itself; headers
            continue
        if not p.is_file() or (size is not None and p.stat().st_size != size):
            problems.append(f"{rel}: missing or wrong size")
        elif full:
            algo, _, want = digest.partition("=")
            h = hashlib.new(algo)
            with open(p, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            got = base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode()
            if got != want:
                problems.append(f"{rel}: checksum does not match")
        if len(problems) > 20:
            break
    return problems


def status(cache_dir):
    """('ready' | 'missing' | 'invalid', plain detail)."""
    problems = check(cache_dir)
    if problems == ["not installed"]:
        return "missing", "Not downloaded yet."
    if problems:
        return "invalid", ("Damaged or incomplete. Repair downloads a fresh, verified copy; "
                           "the damaged copy is kept aside, not deleted.")
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
                    if member.startswith("/") or ":" in member or ".." in parts:
                        raise GpuError("The download contains an unsafe path.")
                z.extractall(site, members=members)
            dest.unlink()
        (staging / "installed.json").write_text(json.dumps(
            {"version": VERSION, "wheels": [{k: w[k] for k in ("name", "version", "sha256")}
                                            for w in WHEELS]}, indent=1), encoding="utf-8")
        shutil.rmtree(staging / "wheels", ignore_errors=True)
        log("Checking every file…")
        bad = [p for p in _check_site(staging) if p]
        if bad:
            raise GpuError("The unpacked files do not match their checksums. Try again.")
        if base.exists():                   # an earlier damaged copy is kept aside
            base.rename(base.parent / (VERSION + ".invalid-" + os.urandom(3).hex()))
        staging.rename(base)
        return {"version": VERSION}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _check_site(folder):
    """Full check of a component folder that is not yet in place."""
    site = folder / "site"
    out = []
    for rel, digest, _size in _record_entries(site):
        if not digest or rel.startswith(SKIP_PREFIXES):
            continue
        p = site / rel
        algo, _, want = digest.partition("=")
        if not p.is_file():
            out.append(rel)
            continue
        h = hashlib.new(algo)
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode() != want:
            out.append(rel)
    return out


def activate(site):
    """In a fresh worker process, BEFORE anything imports torch: put the CUDA
    build first on the import path and make its DLLs loadable."""
    import sys
    site = str(site)
    sys.path.insert(0, site)
    lib = os.path.join(site, "torch", "lib")
    if os.path.isdir(lib):
        os.add_dll_directory(lib)
