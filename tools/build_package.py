# tools/build_package.py
# Build the preassembled Windows package for the meeting workspace (#11).
#
#   python tools/build_package.py --version 0.2.0 [--out dist] [--cache DIR]
#
# Run on a build machine (this is a deliberate download step; end users never
# assemble anything). The result is one versioned zip plus a .sha256 file:
#
#   WhisperWorkspace-<version>/
#     runtime/      private CPython 3.12 (official embeddable package, hash-pinned
#                   and Authenticode-checked) with the pinned wheels installed
#     app/          asr_engines.py, offline_guard.py, workspace/ (no tests)
#     install.ps1, uninstall.ps1   per-user installer (see docs/PACKAGING.md)
#     version.json, package-manifest.json (SHA-256 of every file)
#
# Models are NOT included: they are separate, verified downloads (#19).

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PYTHON_VERSION = "3.12.10"
EMBED_URL = (f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
             f"python-{PYTHON_VERSION}-embed-amd64.zip")
# Pinned after checking the Authenticode signatures of python.exe,
# pythonw.exe and python312.dll (Python Software Foundation) on 13 October 2026.
EMBED_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

APP_FILES = ["asr_engines.py", "offline_guard.py", "LICENSE", "README.md", "CHANGELOG.md",
             "whisper.ico"]
# Windows PowerShell 5.1 and many tools stop at 260-character paths. With the
# default install folder (about 85 characters plus the user name) every path
# inside the package must stay at or under this length.
MAX_RELATIVE_PATH = 140
SITE = "lib"                 # runtime/lib instead of runtime/Lib/site-packages
PACKAGING = ROOT / "packaging"
# proxy_tools (MIT) has no published wheel. This one was built once from the
# PyPI sdist and is committed with its hash, so the build installs nothing
# unpinned and needs no sdist build step.
VENDORED = {"proxy_tools-0.1.0-py3-none-any.whl":
            "b299ba191b86e8173bf116ee9b2324aa58622bdb4d58b26ec18379b713007ffa"}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch_runtime(cache):
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / Path(EMBED_URL).name
    if not target.exists() or sha256(target) != EMBED_SHA256:
        print(f"Downloading {EMBED_URL}")
        with urllib.request.urlopen(EMBED_URL, timeout=120) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != EMBED_SHA256:
            sys.exit("The Python runtime download does not match the pinned SHA-256.")
        target.write_bytes(data)
    return target


def check_signatures(runtime):
    """The runtime's executables must carry a valid PSF Authenticode signature."""
    if os.name != "nt":
        return
    names = ["python.exe", "pythonw.exe", f"python{PYTHON_VERSION.replace('.', '')[:3]}.dll"]
    script = "; ".join(
        f"$s = Get-AuthenticodeSignature '{runtime / n}'; "
        f"if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch "
        f"'Python Software Foundation') {{ exit 1 }}" for n in names)
    if subprocess.run(["powershell", "-NoProfile", "-Command", script]).returncode:
        sys.exit("The Python runtime's signature check failed.")


def install_wheels(site, wheel_cache):
    """Pinned requirements, wheels only, for Windows x64 CPython 3.12. The one
    package without a published wheel comes from packaging/wheels (hash-checked)."""
    reqs = [ROOT / "requirements.txt", ROOT / "workspace" / "requirements.txt"]
    pip = [sys.executable, "-m", "pip", "--disable-pip-version-check"]
    vendored = PACKAGING / "wheels"
    for name, digest in VENDORED.items():
        if sha256(vendored / name) != digest:
            sys.exit(f"{name} does not match its pinned SHA-256.")
    cmd = pip + ["install", "--no-compile", "--target", str(site), "--platform",
                 "win_amd64", "--python-version", "3.12", "--implementation", "cp",
                 "--only-binary=:all:", "--find-links", str(vendored)]
    for r in reqs:
        cmd += ["-r", str(r)]
    subprocess.run(cmd, check=True)


def write_pth(runtime):
    """Fixed import path: the runtime's own zip, its site-packages and the app.
    A ._pth file also makes Python ignore PYTHONPATH and user site-packages."""
    pth = next(runtime.glob("python3*._pth"))
    pth.write_text(f"python312.zip\n.\n{SITE}\n..\\app\nimport site\n", encoding="ascii")
    # site only reads .pth files from Lib\site-packages; runtime\lib is ours,
    # so register it as a site directory (sitecustomize runs at start-up).
    (runtime / "sitecustomize.py").write_text(
        "import os\nimport site\n\n"
        f"site.addsitedir(os.path.join(os.path.dirname(os.path.abspath(__file__)), {SITE!r}))\n",
        encoding="ascii")


