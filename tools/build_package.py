# tools/build_package.py
# Build the preassembled Windows package for Unminute (#11).
#
#   python tools/build_package.py [--version X.Y.Z] [--out dist] [--cache DIR]
#                                 [--release] [--update-binary-allowlist]
#
# The version comes from the VERSION file (#131); --version, if given, must match it.
# --release refuses a working tree with any change, untracked file or file
# hidden from git status, and a HEAD that is not the commit of tag vX.Y.Z. It
# resolves that commit once and reads everything it packages or obeys (the app
# files, the lock, VERSION and the binary allowlist) from it as stored, with no
# git filters, attributes or line-ending settings applied; .cmd and .ps1 get
# CRLF, as .gitattributes says (#111 B2). A development build packages the
# working folder's tracked and untracked files (never ignored ones) and says so
# in version.json.
#
# A release build is reproducible: the same commit and lock, built with the same
# Python, pip and zlib (recorded in the SBOM), give the same zip, byte for byte.
# File times come from SOURCE_DATE_EPOCH, or else the commit's time.
#
# Run on a build machine (this is a deliberate download step; end users never
# assemble anything). The result is one versioned zip plus a .sha256 file:
#
#   Unminute-<version>/
#     runtime/      private CPython 3.14 (official embeddable package, hash-pinned
#                   and Authenticode-checked) with the pinned wheels installed
#     app/          asr_engines.py, offline_guard.py, workspace/ (no tests)
#     install.ps1, uninstall.ps1   per-user installer (see docs/PACKAGING.md)
#     version.json, package-manifest.json (SHA-256 of every file)
#     sbom.cdx.json           CycloneDX software bill of materials (also beside the zip)
#     THIRD_PARTY_NOTICES.txt every bundled component, its licence and licence texts
#     binary-inventory.json   every executable file with its SHA-256 and signer
#     model-manifest.json     the pinned model files (models are not in the package)
#
# What the app never uses is left out (#111 B2): packages nothing imports, the
# test suites inside packages, files only needed to compile against a package,
# and binaries for other processors. Every module is precompiled (unchecked-hash
# .pyc, the same bytes in every build), and the app starts with -I -B, so it
# writes no bytecode into its folder. The zip's size and file count are held to
# the ratchets in tests/budgets.json.
#
# Models are NOT included: they are separate, verified downloads (#19). Every
# executable file (by suffix or by its PE, ELF or Mach-O header) must be listed
# in packaging/binary-allowlist.json with its signer, so a new one, or one that
# changes signer, fails the build until it is reviewed (--update-binary-allowlist
# rewrites the list).

import argparse
import csv
import datetime
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib
from pathlib import Path

# tools/windows_powershell.py, also under -I; after the standard library.
sys.path.append(str(Path(__file__).resolve().parent))
import windows_powershell  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

PYTHON_VERSION = "3.14.8"
EMBED_URL = (f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
             f"python-{PYTHON_VERSION}-embed-amd64.zip")
# Pinned after checking the Authenticode signatures of python.exe,
# pythonw.exe and python314.dll (Python Software Foundation, time-stamped) on
# 17 October 2026; the SHA-256 matches the one python.org publishes for the file.
EMBED_SHA256 = "a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310"
# "3.14" and "314": the wheel tags, the runtime's zip and DLL, and the build host.
PYTHON_MINOR = ".".join(PYTHON_VERSION.split(".")[:2])
PYTHON_TAG = PYTHON_MINOR.replace(".", "")

APP_FILES = ["asr_engines.py", "offline_guard.py", "LICENSE", "README.md", "CHANGELOG.md",
             "unminute.ico"]
# Windows PowerShell 5.1 and many tools stop at 260-character paths. With the
# default install folder (68 characters plus the user name, with a 16-character
# version) every path
# inside the package must stay at or under this length.
MAX_RELATIVE_PATH = 140
SITE = "lib"                 # runtime/lib instead of runtime/Lib/site-packages
PACKAGING = ROOT / "packaging"
# proxy_tools (MIT) has no published wheel. This one was built once from the
# PyPI sdist and is committed with its hash, so the build installs nothing
# unpinned and needs no sdist build step.
VENDORED = {"proxy_tools-0.1.0-py3-none-any.whl":
            "b299ba191b86e8173bf116ee9b2324aa58622bdb4d58b26ec18379b713007ffa"}
BINARY_SUFFIXES = (".exe", ".dll", ".pyd", ".so", ".dylib")
PACKAGED_SCRIPTS = ("install.ps1", "install.cmd", "uninstall.ps1")
# Files git checks out with CRLF (.gitattributes "eol=crlf"; a test keeps the two
# the same). A release writes these with CRLF and every other file as stored.
CRLF_SUFFIXES = (".cmd", ".ps1")
# Signature states a package binary may have; anything else (a hash mismatch, an
# untrusted or unreadable signature) fails the build, and so does a signature
# found in this machine's catalogs rather than in the file itself.
SIGNATURE_OK = ("Valid", "NotSigned")
ALLOWLIST = PACKAGING / "binary-allowlist.json"
APP_ALLOWLIST = PACKAGING / "app-allowlist.json"     # the app folder's source files (#136)
BUDGETS = "tests/budgets.json"
# Left out after installing (#111 B2). An import trace of the window process,
# transcription and speaker identification on CPython 3.14 (17 October 2026)
# loaded none of these. Each must still be there to remove, so a dependency
# change cannot leave a stale entry.
PRUNED_DISTRIBUTIONS = {
    "grpcio": "only opentelemetry's gRPC exporter uses it, and telemetry is off",
    "pyannoteai-sdk": "the client for pyannote's cloud service, never called",
    "setuptools": "a packaging tool; its .pth hook ran at every start",
}
PRUNED_DIRS = ("tests", "test")         # test suites shipped inside packages
# Only needed to compile or link against a package, or to type-check it.
PRUNED_SUFFIXES = (".h", ".hpp", ".hxx", ".cuh", ".lib", ".pdb", ".pyi", ".pyx", ".pxd",
                   ".pxi", ".c", ".cc", ".cpp", ".cxx")
PRUNED_PATHS = (                        # under runtime/lib
    "torch/bin/protoc.exe",             # the protocol buffers compiler
    "clr_loader/ffi/dlls/x86",          # 32-bit: the app is x64 only
    # Only a 32-bit Python loads it (webview/util.py). pywebview's runtimes\win-x86
    # and win-arm64 folders stay: importing its WebView2 module looks them all up
    # and fails if one is missing, which stops the window opening.
    "webview/lib/WebBrowserInterop.x86.dll",
)
PRUNED_RUNTIME = ("libtommath.dll",)   # in the embeddable package; nothing loads it
ZIP_EPOCH_MIN = 315532800      # 1980-01-01, the earliest time a zip entry can hold
# SBOM names. Written as constants: tests/test_consistency.py expects exactly
# one literal "name" value in this file, the package name in version.json.
RUNTIME_NAME = "CPython embeddable package"
BUILDER_NAME = "tools/build_package.py"


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


