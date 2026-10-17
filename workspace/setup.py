# workspace/setup.py
# Guided model setup (#19): model cards, downloads, verification and repair.
#
# Downloads run in a separate, short-lived download process. The app process
# keeps its offline network guard and never toggles network flags; inference
# workers never download. The Hugging Face token (only needed for the gated
# speaker model) is handed to the download process through a pipe, never on
# a command line or in the environment, and is used only for that download.
#
# Every download is verified file by file against the pinned SHA-256
# manifest before it replaces anything (asr_engines.install_model); a damaged
# copy is set aside, never deleted, and an interrupted download is retried by
# simply starting it again.

import multiprocessing as mp
import os
import threading
import time
import urllib.request
from pathlib import Path

import asr_engines
from workspace import gpu

HELP_LINKS = {   # fixed pages the user can open in their own browser (allowlist)
    "account": "https://huggingface.co/join",
    "terms": "https://huggingface.co/pyannote/speaker-diarization-community-1",
    "tokens": "https://huggingface.co/settings/tokens",
    # The user guide's recovery steps, from the database damage message (R11).
    "recovery": "https://github.com/thatskiff33/unminute/blob/main/docs/WORKSPACE_GUIDE.md"
                "#if-unminute-finds-damage",
}

def _speech_card(name, recommended, about):
    label = asr_engines.label(name)
    return {"name": name, "role": "speech", "recommended": recommended, "label": label,
            "title": f"Speech to text: {label}" + (" (recommended)" if recommended else ""),
            "about": about}


# The page shows "label" as it is and never reads a title apart. Which speech
# model is recommended comes from asr_engines, never from the order here.
SPEECH = [
    (asr_engines.PARAKEET_V2, "Fast and accurate for English. Runs on this computer's processor."),
    (asr_engines.PARAKEET_V3,
     "Newer multilingual version. In our tests it was less accurate for English."),
    (asr_engines.WHISPER_TURBO, "Alternative engine; slower on a processor."),
]
CARDS = [
    *(_speech_card(name, name == asr_engines.RECOMMENDED_PARAKEET, about)
      for name, about in SPEECH),
    {"name": asr_engines.SPEAKER_MODEL.name, "role": "speakers", "recommended": True,
     "label": "Speaker identification", "title": "Speaker identification (optional)",
     "about": "Works out who spoke when. Downloading it needs a free Hugging Face account "
              "and token; once downloaded it works offline and the token is not needed."},
]


class SetupError(RuntimeError):
    """A refused or failed setup step (message is safe to show)."""


def _size(name):
    spec = asr_engines.MANAGED_MODELS[name]
    extra = asr_engines.SILERO_VAD.download_bytes if asr_engines.is_parakeet(name) else 0
    return spec.download_bytes + extra


def status(name, cache_dir):
    """('ready' | 'missing' | 'invalid', plain-language detail)."""
    if asr_engines.is_parakeet(name):
        state, detail = asr_engines.parakeet_status(name, cache_dir)
    else:
        state, detail = asr_engines.hub_model_status(name, cache_dir)
    plain = {"ready": "Downloaded and verified.",
             "missing": "Not downloaded yet.",
             "invalid": "Damaged or incomplete. Repair downloads a fresh, verified copy; "
                        "the damaged copy is kept aside, not deleted."}[state]
    return state, plain, detail


def gpu_card(cache_dir):
    """The optional graphics card component, offered only with an NVIDIA card."""
    info = gpu.detected()
    if info is None:
        return None
    state, plain = gpu.status(cache_dir)
    about = (f"Speeds up speaker identification on this computer's {info['name']}. "
             "Used only in Faster mode, once turned on in Settings. Transcription stays "
             "on the processor.")
    if not info["driver_ok"]:
        about += (f" The NVIDIA driver ({info['driver']}) is too old: version "
                  f"{gpu.MIN_DRIVER_TEXT} or later is needed. Ask IT to update it.")
    return {"name": gpu.COMPONENT, "role": "gpu", "recommended": False,
            "label": "Graphics card acceleration",
            "title": "Graphics card acceleration (optional)", "about": about,
            "state": state, "status": plain, "licence": gpu.LICENCE[0],
            "source": gpu.LICENCE[1], "size_gb": round(gpu.DOWNLOAD_BYTES / 1e9, 2),
            "revision": gpu.VERSION, "needs_token": False}


