# tools/build_package.py
# Build the preassembled Windows package for Unminute (#11).
#
#   python tools/build_package.py [--version X.Y.Z] [--out dist] [--cache DIR]
#                                 [--release] [--update-binary-allowlist]
#
# The version comes from the VERSION file (#131); --version, if given, must match it.
# --release refuses a working tree with any change or untracked file, and a HEAD
# that is not the commit tagged vX.Y.Z (#111 B2).
#
# The build is reproducible: the same commit and lock give the same zip, byte for
# byte. File times come from SOURCE_DATE_EPOCH, or else the commit's time.
#
# Run on a build machine (this is a deliberate download step; end users never
# assemble anything). The result is one versioned zip plus a .sha256 file:
#
#   Unminute-<version>/
#     runtime/      private CPython 3.12 (official embeddable package, hash-pinned
#                   and Authenticode-checked) with the pinned wheels installed
#     app/          asr_engines.py, offline_guard.py, workspace/ (no tests)
#     install.ps1, uninstall.ps1   per-user installer (see docs/PACKAGING.md)
#     version.json, package-manifest.json (SHA-256 of every file)
#     sbom.cdx.json           CycloneDX software bill of materials (also beside the zip)
#     THIRD_PARTY_NOTICES.txt every bundled component, its licence and licence texts
#     binary-inventory.json   every .exe, .dll and .pyd with its SHA-256 and signer
#     model-manifest.json     the pinned model files (models are not in the package)
#
# Models are NOT included: they are separate, verified downloads (#19). Every
# binary must be listed in packaging/binary-allowlist.json, so a new one fails
# the build until it is reviewed (--update-binary-allowlist rewrites the list).

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))                 # asr_engines, for the model manifest

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
BINARY_SUFFIXES = (".exe", ".dll", ".pyd")
ALLOWLIST = PACKAGING / "binary-allowlist.json"
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


TARGET = ["--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp",
          "--only-binary=:all:", "--require-hashes", "--no-deps"]


def install_wheels(site, wheel_cache, report):
    """The hash-locked requirements (tools/lock.py), wheels only, for Windows
    x64 CPython 3.12: every file must match a pinned SHA-256, and nothing
    outside the lock is installed (--no-deps). The wheels are downloaded into
    the cache first, then installed from there with no index, and pip's report
    names the exact file used for each package (for the SBOM and notices). The
    one package without a published wheel comes from packaging/wheels (also
    hash-checked)."""
    pip = [sys.executable, "-m", "pip", "--disable-pip-version-check"]
    vendored = PACKAGING / "wheels"
    for name, digest in VENDORED.items():
        if sha256(vendored / name) != digest:
            sys.exit(f"{name} does not match its pinned SHA-256.")
    lock = ["-r", str(ROOT / "requirements.txt")]
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


def drop_launchers(site):
    """pip --target writes console launchers into lib/bin with the build
    machine's Python path baked in (so their bytes differ from build to build,
    and they would not work elsewhere). They go, and so do the RECORD lines for
    everything installed outside the package folder ("../": the launchers, and
    share/ and include/ files pip leaves behind), so the package stays
    reproducible and RECORD lists only what is there."""
    shutil.rmtree(site / "bin", ignore_errors=True)
    for record in sorted(site.glob("*.dist-info/RECORD")):
        raw = record.read_bytes().decode("utf-8")
        newline = "\r\n" if "\r\n" in raw else "\n"
        lines = raw.splitlines()
        kept = [line for line in lines if not line.startswith("../")]
        if kept != lines:
            record.write_bytes((newline.join(kept) + newline).encode("utf-8"))


def copy_app(app):
    app.mkdir(parents=True)
    for name in APP_FILES:
        shutil.copy2(ROOT / name, app / name)
    # The shell trial page (tools/webview_trial.py) is a development tool: it
    # is left out so the package holds no page other than the app's own.
    shutil.copytree(ROOT / "workspace", app / "workspace",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                  "trial.html", "trial.js", "trial.css"))


def git(*args):
    """git output, or None when git is missing or the command fails."""
    try:
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_commit():
    return git("rev-parse", "HEAD") or "unknown"