def powershell(script, **paths):
    """Run a Windows PowerShell 5.1 script with paths passed as environment
    variables ($env:UNMINUTE_<NAME>), never inside the command text, so a quote
    in a folder or user name cannot break it.

    tools/windows_powershell.py starts the child by its absolute path in the
    system folder, with an allowlisted environment (#237): nothing the build
    inherits, such as COR_ENABLE_PROFILING or COMPlus_* (which can load a DLL
    into powershell.exe), reaches it. Its PSModulePath is pinned to the system
    folder's modules (5.1 adds Program Files' modules; both need an
    administrator to change). Inherited, the variable breaks the check when the
    build starts from PowerShell 7 (the shell of GitHub's Windows runners): 7's
    module folders come first, and 5.1 then fails to load
    Microsoft.PowerShell.Security, which holds Get-AuthenticodeSignature. Left
    unset, 5.1 would put the user's own Documents modules folder first, which
    any program running as the user can write to (#235)."""
    try:
        return windows_powershell.run(
            script, {f"UNMINUTE_{k.upper()}": str(v) for k, v in paths.items()})
    except OSError as e:
        sys.exit(str(e))


def said(text, limit=500):
    """A child's output on one line, cut to `limit` characters, for an error
    message (the build's own scripts and paths only: nothing secret)."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "..."


def check_signatures(runtime):
    """The runtime's executables must carry a valid PSF Authenticode signature."""
    if os.name != "nt":
        return
    names = ["python.exe", "pythonw.exe", f"python{PYTHON_TAG}.dll"]
    for name in names:
        r = powershell("$s = Get-AuthenticodeSignature -LiteralPath $env:UNMINUTE_FILE; "
                       "if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch "
                       "'Python Software Foundation') { "
                       "'status ' + $s.Status + ', signer ' + $s.SignerCertificate.Subject; "
                       "exit 1 }",
                       file=str(runtime / name).removeprefix("\\\\?\\"))
        if r.returncode:
            sys.exit(f"The Python runtime's signature check failed ({name}): "
                     + (said(r.stdout + " " + r.stderr) or f"exit code {r.returncode}"))


TARGET = ["--platform", "win_amd64", "--python-version", PYTHON_MINOR, "--implementation", "cp",
          "--only-binary=:all:", "--require-hashes", "--no-deps"]


