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

import json
import multiprocessing as mp
import os
import threading
import time
import urllib.request
from pathlib import Path

from workspace import asr_engines
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
    # #246 (owner's decision, 20 Oct 2026): offered so that a meeting set to it
    # can run in a new install. Turbo stays the suggested Whisper model.
    (asr_engines.WHISPER_LARGE,
     "OpenAI's most accurate Whisper model. In our tests on Intel Arc graphics it was no "
     "more accurate than turbo. It is the largest model, so expect it to be the slowest "
     "on a processor."),
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


def _old_driver(info):
    return (f" The NVIDIA driver ({info['driver']}) is too old: version "
            f"{gpu.MIN_DRIVER_TEXT} or later is needed. Ask IT to update it.")


def gpu_card(cache_dir):
    """The optional graphics card component for speaker identification,
    offered only with an NVIDIA card."""
    info = gpu.detected()
    if info is None:
        return None
    state, plain = gpu.status(cache_dir)
    about = (f"Speeds up speaker identification on this computer's {info['name']}. "
             "Used only in Faster mode, once turned on in Settings.")
    if not info["driver_ok"]:
        about += _old_driver(info)
    return {"name": gpu.COMPONENT, "role": "gpu", "recommended": False,
            "label": "Graphics card acceleration",
            "title": "Graphics card acceleration (optional)", "about": about,
            "state": state, "status": plain, "licence": gpu.LICENCE[0],
            "source": gpu.LICENCE[1], "size_gb": round(gpu.DOWNLOAD_BYTES / 1e9, 2),
            "revision": gpu.VERSION, "needs_token": False}


def whisper_gpu_card(cache_dir):
    """The optional graphics card component for Whisper (#248), offered only
    with an NVIDIA card."""
    info = gpu.detected()
    if info is None:
        return None
    state, plain = gpu.WHISPER.status(cache_dir)
    models = " and ".join(asr_engines.label(n) for n in gpu.WHISPER_PRECISION)
    about = (f"Runs {models} on this computer's {info['name']}, several times faster "
             "than on the processor. Used only in Faster mode, once the graphics card is "
             "turned on in Settings. Parakeet stays on the processor.")
    if not info["driver_ok"]:
        about += _old_driver(info)
    return {"name": gpu.WHISPER_COMPONENT, "role": "gpu", "recommended": False,
            "label": "Graphics card for Whisper",
            "title": "Graphics card for Whisper (optional)", "about": about,
            "state": state, "status": plain, "licence": gpu.WHISPER_LICENCE[0],
            "source": gpu.WHISPER_LICENCE[1],
            "size_gb": round(gpu.WHISPER_DOWNLOAD_BYTES / 1e9, 2),
            "revision": gpu.WHISPER_VERSION, "needs_token": False}


def cards(cache_dir):
    out = []
    for c in CARDS:
        state, plain, _ = status(c["name"], cache_dir)
        lic, source = asr_engines.LICENCES[c["name"]]
        spec = asr_engines.MANAGED_MODELS[c["name"]]
        out.append({**c, "state": state, "status": plain, "licence": lic, "source": source,
                    "size_gb": round(_size(c["name"]) / 1e9, 2), "revision": spec.revision[:7],
                    "needs_token": spec.gated})
    for card in (gpu_card(cache_dir), whisper_gpu_card(cache_dir)):
        if card is not None:
            out.append(card)
    for c in out:
        # Remove is offered whenever something is on disk, including leftover
        # damaged or unfinished copies of a model that is not installed.
        c["removable"] = bool(removal_targets(c["name"], cache_dir))
    return out


# -- the download process ------------------------------------------------------------

BLOCKED_MESSAGE = ("The download site sent the download to an address this app does not "
                   "allow, so it was stopped. Nothing was installed.")


def _blocked(exc):
    """True if this error, or one it was raised from, is the network guard."""
    from workspace import offline_guard
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
        # Cut at the technical part in brackets; a message without one is
        # already complete and says what to do (a refused link: ask IT).
        message = str(exc)
        return message.split(" (")[0] + ". Try again." if " (" in message else message
    return "The download did not finish. Nothing was changed; try again."