def release_guard(version):
    """A release builds only the tagged commit, from a tree with no change and
    no untracked file (anything in workspace/ would otherwise be packaged)."""
    status = git("status", "--porcelain", "--untracked-files=all")
    if status is None:
        sys.exit("--release needs git, to check the working tree and the tag.")
    if status:
        sys.exit("--release refuses a working tree with changes or untracked files:\n"
                 + status)
    head, tagged = git("rev-parse", "HEAD"), git("rev-parse", f"v{version}^{{commit}}")
    if not tagged or head != tagged:
        sys.exit(f"--release builds only the commit tagged v{version} "
                 f"(HEAD is {head}, the tag is {tagged or 'missing'}).")


def source_date_epoch():
    """The time stamped on every file: SOURCE_DATE_EPOCH, or the commit's time."""
    raw = os.environ.get("SOURCE_DATE_EPOCH") or git("log", "-1", "--format=%ct", "HEAD")
    try:
        return max(int(raw), ZIP_EPOCH_MIN)
    except (TypeError, ValueError):
        sys.exit("No SOURCE_DATE_EPOCH and no commit time: cannot stamp a reproducible build.")


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def licence_of(meta):
    """A short licence name from a wheel's metadata."""
    if meta.get("license_expression"):
        return meta["license_expression"]
    named = [c.split(" :: ")[-1] for c in meta.get("classifier", [])
             if c.startswith("License :: ") and c.count(" :: ") >= 2]
    if named:
        return " OR ".join(named)
    text = (meta.get("license") or "").strip()
    if text and len(text) <= 80 and "\n" not in text:
        return text
    return "see the licence texts"


def packages(installed):
    """[(name, version, licence, wheel file, sha256)] from pip's report, sorted."""
    rows = []
    for item in installed:
        meta = item["metadata"]
        info = item.get("download_info") or {}
        digest = (info.get("archive_info") or {}).get("hashes", {}).get("sha256", "")
        wheel = info.get("url", "").replace("\\", "/").rsplit("/", 1)[-1]
        rows.append((meta["name"], meta["version"], licence_of(meta), wheel, digest))
    return sorted(rows, key=lambda r: r[0].lower())


def sbom(installed, app_name, version, commit, epoch, runtime_sha):
    """A CycloneDX 1.6 bill of materials for the package, reproducible (its
    serial number comes from the version and commit)."""
    def prop(key, value):
        return dict(name=key, value=value)

    def component(name, ver, licence, wheel, digest):
        purl = f"pkg:pypi/{re.sub(r'[-_.]+', '-', name).lower()}@{ver}"
        return {"type": "library", "bom-ref": purl, "name": name, "version": ver,
                "purl": purl, "hashes": [{"alg": "SHA-256", "content": digest}],
                "licenses": [{"license": dict(name=licence)}],
                "properties": [prop("unminute:wheel", wheel)]}
    runtime = {"type": "platform", "bom-ref": "cpython-embed", "name": RUNTIME_NAME,
               "version": PYTHON_VERSION, "hashes": [{"alg": "SHA-256",
               "content": runtime_sha}], "licenses": [{"license": {"id": "PSF-2.0"}}],
               "externalReferences": [{"type": "distribution", "url": EMBED_URL}]}
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
        "serialNumber": "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                     f"unminute/{version}/{commit}")),
        "metadata": {"timestamp": iso(epoch),
                     "tools": {"components": [{"type": "application", "name": BUILDER_NAME}]},
                     "component": {"type": "application", "bom-ref": "unminute",
                                   "name": app_name, "version": version,
                                   "properties": [prop("unminute:commit", commit)]}},
        "components": [runtime] + [component(*row) for row in packages(installed)],
    }


def wheel_files(site, name):
    """The files a wheel installed (its RECORD), relative to site, or []."""
    norm = re.sub(r"[-_.]+", "_", name).lower()
    for info in site.glob("*.dist-info"):
        stem = info.name[:-len(".dist-info")]
        if re.sub(r"[-_.]+", "_", stem.rsplit("-", 1)[0]).lower() == norm:
            record = (info / "RECORD").read_text(encoding="utf-8").splitlines()
            return sorted((line.rsplit(",", 2)[0] for line in record if line),
                          key=str.lower)
    return []


def licence_files(site, name):
    """The licence and notice texts a wheel installed, anywhere in it (some keep
    them beside the code, not in dist-info), as paths inside the package."""
    return [f"runtime/{SITE}/{f}" for f in wheel_files(site, name)
            if re.match(r"(?i)(licen[cs]e|copying|notice|authors|thirdpartynotices)",
                        f.rsplit("/", 1)[-1])]