def install_wheels(site, wheel_cache, report, lock_file):
    """The hash-locked requirements (tools/lock.py), wheels only, for Windows
    x64 CPython 3.14: every file must match a pinned SHA-256, and nothing
    outside the lock is installed (--no-deps). The wheels are downloaded into
    the cache first, then installed from there with no index, and pip's report
    names the exact file used for each package (for the SBOM and notices). The
    one package without a published wheel comes from packaging/wheels (also
    hash-checked)."""
    # -I: no PYTHON* variables, user site-packages or current folder on the
    # import path; --isolated: no PIP_* variables or pip configuration.
    pip = [sys.executable, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check"]
    vendored = PACKAGING / "wheels"
    for name, digest in VENDORED.items():
        if sha256(vendored / name) != digest:
            sys.exit(f"{name} does not match its pinned SHA-256.")
    lock = ["-r", str(lock_file)]
    wheel_cache.mkdir(parents=True, exist_ok=True)
    subprocess.run(pip + ["download", "--dest", str(wheel_cache), "--find-links",
                          str(vendored)] + TARGET + lock, check=True)
    subprocess.run(pip + ["install", "--no-compile", "--target", str(site), "--no-index",
                          "--find-links", str(wheel_cache), "--find-links", str(vendored),
                          "--report", str(report)] + TARGET + lock, check=True)
    return json.loads(Path(report).read_text(encoding="utf-8"))["install"]


def write_pth(runtime):
    """Fixed import path: the runtime's own zip, its site-packages and the app.
    A ._pth file also makes Python ignore PYTHONPATH and user site-packages."""
    pth = next(runtime.glob("python3*._pth"))
    pth.write_text(f"python{PYTHON_TAG}.zip\n.\n{SITE}\n..\\app\nimport site\n",
                   encoding="ascii")
    # site only reads .pth files from Lib\site-packages; runtime\lib is ours,
    # so register it as a site directory (sitecustomize runs at start-up).
    (runtime / "sitecustomize.py").write_text(
        "import os\nimport site\n\n"
        f"site.addsitedir(os.path.join(os.path.dirname(os.path.abspath(__file__)), {SITE!r}))\n",
        encoding="ascii")


def read_record(info):
    """(rows, newline) of a dist-info folder's RECORD (CSV, as pip writes it)."""
    raw = (info / "RECORD").read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return [row for row in csv.reader(io.StringIO(raw, newline="")) if row], newline


def write_record(info, rows, newline):
    out = io.StringIO()
    csv.writer(out, lineterminator=newline).writerows(rows)
    (info / "RECORD").write_bytes(out.getvalue().encode("utf-8"))


def inside(rel):
    """A RECORD path that stays inside the folder it is relative to."""
    parts = rel.replace("\\", "/").split("/")
    return bool(rel) and ".." not in parts and not rel.startswith(("/", "\\")) and ":" not in rel


def sync_records(site):
    """Every RECORD lists only files that are in the package: rows for files
    removed or never placed here (pip's launchers, headers and man pages, and
    pruned or moved files) go, so RECORD lists only installed files that ship
    (the .pyc added later by precompile are not listed)."""
    for info in sorted(site.glob("*.dist-info")):
        if not (info / "RECORD").is_file():
            continue
        rows, newline = read_record(info)
        # A row with ".." or an absolute path points outside the package folder
        # (lib/../bin and the like).
        kept = [row for row in rows if inside(row[0]) and (site / row[0]).is_file()]
        if kept != rows:
            write_record(info, kept, newline)


def prune_portaudio(site):
    """sounddevice's wheel carries PortAudio for every platform, plus builds
    with Steinberg's ASIO SDK (separate licence terms). Keep only the 64-bit
    Windows library without ASIO, which is the one sounddevice loads by default
    (MIT-licensed PortAudio)."""
    folder = site / "_sounddevice_data" / "portaudio-binaries"
    if not folder.is_dir():
        return
    for p in sorted(folder.iterdir()):
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
    came from, so no path is too long for Windows. RECORD is updated to match."""
    for info in sorted(site.glob("*.dist-info")):
        lic = info / "licenses"
        if not lic.is_dir():
            continue
        limit = MAX_RELATIVE_PATH - 10
        deep = sorted((p for p in lic.rglob("*") if p.is_file()
                       and len(f"runtime/{SITE}/{p.relative_to(site).as_posix()}") > limit),
                      key=lambda p: p.relative_to(lic).as_posix())
        if not deep:
            continue
        flat = lic / "flat"
        flat.mkdir(exist_ok=True)
        index, moved = [], {}
        for i, p in enumerate(deep):
            new = flat / f"{i:03d}-{p.name}"
            index.append(f"{new.name}\t{p.relative_to(lic).as_posix()}")
            moved[p.relative_to(site).as_posix()] = new.relative_to(site).as_posix()
            p.replace(new)
        (flat / "INDEX.txt").write_text("\n".join(index) + "\n", encoding="utf-8")
        for d in sorted((d for d in lic.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
            if d != flat and not any(d.iterdir()):
                d.rmdir()
        if (info / "RECORD").is_file():
            rows, newline = read_record(info)
            rows = [[moved.get(row[0], row[0]), *row[1:]] for row in rows]
            rows.append([(flat / "INDEX.txt").relative_to(site).as_posix(), "", ""])
            write_record(info, rows, newline)


def drop_launchers(site):
    """pip --target writes console launchers into lib/bin with the build
    machine's Python path baked in (so their bytes differ from build to build,
    and they would not work elsewhere), and C headers and man pages into
    lib/include and lib/share. None is used at run time; they go (sync_records
    then drops their RECORD rows)."""
    for extra in ("bin", "include", "share"):
        shutil.rmtree(site / extra, ignore_errors=True)


def prune(site, runtime):
    """Remove what the app never uses (see PRUNED_*). Returns the distributions
    removed whole, which then leave the bill of materials and the notices."""
    for name in sorted(PRUNED_DISTRIBUTIONS):
        info = dist_info(site, name)
        rows, _ = read_record(info)
        for row in rows:
            p = site / row[0]
            if inside(row[0]) and p.is_file():
                p.unlink()
        shutil.rmtree(info)
    for rel in PRUNED_PATHS:
        p = site / rel
        if not p.exists():
            sys.exit(f"{rel} is no longer in the package: update PRUNED_PATHS.")
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    for name in PRUNED_RUNTIME:
        if not (runtime / name).is_file():
            sys.exit(f"{name} is no longer in the Python runtime: update PRUNED_RUNTIME.")
        (runtime / name).unlink()
    packages_only = [p for p in sorted(site.iterdir()) if not p.name.endswith(".dist-info")]
    for top in packages_only:
        if not top.is_dir():
            if top.suffix.lower() in PRUNED_SUFFIXES:
                top.unlink()
            continue
        for d in sorted((d for d in top.rglob("*") if d.is_dir() and d.name in PRUNED_DIRS),
                        key=lambda d: len(d.parts)):
            if d.exists():
                shutil.rmtree(d)
        for f in sorted(p for p in top.rglob("*") if p.is_file()
                        and p.suffix.lower() in PRUNED_SUFFIXES):
            f.unlink()
    remove_empty_dirs(site)
    return set(PRUNED_DISTRIBUTIONS)


def remove_empty_dirs(root):
    for d in sorted((d for d in root.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
        if not any(d.iterdir()):
            d.rmdir()


def precompile(folder, python=None):
    """Unchecked-hash .pyc for every module in the packages, the app and the
    runtime's sitecustomize, so nothing is compiled or written when the app
    runs (it starts with -B). Compiled by the package's own Python (python=
    is for tests) with no user site-packages (-s; the runtime's ._pth says
    "import site", which overrides -S, so the package's own site still runs)
    and no import-time caching (-B), rewriting any .pyc already there (-f:
    compileall would keep a timestamp .pyc it thinks is current, with the
    build path in it). A fixed hash seed (checked first), one process (marshal
    marks an object shared only if something else holds it at that moment,
    which parallel workers change from run to run) and paths relative to the
    package make the bytes the same in every build; at import Python records
    the file's real path."""
    python = python or str(folder / "runtime" / "python.exe").removeprefix("\\\\?\\")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env["PYTHONHASHSEED"] = "0"
    base = [python, "-S", "-s", "-B", "-P"]       # -P: nothing from the current folder
    probe = subprocess.run(base + ["-c", "import sys; print(sys.flags.hash_randomization, "
                                         "sys.flags.no_user_site)"],
                           env=env, capture_output=True, text=True)
    if probe.stdout.split() != ["0", "1"]:
        sys.exit("The compile run would not have a fixed hash seed and no user "
                 f"site-packages ({probe.stdout.strip() or probe.stderr.strip()[-200:]}).")
    for sub, flags in ((f"runtime/{SITE}", []), ("app", []), ("runtime", ["-l"])):
        r = subprocess.run(base + ["-m", "compileall", "-q", "-f", *flags,
                            "--invalidation-mode", "unchecked-hash", "-s", str(folder),
                            str(folder / sub)], env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = r.stdout + r.stderr
        if r.returncode or "not a valid prefix" in out:      # compileall only warns of that
            sys.exit(f"Precompiling {sub} failed:\n" + out[-2000:])
    check_precompiled(folder)


def check_precompiled(folder):
    """Every module has its unchecked-hash .pyc, and no .pyc holds the build
    folder's path (which would also name the build machine's user)."""
    marks = {str(folder).encode(), str(folder).removeprefix("\\\\?\\").encode()}
    problems = []
    for sub, pattern in ((f"runtime/{SITE}", "**/*.py"), ("app", "**/*.py"), ("runtime", "*.py")):
        for py in sorted((folder / sub).glob(pattern)):
            pyc = py.parent / "__pycache__" / f"{py.stem}.cpython-{PYTHON_TAG}.pyc"
            if not pyc.is_file():
                problems.append(f"no .pyc: {py.relative_to(folder).as_posix()}")
                continue
            data = pyc.read_bytes()
            if int.from_bytes(data[4:8], "little") != 1:
                problems.append(f"not unchecked-hash: {pyc.relative_to(folder).as_posix()}")
            if any(m in data for m in marks):
                problems.append(f"build path inside: {pyc.relative_to(folder).as_posix()}")
    if problems:
        sys.exit("Precompiled files are wrong:\n" + "\n".join(problems[:20]))


def verify_precompiled(zpath):
    """Every shipped .pyc is what its .py compiles to (#111 B3). Python runs
    the .pyc and never checks it against the source (unchecked-hash), so a
    reviewer reading the shipped .py files would not see an altered .pyc.
    Unpacks the package, notes every .pyc, compiles every module again as the
    build does (precompile is deterministic and path-independent), and compares
    the bytes. The compiler is this Python, which must be the package's
    version, not the package's own runtime, so nothing in the package takes
    part in checking itself. A .pyc that is not __pycache__/<module>.cpython-
    <tag>.pyc beside its .py (a module shipped without its source) cannot be
    checked and is refused. The standard library inside python3XX.zip is not
    covered here: the build checks the runtime's pinned SHA-256, and a
    downloaded zip's attestation covers it. Returns the count compared."""
    if platform.python_version() != PYTHON_VERSION or sys.implementation.name != "cpython":
        sys.exit(f"Verify with CPython {PYTHON_VERSION}, the package's version "
                 f"(this is {sys.implementation.name} {platform.python_version()}).")
    work = Path(tempfile.mkdtemp(prefix="umverify-")).resolve()
    if os.name == "nt":
        work = Path("\\\\?\\" + str(work))
    try:
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            tops = {n.split("/", 1)[0] for n in names}
            if len(tops) != 1 or not all(inside(n) for n in names):
                sys.exit(f"{Path(zpath).name} is not one package folder.")
            z.extractall(work)
        folder = work / tops.pop()

        def digests():
            return {p.relative_to(folder).as_posix(): sha256(p) for p in folder.rglob("*.pyc")}
        shipped = digests()
        suffix = f".cpython-{PYTHON_TAG}.pyc"

        def has_source(name):
            parts = name.split("/")
            if len(parts) < 2 or parts[-2] != "__pycache__" or not parts[-1].endswith(suffix):
                return False
            return (folder.joinpath(*parts[:-2]) / (parts[-1][:-len(suffix)] + ".py")).is_file()
        sourceless = sorted(n for n in shipped if not has_source(n))
        if sourceless:
            sys.exit("The package has .pyc files without their source beside them (outside "
                     "__pycache__, an unexpected name, or no .py), which cannot be checked:\n"
                     + "\n".join(sourceless[:20]))
        for name in shipped:                  # a .pyc the compile does not remake shows
            (folder / name).unlink()
        precompile(folder, python=sys.executable)
        rebuilt = digests()
        differ = sorted(n for n in shipped.keys() | rebuilt.keys()
                        if shipped.get(n) != rebuilt.get(n))
        if differ:
            sys.exit(f"{len(differ)} shipped .pyc files are not what their sources compile "
                     "to (or a module has none):\n" + "\n".join(differ[:20]))
        return len(shipped)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def package_budget(zip_bytes, files, budgets_text):
    """Over-budget messages for the zip's size (MB, one decimal) and file count,
    against the ratchets in tests/budgets.json."""
    budgets = json.loads(budgets_text)
    measured = {"package_zip_mb": round(zip_bytes / 1e6, 1), "package_files": files}
    return [f"{key}: {value} is over its budget of {budgets[key]['value']} ({BUDGETS})"
            for key, value in measured.items() if value > budgets[key]["value"]], measured


def enforce_budget(zpath, files, budgets_text):
    """Delete the zip and stop the build if it is over its size or file-count
    budget. Returns the measured figures."""
    problems, measured = package_budget(zpath.stat().st_size, files, budgets_text)
    if problems:
        zpath.unlink()
        sys.exit("The package is over budget (nothing was written):\n" + "\n".join(problems))
    return measured


def copy_app(app):
    """A development build: the app as it is in the working folder, tracked and
    untracked files only, so an ignored file (a recording or log left in
    workspace/) never ships. Not reproducible."""
    app.mkdir(parents=True)
    for name in APP_FILES:
        shutil.copy2(ROOT / name, app / name)
    listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                 "workspace")
    if listed is None:
        sys.exit("A development build needs git, to leave ignored files out.")
    for rel in sorted(filter(None, listed.split("\0"))):
        source = ROOT / rel
        if not source.is_file():                         # deleted, not yet committed
            continue
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, app / rel)
    for script in PACKAGED_SCRIPTS:
        shutil.copy2(PACKAGING / script, app.parent / script)


