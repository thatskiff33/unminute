# tools/select_tests.py
# Which unit tests a pull request runs (#500; the owner's decision, 1 Nov 2026).
#
# A pull request runs only the test modules mapped to the files it changes; a
# push to main and the nightly run take everything. The map is
# tools/test_map.json, kept next to this file:
#
# - "run_everything": globs of shared or core files (the test runner and its
#   helpers, this selector and its map, the duration table, the locks, the
#   workflows, the storage and encryption core). Changing one runs every test.
# - "always": the cheap tests that scan the whole repository (consistency,
#   source text, content access, the map's own completeness test and so on).
#   They run whenever anything changed.
# - "only_always": globs of files whose changes need nothing beyond "always"
#   (most docs, tools without tests of their own, the Pester scripts).
# - "tests": {test module: globs of the files it covers}. A change to a test
#   module runs that module.
#
# A changed file that nothing maps runs everything, and so does an unusual path
# or a test module that is gone: the selection only ever narrows towards the
# full suite, never away from it. tests/test_select_tests.py fails when a
# tracked file or a test module is not mapped, when a test module imports or
# names a repository file the map does not tie to it (an import, a
# ROOT / "docs" / "X.md" join, or a path in a string such as a page's
# "/tools/design_check/fixtures.js"), or when a shared Python file under
# workspace/ imports one that is not shared too.
#
# In CI (.github/workflows/ci.yml, job "Select tests") this file, its map and
# the duration table run from the base commit, not the pull request's, so a
# pull request cannot change its own selection; a pull request that changes
# them runs everything anyway. The selection is dealt out to as many Windows
# runners as it needs, by the measured times in tools/test_durations.json: about
# a full-suite chunk's worth of seconds each, so a small selection takes one
# runner and the full suite the usual FULL_CHUNKS.
#
# Standard library only: the CI job uses the runner image's Python.
#
#   python tools/select_tests.py --paths workspace/drafts.py docs/RELEASING.md
#   python tools/select_tests.py --changed BASE HEAD --github-output FILE
#   python tools/select_tests.py --all --github-output FILE
#   python tools/select_tests.py --check        # what the map misses, if anything

import argparse
import ast
import functools
import json
import math
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAP = HERE / "test_map.json"
DURATIONS = HERE / "test_durations.json"

# The full suite's parts, one Windows runner each (#386).
FULL_CHUNKS = 8
# The weight of a test module the duration table does not know, and of a class.
UNKNOWN_MODULE_SECONDS = 30.0
UNKNOWN_CLASS_SECONDS = 1.0
# A runner never gets less than this, so a thin table cannot spread a small
# selection over many runners.
MIN_CHUNK_SECONDS = 60.0

MODES = ("all", "some", "none")
# A test module, or one class in it, as run_tests.py --only takes them.
NAME = re.compile(r"test_\w+(\.[A-Za-z_]\w*)?")
TEST_MODULE = re.compile(r"tests/(test_\w+)\.py")
# A path git can print that this selector reads as itself: no parent steps,
# backslashes, absolute paths or control characters.
PLAIN_PATH = re.compile(r"(?!/)(?!.*(?:^|/)\.\.?(?:/|$))[^\\\x00-\x1f\x7f\ud800-\udfff]+")


class Selection:
    """The outcome: mode "all", "some" (names) or "none", and why."""

    def __init__(self, mode, names=(), reasons=()):
        assert mode in MODES
        self.mode = mode
        self.names = sorted(names)
        self.reasons = list(reasons)

    def __repr__(self):
        return f"Selection({self.mode!r}, {self.names!r})"


