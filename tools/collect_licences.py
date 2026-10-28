# tools/collect_licences.py
# Collect licence texts from each project's own release into packaging/licences/
# (#176). A developer tool: the build and the app never run it, and it is not in
# the package. The build only checks what this writes (tools/build_package.py,
# licence_texts).
#
#   python tools/collect_licences.py --list              every component and its files
#   python tools/collect_licences.py ID [ID ...]         fetch, check and write these texts,
#                                                        and their SHA-256 in the manifest
#   python tools/collect_licences.py --verify [ID ...]   fetch again and compare with the
#                                                        manifest; writes nothing
#   python tools/collect_licences.py --crates WHEEL      make the Rust crate entries of one
#                                                        locked wheel again (#448); writes
#                                                        the manifest only, no texts
#
# Add or change an entry in packaging/licences/manifest.json by hand first (a new
# file's "sha256" may be 64 zeros until it is collected), then collect it and
# review the diff: the manifest says where each text came from, and the hash only
# proves it has not changed since. Rules (#176 review, 19 Oct 2026):
#   - Only https, only the hosts in build_package.LICENCE_HOSTS, every redirect
#     included. GitHub raw URLs name the source commit, never a tag or branch.
#   - A text inside an archive (a source tarball, a wheel or a NuGet package) is
#     read in memory, only after the archive matches the SHA-256 its publisher
#     gives (recorded in the manifest). Nothing is extracted or run.
#   - A text must look like text: no NUL bytes and no HTML page (a GitHub "blob"
#     URL returns HTML).
#   - Every text is fetched and checked before any file is written.
# The crate mode (--crates, #448) is the one part that reaches two more hosts,
# CRATE_HOSTS below, and nothing else does: see crate_entries.
# Run it with the network, from a checkout, with python -I.

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_package as bp  # noqa: E402

TEXT_LIMIT = 2 * 1024 * 1024          # one licence text
ARCHIVE_LIMIT = 256 * 1024 * 1024     # a source tarball, wheel or NuGet package
TIMEOUT = 60
PLACEHOLDER = "0" * 64                # a new entry's hash before it is collected


class CollectError(Exception):
    pass


def allowed_url(url):
    # https on a collector host, with no credentials or port (a malformed one is
    # refused, not raised); a redirect may add a query.
    return bp.licence_url_parts(url) is not None


class CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to https on a collector host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            try:
                where = urllib.parse.urlsplit(newurl).hostname
            except ValueError:
                where = "a malformed address"
            raise CollectError(f"{req.full_url} redirects off the collector hosts ({where}).")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener():
    # Proxies come from the environment, as urllib does by default (a developer's
    # proxy is theirs to use). Redirects are checked.
    return urllib.request.build_opener(CheckedRedirects)