def git_env():
    """The environment for git: no GIT_DIR, GIT_WORK_TREE or other git setting
    from outside, so the build reads this checkout and nothing else."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}


def git_bytes(*args, data=None):
    """Raw git output; replace refs are ignored so a commit means what it says."""
    r = subprocess.run(["git", "--no-replace-objects", "-C", str(ROOT), *args],
                       input=data, capture_output=True, env=git_env())
    if r.returncode:
        sys.exit(f"git {args[0]} failed: " + r.stderr.decode("utf-8", "replace").strip())
    return r.stdout


def committed(sha, path):
    """A file's bytes in a commit, exactly as stored (no filters or conversion)."""
    return git_bytes("cat-file", "blob", f"{sha}:{path}")


def committed_tree(sha, paths):
    """{path: bytes} for every file under `paths` in a commit, as stored. Only
    regular files: a symbolic link or submodule stops the build, and so does a
    requested path the commit does not have."""
    listing = git_bytes("ls-tree", "-r", "-z", "--full-tree", sha, "--", *paths)
    blobs, folded = {}, set()
    for entry in filter(None, listing.split(b"\0")):
        meta, name = entry.split(b"\t", 1)
        mode, kind, oid = meta.decode("ascii").split()
        name = name.decode("utf-8")
        if mode not in ("100644", "100755") or kind != "blob":
            sys.exit(f"{name} in the commit is not a regular file ({mode}).")
        # git allows names Windows reads as other paths ("a\..\b", "C:x") or as
        # devices ("NUL.py"), and names Windows trims ("a. ", "b "): refuse
        # anything but plain relative names.
        if (".." in name.split("/") or name.startswith("/")
                or re.search(r'[\\:*?"<>|\x00-\x1f]', name)
                or any(re.fullmatch(r"(?i)(con|prn|aux|nul|conin\$|conout\$|"
                                    r"(com|lpt)[0-9\u00b9\u00b2\u00b3])(\..*)?", part)
                       or part.endswith((".", " ")) for part in name.split("/"))):
            sys.exit(f"Unexpected path in the commit: {name!r}")
        if name.casefold() in folded:       # one would overwrite the other on Windows
            sys.exit(f"Two files in the commit differ only by case: {name!r}")
        folded.add(name.casefold())
        blobs[name] = oid
    for p in paths:
        if not any(n == p or n.startswith(p + "/") for n in blobs):
            sys.exit(f"{p} is not in commit {sha}.")
    out = {}
    # One cat-file process reads every blob: "<oid> blob <size>\n<bytes>\n" each.
    stream = git_bytes("cat-file", "--batch",
                       data="".join(f"{oid}\n" for oid in blobs.values()).encode("ascii"))
    pos = 0
    for name, oid in blobs.items():
        end = stream.index(b"\n", pos)
        got, kind, size = stream[pos:end].decode("ascii").split()
        if got != oid or kind != "blob":
            sys.exit(f"git returned {got} {kind} for {name}.")
        start = end + 1
        out[name] = stream[start:start + int(size)]
        pos = start + int(size) + 1
    return out


