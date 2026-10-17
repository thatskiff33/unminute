# tools/evidence.py
# Release evidence that names its package (#111 B3; docs/RELEASE_STANDARD.md,
# section 8: "each naming the package's checksum").
#
# Each release-time check runs against an installed (or extracted) package and,
# given --zip, records in its report:
#   - the package it ran on: the zip's SHA-256, version, commit and release flag;
#   - the checker: this checkout's commit, and whether its tools, tests and docs
#     (which hold the budgets) had any change or extra file;
#   - whether the version folder held exactly the zip's files before and after
#     the run: names compared exactly, nothing missing, changed or added, and no
#     link or extra folder (install.ps1 copies the package unchanged);
#   - for performance, the run's shape (a full release run or not).
# The Release workflow refuses to publish unless one passing report of each kind
# is attached, each naming the zip it publishes and made by a clean checkout of
# the commit the zip was built from:
#
#   python -I tools/evidence.py check <zip SHA-256> [--commit <sha>] <report.json>...
#
# Exit codes for "check": 0 every kind present and passing; 1 otherwise.

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
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
# A performance run that counts as evidence (docs/RELEASING.md): at least this.
PERF_RELEASE = {"runs": 3, "settle_s": 60, "window_s": 300, "leak_minutes": 30}
APP_MODULES = ("workspace", "asr_engines", "offline_guard")     # the app folder's own
CHECKER_PATHS = ("tools", "tests", "docs")    # the checks, their helpers and the budgets
APP_FLAGS = "-I -B -X disable-remote-debug"   # as the Start menu shortcut starts the app
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


def add_app(app):
    """Put the app folder on the import path for a check, unless it is already
    there: the package's own runtime lists it (its ._pth names ..\\app), and
    keeping that order checks the app as it runs."""
    app = Path(app).resolve()
    for entry in sys.path:
        try:
            if entry and Path(entry).resolve() == app:
                return
        except OSError:
            continue
    sys.path.insert(0, str(app))


def require_package_runtime(app):
    """A check run against a package uses that package's own Python, started as
    the app is, so it exercises the shipped runtime and libraries."""
    runtime = Path(app).resolve().parent / "runtime"
    here = Path(getattr(sys, "_base_executable", sys.executable)).resolve().parent
    if os.path.normcase(str(here)) != os.path.normcase(str(runtime.resolve())):
        sys.exit(f"Run this check with the package's own Python: {runtime / 'python.exe'} "
                 f"{APP_FLAGS} ...")
    missing = [flag for flag, on in (("-I", sys.flags.isolated),
                                     ("-B", sys.flags.dont_write_bytecode),
                                     ("-X disable-remote-debug",
                                      "disable-remote-debug" in getattr(sys, "_xoptions", {})))
               if not on]
    if missing:
        sys.exit(f"Run this check as the app runs, with {', '.join(missing)} "
                 f"({runtime / 'python.exe'} {APP_FLAGS} ...).")


def require_app_modules(app):
    """With --app, the app's modules imported so far came from that folder: a
    checkout earlier on the import path would mix its code into the check."""
    app = Path(app).resolve()
    for name in APP_MODULES:
        module = sys.modules.get(name)
        if module is None:
            continue
        where = Path(module.__file__).resolve()
        if app not in where.parents:
            sys.exit(f"{name} was imported from {where.parent}, not from {app}.")


def check_zip(zpath):
    """The package's top folder, version.json and package-manifest.json bytes,
    or a stop with a message if zpath is not a package zip."""
    try:
        with zipfile.ZipFile(zpath) as z:
            tops = {n.split("/", 1)[0] for n in z.namelist()}
            if len(tops) != 1:
                raise ValueError("it does not hold one package folder")
            top = tops.pop()
            info = json.loads(z.read(f"{top}/version.json").decode("utf-8"))
            manifest = z.read(f"{top}/package-manifest.json")
            if not isinstance(info, dict) or not isinstance(json.loads(manifest), dict):
                raise ValueError("its version.json or manifest is not an object")
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
        sys.exit(f"{Path(zpath).name} is not a package zip ({type(exc).__name__}: {exc}).")
    return top, info, manifest


