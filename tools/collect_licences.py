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
# Run it with the network, from a checkout, with python -I.

import argparse
import hashlib
import io
import json
import sys
import tarfile
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


def main(argv=None):
    p = argparse.ArgumentParser(description="Collect licence texts into packaging/licences "
                                            "(developer tool, #176).")
    p.add_argument("ids", nargs="*", metavar="ID", help="components to collect or verify")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="list the manifest; no network")
    mode.add_argument("--verify", action="store_true",
                      help="fetch again and compare; writes nothing")
    args = p.parse_args(argv)
    folder = bp.LICENCES
    try:
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