def bundled_libraries(site, name):
    """Native libraries a wheel bundles in its <package>.libs folder (copied in
    by the wheel's build, often from other projects with their own licences)."""
    return [f.rsplit("/", 1)[-1] for f in wheel_files(site, name)
            if re.match(r"[^/]+\.libs/[^/]+\.dll$", f, re.IGNORECASE)]


def notices(installed, site, version):
    """THIRD_PARTY_NOTICES.txt: every bundled component, its licence and where
    its licence texts are in the package."""
    lines = [f"Unminute {version}: third-party software", "",
             "Unminute includes the software below, each under its own licence. The",
             "licence texts are in the files named for each component (paths are inside",
             "the package). Models are not included: see model-manifest.json.", "",
             f"Python {PYTHON_VERSION} (CPython embeddable package)",
             "  Licence: PSF-2.0", "  Texts: runtime/LICENSE.txt", ""]
    for name, ver, licence, _, _ in packages(installed):
        texts = licence_files(site, name)
        lines += [f"{name} {ver}", f"  Licence: {licence}",
                  "  Texts: " + (", ".join(texts) if texts else "none shipped in the wheel")]
        libs = bundled_libraries(site, name)
        if libs:
            lines += ["  Bundled native libraries (each under its own licence, not all of",
                      "  which the wheel's licence texts cover): " + ", ".join(libs)]
        lines.append("")
    return "\n".join(lines)


def model_manifest():
    """The pinned model files, for IT to verify a pre-staged copy (#111 B7)."""
    import asr_engines as ae
    specs = [*ae.PARAKEET_MODELS.values(), ae.SILERO_VAD, *ae.WHISPER_MODELS.values(),
             ae.SPEAKER_MODEL]
    return {"_about": "Models are not in the package. These are the files each model "
                      "must have, by size and SHA-256 (docs/MODEL_MANIFEST.md).",
            "models": [{"name": s.name, "licence": ae.LICENCES[s.name][0],
                        "repository": s.repo, "revision": s.revision, "gated": s.gated,
                        "files": [{"name": f.name, "size": f.size, "sha256": f.sha256}
                                  for f in s.files]} for s in specs]}


def binaries(folder):
    """Every executable file in the package, as sorted relative paths."""
    return sorted((p.relative_to(folder).as_posix() for p in folder.rglob("*")
                   if p.is_file() and p.suffix.lower() in BINARY_SUFFIXES), key=str.lower)


def check_binaries(found, path=ALLOWLIST, update=False):
    """The package's binaries must be exactly those allowlisted. Returns the
    problems, or rewrites the list when asked (for review in the pull request)."""
    if update:
        path.write_text(json.dumps({
            "_about": "Every .exe, .dll and .pyd the package may contain. A binary not "
                      "listed fails the build (tools/build_package.py); rewrite with "
                      "--update-binary-allowlist and review the change.",
            "binaries": found}, indent=1) + "\n", encoding="utf-8", newline="\n")
        return []
    allowed = json.loads(path.read_text(encoding="utf-8"))["binaries"]
    new = sorted(set(found) - set(allowed), key=str.lower)
    gone = sorted(set(allowed) - set(found), key=str.lower)
    return ([f"not allowlisted: {p}" for p in new] + [f"allowlisted but missing: {p}"
                                                       for p in gone])


def signers(folder, paths):
    """{path: (status, signer subject)} from Authenticode, in one PowerShell run."""
    plain = str(folder).removeprefix("\\\\?\\")
    with tempfile.TemporaryDirectory() as tmp:
        listed, out = Path(tmp) / "files.txt", Path(tmp) / "signers.json"
        listed.write_text("\n".join(f"{plain}\\{p.replace('/', chr(92))}" for p in paths),
                          encoding="utf-8")
        script = (f"$r = foreach ($p in Get-Content -LiteralPath '{listed}' -Encoding UTF8) {{"
                  " $s = Get-AuthenticodeSignature -LiteralPath $p;"
                  " [pscustomobject]@{ status = [string]$s.Status;"
                  " signer = if ($s.SignerCertificate) { $s.SignerCertificate.Subject }"
                  " else { '' } } };"
                  f" ConvertTo-Json -InputObject @($r) -Compress | Set-Content -LiteralPath"
                  f" '{out}' -Encoding UTF8")
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
        rows = json.loads(out.read_text(encoding="utf-8-sig"))
    if len(rows) != len(paths):
        sys.exit("The signature check did not return one result per binary.")
    return {p: (r["status"], r["signer"]) for p, r in zip(paths, rows, strict=True)}