def fetch(url, limit, open_url=None):
    """The bytes at url, at most `limit` of them."""
    if not allowed_url(url):
        raise CollectError(f"{url} is not https on a collector host.")
    open_url = open_url or opener().open
    try:
        with open_url(urllib.request.Request(url, headers={"User-Agent": "collect_licences"}),
                      timeout=TIMEOUT) as r:
            if not allowed_url(r.geturl()):
                raise CollectError(f"{url} ended at {r.geturl()}, off the collector hosts.")
            data = r.read(limit + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise CollectError(f"Could not fetch {url} ({type(exc).__name__}: {exc}).") from None
    if len(data) > limit:
        raise CollectError(f"{url} is larger than {limit} bytes.")
    return data


def member(blob, name):
    """One regular file's bytes from a zip or tar archive held in memory."""
    if zipfile.is_zipfile(io.BytesIO(blob)):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            infos = [i for i in z.infolist() if i.filename == name]
            if len(infos) != 1 or infos[0].is_dir():
                raise CollectError(f"The archive has no single file {name}.")
            if infos[0].file_size > TEXT_LIMIT:
                raise CollectError(f"{name} in the archive is larger than {TEXT_LIMIT} bytes.")
            return z.read(infos[0])
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as t:
            infos = [i for i in t.getmembers() if i.name == name]
            if len(infos) != 1 or not infos[0].isreg():
                raise CollectError(f"The archive has no single regular file {name}.")
            if infos[0].size > TEXT_LIMIT:
                raise CollectError(f"{name} in the archive is larger than {TEXT_LIMIT} bytes.")
            return t.extractfile(infos[0]).read()
    except tarfile.TarError:
        raise CollectError("The archive is neither a zip nor a tar file.") from None


def check_text(data, where):
    if not data.strip():
        raise CollectError(f"{where} is empty.")
    if b"\0" in data:
        raise CollectError(f"{where} is not text (it holds NUL bytes).")
    head = data[:512].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        raise CollectError(f"{where} is an HTML page, not the text (a 'blob' URL?).")
    return data


def collect_file(f, open_url=None):
    """A manifest file entry's text, fetched and checked."""
    if "url" in f:
        return check_text(fetch(f["url"], TEXT_LIMIT, open_url), f["url"])
    archive = f["archive"]
    blob = fetch(archive["url"], ARCHIVE_LIMIT, open_url)
    digest = hashlib.sha256(blob).hexdigest()
    if digest != archive["sha256"]:
        raise CollectError(f"{archive['url']} has SHA-256 {digest}, not the published "
                           f"{archive['sha256']}.")
    return check_text(member(blob, archive["member"]), f"{archive['member']} in {archive['url']}")


def read_manifest(folder):
    text = (folder / bp.LICENCE_MANIFEST).read_text(encoding="utf-8")
    return json.loads(text), bp.licence_manifest(text)


def write_manifest(folder, data):
    (folder / bp.LICENCE_MANIFEST).write_text(
        json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def chosen(components, ids):
    known = {c["id"]: c for c in components}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise CollectError("Not in the manifest: " + ", ".join(unknown))
    return [known[i] for i in ids] if ids else components


def collect(folder, ids, open_url=None, out=print):
    """Fetch and check every text of these components, then write them all and
    their hashes. Returns the number of files that changed."""
    data, components = read_manifest(folder)
    picked = chosen(components, ids)
    fetched = [(f, collect_file(f, open_url)) for c in picked for f in c["files"]]
    changed = 0
    by_path = {f["path"]: f for c in data["components"] for f in c.get("files", [])}
    for f, text in fetched:
        digest = hashlib.sha256(text).hexdigest()
        target = (folder / f["path"]).resolve()
        if folder.resolve() not in target.parents:
            raise CollectError(f"{f['path']} is outside {folder}.")
        state = ("new" if f["sha256"] == PLACEHOLDER else
                 "unchanged" if f["sha256"] == digest else "CHANGED")
        changed += state != "unchanged"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text)
        by_path[f["path"]]["sha256"] = digest
        out(f"{f['path']}: {digest} ({state})")
    write_manifest(folder, data)
    return changed


def verify(folder, ids, open_url=None, out=print):
    """Fetch every text again and compare it with the manifest and the file
    beside it. Returns the number that differ; writes nothing."""
    _, components = read_manifest(folder)
    differ = 0
    for c in chosen(components, ids):
        for f in c["files"]:
            digest = hashlib.sha256(collect_file(f, open_url)).hexdigest()
            path = folder / f["path"]
            here = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            ok = digest == f["sha256"] == here
            differ += not ok
            out(f"{f['path']}: {'matches' if ok else 'DIFFERS'} (upstream {digest})")
    return differ


def listing(folder, out=print):
    _, components = read_manifest(folder)
    for c in components:
        kind = "in the wheel's own texts" if c.get("in_wheel") else f"{len(c['files'])} text(s)"
        out(f"{c['id']}: {c['name']} {c['version']} ({c['licence']}), in {c['wheel']} "
            f"{c['wheel_version']}, {kind}")
        for p in c["covers"]:
            out(f"    covers {p}")
        for p in c.get("linked_into", []):
            out(f"    compiled into {p}")


# Crate mode (#448). For one locked wheel, make again the entries of the Rust
# crates its own CycloneDX SBOM names, as the #176 D1 entries were made by hand:
#   1. read the wheel's SBOM (<dist-info>/sboms/*.json): the crates in use, as
#      build_package.sbom_crates counts them, each with its SHA-256;
#   2. download each .crate from static.crates.io, check it against that SHA-256,
#      and read its .cargo_vcs_info.json (commit and path), Cargo.toml and
#      licence files, in memory;
#   3. find through the GitHub API the file at that commit whose git blob equals
#      each licence file (the crate's own folder first, then the folders above
#      it: a workspace's root text when the crate's copy is a symbolic link), and
#      write the entry with its raw.githubusercontent.com URL.
# It writes the manifest only. Each new file's "sha256" is the SHA-256 of the
# crate's own copy, so collecting the entries afterwards (the normal mode, from
# the collector hosts only) reports "unchanged" exactly when the text it fetches
# is byte-identical to the crate's (step 4). A crate it cannot make keeps its
# entry made by hand for the same version, or is reported as a gap, which stops
# a release build until an entry is made by hand.
# Owner decision DEC-075 (27 Oct 2026, #448): this mode alone may reach
# CRATE_HOSTS. They are not collector hosts (build_package.LICENCE_HOSTS): the
# normal mode, the build and the app never reach them, and no text comes from
# them. A GITHUB_TOKEN in the environment, if set, goes to api.github.com only,
# in a header urllib never carries across a redirect; without one GitHub allows
# 60 API requests an hour, one for each repository and commit.
CRATE_HOSTS = ("static.crates.io", "api.github.com")
TREE_LIMIT = 64 * 1024 * 1024          # one repository tree from the GitHub API
CRATE_URL = "https://static.crates.io/crates/{name}/{name}-{version}.crate"
TREE_URL = "https://api.github.com/repos/{owner}/{repo}/git/trees/{commit}?recursive=1"
RAW_URL = "https://raw.githubusercontent.com/{owner}/{repo}/{commit}/{path}"
# A licence file in a crate: its name starts with one of these words, and it is
# not source code or a manifest.
LICENCE_FILE = re.compile(r"(?i)(?:un)?licen[cs]e|copying|copyright|notice")
NOT_A_TEXT = (".rs", ".toml", ".json", ".yml", ".yaml", ".sh", ".py", ".c", ".h", ".cc",
              ".cpp", ".js", ".html", ".in")
SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+-]*")
GITHUB_NAME = re.compile(r"[A-Za-z0-9_.-]+")
HEX40 = re.compile(r"[0-9a-f]{40}")