@functools.cache
def glob_regex(pattern):
    """A glob as a regular expression over a whole path: ** crosses folders,
    * and ? do not."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


@functools.cache
def any_of(patterns):
    """One expression for a tuple of globs."""
    return re.compile("|".join(f"(?:{glob_regex(p).pattern})" for p in patterns) or "(?!)")


def matches(patterns, path):
    return any_of(tuple(patterns)).fullmatch(path) is not None


def load_map(path=MAP):
    """The map, checked for shape. A malformed map is an error, never an empty one."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("run_everything", "always", "only_always"):
        if not isinstance(data.get(key), list) or not all(isinstance(x, str) for x in data[key]):
            raise ValueError(f"test map: {key} must be a list of strings")
    tests = data.get("tests")
    if not isinstance(tests, dict) or not all(
            isinstance(v, list) and all(isinstance(x, str) for x in v) for v in tests.values()):
        raise ValueError("test map: tests must map each test module to a list of globs")
    for name in data["always"] + list(tests):
        if not NAME.fullmatch(name):
            raise ValueError(f"test map: {name!r} is not a test module or class")
    return data


def load_durations(path=DURATIONS):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))["classes"]
    except FileNotFoundError:
        return {}


def module_exists(root, module):
    return (Path(root) / "tests" / f"{module}.py").is_file()


def resolve_name(root, name):
    """name as it can run in the checkout at root: a class that is not in its
    module (any more) widens to the whole module; a module that is gone is None."""
    module, _, cls = name.partition(".")
    if not module_exists(root, module):
        return None
    if cls:
        text = (Path(root) / "tests" / f"{module}.py").read_text(encoding="utf-8")
        if not re.search(rf"^class {re.escape(cls)}\b", text, re.MULTILINE):
            return module
    return name


def code(text):
    """text as one Markdown code span, whatever backticks it holds, so a path
    from a pull request renders as itself in the job summary."""
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def select(paths, test_map, root):
    """The tests a change to paths needs, for the checkout at root (the merged
    result being tested)."""
    paths = sorted(set(paths))
    if not paths:
        return Selection("none", reasons=["no file changed"])
    chosen = set()
    reasons = []
    for path in paths:
        if not PLAIN_PATH.fullmatch(path):
            return Selection("all", reasons=[f"an unusual path: {code(repr(path))}"])
        if matches(test_map["run_everything"], path):
            return Selection("all", reasons=[f"{code(path)} is shared: everything runs"])
        hit = []
        module = TEST_MODULE.fullmatch(path)
        if module:
            if not module_exists(root, module[1]):
                return Selection("all", reasons=[f"{code(path)} was removed or renamed"])
            hit.append(module[1])
        hit += [test for test, globs in test_map["tests"].items() if matches(globs, path)]
        if not hit and not matches(test_map["only_always"], path):
            return Selection("all", reasons=[f"{code(path)} is not in the test map"])
        chosen.update(hit)
        reasons.append(f"{code(path)}: {', '.join(sorted(hit)) or 'the always set'}")
    chosen.update(test_map["always"])
    names = set()
    for name in chosen:
        resolved = resolve_name(root, name)
        if resolved is None:
            missing = f"tests/{name.partition('.')[0]}.py"
            return Selection("all", reasons=[f"{code(missing)} is mapped but missing"])
        names.add(resolved)
    # A class whose module runs anyway adds nothing.
    names = {n for n in names if "." not in n or n.partition(".")[0] not in names}
    return Selection("some", names, reasons)


def seconds(names, durations):
    """The measured seconds the names take, as the duration table has them."""
    total = 0.0
    for name in names:
        if "." in name:
            total += durations.get(name, UNKNOWN_CLASS_SECONDS)
        else:
            own = [s for cls, s in durations.items() if cls.split(".")[0] == name]
            total += sum(own) if own else UNKNOWN_MODULE_SECONDS
    return total


def chunk_count(selection, durations):
    """How many Windows runners the selection gets: a full-suite chunk's worth
    of seconds each, at least one, at most FULL_CHUNKS; none for "none"."""
    if selection.mode == "none":
        return 0
    if selection.mode == "all":
        return FULL_CHUNKS
    per_chunk = max(MIN_CHUNK_SECONDS, sum(durations.values()) / FULL_CHUNKS)
    wanted = math.ceil(seconds(selection.names, durations) / per_chunk)
    # Never more runners than the table knows classes for (each runner gets whole classes).
    classes = sum(1 if "." in name else
                  max(1, sum(1 for cls in durations if cls.split(".")[0] == name))
                  for name in selection.names)
    return max(1, min(FULL_CHUNKS, classes, wanted))