# The only hosts the download process may reach (and their subdomains), plus
# any proxy configured for this user. Hugging Face serves files from
# huggingface.co and its CDN hosts under huggingface.co and hf.co; the
# optional GPU component comes from the pinned PyTorch host. Every HTTP
# request and redirect is checked against this list (_check_request), which
# also holds behind a proxy; the network guard checks DNS lookups as well.
DOWNLOAD_HOSTS = ("huggingface.co", "hf.co", "download-r2.pytorch.org")
# Hosts added for one download only. NVIDIA publishes the Whisper GPU
# component's cuBLAS on PyPI, whose files are served from
# files.pythonhosted.org: the owner allowed that host for this component only
# (#248, 20 October 2026). No other download's process can reach it.
COMPONENT_HOSTS = {gpu.WHISPER_COMPONENT: ("files.pythonhosted.org",)}


def download_hosts(name):
    """The hosts the download process for this model or component may reach."""
    return DOWNLOAD_HOSTS + COMPONENT_HOSTS.get(name, ())


def _check_request(request):
    """httpx request hook, run for every request a download makes and for each
    redirect it follows. Behind a proxy this computer never looks up the
    target name (the proxy does), so the guard's DNS check alone would not see
    a redirect to another host: this check does."""
    from workspace import offline_guard
    host = request.url.raw_host       # the exact name sent (httpx decodes xn-- in .host)
    # HTTPS on its standard port only (#111 D2): httpx gives None for 443.
    if (request.url.scheme != "https" or request.url.port not in (None, 443)
            or not offline_guard.host_allowed(host)):
        raise offline_guard.NetworkBlockedError(
            13, f"Download address not allowed: {request.url.host}")