class CrateGap(CollectError):
    """A crate this mode cannot make an entry for; the run goes on without it."""


def crate_url_parts(url):
    """urlsplit's parts if url is https on a crate host with nothing but the host
    before the path, else None. The crate mode's only hosts."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme != "https" or host not in CRATE_HOSTS or parts.netloc.lower() != host:
        return None
    return parts


class CrateRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to https on a crate host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if crate_url_parts(newurl) is None:
            raise CollectError(f"{req.full_url} redirects off the crate hosts.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def crate_fetch(url, limit, open_url=None, token=None):
    """The bytes at url on a crate host, at most `limit` of them. A missing
    crate, repository or commit is a CrateGap; anything else stops the run."""
    parts = crate_url_parts(url)
    if parts is None:
        raise CollectError(f"{url} is not https on a crate host.")
    request = urllib.request.Request(url, headers={"User-Agent": "collect_licences"})
    api = parts.hostname == "api.github.com"
    if api:
        request.add_header("Accept", "application/vnd.github+json")
        if token:
            # Never carried across a redirect, never printed.
            request.add_unredirected_header("Authorization", f"Bearer {token}")
    open_url = open_url or urllib.request.build_opener(CrateRedirects).open
    try:
        with open_url(request, timeout=TIMEOUT) as r:
            if crate_url_parts(r.geturl()) is None:
                raise CollectError(f"{url} ended off the crate hosts.")
            data = r.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410, 422):
            raise CrateGap(f"{url} is not there (HTTP {exc.code}).") from None
        if api and exc.code in (403, 429):
            raise CollectError(f"GitHub's API refused {url} (HTTP {exc.code}), probably its "
                               "rate limit: set GITHUB_TOKEN and run again.") from None
        raise CollectError(f"Could not fetch {url} (HTTP {exc.code}).") from None
    except (urllib.error.URLError, OSError) as exc:
        raise CollectError(f"Could not fetch {url} ({type(exc).__name__}: {exc}).") from None
    if len(data) > limit:
        raise CollectError(f"{url} is larger than {limit} bytes.")
    return data


def locked_hashes(text):
    """{(canonical name, version): {sha256, ...}} from a hash-locked requirements file."""
    found, current = {}, None
    for line in text.replace("\\\n", " ").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        words = line.split()
        if "==" in words[0]:
            name, version = words[0].split("==", 1)
            current = found.setdefault((bp.canonical(name.split("[")[0]), version), set())
            words = words[1:]
        for w in words:
            if w.startswith("--hash=sha256:") and current is not None:
                current.add(w[len("--hash=sha256:"):])
    return found


def sbom_crate_hashes(data):
    """{purl: (SHA-256 or None, licence expression or None)} for the crates
    build_package.sbom_crates counts in one SBOM (the same walk, so the two cannot
    disagree on which crates)."""
    try:
        doc = json.loads(data)
    except ValueError as exc:
        raise CollectError(f"A wheel's SBOM is not JSON ({exc}).") from None
    found, licences = {}, {}
    todo = list(doc.get("components") or []) if isinstance(doc, dict) else []
    while todo:
        c = todo.pop()
        if not isinstance(c, dict):
            continue
        todo += c.get("components") or []
        purl = c.get("purl")
        if (not isinstance(purl, str) or not purl.startswith("pkg:cargo/")
                or c.get("scope") == "excluded" or "download_url=file" in purl):
            continue
        purl = purl.split("?", 1)[0].split("#", 1)[0]
        sums = {h.get("content") for h in c.get("hashes") or []
                if isinstance(h, dict) and h.get("alg") == "SHA-256"}
        sums = {s for s in sums if isinstance(s, str) and bp._HEX64.fullmatch(s)}
        found.setdefault(purl, set()).update(sums)
        terms = [x.get("expression") for x in c.get("licenses") or [] if isinstance(x, dict)]
        if len(terms) == 1 and isinstance(terms[0], str) and terms[0].strip():
            licences.setdefault(purl, set()).add(terms[0].strip())
    try:
        counted = set(bp.sbom_crates(data))
    except SystemExit as exc:
        raise CollectError(str(exc.code)) from None
    if counted != set(found):
        raise CollectError("The SBOM's crates differ from what the build counts.")
    return {p: (only(s), only(licences.get(p, set()))) for p, s in found.items()}


def only(values):
    """The one value in a set, or None when there is none or more than one."""
    return next(iter(values)) if len(values) == 1 else None


def wheel_crates(wheel, lock_text):
    """(name, version, its one .pyd, {purl: (SHA-256, licence)}) of a wheel file,
    which must be one the lock pins by SHA-256."""
    try:
        blob = wheel.read_bytes()
    except OSError as exc:
        raise CollectError(f"Cannot read {wheel} ({type(exc).__name__}).") from None
    stem = wheel.name[:-len(".whl")] if wheel.name.endswith(".whl") else ""
    parts = stem.split("-")
    if len(parts) < 5:
        raise CollectError(f"{wheel.name} is not a wheel file name.")
    name, version = bp.canonical(parts[0]), parts[1]
    hashes = locked_hashes(lock_text).get((name, version))
    if not hashes:
        raise CollectError(f"{name} {version} is not in the lock.")
    if hashlib.sha256(blob).hexdigest() not in hashes:
        raise CollectError(f"{wheel.name} is not a file the lock pins (its SHA-256 differs).")
    crates = {}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = z.namelist()
            sboms = [z.read(n) for n in names
                     if re.fullmatch(r"[^/]+\.dist-info/sboms/[^/]+\.json", n)]
    except zipfile.BadZipFile:
        raise CollectError(f"{wheel.name} is not a zip file.") from None
    for sbom in sboms:
        crates.update(sbom_crate_hashes(sbom))
    pyds = [n for n in names if n.lower().endswith(".pyd")]
    if len(pyds) != 1:
        raise CollectError(f"{wheel.name} has {len(pyds)} .pyd libraries, not one: make "
                           "its entries by hand.")
    if not crates:
        raise CollectError(f"{wheel.name}'s SBOM names no crate in use.")
    return name.replace("_", "-"), version, pyds[0], crates


def github_repository(url):
    """(owner, repo) of a https://github.com/ repository URL, else None."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except (AttributeError, ValueError):
        return None
    segments = [s for s in parts.path.split("/") if s]
    if (parts.scheme != "https" or parts.netloc.lower() not in ("github.com", "www.github.com")
            or len(segments) < 2):
        return None
    owner, repo = segments[0], segments[1].removesuffix(".git")
    if not all(GITHUB_NAME.fullmatch(s) and s not in (".", "..") for s in (owner, repo)):
        return None
    return owner, repo


