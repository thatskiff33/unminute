# tools/evidence.py
# Release evidence that names its package (#111 B3; docs/RELEASE_STANDARD.md,
# section 8: "each naming the package's checksum").
#
# Each release-time check runs against an installed (or extracted) package and,
# given --zip, records which package it ran on: the zip's SHA-256, its version
# and commit, and whether the version folder still holds exactly the zip's files
# after the run (nothing missing, changed or added; install.ps1 copies the
# package into the folder unchanged). The Release workflow refuses to publish
# unless one passing report of every kind is attached and each names the zip it
# is publishing:
#
#   python tools/evidence.py check <zip SHA-256> <report.json>...
#
# Exit codes for "check": 0 every kind present and passing; 1 otherwise.

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path

# Kind -> the tool that writes it, and the release standard's section.
KINDS = {
    "perf": "tools/perf_check.py (section 2)",
    "crash": "tools/packaged_crash_check.py (section 4)",
    "packaged": "tools/packaged_check.py (section 7)",
    "ui": "tools/workspace_ui_check.py (section 7)",
    "accessibility": "tools/accessibility_check.py (section 12)",
}
MAX_LISTED = 20          # differences listed in a report; the count is always given


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def app_folder(default, argv=None):
    """The app folder a check imports: --app's value (an installed or extracted
    package's app folder), else the checkout. Read straight from the command
    line, because the checks import the app at the top of the module, before
    their arguments are parsed."""
    argv = sys.argv if argv is None else argv
    for i, arg in enumerate(argv):
        if arg == "--app" and i + 1 < len(argv):
            return Path(argv[i + 1]).resolve()
        if arg.startswith("--app="):
            return Path(arg.split("=", 1)[1]).resolve()
    return Path(default)


def require_package_runtime(app):
    """A check run against a package must use that package's own Python, so it
    exercises the shipped runtime and libraries rather than a development one."""
    runtime = Path(app).resolve().parent / "runtime"
    here = Path(getattr(sys, "_base_executable", sys.executable)).resolve().parent
    if os.path.normcase(str(here)) != os.path.normcase(str(runtime.resolve())):
        sys.exit(f"Run this check with the package's own Python: {runtime / 'python.exe'} "
                 "-I -B -X disable-remote-debug ...")


APP_MODULES = ("workspace", "asr_engines", "offline_guard")     # the app folder's own


def require_app_modules(app):
    """With --app, the app's modules imported so far must have come from that
    folder: a checkout earlier on the import path would mix its code into the
    check."""
    app = Path(app).resolve()
    for name in APP_MODULES:
        module = sys.modules.get(name)
        if module is None:
            continue
        where = Path(module.__file__).resolve()
        if app not in where.parents:
            sys.exit(f"{name} was imported from {where.parent}, not from {app}.")


def zip_contents(zpath):
    """The package's top folder, version.json and package-manifest.json bytes."""
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) != 1:
            raise ValueError("the zip does not hold one package folder")
        top = tops.pop()
        return (top, json.loads(z.read(f"{top}/version.json").decode("utf-8")),
                z.read(f"{top}/package-manifest.json"))


def folder_differences(version_dir, manifest_bytes):
    """How the version folder differs from the package's manifest: files
    missing, changed (size or SHA-256) or not in the manifest. Names compare
    without case, as on Windows and in install.ps1."""
    folder = Path(version_dir)
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    listed = {name.casefold() for name in manifest}
    out = []
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder).as_posix()
        if p.is_file() and rel != "package-manifest.json" and rel.casefold() not in listed:
            out.append(f"added: {rel}")
    for name, (size, digest) in sorted(manifest.items()):
        p = folder / name
        if not p.is_file():
            out.append(f"missing: {name}")
        elif p.stat().st_size != size or sha256(p) != digest:
            out.append(f"changed: {name}")
    if (folder / "package-manifest.json").read_bytes() != manifest_bytes:
        out.append("changed: package-manifest.json")
    return out


def identity(zpath, version_dir):
    """Which package a check ran on, and whether the folder it ran from still
    holds exactly that package's files."""
    top, info, manifest = zip_contents(zpath)
    try:
        differences = folder_differences(version_dir, manifest)
    except (OSError, ValueError) as exc:
        differences = [f"cannot compare the folder ({type(exc).__name__})"]
    return {"package_sha256": sha256(zpath), "package": top,
            "version": info.get("version"), "commit": info.get("commit"),
            "release": info.get("release") is True,
            "folder_matches_package": not differences,
            "folder_differences": len(differences),
            "folder_differences_listed": differences[:MAX_LISTED]}


def stamp(report, kind, zpath, version_dir, passed):
    """Add the evidence block to a check's report and return it. The check
    counts only if it passed and ran on the unchanged package."""
    if kind not in KINDS:
        raise ValueError(f"unknown evidence kind {kind!r}")
    ev = {"kind": kind, **identity(zpath, version_dir), "check_passed": bool(passed)}
    ev["pass"] = ev["check_passed"] and ev["folder_matches_package"]
    report["evidence"] = ev
    return ev


def stamp_file(report_path, kind, zpath, version_dir, passed):
    """Stamp a report already written to report_path, in place."""
    path = Path(report_path)
    report = json.loads(path.read_text(encoding="utf-8"))
    ev = stamp(report, kind, zpath, version_dir, passed)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"EVIDENCE {kind}: {'PASS' if ev['pass'] else 'FAIL'} for {ev['package']} "
          f"(SHA-256 {ev['package_sha256']})"
          + ("" if ev["folder_matches_package"]
             else f"; the folder differs from the package in {ev['folder_differences']} files"))
    return ev


def check(expected_sha256, paths):
    """Problems with a release's evidence (empty when it is complete): one
    report of every kind, each from a release build, naming the expected zip,
    run on its unchanged files, and passing."""
    expected = expected_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return [f"the zip's SHA-256 should be 64 hex digits, not {expected_sha256!r}"]
    problems, seen = [], {}
    for path in paths:
        name = Path(path).name
        try:
            ev = json.loads(Path(path).read_text(encoding="utf-8"))["evidence"]
            kind = ev["kind"]
        except (OSError, ValueError, KeyError, TypeError):
            problems.append(f"{name}: not an evidence report (no evidence block)")
            continue
        if kind not in KINDS:
            problems.append(f"{name}: unknown kind {kind!r}")
            continue
        if kind in seen:
            problems.append(f"{name}: a second {kind} report (also {seen[kind]})")
            continue
        seen[kind] = name
        if ev.get("package_sha256") != expected:
            problems.append(f"{name}: {kind} names another package "
                            f"({ev.get('package_sha256')}, not {expected})")
        if ev.get("release") is not True:
            problems.append(f"{name}: {kind} ran on a development build")
        if ev.get("folder_matches_package") is not True:
            problems.append(f"{name}: {kind} ran on a folder that differs from the package")
        if ev.get("check_passed") is not True or ev.get("pass") is not True:
            problems.append(f"{name}: {kind} did not pass")
    for kind, tool in KINDS.items():
        if kind not in seen:
            problems.append(f"missing: {kind} ({tool})")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check a release's evidence (#111 B3).")
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="every kind present, naming this zip, and passing")
    c.add_argument("sha256", help="the SHA-256 of the zip being released")
    c.add_argument("reports", nargs="*", help="the evidence reports (JSON)")
    a = ap.parse_args(argv)
    problems = check(a.sha256, a.reports)
    for p in problems:
        print(f"EVIDENCE PROBLEM: {p}")
    if not problems:
        print(f"EVIDENCE COMPLETE: {', '.join(KINDS)} name {a.sha256.strip().lower()} and passed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