def cards(cache_dir):
    out = []
    for c in CARDS:
        state, plain, _ = status(c["name"], cache_dir)
        lic, source = asr_engines.LICENCES[c["name"]]
        spec = asr_engines.MANAGED_MODELS[c["name"]]
        out.append({**c, "state": state, "status": plain, "licence": lic, "source": source,
                    "size_gb": round(_size(c["name"]) / 1e9, 2), "revision": spec.revision[:7],
                    "needs_token": spec.gated})
    card = gpu_card(cache_dir)
    if card is not None:
        out.append(card)
    for c in out:
        # Remove is offered whenever something is on disk, including leftover
        # damaged or unfinished copies of a model that is not installed.
        c["removable"] = bool(removal_targets(c["name"], cache_dir))
        c["shared_with_earlier_app"] = c["role"] != "gpu" and not asr_engines.is_parakeet(
            c["name"])
    return out


# -- the download process ------------------------------------------------------------

BLOCKED_MESSAGE = ("The download site sent the download to an address this app does not "
                   "allow, so it was stopped. Nothing was installed.")


def _blocked(exc):
    """True if this error, or one it was raised from, is the network guard."""
    import offline_guard
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, offline_guard.NetworkBlockedError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


PROXY_MESSAGE = ("Your organisation's proxy refused the connection to the download site. Ask "
                 "your IT team to allow huggingface.co and hf.co, or to install the models "
                 "for you.")