def crate_parts(blob, name, version):
    """What the crate mode reads from a .crate, in memory: (commit, path_in_vcs,
    Cargo.toml's package table, {path in the crate: bytes} of its licence files)."""
    prefix = f"{name}-{version}/"
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
            members = {m.name[len(prefix):]: m for m in t.getmembers()
                       if m.name.startswith(prefix)}

            def read(rel, m):
                if not m.isreg():
                    raise CrateGap(f"{name} {version}: {rel} is a link in the crate.")
                if m.size > TEXT_LIMIT:
                    raise CrateGap(f"{name} {version}: {rel} is larger than {TEXT_LIMIT} bytes.")
                return t.extractfile(m).read()

            if ".cargo_vcs_info.json" not in members:
                raise CrateGap(f"{name} {version}: the crate records no commit.")
            vcs = json.loads(read(".cargo_vcs_info.json", members[".cargo_vcs_info.json"]))
            if "Cargo.toml" not in members:
                raise CrateGap(f"{name} {version}: the crate has no Cargo.toml.")
            package = tomllib.loads(read("Cargo.toml", members["Cargo.toml"])
                                    .decode("utf-8")).get("package")
            texts = {}
            for rel, m in members.items():
                base = rel.rsplit("/", 1)[-1]
                if (rel and LICENCE_FILE.match(base) and not base.lower().endswith(NOT_A_TEXT)
                        and not m.isdir()):
                    texts[rel] = read(rel, m)
    except (tarfile.TarError, EOFError, OSError):
        raise CrateGap(f"{name} {version}: the .crate is not a gzipped tar file.") from None
    except (ValueError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CrateGap(f"{name} {version}: cannot read its metadata ({exc}).") from None
    git = vcs.get("git") if isinstance(vcs, dict) else None
    commit = git.get("sha1") if isinstance(git, dict) else None
    if not isinstance(commit, str) or not HEX40.fullmatch(commit):
        raise CrateGap(f"{name} {version}: the crate records no commit.")
    # A crate published from changed files ("dirty") is fine here: each text is
    # matched byte for byte with the file at the commit, or not used.
    where = vcs.get("path_in_vcs", "")
    if not isinstance(where, str) or (where and not all(SEGMENT.fullmatch(s)
                                                        for s in where.split("/"))):
        raise CrateGap(f"{name} {version}: its path_in_vcs is not a plain path.")
    if not isinstance(package, dict) or (package.get("name"), package.get("version")) != (
            name, version):
        raise CrateGap(f"{name} {version}: its Cargo.toml names another package.")
    if not texts:
        raise CrateGap(f"{name} {version}: the crate has no licence file.")
    for rel, data in texts.items():
        if not all(SEGMENT.fullmatch(s) for s in rel.split("/")):
            raise CrateGap(f"{name} {version}: {rel} is not a plain file name.")
        try:
            check_text(data, f"{rel} in {name} {version}")
        except CollectError as exc:
            raise CrateGap(str(exc)) from None
    return commit, where, package, texts


def git_blob(data):
    """The git blob id of these bytes, as a GitHub tree lists it."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data,     # git's object id, not a check
                        usedforsecurity=False).hexdigest()


def source_path(tree, where, rel, blob):
    """The path at the commit whose blob is `blob`: the crate's own path, then the
    same name in each folder above it, then any name in those folders. None if no
    file there has those bytes."""
    full = "/".join(p for p in (where, rel) if p)
    folder, _, base = full.rpartition("/")
    folders = []
    while True:
        folders.append(folder)
        if not folder:
            break
        folder = folder.rpartition("/")[0]
    candidates = [full] + [f"{f}/{base}" if f else base for f in folders[1:]]
    for c in candidates:
        if tree.get(c) == blob:
            return c
    near = sorted((p for p, b in tree.items() if b == blob and p.rpartition("/")[0] in folders
                   and SEGMENT.fullmatch(p.rpartition("/")[2])),
                  key=lambda p: (-p.count("/"), p))
    return near[0] if near else None


def crate_tree(owner, repo, commit, cache, open_url, token):
    """(owner, repo) as GitHub spells them, {path: blob id} of the regular files
    at a commit, and whether GitHub cut the listing short. One API request per
    repository and commit."""
    key = (owner.lower(), repo.lower(), commit)
    if key not in cache:
        url = TREE_URL.format(owner=owner, repo=repo, commit=commit)
        try:
            doc = json.loads(crate_fetch(url, TREE_LIMIT, open_url, token))
            tree = {e["path"]: e["sha"] for e in doc["tree"]
                    if e.get("type") == "blob" and e.get("mode") in ("100644", "100755")}
            # The repository's name as GitHub gives it now: its own spelling (a
            # crate may write "pyo3/pyo3" for PyO3/pyo3), or its new name when it
            # was renamed or moved (Geal/nom is now rust-bakery/nom).
            named = re.fullmatch(r"https://api\.github\.com/repos/([^/]+)/([^/]+)/git/trees/.*",
                                 str(doc.get("url", "")))
            if named and GITHUB_NAME.fullmatch(named[1]) and GITHUB_NAME.fullmatch(named[2]):
                owner, repo = named[1], named[2]
            cache[key] = (owner, repo, tree, bool(doc.get("truncated")))
        except (ValueError, KeyError, TypeError):
            raise CollectError(f"GitHub's tree for {owner}/{repo} {commit} is not as "
                               "expected.") from None
        except CrateGap as exc:
            cache[key] = exc
    if isinstance(cache[key], CrateGap):
        raise cache[key]
    return cache[key]


def manifest_name(rel):
    """A crate's licence file as the manifest stores it: one file name, its folders
    joined by dots, and a Markdown text as .md.txt."""
    flat = rel.replace("/", ".")
    return flat + ".txt" if flat.lower().endswith(".md") else flat


def crate_entry(wheel, wheel_version, pyd, purl, sbom, dup, cache, open_url, token):
    """One crate's manifest entry, made from the crate itself. sbom: (SHA-256,
    licence expression) as the wheel's SBOM gives them. Raises CrateGap."""
    if not bp._LICENCE_CRATE.fullmatch(purl):
        raise CrateGap(f"{purl} is not a plain pkg:cargo name@version.")
    name, version = purl[len("pkg:cargo/"):].split("@", 1)
    sha, licence = sbom
    if sha is None:
        raise CrateGap(f"{name} {version}: the SBOM gives no single SHA-256 for it.")
    url = CRATE_URL.format(name=name, version=version)
    blob = crate_fetch(url, ARCHIVE_LIMIT, open_url)
    if hashlib.sha256(blob).hexdigest() != sha:
        raise CrateGap(f"{name} {version}: {url} does not match the SBOM's SHA-256.")
    commit, where, package, texts = crate_parts(blob, name, version)
    repository = package.get("repository")
    found = github_repository(repository) if isinstance(repository, str) else None
    if found is None:
        raise CrateGap(f"{name} {version}: its repository is not on GitHub ({repository}).")
    # The licence as the SBOM states it (SPDX, as the #176 entries record it), else
    # as the crate's Cargo.toml does.
    licence = licence or package.get("license")
    if not isinstance(licence, str) or not licence.strip():
        raise CrateGap(f"{name} {version}: neither the SBOM nor its Cargo.toml gives a "
                       "licence expression.")
    owner, repo, tree, truncated = crate_tree(*found, commit, cache, open_url, token)
    cid = f"{wheel}.{name}.{version}" if dup else f"{wheel}.{name}"
    cid = cid.lower()
    files, seen = [], set()
    # A text the crate holds twice (pyo3's pyo3-runtime/ copies of its root texts)
    # is listed once, at the shallowest place.
    for rel, data in sorted(texts.items(), key=lambda t: (t[0].count("/"), t[0])):
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        path = source_path(tree, where, rel, git_blob(data))
        if path is None:
            raise CrateGap(f"{name} {version}: no file at {owner}/{repo} {commit} has the "
                           f"bytes of its {rel}" + (" (GitHub cut the listing short)."
                                                    if truncated else "."))
        files.append({"path": f"{cid}-{version}/{manifest_name(rel)}",
                      "url": RAW_URL.format(owner=owner, repo=repo, commit=commit, path=path),
                      "sha256": digest})
    return {"id": cid, "name": name, "version": version, "licence": licence.strip(),
            "wheel": wheel, "wheel_version": wheel_version, "covers": [], "linked_into": [pyd],
            "sbom": purl, "bundle": f"{wheel}-{wheel_version}-rust-crates",
            "source": {"repository": f"https://github.com/{owner}/{repo}",
                       "ref": f"{name} {version} (crates.io)", "commit": commit},
            "files": sorted(files, key=lambda f: f["path"])}