def checkout_bytes(name, data):
    """A file as a checkout with this repo's .gitattributes writes it."""
    if name.lower().endswith(CRLF_SUFFIXES):
        return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return data


def check_app_files(app, path=APP_ALLOWLIST):
    """Stop unless the app folder holds exactly the source files the allowlist
    names (#136): a development build also copies untracked files."""
    allowed = set(json.loads(Path(path).read_text(encoding="utf-8"))["app"])
    found = {p.relative_to(app).as_posix() for p in Path(app).rglob("*") if p.is_file()}
    problems = ([f"not listed: {p}" for p in sorted(found - allowed)]
                + [f"listed but missing: {p}" for p in sorted(allowed - found)])
    if problems:
        sys.exit("The app folder differs from packaging/app-allowlist.json ("
                 + "; ".join(problems[:5]) + "). List a new shipped file there, or remove it.")


def export_app(app, sha):
    """A release build: the app and installer files of commit `sha`, as stored
    in git (so no ignored, untracked, locally changed or hidden file can ship,
    and no git setting on this machine changes a byte)."""
    paths = ["workspace", *APP_FILES, *(f"packaging/{s}" for s in PACKAGED_SCRIPTS)]
    for name, data in sorted(committed_tree(sha, paths).items()):
        if name.startswith("packaging/"):
            target = app.parent / name.split("/", 1)[1]
        else:
            target = app / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(checkout_bytes(name, data))


def git(*args):
    """git output, or None when git is missing or the command fails."""
    try:
        return subprocess.run(["git", "--no-replace-objects", "-C", str(ROOT), *args],
                              check=True, capture_output=True, encoding="utf-8",
                              errors="replace", env=git_env()).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_commit():
    return git("rev-parse", "HEAD") or "unknown"


def hidden_files():
    """Tracked files git status does not report (assume-unchanged, lower-case
    tag in ls-files -v, or skip-worktree, "S")."""
    flags = git("ls-files", "-v") or ""
    return [line[2:] for line in flags.splitlines() if line[:1].islower() or line[:1] == "S"]


def clean_tree():
    """True when the working tree has no change, untracked file or hidden file,
    False when it has, None when git cannot tell."""
    status = git("status", "--porcelain", "--untracked-files=all")
    return None if status is None else not status and not hidden_files()


def release_guard(version):
    """A release builds only the commit of tag vX.Y.Z (a tag, not a branch of
    that name), from a tree with no change, no untracked file and no file hidden
    from git status (assume-unchanged or skip-worktree). Returns the commit."""
    status = git("status", "--porcelain", "--untracked-files=all")
    if status is None:
        sys.exit("--release needs git, to check the working tree and the tag.")
    if status:
        sys.exit("--release refuses a working tree with changes or untracked files:\n"
                 + status)
    hidden = hidden_files()
    if hidden:
        sys.exit("--release refuses files hidden from git status (assume-unchanged or "
                 "skip-worktree):\n" + "\n".join(hidden))
    head = git("rev-parse", "HEAD")
    tagged = git("rev-parse", "--verify", "--quiet", f"refs/tags/v{version}^{{commit}}")
    if not tagged or head != tagged:
        sys.exit(f"--release builds only the commit tagged v{version} "
                 f"(HEAD is {head}, the tag is {tagged or 'missing'}).")
    stored = committed(head, "VERSION").decode("utf-8-sig").strip()
    if stored != version:
        sys.exit(f"VERSION in commit {head} is {stored!r}, not {version}.")
    return head


def unchanged_since(version, sha):
    """Just before the zip is written: the release checks still pass, for the
    same commit (nobody checked out another branch or edited the tree while the
    build ran)."""
    if release_guard(version) != sha:
        sys.exit("HEAD or the tree changed during the build; nothing was written.")


def same_as_commit(sha, path):
    """A file this build runs or trusts from the working folder must match the
    commit (line endings aside, for text)."""
    here = (ROOT / path).read_bytes()
    stored = committed(sha, path)
    if here != stored and here.replace(b"\r\n", b"\n") != stored.replace(b"\r\n", b"\n"):
        sys.exit(f"{path} differs from commit {sha}.")


def source_date_epoch(commit="HEAD"):
    """The time stamped on every file: SOURCE_DATE_EPOCH, or the commit's time."""
    given = os.environ.get("SOURCE_DATE_EPOCH")
    raw = given or git("log", "-1", "--format=%ct", commit)
    try:
        return max(int(raw), ZIP_EPOCH_MIN)
    except (TypeError, ValueError):
        if given:
            sys.exit(f"SOURCE_DATE_EPOCH must be whole seconds since 1970, not {given!r}.")
        sys.exit("No SOURCE_DATE_EPOCH and no git commit time (a build outside a git "
                 "checkout needs SOURCE_DATE_EPOCH).")


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def short(text, limit=120):
    """One line of at most `limit` characters (metadata comes from the wheels)."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def licence_of(meta):
    """(kind, value) for a wheel's licence: ("expression", SPDX) when the wheel
    gives one, ("name", text) from classifiers or a short licence field, or
    (None, None) when it states none."""
    if meta.get("license_expression"):
        return "expression", short(meta["license_expression"])
    named = [c.split(" :: ")[-1] for c in meta.get("classifier", [])
             if c.startswith("License :: ") and c.count(" :: ") >= 2]
    if named:
        return "name", short("; ".join(named))
    text = (meta.get("license") or "").strip()
    if text and len(text) <= 80 and "\n" not in text:
        return "name", short(text)
    return None, None


def packages(installed):
    """[(name, version, (kind, licence), wheel file, sha256)] from pip's report,
    sorted by name. A package without a recorded hash stops the build."""
    rows = []
    for item in installed:
        meta = item["metadata"]
        info = item.get("download_info") or {}
        digest = (info.get("archive_info") or {}).get("hashes", {}).get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            sys.exit(f"pip's report has no SHA-256 for {meta.get('name')}.")
        wheel = info.get("url", "").replace("\\", "/").rsplit("/", 1)[-1]
        rows.append((meta["name"], meta["version"], licence_of(meta), wheel, digest))
    return sorted(rows, key=lambda r: r[0].lower())


def build_tools():
    """The build's own tool versions, which the bytes of the zip depend on."""
    return [("python", platform.python_version()),
            ("pip", importlib.metadata.version("pip")),
            ("zlib", zlib.ZLIB_RUNTIME_VERSION)]