def outputs(selection, durations):
    """The job outputs, as name=value lines for $GITHUB_OUTPUT. names is empty
    unless the mode is "some"; chunks is the matrix list, [] for "none"."""
    for name in selection.names:
        if not NAME.fullmatch(name):                     # never handed on
            raise ValueError(f"not a test name: {name!r}")
    chunks = list(range(1, chunk_count(selection, durations) + 1))
    return {"mode": selection.mode,
            "names": " ".join(selection.names) if selection.mode == "some" else "",
            "chunks": json.dumps(chunks)}


def summary(selection, durations, changed):
    """A short Markdown report for the job summary: file paths and test names only."""
    out = outputs(selection, durations)
    lines = [f"Test selection: **{selection.mode}**, {len(json.loads(out['chunks']))} "
             f"Windows runner(s), {changed} file(s) changed."]
    if selection.mode == "some":
        lines.append(f"About {seconds(selection.names, durations):.0f} s of measured test time.")
        lines.append("")
        lines.append("Runs: " + ", ".join(f"`{n}`" for n in selection.names))
    lines.append("")
    lines += [f"- {r}" for r in selection.reasons[:200]]
    return "\n".join(lines) + "\n"


def changed_paths(root, base, head):
    """The files whose content differs between base and head (both sides of a
    rename), as git prints them, NUL-separated so no name can split."""
    done = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", base, head, "--"],
                          cwd=root, capture_output=True, check=True)
    return [p for p in done.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


# ---- Completeness (tests/test_select_tests.py, and --check) ----

def tracked_files(root):
    done = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    return [p for p in done.stdout.decode("utf-8").split("\0") if p]


def test_modules(root):
    return sorted(p.stem for p in (Path(root) / "tests").glob("test_*.py"))


def direct_imports(root, path, files):
    """The repository Python files that the file at path imports itself, by any
    import statement (top level or not) or by a "workspace.<name>" string, as
    importlib and mock.patch name them."""
    py = {f for f in files if f.endswith(".py")}

    def resolve(dotted):
        parts = dotted.split(".")
        found = ["/".join(parts) + ".py"]
        found += [f"{base}/{'/'.join(parts)}.py" for base in ("tools", "tests")]
        return [f for f in found if f in py]

    tree = ast.parse((Path(root) / path).read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.update(resolve(alias.name))
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for alias in node.names:
                out.update(resolve(f"{node.module}.{alias.name}") or resolve(node.module))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if re.fullmatch(r"workspace\.\w+", node.value):
                out.update(resolve(node.value))
    out.discard(path)
    out.discard("workspace/__init__.py")       # the package itself, in run_everything
    return out


# A run of path characters with at least one slash, as a string names a file.
PATH_TOKEN = re.compile(r"[\w.\-]+(?:/[\w.\-]+)+")


def named_files(root, path, files):
    """The tracked files that the file at path names other than by an import:
    a join of string literals onto a base, as ROOT / "docs" / "RELEASING.md",
    or a path inside a string, as a page's "/tools/design_check/fixtures.js".
    Docstrings do not count: they explain, they do not read."""
    tracked = set(files)
    tree = ast.parse((Path(root) / path).read_text(encoding="utf-8"))
    docstrings = {id(node.value) for node in ast.walk(tree)
                  if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = []
            while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                parts.append(node.right)
                node = node.left
            joined = []
            for part in reversed(parts):
                if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                    break
                joined.append(part.value)
            if "/".join(joined) in tracked:
                out.add("/".join(joined))
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings):
            out.update(t for t in (m.lstrip("/") for m in PATH_TOKEN.findall(node.value))
                       if t in tracked)
    out.discard(path)
    return out


def problems(root, test_map, files=None):
    """What the map gets wrong for the checkout at root, as readable lines."""
    files = tracked_files(root) if files is None else files
    found = []
    modules = test_modules(root)
    always_modules = {n for n in test_map["always"] if "." not in n}
    # Every test module is reachable: it is mapped, or always runs.
    for module in modules:
        if module not in test_map["tests"] and module not in always_modules:
            found.append(f"tests/{module}.py is not in the map's tests (or always)")
    for name in list(test_map["tests"]) + test_map["always"]:
        if resolve_name(root, name) != name:
            found.append(f"the map names {name}, which is not in tests/")
    for module in set(test_map["tests"]) & always_modules:
        found.append(f"{module} is both always run and mapped")
    # Every tracked file is classified.
    for path in files:
        if TEST_MODULE.fullmatch(path):
            continue
        if not (matches(test_map["run_everything"], path)
                or matches(test_map["only_always"], path)
                or any(matches(g, path) for g in test_map["tests"].values())):
            found.append(f"{path} is not in the map")
    # No glob is dead.
    globs = (test_map["run_everything"] + test_map["only_always"]
             + [g for v in test_map["tests"].values() for g in v])
    for pattern in sorted(set(globs)):
        if not any(glob_regex(pattern).fullmatch(p) for p in files):
            found.append(f"the map's glob {pattern} matches no tracked file")
    # What a test module imports or names maps to it.
    for module in modules:
        path = f"tests/{module}.py"
        if path not in files or module in always_modules:
            continue
        imported = direct_imports(root, path, files)
        for dep in sorted(imported | named_files(root, path, files)):
            if TEST_MODULE.fullmatch(dep) or matches(test_map["run_everything"], dep):
                continue
            if not matches(test_map["tests"].get(module, []), dep):
                how = "imports" if dep in imported else "names"
                found.append(f"{path} {how} {dep}, which the map does not tie to {module}")
    # A shared workspace file's own imports are shared too: a change to one
    # changes the core every test runs on.
    for path in files:
        if (path.startswith("workspace/") and path.endswith(".py")
                and matches(test_map["run_everything"], path)):
            for dep in sorted(direct_imports(root, path, files)):
                if not matches(test_map["run_everything"], dep):
                    found.append(f"{path} is shared and imports {dep}, which is not")
    return found


def write_outputs(path, values):
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for key, value in values.items():
            if "\n" in value:
                raise ValueError(f"{key} has a line break")
            f.write(f"{key}={value}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Select the unit tests a change needs.")
    how = parser.add_mutually_exclusive_group(required=True)
    how.add_argument("--changed", nargs=2, metavar=("BASE", "HEAD"),
                     help="the files git says differ between two commits")
    how.add_argument("--paths", nargs="*", metavar="PATH", help="these changed paths")
    how.add_argument("--all", action="store_true", help="everything (main and nightly)")
    how.add_argument("--check", action="store_true", help="report what the map misses")
    parser.add_argument("--root", default=str(HERE.parent), help="the checkout being tested")
    parser.add_argument("--github-output", metavar="FILE", help="append the job outputs here")
    parser.add_argument("--summary", metavar="FILE", help="append a Markdown summary here")
    args = parser.parse_args(argv)

    test_map = load_map()
    if args.check:
        found = problems(args.root, test_map)
        print("\n".join(found) if found else "The test map covers every file and test module.")
        return 1 if found else 0
    durations = load_durations()
    if args.all:
        selection, changed = Selection("all", reasons=["a full run"]), 0
    else:
        paths = (changed_paths(args.root, *args.changed) if args.changed is not None
                 else args.paths)
        selection, changed = select(paths, test_map, args.root), len(paths)
    values = outputs(selection, durations)
    text = summary(selection, durations, changed)
    print(text, end="")
    for key, value in values.items():
        print(f"{key}={value}")
    if args.github_output:
        write_outputs(args.github_output, values)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