def crate_order(c):
    return (c["name"], c["version"], c["wheel"])


def crate_entries(folder, wheel, lock_text, open_url=None, token=None, out=print):
    """Make the crate entries of one locked wheel file again and write the
    manifest (no texts). Returns the number of crates left without an entry."""
    data, _ = read_manifest(folder)
    name, version, pyd, crates = wheel_crates(wheel, lock_text)
    counts = {}
    for purl in crates:
        crate = purl[len("pkg:cargo/"):].split("@", 1)[0]
        counts[crate] = counts.get(crate, 0) + 1
    old = {c["sbom"]: c for c in data["components"]
           if "sbom" in c and bp.canonical(c["wheel"]) == bp.canonical(name)}
    cache, made, kept, gaps = {}, {}, {}, []
    for purl in sorted(crates):
        crate = purl[len("pkg:cargo/"):].split("@", 1)[0]
        try:
            made[purl] = crate_entry(name, version, pyd, purl, crates[purl], counts[crate] > 1,
                                     cache, open_url, token)
            out(f"{purl}: made ({len(made[purl]['files'])} text(s))")
            if old.get(purl, {}).get("note"):
                out(f"    its entry made by hand had a note, now dropped: {old[purl]['note']}")
        except CrateGap as exc:
            if purl in old:
                kept[purl] = {**old[purl], "wheel_version": version,
                              "bundle": f"{name}-{version}-rust-crates"}
                out(f"{purl}: kept the entry made by hand ({exc})")
            else:
                gaps.append(purl)
                out(f"{purl}: NO ENTRY ({exc})")
    new = {**made, **kept}
    # Replace this wheel's crate entries in place, drop the ones its SBOM no longer
    # names, and put each new one among the crate entries in order.
    components = []
    for c in data["components"]:
        if "sbom" in c and bp.canonical(c["wheel"]) == bp.canonical(name):
            if c["sbom"] in new:
                components.append(new.pop(c["sbom"]))
        else:
            components.append(c)
    for entry in sorted(new.values(), key=crate_order):
        at = next((i for i, c in enumerate(components)
                   if "sbom" in c and crate_order(c) > crate_order(entry)), None)
        if at is None:
            crate_at = [i for i, c in enumerate(components) if "sbom" in c]
            at = crate_at[-1] + 1 if crate_at else len(components)
        components.insert(at, entry)
    data["components"] = components
    try:
        bp.licence_manifest(json.dumps(data))
    except SystemExit as exc:
        raise CollectError(f"The entries made are not valid, so nothing was written:\n"
                           f"{exc.code}") from None
    write_manifest(folder, data)
    listed = {f["path"] for c in components for f in c.get("files", [])}
    for path in sorted({f["path"] for c in old.values() for f in c["files"]} - listed):
        out(f"no longer listed, remove it: {bp.LICENCES_DIR}/{path}")
    ids = sorted(c["id"] for c in made.values())
    out(f"Made {len(made)}, kept {len(kept)}, {len(gaps)} without an entry. Next, collect "
        "the ones made; each text should say 'unchanged' (byte-identical to the crate's):")
    out("    python -I tools/collect_licences.py " + " ".join(ids))
    return len(gaps)