def sbom(installed, app_name, version, commit, epoch, runtime_sha, release=False,
         tools=(), removed=()):
    """A CycloneDX 1.6 bill of materials for the package, reproducible (its
    serial number comes from the version, the commit and the kind of build)."""
    def prop(key, value):
        return dict(name=key, value=value)

    def component(name, ver, licence, wheel, digest):
        purl = (f"pkg:pypi/{re.sub(r'[-_.]+', '-', name).lower()}"
                f"@{urllib.parse.quote(ver, safe='')}")
        c = {"type": "library", "bom-ref": purl, "name": name, "version": ver,
             "purl": purl, "hashes": [{"alg": "SHA-256", "content": digest}],
             "properties": [prop("unminute:wheel", wheel)]}
        kind, value = licence
        if kind == "expression":
            c["licenses"] = [{"expression": value}]
        elif kind == "name":
            c["licenses"] = [{"license": dict(name=value)}]
        return c
    runtime = {"type": "platform", "bom-ref": "cpython-embed", "name": RUNTIME_NAME,
               "version": PYTHON_VERSION, "hashes": [{"alg": "SHA-256",
               "content": runtime_sha}], "licenses": [{"license": {"id": "PSF-2.0"}}],
               "externalReferences": [{"type": "distribution", "url": EMBED_URL}]}
    kind = "release" if release else "development"
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
        "serialNumber": "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                     f"unminute/{version}/{commit}/{kind}")),
        "metadata": {"timestamp": iso(epoch),
                     "tools": {"components": [{"type": "application", "name": BUILDER_NAME}]
                               + [{"type": "application", "name": tool, "version": ver}
                                  for tool, ver in tools]},
                     "component": {"type": "application", "bom-ref": "unminute",
                                   "name": app_name, "version": version,
                                   "properties": [prop("unminute:commit", commit),
                                                  prop("unminute:build", kind)]
                                   + ([prop("unminute:removed", ", ".join(sorted(removed)))]
                                      if removed else [])}},
        "components": [runtime] + [component(*row) for row in packages(installed)],
    }


def canonical(name):
    return re.sub(r"[-_.]+", "_", name).lower()


def dist_info(site, name):
    """A distribution's dist-info folder. None stops the build."""
    for info in sorted(site.glob("*.dist-info")):
        if canonical(info.name[:-len(".dist-info")].rsplit("-", 1)[0]) == canonical(name):
            return info
    sys.exit(f"No dist-info folder for {name}: cannot list its files.")


def wheel_files(site, name):
    """The files a wheel installed (its RECORD), relative to site. A package
    with no dist-info folder stops the build."""
    rows, _ = read_record(dist_info(site, name))
    return sorted((row[0] for row in rows), key=str.lower)


def licence_files(site, name):
    """The licence and notice texts a wheel installed: everything under its
    dist-info licenses/ folder, and files named like a licence anywhere in it
    (some keep them beside the code), as paths inside the package."""
    return [f"runtime/{SITE}/{f}" for f in wheel_files(site, name)
            if re.match(r"[^/]+\.dist-info/licenses/", f)
            or re.match(r"(?i)(licen[cs]e|copying|notice|authors|thirdpartynotices)",
                        f.rsplit("/", 1)[-1])]


def native_libraries(site, name):
    """The DLLs a wheel installed (its own code is .pyd; a DLL is often a
    library copied in from another project, with its own licence)."""
    return [f for f in wheel_files(site, name) if f.lower().endswith(".dll")]


def notices(installed, site, folder, version, signed):
    """THIRD_PARTY_NOTICES.txt: every bundled component, its licence, where
    its licence texts are in the package, and the native libraries it bundles
    with their signers. Every path named must be in the package."""
    lines = [f"Unminute {version}: third-party software", "",
             "Unminute includes the software below, each under its own licence. The",
             "licence texts are in the files named for each component (paths are inside",
             "the package). Some components bundle native libraries from other projects;",
             "those are listed with their signers, and not all of their licence texts are",
             "included yet (to follow, #111). Models are not included: see",
             "model-manifest.json.", "",
             f"Python {PYTHON_VERSION} (CPython embeddable package)",
             "  Licence: PSF-2.0", "  Texts: runtime/LICENSE.txt", ""]
    named = ["runtime/LICENSE.txt"]
    for name, ver, (_, licence), _, _ in packages(installed):
        texts = licence_files(site, name)
        named += texts
        lines += [f"{name} {ver}",
                  f"  Licence: {licence or 'not stated in the metadata (see the texts)'}",
                  "  Texts: " + (", ".join(texts) if texts else "none shipped in the wheel")]
        libs = native_libraries(site, name)
        if libs:
            lines.append("  Native libraries:")
            lines += [f"    runtime/{SITE}/{f} ({signer_name(signed, f'runtime/{SITE}/{f}')})"
                      for f in libs]
        lines.append("")
    missing = [p for p in named if not (folder / p).is_file()]
    if missing:
        sys.exit("The notices name files that are not in the package:\n" + "\n".join(missing))
    return "\n".join(lines)


def signer_name(signed, path):
    if path not in signed:
        sys.exit(f"{path} is not in the signature results (a path that differs only "
                 "in case?).")
    info = signed[path]
    cn = re.search(r"CN=(\"[^\"]+\"|[^,]+)", info.get("signer") or "")
    return f"signed by {cn.group(1).strip(chr(34))}" if cn else "unsigned"


def model_manifest(source):
    """The pinned model files, for IT to verify a pre-staged copy (#111 B7), from
    the asr_engines.py being packaged. Compiled in memory: an import would write
    a .pyc beside it, into the package, stamped with the file's time."""
    import types
    ae = types.ModuleType("_packaged_asr_engines")
    ae.__file__ = str(source)
    sys.modules[ae.__name__] = ae        # dataclasses look their module up by name
    try:
        exec(compile(Path(source).read_bytes(), str(source), "exec"), ae.__dict__)
    finally:
        sys.modules.pop(ae.__name__, None)
    specs = [*ae.PARAKEET_MODELS.values(), ae.SILERO_VAD, *ae.WHISPER_MODELS.values(),
             ae.SPEAKER_MODEL]
    return {"_about": "Models are not in the package. These are the files each model "
                      "must have, by size and SHA-256 (docs/MODEL_MANIFEST.md).",
            "models": [{"name": s.name, "licence": ae.LICENCES[s.name][0],
                        "repository": s.repo, "revision": s.revision, "gated": s.gated,
                        "files": [{"name": f.name, "size": f.size, "sha256": f.sha256}
                                  for f in s.files]} for s in specs]}