def prune_portaudio(site):
    """sounddevice's wheel carries PortAudio for every platform, plus builds
    with Steinberg's ASIO SDK (separate licence terms). Keep only the 64-bit
    Windows library without ASIO, which is the one sounddevice loads by default
    (MIT-licensed PortAudio)."""
    folder = site / "_sounddevice_data" / "portaudio-binaries"
    if not folder.is_dir():
        return
    for p in folder.iterdir():
        if p.is_file() and p.name not in ("libportaudio64bit.dll", "README.md"):
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)
    if not (folder / "libportaudio64bit.dll").is_file():
        sys.exit("sounddevice's 64-bit PortAudio library is missing.")


def flatten_licences(site):
    """Some wheels ship licence files in very deep folders (torch's dist-info
    reproduces its whole third-party tree). Keep every licence text, but move
    deep ones up to <dist-info>/licenses/flat/ with an index of where each
    came from, so no path is too long for Windows."""
    for info in site.glob("*.dist-info"):
        lic = info / "licenses"
        if not lic.is_dir():
            continue
        limit = MAX_RELATIVE_PATH - 10
        deep = [p for p in lic.rglob("*") if p.is_file()
                and len(f"runtime/{SITE}/{p.relative_to(site).as_posix()}") > limit]
        if not deep:
            continue
        flat = lic / "flat"
        flat.mkdir(exist_ok=True)
        index = []
        for i, p in enumerate(deep):
            new = flat / f"{i:03d}-{p.name}"
            index.append(f"{new.name}\t{p.relative_to(lic).as_posix()}")
            p.replace(new)
        (flat / "INDEX.txt").write_text("\n".join(index) + "\n", encoding="utf-8")
        for d in sorted((d for d in lic.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
            if d != flat and not any(d.iterdir()):
                d.rmdir()


def copy_app(app):
    app.mkdir(parents=True)
    for name in APP_FILES:
        shutil.copy2(ROOT / name, app / name)
    shutil.copytree(ROOT / "workspace", app / "workspace",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "requirements.txt"))


def git_commit():
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def manifest(folder):
    files = {}
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.name != "package-manifest.json":
            files[p.relative_to(folder).as_posix()] = [p.stat().st_size, sha256(p)]
    (folder / "package-manifest.json").write_text(json.dumps(files, indent=0),
                                                  encoding="utf-8")
    return len(files)


def check_host():
    """Environment markers are evaluated against this interpreter."""
    if os.name != "nt" or sys.version_info[:2] != (3, 12) or \
            sys.implementation.name != "cpython" or sys.maxsize < 2**32:
        sys.exit("Build the package with 64-bit CPython 3.12 on Windows.")


def build(version, out, cache):
    check_host()
    name = f"WhisperWorkspace-{version}"
    # Build in an extended-length path (\\?\): the temp folder plus the
    # deepest wheel files can pass 260 characters on the build machine, and
    # Windows APIs then silently skip those files. (Paths inside the package
    # are still kept to MAX_RELATIVE_PATH for the machines it installs on.)
    work = Path(tempfile.mkdtemp(prefix="wwbuild-")).resolve()
    if os.name == "nt":
        work = Path("\\\\?\\" + str(work))
    folder = work / name
    runtime = folder / "runtime"
    try:
        with zipfile.ZipFile(fetch_runtime(cache)) as z:
            z.extractall(runtime)
        check_signatures(runtime)
        write_pth(runtime)
        install_wheels(runtime / SITE, cache / "wheels")
        flatten_licences(runtime / SITE)
        prune_portaudio(runtime / SITE)
        # pip --target writes console launchers with the build machine's
        # Python path baked in; they do not work elsewhere and reveal it.
        shutil.rmtree(runtime / SITE / "bin", ignore_errors=True)
        copy_app(folder / "app")
        for script in ("install.ps1", "install.cmd", "uninstall.ps1"):
            shutil.copy2(PACKAGING / script, folder / script)
        (folder / "version.json").write_text(json.dumps({
            "version": version, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "python": PYTHON_VERSION, "commit": git_commit(),
            # Installers check for features before relying on them (#26).
            "features": ["gate"]}, indent=2), encoding="utf-8")
        too_long = [p.relative_to(folder).as_posix() for p in folder.rglob("*")
                    if len(p.relative_to(folder).as_posix()) > MAX_RELATIVE_PATH]
        if too_long:
            sys.exit("Paths too long for Windows installs:\n" + "\n".join(too_long))
        count = manifest(folder)
        out.mkdir(parents=True, exist_ok=True)
        zpath = out / f"{name}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(folder.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(work).as_posix())
        digest = sha256(zpath)
        (out / f"{name}.zip.sha256").write_text(f"{digest}  {zpath.name}\n", encoding="ascii")
        print(f"{zpath} ({zpath.stat().st_size / 1e6:.0f} MB, {count} files)\nSHA-256 {digest}")
        return zpath
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "wwbuild-cache"))
    a = ap.parse_args()
    build(a.version, Path(a.out), Path(a.cache))


if __name__ == "__main__":
    main()