def safe_name(name):
    """A manifest name that stays inside the package folder."""
    if not isinstance(name, str):
        return False
    return ("\\" not in name and ":" not in name and not name.startswith("/")
            and all(part not in ("", ".", "..") for part in name.split("/")))


def _is_link(entry):
    """A symbolic link, a junction, or any other reparse point (never followed)."""
    if entry.is_symlink() or entry.is_junction():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def walk(folder):
    """Every entry under folder without following any link: (files, folders,
    links, others) as sorted names relative to folder, with "/"."""
    folder = Path(folder)
    found = {"files": [], "folders": [], "links": [], "others": []}
    stack = [folder]
    while stack:
        here = stack.pop()
        with os.scandir(here) as entries:
            for entry in entries:
                rel = Path(entry.path).relative_to(folder).as_posix()
                if _is_link(entry):
                    found["links"].append(rel)
                elif entry.is_dir(follow_symlinks=False):
                    found["folders"].append(rel)
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    found["files"].append(rel)
                else:
                    found["others"].append(rel)
    return tuple(sorted(found[k]) for k in ("files", "folders", "links", "others"))


def folder_differences(version_dir, manifest_bytes):
    """How the version folder differs from the package's manifest. Names compare
    exactly, as install.ps1 copies them: Windows keeps names apart that a looser
    comparison would treat as one (letters such as "ss" and the German sharp s),
    which would let a planted file pass as a listed one."""
    folder = Path(version_dir)
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    out = [f"unsafe name in the manifest: {name!r}" for name in sorted(manifest, key=str)
           if not safe_name(name)]
    listed = {name for name in manifest if safe_name(name)}
    implied = {"/".join(name.split("/")[:i])
               for name in listed for i in range(1, name.count("/") + 1)}
    files, folders, links, others = walk(folder)
    out += [f"link: {rel}" for rel in links]
    out += [f"not a file or folder: {rel}" for rel in others]
    out += [f"added folder: {rel}" for rel in folders if rel not in implied]
    out += [f"added: {rel}" for rel in files
            if rel != "package-manifest.json" and rel not in listed]
    present = set(files)
    for name in sorted(listed):
        size, digest = manifest[name]
        if name not in present:
            out.append(f"missing: {name}")
            continue
        st = (folder / name).stat()
        if st.st_nlink > 1:               # the installer's copies have one link each
            out.append(f"hard link: {name}")
        elif st.st_size != size or sha256(folder / name) != digest:
            out.append(f"changed: {name}")
    if ("package-manifest.json" not in present
            or (folder / "package-manifest.json").read_bytes() != manifest_bytes):
        out.append("changed: package-manifest.json")
    return out


def identity(zpath, version_dir):
    """Which package a check ran on, and whether the folder it ran from holds
    exactly that package's files."""
    top, info, manifest = check_zip(zpath)
    try:
        differences = folder_differences(version_dir, manifest)
    except (OSError, ValueError, TypeError) as exc:
        differences = [f"cannot compare the folder ({type(exc).__name__})"]
    return {"package_sha256": sha256(zpath), "package": top,
            "version": info.get("version"), "commit": info.get("commit"),
            "release": info.get("release") is True,
            "folder_matches_package": not differences,
            "folder_differences": len(differences),
            "folder_differences_listed": differences[:MAX_LISTED]}


def tools_identity():
    """The checkout these checks ran from: its commit, and whether its tools,
    tests and docs are exactly as committed. Clean means no change and no extra
    file, ignored ones included (an ignored .pyc in tools/__pycache__ would run
    instead of its source), and no file hidden from git status
    (assume-unchanged or skip-worktree), as the release build requires."""
    root = Path(__file__).resolve().parents[1]

    # No GIT_DIR, GIT_WORK_TREE or other git setting from the environment: the
    # check is of this checkout, as it is.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}

    def git(*args):
        try:
            r = subprocess.run(["git", "--no-replace-objects", "-C", str(root), *args],
                               capture_output=True, text=True, timeout=120, env=env)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None
    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=all", "--ignored", "--",
                 *CHECKER_PATHS)
    tags = git("ls-files", "-v", "--", *CHECKER_PATHS)
    hidden = None if tags is None else [line for line in tags.splitlines()
                                        if line[:1].islower() or line.startswith("S ")]
    return {"tools_commit": commit.strip() if commit else None,
            "tools_clean": status == "" and hidden == []}