def _in_chain(exc, type_name):
    """True if this error, or one it was raised from, has this type name
    (httpx is not imported here)."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if type(exc).__name__ == type_name:
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def _friendly(exc):
    """Download errors in plain language. Never includes the token."""
    if _blocked(exc):
        return BLOCKED_MESSAGE
    if _in_chain(exc, "ProxyError"):
        return PROXY_MESSAGE
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if "gated" in low or "403" in low or "awaiting a review" in low:
        return ("Your Hugging Face account has not accepted the speaker model's terms yet. "
                "Open the model page, accept the terms, then try again.")
    if "401" in low or "unauthorized" in low or ("invalid" in low and "token" in low):
        return "Hugging Face did not accept the token. Check it, or create a new read token."
    if any(k in low for k in ("connection", "timeout", "temporarily", "resolve", "offline")):
        return "Could not reach the download site. Check the internet connection and try again."
    if isinstance(exc, asr_engines.ModelLoadError):
        return str(exc).split(" (")[0] + ". Try again."
    return "The download did not finish. Nothing was changed; try again."


# The only hosts the download process may reach (and their subdomains), plus
# any proxy configured for this user. Hugging Face serves files from
# huggingface.co and its CDN hosts under huggingface.co and hf.co; the
# optional GPU component comes from the pinned PyTorch host. Every HTTP
# request and redirect is checked against this list (_check_request), which
# also holds behind a proxy; the network guard checks DNS lookups as well.
DOWNLOAD_HOSTS = ("huggingface.co", "hf.co", "download-r2.pytorch.org")


def _check_request(request):
    """httpx request hook, run for every request a download makes and for each
    redirect it follows. Behind a proxy this computer never looks up the
    target name (the proxy does), so the guard's DNS check alone would not see
    a redirect to another host: this check does."""
    import offline_guard
    host = request.url.raw_host       # the exact name sent (httpx decodes xn-- in .host)
    if request.url.scheme != "https" or not offline_guard.host_allowed(host):
        raise offline_guard.NetworkBlockedError(
            13, f"Download address not allowed: {request.url.host}")


def prepare_download_process():
    """Set up the download process. Must run before huggingface_hub is
    imported anywhere in it:
      - telemetry off, and the native Xet downloader off (it would bypass the
        guard and Python's TLS and proxy handling);
      - the network guard with the DOWNLOAD_HOSTS allowlist;
      - certificates checked against the Windows certificate store, so
        downloads work behind a firm's TLS-inspecting proxy;
      - every HTTP request and redirect checked against the allowlist."""
    import sys
    if "huggingface_hub" in sys.modules:
        raise RuntimeError("huggingface_hub was imported before the download process was "
                           "set up")
    import offline_guard
    offline_guard.disable_telemetry()
    os.environ["HF_HUB_OFFLINE"] = "0"          # this process exists to download
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    offline_guard.install()
    offline_guard.allow_hosts(DOWNLOAD_HOSTS)
    import truststore
    truststore.inject_into_ssl()
    import huggingface_hub
    from huggingface_hub.utils import _http as hf_http

    def checked_client():
        client = hf_http.default_client_factory()
        hooks = client.event_hooks
        hooks["request"] = [*hooks.get("request", []), _check_request]
        client.event_hooks = hooks
        return client
    huggingface_hub.set_client_factory(checked_client)


def model_downloader(token):
    """The download function for asr_engines.install(): exactly the pinned
    files of one revision. A gated model gets the token from the pipe and a
    public one gets none. huggingface_hub is never left to find a token itself
    (token=None would read HF_TOKEN or its token file), so False, not None."""
    def download(spec, dest):
        from huggingface_hub import snapshot_download
        snapshot_download(spec.repo, revision=spec.revision, local_dir=str(dest),
                          allow_patterns=[f.name for f in spec.files],
                          token=(token or False) if spec.gated else False)
    return download


def _download_entry(target, conn, name, cache_dir, log_dir):
    """The download process's start: its own content-free log (R9: unexpected
    errors, native crashes, and console output reduced to type names), then
    the download itself (target)."""
    import sys
    from workspace import diag
    log = diag.Log(log_dir, "setup") if log_dir else None
    if log is not None:
        if sys.stderr is None or sys.stdout is None:          # pythonw: no console
            sys.stdout, sys.stderr = diag.OutputSummary(log, "stdout"), \
                diag.OutputSummary(log, "stderr")
        diag.use(log)
    diag.quiet_crashes()
    try:
        target(conn, name, cache_dir)
    except BaseException as exc:
        diag.incident("setup", exc)
        raise


def _download_main(conn, name, cache_dir):
    """Runs in the download process. Network is allowed here only, and only
    to DOWNLOAD_HOSTS."""
    import sys
    if sys.stderr is None or sys.stdout is None:   # pythonw, and no log: no console
        sys.stdout = sys.stderr = open(os.devnull, "w")
    try:
        prepare_download_process()
    except Exception:
        conn.send(("error", "Downloads could not be set up safely on this computer. "
                            "Ask your IT team."))
        return
    token = conn.recv()                          # via the pipe, never argv/env
    from workspace.worker import safe_message

    download = model_downloader(token)

    def log(message):
        conn.send(("log", safe_message(message)[:300]))
    try:
        if name == gpu.COMPONENT:
            conn.send(("done", gpu.install(cache_dir, log, _fetch)))
            return
        if asr_engines.MANAGED_MODELS[name].gated and not token:
            raise SetupError("The speaker model needs your Hugging Face token to download.")
        result = asr_engines.install(name, cache_dir, log, download,
                                     has_token=bool(token))
        conn.send(("done", result))
    except (SetupError, gpu.GpuError) as exc:
        conn.send(("error", str(exc)))
    except Exception as exc:
        conn.send(("error", _friendly(exc.__cause__ or exc)))


def _size_of(path, since=None):
    """Bytes in the files under path (with since, only files changed since
    then: a cancelled attempt's leftovers do not count). A link or junction
    (any reparse point) is never followed, at the top or below (T5): a link
    planted in a download folder cannot point the count elsewhere or loop it."""
    if _not_followed(path):
        return 0
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not _not_followed(os.path.join(root, d))]
        for f in files:
            total += _file_size(os.path.join(root, f), since)
    return total


def _not_followed(path):
    try:
        return _is_link(path)
    except OSError:
        return True                             # gone or unreadable: nothing to count


def _file_size(path, since=None):
    """A file's own size; 0 for a link, a junction, a file that is gone, or
    (with since) one last changed before then."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    links = os.path.islink(path) or getattr(st, "st_file_attributes", 0) & 0x400
    stale = since is not None and st.st_mtime < since - 2       # 2 s: coarse clocks
    return 0 if links or stale else st.st_size


def removal_targets(name, cache_dir):
    """Every folder that holds this model on this computer: the managed copy,
    copies set aside as damaged, and older Hugging Face cache copies. The voice
    detector shared by the Parakeet models goes only with the last of them."""
    from pathlib import Path
    cache = Path(cache_dir)
    if name == gpu.COMPONENT:
        base = gpu.component_dir(cache)
        return [p for p in base.parent.glob(gpu.VERSION + "*") if p.is_dir()]
    spec = asr_engines.MANAGED_MODELS[name]
    folder = asr_engines.model_dir(spec, cache)
    out = [folder] if folder.is_dir() else []
    out += [p for p in folder.parent.glob(spec.folder + ".invalid-*") if p.is_dir()]
    staging = asr_engines.staging_dir(spec, cache)      # a download that did not finish
    if staging.is_dir():
        out.append(staging)
    for repo in spec.hub_repos or ():
        hub = cache / ("models--" + repo.replace("/", "--"))
        if hub.is_dir():
            out.append(hub)
    if asr_engines.is_parakeet(name):
        others = [n for n in asr_engines.PARAKEET_MODELS if n != name
                  and asr_engines.model_dir(asr_engines.PARAKEET_MODELS[n], cache).is_dir()]
        vad = asr_engines.model_dir(asr_engines.SILERO_VAD, cache)
        if not others and vad.is_dir():
            out.append(vad)
    return out


class RemovalRefused(OSError):
    """Removal refused for safety (message is safe to show: no paths)."""


def _is_link(path):
    """A symbolic link or a junction (any reparse point): never followed."""
    st = os.lstat(path)
    return os.path.islink(path) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def remove(name, cache_dir):
    """Delete the model's folders; returns the bytes freed. Paths come only
    from the pinned model list, never from the page. A folder that is a link
    (symbolic link or junction) is refused, and every target must lie inside
    the model cache, so nothing outside it can be deleted."""
    import shutil
    from pathlib import Path
    cache = Path(cache_dir).resolve()
    targets = removal_targets(name, cache_dir)
    for p in targets:
        inside = [a for a in Path(p).parents
                  if a != Path(cache_dir) and str(a).startswith(str(Path(cache_dir)))]
        if _is_link(p) or any(_is_link(a) for a in inside if a.exists()):
            raise RemovalRefused("A model folder is a link to somewhere else, so nothing was "
                                 "removed.")
        if not Path(p).resolve().is_relative_to(cache):
            raise RemovalRefused("A model folder is outside the model cache, so nothing was "
                                 "removed.")
    freed = 0
    for p in targets:
        long = gpu._long(p)
        freed += _size_of(long)
        shutil.rmtree(long)
    if name != gpu.COMPONENT:
        spec = asr_engines.MANAGED_MODELS[name]
        marks = Path(cache_dir) / spec.family / ".verified"
        # Exactly "<folder>-<16 hex>.json": large-v3 must not match large-v3-turbo.
        for m in marks.glob(spec.folder + "-" + "?" * 16 + ".json") if marks.is_dir() else ():
            m.unlink(missing_ok=True)
    return freed


class _SameHost(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the pinned HTTPS location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(gpu.DOWNLOAD_HOST):
            raise SetupError("The download site redirected elsewhere. Nothing was installed.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url, dest, expected_size):
    """Stream one file to disk (download process only); the caller checks the
    SHA-256. Only the pinned PyTorch download host is accepted."""
    import urllib.request
    if not url.startswith(gpu.DOWNLOAD_HOST):
        raise SetupError("Unexpected download address.")
    opener = urllib.request.build_opener(_SameHost)
    req = urllib.request.Request(url, headers={"User-Agent": "MeetingWorkspace"})
    done = 0
    with opener.open(req, timeout=120) as r, open(dest, "wb") as out:
        while block := r.read(8 << 20):
            done += len(block)
            if done > expected_size:
                raise SetupError("The download is larger than expected. Nothing was installed.")
            out.write(block)
    if done != expected_size:
        raise SetupError("The download was incomplete. Try again.")


def known(name):
    return name in asr_engines.MANAGED_MODELS or name == gpu.COMPONENT


class Downloads:
    """One download at a time, in its own process; progress for the UI."""

    def __init__(self, cache_dir, before_install=None, target=None, before_gpu_install=None):
        self.cache_dir = str(cache_dir)
        self.target = target or _download_main   # tests: a fake download process
        self.after = None                        # called when a download or check ends
        self.before_install = before_install     # e.g. unload the inference worker
        # The graphics card component is used only by the GPU worker: replacing
        # it stops that worker and leaves transcription running.
        self.before_gpu_install = before_gpu_install
        self.state = {}                          # name -> {"state", "message", "log"}
        self.log_dir = None                      # the app's logs folder (R9), set by the app
        self._lock = threading.Lock()
        self._running = None

    def start(self, name, token=None, on_success=None):
        """on_success runs only after a verified download (e.g. remember the
        token only once it has been shown to work)."""
        if not known(name):
            raise SetupError("Unknown model.")
        with self._lock:
            if self._running:
                raise SetupError("Another download is in progress. Wait for it to finish, "
                                 "or cancel it.")
            self._running = name
            self._proc = None
            # started: only what this attempt writes counts towards its progress.
            self.state[name] = {"state": "running", "message": "Starting the download…",
                                "log": [], "started": time.time()}
        threading.Thread(target=self._run, args=(name, token, on_success), daemon=True,
                         name="model-download").start()

    def cancel(self):
        """Stop a stalled or unwanted download. A later attempt starts again
        (anything half-downloaded is discarded or verified first)."""
        proc = getattr(self, "_proc", None)
        if self._running and proc is not None and proc.is_alive():
            self._cancelled = True
            proc.terminate()
            return True
        return False

    def verify(self, name):
        """Full SHA-256 check of an installed model (in the background)."""
        if not known(name):
            raise SetupError("Unknown model.")
        with self._lock:
            if self._running:
                raise SetupError("Wait for the current download or check to finish.")
            self._running = name
            self.state[name] = {"state": "running", "message": "Checking every file…",
                                "log": [], "verify": True}
        threading.Thread(target=self._verify, args=(name,), daemon=True,
                         name="model-verify").start()

    def _verify(self, name):
        entry, final = self.state[name], {"state": "failed",
                                          "message": "The check could not be completed."}
        try:
            if name == gpu.COMPONENT:
                ok = not gpu.full_check(self.cache_dir)      # remembered: #153
                spec = None
            else:
                spec = asr_engines.MANAGED_MODELS[name]
            if spec is None:
                pass
            elif asr_engines.is_parakeet(name):
                problems = []
                for s in (spec, asr_engines.SILERO_VAD):
                    folder = asr_engines.model_dir(s, self.cache_dir)
                    problems += (asr_engines.check_files(s, folder, full=True)
                                 if folder.exists() else ["not installed"])
                ok = not problems
            else:
                ok = asr_engines.find_verified(spec, self.cache_dir, full=True) is not None
            final = ({"state": "ready", "message": "Every file matches its checksum."} if ok
                     else {"state": "failed", "message": "Some files do not match their "
                           "checksums. Use Repair to download a fresh copy."})
        except Exception:
            pass
        finally:
            with self._lock:
                self._running = None
            entry.update(final)
            if self.after:
                self.after()

    def _run(self, name, token, on_success=None):
        entry = self.state[name]
        final = {"state": "failed", "message": "The download stopped unexpectedly. Try again."}
        proc = None
        try:
            hook = self.before_gpu_install if name == gpu.COMPONENT else self.before_install
            if hook:
                hook()
            ctx = mp.get_context("spawn")
            parent, child = ctx.Pipe()
            started = ctx.Process(target=_download_entry,
                                  args=(self.target, child, name, self.cache_dir, self.log_dir),
                                  daemon=True, name="model-download")
            self._cancelled = False
            started.start()
            proc = self._proc = started      # only a started process is joined below
            child.close()
            parent.send(token)
            del token
            while True:
                if not parent.poll(1.0):
                    if not proc.is_alive():
                        break
                    continue
                kind, payload = parent.recv()
                if kind == "log":
                    entry["log"] = (entry["log"] + [payload])[-8:]
                    entry["message"] = payload
                    continue
                if kind == "done":
                    final = {"state": "ready", "message": "Downloaded and verified.",
                             "result": payload}
                    if on_success:
                        try:
                            on_success()
                        except Exception as exc:     # e.g. could not remember the token
                            final["message"] += f" {exc}"
                else:
                    final = {"state": "failed", "message": payload}
                break
        except (EOFError, OSError):
            pass                              # reported as "stopped unexpectedly"
        finally:
            if proc is not None:
                proc.join(10)
                self._note_exit(proc)         # also when it crashed mid-download
            if getattr(self, "_cancelled", False):
                final = {"state": "failed", "message": "Download cancelled. Start it again "
                                                         "when you are ready."}
            # Free the slot first, so the finished state never refuses a retry.
            with self._lock:
                self._running = None
            entry.update(final)
            if self.after:
                self.after()

    def _note_exit(self, proc):
        """A download process that ended by itself with an error code (not
        cancelled) is recorded in the app's log, with its native crash."""
        from workspace import diag
        log = diag.current()
        if log is None or self.log_dir is None or getattr(self, "_cancelled", False):
            return
        died = proc.exitcode not in (0, None)
        code = log.collect_crash(died, crash_path=os.path.join(self.log_dir, "setup.crash"),
                                 process="setup")
        if died:
            log.write("worker_exit", process="setup", exitcode=proc.exitcode, code=code)

    def percent(self, name):
        """How far a running download is, from the size of what this attempt
        has written so far against what it fetches (R12: a number for the
        page, never read out of a message). While a downloaded file is checked
        or unpacked it shows how much has arrived. None for a check of an
        installed copy (Check every file), or when nothing has been written."""
        entry = self.state.get(name)
        if not entry or entry.get("state") != "running" or entry.get("verify"):
            return None
        since = entry.get("started")
        cache = Path(self.cache_dir)
        if name == gpu.COMPONENT:
            # One wheel at a time, each deleted once it is unpacked: the ones
            # before it count at their pinned size.
            wheels = gpu.staging_dir(cache) / "wheels"
            done = 0
            for w in gpu.WHEELS:
                current = wheels / (w["name"] + ".whl")
                if not _not_followed(current):
                    if since is not None and _file_size(current, since) == 0 \
                            and _file_size(current):
                        return None                  # left by a cancelled attempt
                    return min(100, round(100 * (done + _file_size(current, since))
                                          / gpu.DOWNLOAD_BYTES))
                done += w["size"]
            return None
        if name in asr_engines.MANAGED_MODELS:
            spec = asr_engines.MANAGED_MODELS[name]
            folder, total = asr_engines.staging_dir(spec, cache), spec.download_bytes
        else:
            return None
        if not total or not folder.is_dir():
            return None
        written = _size_of(folder, since)
        return min(100, round(100 * written / total)) if written else None

    def busy(self):
        return self._running is not None