def main(argv=None):
    p = argparse.ArgumentParser(description="Collect licence texts into packaging/licences "
                                            "(developer tool, #176).")
    p.add_argument("ids", nargs="*", metavar="ID", help="components to collect or verify")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="list the manifest; no network")
    mode.add_argument("--verify", action="store_true",
                      help="fetch again and compare; writes nothing")
    mode.add_argument("--crates", metavar="WHEEL", type=Path,
                      help="make the Rust crate entries of this locked wheel file again "
                           "(#448; reaches static.crates.io and api.github.com)")
    args = p.parse_args(argv)
    folder = bp.LICENCES
    try:
        if args.crates:
            if args.ids:
                p.error("--crates takes a wheel file, not component ids")
            lock = (bp.ROOT / "requirements.txt").read_text(encoding="utf-8")
            gaps = crate_entries(folder, args.crates, lock,
                                 token=os.environ.get("GITHUB_TOKEN") or None)
            print("Written: review the diff." if not gaps else
                  f"Written, with {gaps} crate(s) left for entries by hand: review the diff.")
            return 1 if gaps else 0
        if args.list:
            listing(folder)
            return 0
        if args.verify:
            differ = verify(folder, args.ids)
            print("Every text matches upstream." if not differ else f"{differ} differ.")
            return 1 if differ else 0
        if not args.ids:
            p.error("name the components to collect (or --list, --verify)")
        changed = collect(folder, args.ids)
        print(f"Written. {changed} file(s) new or changed: review the diff before committing.")
        return 0
    except (CollectError, SystemExit) as exc:
        if isinstance(exc, SystemExit) and not isinstance(exc.code, str):
            raise
        print(exc if isinstance(exc, CollectError) else exc.code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