def prepare(parser, args, app=None, version_dir=None):
    """Before a check runs: --zip needs the package's folder (--app, or the
    version folder); with --app the check runs on that package's Python, as the
    app runs, with the app's modules from that folder; and given --zip, the
    folder must already hold exactly that zip's files. Returns that first
    comparison (None without --zip), for stamp()."""
    if args.zip and app is None and version_dir is None:
        parser.error("--zip needs --app, the app folder installed from that zip")
    if app is not None:
        require_package_runtime(app)
        require_app_modules(app)
        version_dir = Path(app).resolve().parent
    if not args.zip:
        return None
    if not tools_identity()["tools_clean"]:
        sys.exit("This checkout's tools, tests or docs are not exactly as committed (a change, "
                 "an extra or ignored file such as tools/__pycache__, or a file hidden from git "
                 "status). Check out the release's commit, see what "
                 "`git clean -ndX -- tools tests docs` would remove, then run "
                 "`git clean -fdX -- tools tests docs`, and start the check with -B.")
    before = identity(args.zip, version_dir)
    if not before["folder_matches_package"]:
        sys.exit(f"{version_dir} does not hold exactly the files of {Path(args.zip).name} "
                 f"({before['folder_differences']} differences, such as "
                 f"{'; '.join(before['folder_differences_listed'][:3])}). "
                 "Install that zip again, or name the zip the folder came from.")
    return before


def perf_release_run(run):
    """A performance run with at least the release run's shape."""
    if not isinstance(run, dict):
        return False
    return run.get("quick") is False and all(
        isinstance(run.get(key), int) and run[key] >= least
        for key, least in PERF_RELEASE.items())


def stamp(report, kind, zpath, version_dir, passed, before=None, run=None):
    """Add the evidence block to a check's report and return it. It counts only
    if the check passed and the folder held exactly the package before and
    after the run."""
    if kind not in KINDS:
        raise ValueError(f"unknown evidence kind {kind!r}")
    ev = {"kind": kind, **identity(zpath, version_dir), **tools_identity(),
          "folder_matched_before": bool(before and before.get("folder_matches_package")),
          "check_passed": bool(passed)}
    if run is not None:
        ev["run"] = run
    ev["pass"] = (ev["check_passed"] and ev["folder_matches_package"]
                  and ev["folder_matched_before"])
    report["evidence"] = ev
    return ev


def stamp_file(report_path, kind, zpath, version_dir, passed, before=None, run=None):
    """Stamp a report already written to report_path, in place."""
    path = Path(report_path)
    report = json.loads(path.read_text(encoding="utf-8"))
    ev = stamp(report, kind, zpath, version_dir, passed, before, run)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"EVIDENCE {kind}: {'PASS' if ev['pass'] else 'FAIL'} for {ev['package']} "
          f"(SHA-256 {ev['package_sha256']})"
          + ("" if ev["folder_matches_package"]
             else f"; the folder differs from the package in {ev['folder_differences']} places"))
    return ev


def _no_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("a key appears twice")
    return dict(pairs)