def is_native(path):
    """A PE (Windows), ELF or Mach-O file, whatever its name."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if head[:2] == b"MZ" and len(head) == 64:
                f.seek(int.from_bytes(head[0x3C:0x40], "little"))
                return f.read(4) == b"PE\0\0"
    except OSError:
        return False
    return head[:4] in (b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
                        b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe")


def binaries(folder):
    """Every executable file in the package, by suffix or by header, as sorted
    relative paths."""
    return sorted((p.relative_to(folder).as_posix() for p in folder.rglob("*")
                   if p.is_file() and (p.suffix.lower() in BINARY_SUFFIXES or is_native(p))),
                  key=str.lower)


def check_binaries(signed, path=ALLOWLIST, update=False):
    """The package's executables must be exactly those allowlisted, each with the
    same signer (or still unsigned). Returns the problems, or rewrites the list
    when asked (for review in the pull request)."""
    found = {p: info["signer"] for p, info in signed.items()}
    if update:
        path.write_text(json.dumps({
            "_about": "Every executable file the package may contain, with its "
                      "Authenticode signer ('' when unsigned). A file not listed, missing, "
                      "or with another signer fails the build (tools/build_package.py); "
                      "rewrite with --update-binary-allowlist and review the change.",
            "binaries": [{"path": p, "signer": found[p]}
                         for p in sorted(found, key=str.lower)]},
            indent=1) + "\n", encoding="utf-8", newline="\n")
        return []
    allowed = {e["path"]: e["signer"]
               for e in json.loads(path.read_text(encoding="utf-8"))["binaries"]}
    problems = [f"not allowlisted: {p}" for p in sorted(set(found) - set(allowed), key=str.lower)]
    problems += [f"allowlisted but missing: {p}"
                 for p in sorted(set(allowed) - set(found), key=str.lower)]
    problems += [f"signer changed: {p} ({allowed[p] or 'unsigned'} -> {found[p] or 'unsigned'})"
                 for p in sorted(set(found) & set(allowed), key=str.lower)
                 if allowed[p] != found[p]]
    return problems


def signers(folder, paths):
    """{path: {status, signer, thumbprint, timestamped}} from Authenticode, in
    one PowerShell run. A state other than Valid or NotSigned stops the build."""
    plain = str(folder).removeprefix("\\\\?\\")
    full = [f"{plain}\\{p.replace('/', chr(92))}" for p in paths]
    too_long = [f for f in full if len(f) >= 260]
    if too_long:
        sys.exit("Build folder paths too long for the signature check (move the temp "
                 "folder): " + too_long[0])
    with tempfile.TemporaryDirectory() as tmp:
        listed, out = Path(tmp) / "files.txt", Path(tmp) / "signers.json"
        listed.write_text("\n".join(full), encoding="utf-8")
        r = powershell(
            "$r = foreach ($p in Get-Content -LiteralPath $env:UNMINUTE_LIST -Encoding UTF8) {"
            " $s = Get-AuthenticodeSignature -LiteralPath $p;"
            " [pscustomobject]@{ status = [string]$s.Status;"
            " signer = if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '' };"
            " thumbprint = if ($s.SignerCertificate) { $s.SignerCertificate.Thumbprint }"
            " else { '' }; timestamped = [bool]$s.TimeStamperCertificate;"
            " type = [string]$s.SignatureType } };"
            " ConvertTo-Json -InputObject @($r) -Compress | Set-Content -LiteralPath"
            " $env:UNMINUTE_OUT -Encoding UTF8", list=listed, out=out)
        if r.returncode or not out.is_file():
            sys.exit("The signature check failed: "
                     + (said(r.stderr) or f"exit code {r.returncode}"))
        rows = json.loads(out.read_text(encoding="utf-8-sig"))
        # A cmdlet error is not fatal to the script, so it can exit 0 with
        # empty results: keep what it said for the message below.
        errors = said(r.stderr)
    if len(rows) != len(paths):
        sys.exit("The signature check did not return one result per binary.")
    result = dict(zip(paths, rows, strict=True))
    bad = [f"{p}: {r['status'] or 'no result'}" for p, r in result.items()
           if r["status"] not in SIGNATURE_OK]
    bad += [f"{p}: signature from this machine's catalog, not the file" for p, r
            in result.items() if r["status"] == "Valid" and r.get("type") == "Catalog"]
    if bad:
        sys.exit("Binaries with a bad or unreadable signature:\n" + "\n".join(bad)
                 + (f"\nPowerShell said: {errors}" if errors else ""))
    return result


def binary_inventory(folder, signed):
    """Facts about each executable file, not trust verdicts (those depend on
    the build machine's certificate store and clock)."""
    return {"_about": "Every executable file in the package: size, SHA-256 and its "
                      "Authenticode signature (signer, certificate thumbprint, whether it "
                      "carries a time stamp). Unsigned files have no signer.",
            "binaries": [{"path": p, "size": (folder / p).stat().st_size,
                          "sha256": sha256(folder / p), "signed": bool(info["signer"]),
                          "signer": info["signer"], "thumbprint": info["thumbprint"],
                          "timestamped": bool(info["timestamped"])}
                         for p, info in signed.items()]}


def write_zip(folder, zpath, epoch):
    """A reproducible zip: sorted entries, one fixed time stamp, fixed
    permissions and compression."""
    stamp = datetime.datetime.fromtimestamp(epoch, datetime.UTC).timetuple()[:6]
    files = sorted((p for p in folder.rglob("*") if p.is_file()),
                   key=lambda p: p.relative_to(folder.parent).as_posix())
    with zipfile.ZipFile(zpath, "w") as z:
        for p in files:
            info = zipfile.ZipInfo(p.relative_to(folder.parent).as_posix(), date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 0
            z.writestr(info, p.read_bytes(), compresslevel=6)


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
    if os.name != "nt" or f"{sys.version_info[0]}.{sys.version_info[1]}" != PYTHON_MINOR or \
            sys.implementation.name != "cpython" or sys.maxsize < 2**32:
        sys.exit(f"Build the package with 64-bit CPython {PYTHON_MINOR} on Windows.")


def build(version, out, cache, release=False, update_allowlist=False):
    check_host()
    if release and update_allowlist:
        sys.exit("A release build cannot rewrite the binary allowlist.")
    if release and not sys.flags.isolated:
        # Without -I, PYTHONPATH, user site-packages and the script's folder
        # (where a stray ignored .pyc would not show in git status) are imported from.
        sys.exit("Run a release build with python -I tools/build_package.py --release.")
    sha = release_guard(version) if release else None
    epoch = source_date_epoch(sha or "HEAD")
    commit = sha or git_commit()
    name = f"Unminute-{version}"
    # Build in an extended-length path (\\?\): the temp folder plus the
    # deepest wheel files can pass 260 characters on the build machine, and
    # Windows APIs then silently skip those files. (Paths inside the package
    # are still kept to MAX_RELATIVE_PATH for the machines it installs on.)
    work = Path(tempfile.mkdtemp(prefix="umbuild-")).resolve()
    if os.name == "nt":
        work = Path("\\\\?\\" + str(work))
    folder = work / name
    runtime = folder / "runtime"
    site = runtime / SITE
    try:
        work.mkdir(exist_ok=True)
        # What the build obeys: from the commit for a release, else the folder.
        lock_file, allowlist = ROOT / "requirements.txt", ALLOWLIST
        app_list = APP_ALLOWLIST
        budgets = (ROOT / BUDGETS).read_text(encoding="utf-8")
        if release:
            lock_file, allowlist = work / "requirements.txt", work / "binary-allowlist.json"
            lock_file.write_bytes(committed(sha, "requirements.txt"))
            allowlist.write_bytes(committed(sha, "packaging/binary-allowlist.json"))
            app_list = work / "app-allowlist.json"
            app_list.write_bytes(committed(sha, "packaging/app-allowlist.json"))
            budgets = committed(sha, BUDGETS).decode("utf-8")
            for path in ("tools/build_package.py",
                         *(f"packaging/wheels/{w}" for w in VENDORED)):
                same_as_commit(sha, path)
        embed = fetch_runtime(cache)
        with zipfile.ZipFile(embed) as z:
            z.extractall(runtime)
        check_signatures(runtime)
        write_pth(runtime)
        installed = install_wheels(site, cache / "wheels", work / "pip-report.json",
                                   lock_file)
        flatten_licences(site)
        prune_portaudio(site)
        drop_launchers(site)
        removed = prune(site, runtime)
        installed = [i for i in installed
                     if canonical(i["metadata"]["name"]) not in map(canonical, removed)]
        sync_records(site)
        if release:
            export_app(folder / "app", sha)
        else:
            copy_app(folder / "app")
        check_app_files(folder / "app", app_list)       # before precompile adds the .pyc
        precompile(folder)
        info = {
            # "name": Start menu and Settings > Apps name; installers read it so a
            # rollback recreates what this version's uninstaller removes.
            "name": "Unminute",
            # built_at is the commit's time (or SOURCE_DATE_EPOCH), so a rebuild of
            # the same commit is identical.
            "version": version, "built_at": iso(epoch),
            "python": PYTHON_VERSION, "commit": commit,
            # Only a --release build is published (docs/RELEASING.md checks this).
            # A development build may hold uncommitted changes: "clean" says
            # whether the working tree had none (None when git cannot tell).
            "release": release, "clean": True if release else clean_tree(),
            # Installers check for features before relying on them (#26): "gate"
            # is --gate, "restore-lid" is --restore-lid (the uninstaller, #191).
            "features": ["gate", "restore-lid"]}
        (folder / "version.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        (folder / "model-manifest.json").write_text(
            json.dumps(model_manifest(folder / "app" / "asr_engines.py"), indent=1),
            encoding="utf-8")
        found = binaries(folder)
        signed = signers(folder, found)
        problems = check_binaries(signed, allowlist, update=update_allowlist)
        if problems:
            sys.exit("The package's binaries differ from packaging/binary-allowlist.json "
                     "(review, then rebuild with --update-binary-allowlist):\n"
                     + "\n".join(problems))
        (folder / "binary-inventory.json").write_text(
            json.dumps(binary_inventory(folder, signed), indent=1), encoding="utf-8")
        (folder / "THIRD_PARTY_NOTICES.txt").write_text(
            notices(installed, site, folder, version, signed), encoding="utf-8")
        bom = sbom(installed, info["name"], version, commit, epoch, sha256(embed), release,
                   build_tools(), removed)
        (folder / "sbom.cdx.json").write_text(json.dumps(bom, indent=1), encoding="utf-8")
        too_long = [p.relative_to(folder).as_posix() for p in folder.rglob("*")
                    if len(p.relative_to(folder).as_posix()) > MAX_RELATIVE_PATH]
        if too_long:
            sys.exit("Paths too long for Windows installs:\n" + "\n".join(too_long))
        count = manifest(folder)
        if release:
            unchanged_since(version, sha)
        out.mkdir(parents=True, exist_ok=True)
        zpath = out / f"{name}.zip"
        write_zip(folder, zpath, epoch)
        measured = enforce_budget(zpath, count + 1, budgets)
        digest = sha256(zpath)
        (out / f"{name}.zip.sha256").write_text(f"{digest}  {zpath.name}\n", encoding="ascii")
        shutil.copyfile(folder / "sbom.cdx.json", out / f"{name}.cdx.json")
        kind = "release" if release else "DEVELOPMENT build (not for publishing)"
        print(f"{zpath} ({measured['package_zip_mb']} MB, {measured['package_files']} files, "
              f"{kind})\nSHA-256 {digest}")
        return zpath
    finally:
        shutil.rmtree(work, ignore_errors=True)


VERSION_SHAPE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(-preview\.[0-9]+)?")


def release_version(asked=None, root=ROOT):
    """The version to build: VERSION's, which --version may repeat but not change."""
    path = Path(root) / "VERSION"
    try:
        version = path.read_text(encoding="utf-8-sig").strip()   # a byte-order mark is dropped
    except OSError as exc:
        raise SystemExit(f"Cannot read {path} ({exc}). It is set by tools/changelog.py "
                         "when a release is prepared.") from None
    # The installer accepts version names up to 16 characters (packaging/install.ps1).
    if not VERSION_SHAPE.fullmatch(version) or len(version) > 16:
        raise SystemExit(f"VERSION holds {version!r}, not X.Y.Z or X.Y.Z-preview.N "
                         "(16 characters at most).")
    if asked is not None and asked != version:
        raise SystemExit(f"--version {asked} does not match VERSION ({version}). "
                         "VERSION is set by tools/changelog.py when a release is prepared.")
    return version


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", help="must match the VERSION file, if given")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "umbuild-cache"))
    ap.add_argument("--release", action="store_true",
                    help="refuse a dirty tree or a HEAD that is not the vX.Y.Z tag")
    ap.add_argument("--update-binary-allowlist", action="store_true",
                    help="rewrite packaging/binary-allowlist.json from this build (review it)")
    ap.add_argument("--verify-precompiled", metavar="ZIP",
                    help="build nothing: check that every .pyc in this package zip is what "
                         "its source compiles to (on Windows, with the package's own Python)")
    a = ap.parse_args()
    if a.verify_precompiled:
        count = verify_precompiled(Path(a.verify_precompiled))
        print(f"{count} .pyc files are what their sources compile to.")
        return
    if a.release and a.update_binary_allowlist:
        ap.error("--release cannot rewrite the binary allowlist")
    build(release_version(a.version), Path(a.out), Path(a.cache), a.release,
          a.update_binary_allowlist)


if __name__ == "__main__":
    main()