def prepare_download_process(hosts=DOWNLOAD_HOSTS):
    """Set up the download process. Must run before huggingface_hub is
    imported anywhere in it:
      - telemetry off, and the native Xet downloader off (it would bypass the
        guard and Python's TLS and proxy handling);
      - the network guard with the hosts allowlist (download_hosts(name));
      - certificates checked against the Windows certificate store, so
        downloads work behind a firm's TLS-inspecting proxy;
      - every HTTP request and redirect checked against the allowlist."""
    import sys
    if "huggingface_hub" in sys.modules:
        raise RuntimeError("huggingface_hub was imported before the download process was "
                           "set up")
    from workspace import offline_guard
    offline_guard.disable_telemetry()
    os.environ["HF_HUB_OFFLINE"] = "0"          # this process exists to download
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    offline_guard.install()
    offline_guard.allow_hosts(hosts)
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
    to download_hosts(name)."""
    import sys
    if sys.stderr is None or sys.stdout is None:   # pythonw, and no log: no console
        sys.stdout = sys.stderr = open(os.devnull, "w")
    try:
        prepare_download_process(download_hosts(name))
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
        comp = gpu.component(name)
        if comp is not None:
            conn.send(("done", comp.install(cache_dir, log, _fetcher(comp.host))))
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
    comp = gpu.component(name)
    if comp is not None:
        base = comp.component_dir(cache)
        return [p for p in base.parent.glob(comp.version + "*") if p.is_dir()]
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
    # The check records' folder and the family folder above it too: matching
    # records are deleted through them, also for a Hugging Face cache copy,
    # whose own parents are not inside the family folder.
    marks = (None if gpu.component(name) is not None
             else Path(cache_dir) / asr_engines.MANAGED_MODELS[name].family / ".verified")
    if marks is not None and (asr_engines.is_link(marks) or asr_engines.is_link(marks.parent)):
        raise RemovalRefused("A model folder is a link to somewhere else, so nothing was "
                             "removed.")
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
    if marks is not None:
        spec = asr_engines.MANAGED_MODELS[name]
        # Exactly "<folder>-<16 hex>.json": large-v3 must not match large-v3-turbo.
        for m in marks.glob(spec.folder + "-" + "?" * 16 + ".json") if marks.is_dir() else ():
            m.unlink(missing_ok=True)
    return freed


class _SameHost(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the pinned HTTPS location."""

    def __init__(self, prefix=gpu.DOWNLOAD_HOST):
        super().__init__()
        self.prefix = prefix

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _within(newurl, self.prefix):
            raise SetupError("The download site redirected elsewhere. Nothing was installed.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _origin(parts):
    """(scheme, host, port) of a split address, the port filled in."""
    port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
    return parts.scheme, parts.hostname, port


def _within(url, prefix):
    """The address lies inside the pinned location prefix: the same scheme,
    host and port, no user name or password, and a path under the prefix's
    path once percent-escapes are decoded, with no . or .. segment and no
    backslash. A plain string comparison would let /whl/cu130/../../x
    through (#111 D2)."""
    from urllib.parse import unquote, urlsplit
    try:
        u, p = urlsplit(url), urlsplit(prefix)
        if _origin(u) != _origin(p) or u.username is not None or u.password is not None:
            return False
    except ValueError:                           # a malformed port
        return False
    path = unquote(u.path)
    if "\\" in path or any(s in (".", "..") for s in path.split("/")):
        return False
    return path.startswith(unquote(p.path))


def _fetcher(prefix):
    """fetch(url, dest, expected_size) for a component pinned to one HTTPS
    location: the address, and every redirect, must start with prefix."""
    return lambda url, dest, expected_size: _fetch(url, dest, expected_size, prefix)


def _fetch(url, dest, expected_size, prefix=gpu.DOWNLOAD_HOST):
    """Stream one file to disk (download process only); the caller checks the
    SHA-256. Only the component's pinned HTTPS download location is accepted."""
    import urllib.request
    if not prefix.startswith("https://") or not url.startswith(prefix) or not _within(url, prefix):
        raise SetupError("Unexpected download address.")
    opener = urllib.request.build_opener(_SameHost(prefix))
    req = urllib.request.Request(url, headers={"User-Agent": "Unminute"})
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
    return name in asr_engines.MANAGED_MODELS or gpu.component(name) is not None


# The download queue (#366, the owner's decisions of 25 October 2026): kept in
# the workspace beside settings.json, with model identifiers only (P3), so
# what the user queued carries on at the next start. Never a token, a path or
# anything else; read only up to QUEUE_MAX_BYTES, and only names known().
QUEUE_FILE = "download_queue.json"
QUEUE_MAX_BYTES = 4096
QUEUED = "Queued. It starts when the download before it finishes."
CANCELLED = "Download cancelled. Start it again when you are ready."


class QueueStopped(SetupError):
    """Nothing queued may start (downloads turned off by policy): every
    queued entry ends with this message."""


class Downloads:
    """One download at a time, in its own process; progress for the UI.
    Further downloads wait in a queue, in click order (#366), and start one
    after another, whether the one before finishes, fails or is cancelled."""

    def __init__(self, cache_dir, before_install=None, target=None, before_gpu_install=None,
                 queue_path=None):
        self.cache_dir = str(cache_dir)
        self.target = target or _download_main   # tests: a fake download process
        self.after = None                        # called when a download or check ends
        self.before_install = before_install     # e.g. unload the inference worker
        # A graphics card component is used only by its own GPU worker:
        # replacing it stops that worker (before_gpu_install(name)) and leaves
        # other work running.
        self.before_gpu_install = before_gpu_install
        self.state = {}                          # name -> {"state", "message", "log"}
        self.log_dir = None                      # the app's logs folder (R9), set by the app
        self._lock = threading.Lock()
        self._running = None                     # the download or check running now
        self._download = None                    # ...when it is a download (kept in the file)
        # Waiting downloads, in click order: [name, token, on_success, resumed].
        # A token given with the click stays in this process's memory only, as
        # it does for a running download; it is never written to the file.
        self._queue = []
        self.queue_path = Path(queue_path) if queue_path else None
        # Called as prepare(name, token) just before a queued download starts:
        # returns the token to send, or raises SetupError (that entry fails)
        # or QueueStopped (every queued entry fails). The app re-reads policy there.
        self.prepare = None
        self._closing = False
        # Cancel on an entry already taken from the queue whose download
        # process has not started yet (prepare, workers shutting down): it
        # stops before the process starts, or as soon as it has.
        self._cancel_claimed = None

    def start(self, name, token=None, on_success=None):
        """Start a download now, or queue it behind the download or check
        running (#366). Returns "started" or "queued". on_success runs only
        after a verified download (e.g. remember the token only once it has
        been shown to work)."""
        if not known(name):
            raise SetupError("Unknown model.")
        with self._lock:
            if self._closing:
                raise SetupError("Unminute is closing.")
            if name == self._running and self._download != name:
                raise SetupError("This model is being checked. Repair it once the check "
                                 "has finished.")
            if name == self._running or any(q[0] == name for q in self._queue):
                raise SetupError("This one is already downloading or queued.")
            # Behind the one running, and behind any still waiting: between one
            # download ending and the next being taken from the queue the slot
            # is free for a moment, and a click then must not jump the queue.
            if self._running or self._queue:
                self._queue.append([name, token, on_success, False])
                self.state[name] = {"state": "queued", "message": QUEUED, "log": []}
                self._save()
                return "queued"
            self._claim(name)
            self._save()
        self._launch(name, token, on_success)
        return "started"

    def _claim(self, name, resumed=False):
        """Under the lock: name takes the one download slot."""
        self._running = self._download = name
        self._proc = None
        self._cancel_claimed = None
        # started: only what this attempt writes counts towards its progress.
        self.state[name] = {"state": "running", "message": "Starting the download…",
                            "log": [], "started": time.time()}
        if resumed:
            self.state[name]["resumed"] = True

    def _launch(self, name, token, on_success):
        threading.Thread(target=self._run, args=(name, token, on_success), daemon=True,
                         name="model-download").start()

    def _next(self):
        """The slot is free: start the first queued download, if any. The
        slot is claimed before the entry leaves the queue, so busy() never
        reads False in between and no model work starts while one waits."""
        while True:
            with self._lock:
                if self._closing or self._running or not self._queue:
                    return
                name, token, on_success, resumed = self._queue[0]
                self._claim(name, resumed)
                del self._queue[0]
            try:
                if self.prepare:
                    token = self.prepare(name, token)
            except QueueStopped as exc:
                self._stop_all(str(exc), also=name)
                return
            except Exception as exc:
                # This one may not start (SetupError says why): it fails alone,
                # and the rest carry on.
                message = (str(exc) if isinstance(exc, SetupError)
                           else "The download stopped unexpectedly. Try again.")
                with self._lock:
                    self._running = self._download = None
                    self._save()
                self.state[name] = {"state": "failed", "message": message, "log": []}
                if self.after:
                    self.after()
                continue
            self._launch(name, token, on_success)
            return

    def _stop_all(self, message, also=None, only=None):
        """End waiting downloads with message: all of them, or those only()
        accepts. also: the entry that was about to start (its slot is freed)."""
        with self._lock:
            ended = [q[0] for q in self._queue if only is None or only(q[0])]
            self._queue = [q for q in self._queue if q[0] not in ended]
            if also is not None:
                self._running = self._download = None
                ended.insert(0, also)
            if ended:
                self._save()
        for name in ended:
            self.state[name] = {"state": "failed", "message": message, "log": []}
        if ended and self.after:
            self.after()
        return ended

    def drop_queued(self, message, only=None):
        """Policy turned downloads off (or, with only, a component): the
        waiting entries end with message. A running download is not stopped."""
        return self._stop_all(message, only=only)

    def queued(self):
        with self._lock:
            return [q[0] for q in self._queue]

    def involves(self, name):
        """name is being downloaded or checked now, or is waiting to be."""
        with self._lock:
            return name == self._running or any(q[0] == name for q in self._queue)

    def cancel(self, name=None):
        """Stop a stalled or unwanted download, or (name) take a waiting one
        out of the queue. A later attempt starts again (anything
        half-downloaded is discarded or verified first)."""
        with self._lock:
            if name is not None and name != self._running:
                for i, q in enumerate(self._queue):
                    if q[0] == name:
                        del self._queue[i]
                        self.state.pop(name, None)       # back to Download
                        self._save()
                        return True
                return False
            if (self._running is not None and self._running == self._download
                    and getattr(self, "_proc", None) is None):
                # Taken from the queue, its process not started yet: it stops
                # before it starts (_run checks this under the lock).
                self._cancel_claimed = self._running
                return True
        proc = getattr(self, "_proc", None)
        if self._running and proc is not None and proc.is_alive():
            self._cancelled = True
            proc.terminate()
            return True
        return False

    def resume(self):
        """At the app's start: queue again what the user queued before the app
        last closed (the one cut off mid-download first), and start it. Only
        names this version knows, each once, in their order; a damaged or
        oversized file queues nothing and is removed. Returns the names."""
        names = self._load()
        with self._lock:
            if self._closing or self._running or self._queue:
                return []
            for name in names:
                self._queue.append([name, None, None, True])
                self.state[name] = {"state": "queued", "message": QUEUED, "log": [],
                                    "resumed": True}
            self._save()
        self._next()
        return names

    def close(self):
        """The app is closing: the file keeps the queue as it is now, for the
        next start; the running download process is stopped (as Cancel does),
        and nothing more starts. It waits briefly for that process to exit, so
        no download process still holds the cache or its log once the app has
        closed (a quick reopen resumes the queue in the same folders)."""
        with self._lock:
            self._closing = True
        proc = getattr(self, "_proc", None)
        if proc is not None and proc.is_alive():
            self._cancelled = True
            proc.terminate()
            proc.join(5)

    def _load(self):
        if self.queue_path is None:
            return []
        try:
            with open(self.queue_path, "rb") as f:
                data = f.read(QUEUE_MAX_BYTES + 1)
        except OSError:                          # none, in use or refused: nothing carries on
            return []
        try:
            saved = json.loads(data.decode("utf-8")) if len(data) <= QUEUE_MAX_BYTES else None
        except (ValueError, RecursionError):
            saved = None
        names = saved.get("queue") if isinstance(saved, dict) else None
        out = []
        for name in names if isinstance(names, list) else []:
            if isinstance(name, str) and known(name) and name not in out:
                out.append(name)
        return out

    def _save(self):
        """Under the lock: write the queue (the running download first), or
        remove the file when nothing waits. Written in full or not at all.
        Best effort: a queue that cannot be saved still runs in this session.
        Never once closing, so a download cut off by the close carries on."""
        if self.queue_path is None or self._closing:
            return
        names = ([self._download] if self._download else []) + [q[0] for q in self._queue]
        tmp = self.queue_path.with_suffix(".tmp")
        try:
            if not names:
                self.queue_path.unlink(missing_ok=True)
                return
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps({"queue": names}))
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.queue_path)
        except OSError:
            try:                                 # a failed write leaves no temporary file
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def verify(self, name):
        """Full SHA-256 check of an installed model (in the background)."""
        if not known(name):
            raise SetupError("Unknown model.")
        with self._lock:
            if self._running or self._queue:
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
            comp = gpu.component(name)
            if comp is not None:
                ok = not comp.full_check(self.cache_dir)     # remembered: #153
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
            self._next()

    def _run(self, name, token, on_success=None):
        entry = self.state[name]
        final = {"state": "failed", "message": "The download stopped unexpectedly. Try again."}
        proc = None
        try:
            if gpu.component(name) is not None:
                if self.before_gpu_install:
                    self.before_gpu_install(name)
            elif self.before_install:
                self.before_install()
            if self._closing:
                return                           # the app closed meanwhile: next start
            if self._cancel_claimed == name:     # cancelled before its process started
                self._cancelled = True
                return
            ctx = mp.get_context("spawn")
            parent, child = ctx.Pipe()
            started = ctx.Process(target=_download_entry,
                                  args=(self.target, child, name, self.cache_dir, self.log_dir),
                                  daemon=True, name="model-download")
            self._cancelled = False
            started.start()
            with self._lock:                 # cancel() and close() read _proc under it
                proc = self._proc = started  # only a started process is joined below
                stop = self._closing or self._cancel_claimed == name
            if stop:                         # closed or cancelled while it started
                self._cancelled = True
                proc.terminate()
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
                final = {"state": "failed", "message": CANCELLED}
            # Free the slot first, so the finished state never refuses a retry.
            # It leaves the saved queue whether it finished, failed or was
            # cancelled; not when the app is closing (it carries on next time).
            with self._lock:
                self._running = self._download = None
                self._save()
            entry.update(final)
            if self.after:
                self.after()
            self._next()

    def _note_exit(self, proc):
        """A download process that ended by itself with an error code (not
        cancelled) is recorded in the app's log, with its native crash."""
        from workspace import diag
        log = diag.current()
        if log is None or self.log_dir is None or getattr(self, "_cancelled", False):
            return
        died = proc.exitcode not in (0, None)
        code = log.collect_crash(died, crash_path=os.path.join(self.log_dir,
                                                             f"setup{diag.CRASH_SUFFIX}"),
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
        comp = gpu.component(name)
        if comp is not None:
            # One wheel at a time, each deleted once it is unpacked: the ones
            # before it count at their pinned size.
            wheels = comp.staging_dir(cache) / "wheels"
            done = 0
            for w in comp.wheels:
                current = wheels / (w["name"] + ".whl")
                if not _not_followed(current):
                    if since is not None and _file_size(current, since) == 0 \
                            and _file_size(current):
                        return None                  # left by a cancelled attempt
                    return min(100, round(100 * (done + _file_size(current, since))
                                          / comp.download_bytes))
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
        """A download or check is running, or downloads are waiting (#366):
        model work, removal and checks wait for the whole queue."""
        with self._lock:
            return self._running is not None or bool(self._queue)