def check(expected_sha256, paths, commit=None):
    """Problems with a release's evidence (empty when it is complete): one
    report of every kind, each from a release build, naming the expected zip
    (and commit, if given), run on its unchanged files by a clean checkout of
    that commit, and passing."""
    expected = expected_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return [f"the zip's SHA-256 should be 64 hex digits, not {expected_sha256!r}"]
    if commit is not None:
        commit = commit.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            return [f"the commit should be 40 hex digits, not {commit!r}"]
    problems, seen = [], {}
    for path in paths:
        name = Path(path).name
        try:
            body = json.loads(Path(path).read_text(encoding="utf-8"),
                              object_pairs_hook=_no_duplicate_keys)
            ev = body["evidence"]
            kind = ev["kind"]
        except (OSError, ValueError, KeyError, TypeError):
            problems.append(f"{name}: not an evidence report (unreadable, or no evidence block)")
            continue
        if not isinstance(kind, str) or kind not in KINDS:
            problems.append(f"{name}: unknown kind {kind!r}")
            continue
        if kind in seen:
            problems.append(f"{name}: a second {kind} report (also {seen[kind]})")
            continue
        seen[kind] = name
        found = []
        if ev.get("package_sha256") != expected:
            found.append(f"names another package ({ev.get('package_sha256')}, not {expected})")
        if ev.get("release") is not True:
            found.append("ran on a development build")
        if commit is not None and ev.get("commit") != commit:
            found.append(f"names commit {ev.get('commit')}, not {commit}")
        if not (ev.get("folder_matches_package") is True
                and ev.get("folder_matched_before") is True
                and type(ev.get("folder_differences")) is int
                and ev["folder_differences"] == 0):
            found.append("ran on a folder that differs from the package")
        if ev.get("tools_commit") is None or ev.get("tools_commit") != ev.get("commit"):
            found.append(f"ran from a checkout of another commit ({ev.get('tools_commit')})")
        if ev.get("tools_clean") is not True:
            found.append("ran from a checkout with changes in its tools, tests or docs")
        if (ev.get("check_passed") is not True or ev.get("pass") is not True
                or body.get("pass", True) is not True or body.get("failed")):
            found.append("did not pass")
        if kind == "perf" and not perf_release_run(ev.get("run")):
            found.append(f"was not a full release run (at least {PERF_RELEASE}, not --quick)")
        problems += [f"{name}: {kind} {what}" for what in found]
    for kind, tool in KINDS.items():
        if kind not in seen:
            problems.append(f"missing: {kind} ({tool})")
    return problems


def names(zpath, version, commit):
    """Problems if the package zip is not the release build of this version and
    commit, by its own version.json (empty when it is)."""
    top, info, _ = check_zip(zpath)
    problems = []
    if top != f"Unminute-{version}":
        problems.append(f"its folder is {top}, not Unminute-{version}")
    if info.get("version") != version:
        problems.append(f"it is version {info.get('version')}, not {version}")
    if info.get("commit") != commit:
        problems.append(f"it was built from {info.get('commit')}, not {commit}")
    if info.get("release") is not True:
        problems.append("it is not a release build")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check a release's evidence (#111 B3).",
                                 allow_abbrev=False)
    sub = ap.add_subparsers(dest="command", required=True)
    n = sub.add_parser("names", help="the zip is the release build of this version and commit",
                       allow_abbrev=False)
    n.add_argument("zip")
    n.add_argument("version", help="X.Y.Z or X.Y.Z-preview.N")
    n.add_argument("commit", help="40 hex digits")
    c = sub.add_parser("check", help="every kind present, naming this zip, and passing",
                       allow_abbrev=False)
    c.add_argument("sha256", help="the SHA-256 of the zip being released")
    c.add_argument("reports", nargs="*", help="the evidence reports (JSON)")
    c.add_argument("--commit", help="the commit the zip was built from (40 hex digits)")
    a = ap.parse_args(argv)
    if a.command == "names":
        problems = names(a.zip, a.version, a.commit)
        for p in problems:
            print(f"PACKAGE PROBLEM: {Path(a.zip).name}: {p}")
        if not problems:
            print(f"{Path(a.zip).name} is the release build of {a.version} from {a.commit}")
        return 1 if problems else 0
    problems = check(a.sha256, a.reports, a.commit)
    for p in problems:
        print(f"EVIDENCE PROBLEM: {p}")
    if not problems:
        print(f"EVIDENCE COMPLETE: {', '.join(KINDS)} name {a.sha256.strip().lower()} and passed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