def binary_inventory(folder, paths, signed):
    return {"_about": "Every executable file in the package: size, SHA-256 and its "
                      "Authenticode signature at build time (unsigned files say NotSigned).",
            "binaries": [{"path": p, "size": (folder / p).stat().st_size,
                          "sha256": sha256(folder / p), "signature": signed[p][0],
                          "signer": signed[p][1]} for p in paths]}


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
    if os.name != "nt" or sys.version_info[:2] != (3, 12) or \
            sys.implementation.name != "cpython" or sys.maxsize < 2**32:
        sys.exit("Build the package with 64-bit CPython 3.12 on Windows.")


def build(version, out, cache, release=False, update_allowlist=False):
    check_host()
    if release:
        release_guard(version)
    epoch = source_date_epoch()
    commit = git_commit()
    name = f"Unminute-{version}"
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
        embed = fetch_runtime(cache)
        with zipfile.ZipFile(embed) as z:
            z.extractall(runtime)
        check_signatures(runtime)
        write_pth(runtime)
        installed = install_wheels(runtime / SITE, cache / "wheels", work / "pip-report.json")
        flatten_licences(runtime / SITE)
        prune_portaudio(runtime / SITE)
        drop_launchers(runtime / SITE)
        copy_app(folder / "app")
        for script in ("install.ps1", "install.cmd", "uninstall.ps1"):
            shutil.copy2(PACKAGING / script, folder / script)
        info = {
            # "name": Start menu and Settings > Apps name; installers read it so a
            # rollback recreates what this version's uninstaller removes.
            "name": "Unminute",
            # built_at is the commit's time (or SOURCE_DATE_EPOCH), so a rebuild of
            # the same commit is identical.
            "version": version, "built_at": iso(epoch),
            "python": PYTHON_VERSION, "commit": commit,
            # Installers check for features before relying on them (#26).
            "features": ["gate"]}
        (folder / "version.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        bom = sbom(installed, info["name"], version, commit, epoch, sha256(embed))
        (folder / "sbom.cdx.json").write_text(json.dumps(bom, indent=1), encoding="utf-8")
        (folder / "THIRD_PARTY_NOTICES.txt").write_text(
            notices(installed, runtime / SITE, version), encoding="utf-8")
        (folder / "model-manifest.json").write_text(json.dumps(model_manifest(), indent=1),
                                                    encoding="utf-8")
        too_long = [p.relative_to(folder).as_posix() for p in folder.rglob("*")
                    if len(p.relative_to(folder).as_posix()) > MAX_RELATIVE_PATH]
        if too_long:
            sys.exit("Paths too long for Windows installs:\n" + "\n".join(too_long))
        found = binaries(folder)
        problems = check_binaries(found, update=update_allowlist)
        if problems:
            sys.exit("The package's binaries differ from packaging/binary-allowlist.json "
                     "(review, then rebuild with --update-binary-allowlist):\n"
                     + "\n".join(problems))
        (folder / "binary-inventory.json").write_text(
            json.dumps(binary_inventory(folder, found, signers(folder, found)), indent=1),
            encoding="utf-8")
        count = manifest(folder)
        out.mkdir(parents=True, exist_ok=True)
        zpath = out / f"{name}.zip"
        write_zip(folder, zpath, epoch)
        digest = sha256(zpath)
        (out / f"{name}.zip.sha256").write_text(f"{digest}  {zpath.name}\n", encoding="ascii")
        shutil.copyfile(folder / "sbom.cdx.json", out / f"{name}.cdx.json")
        print(f"{zpath} ({zpath.stat().st_size / 1e6:.0f} MB, {count} files)\nSHA-256 {digest}")
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
    ap.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "wwbuild-cache"))
    ap.add_argument("--release", action="store_true",
                    help="refuse a dirty tree or a HEAD that is not the vX.Y.Z tag")
    ap.add_argument("--update-binary-allowlist", action="store_true",
                    help="rewrite packaging/binary-allowlist.json from this build (review it)")
    a = ap.parse_args()
    if a.release and a.update_binary_allowlist:
        ap.error("--release cannot rewrite the binary allowlist")
    build(release_version(a.version), Path(a.out), Path(a.cache), a.release,
          a.update_binary_allowlist)


if __name__ == "__main__":
    main()
