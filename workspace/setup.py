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

import asr_engines

HELP_LINKS = {   # fixed pages the user can open in their own browser (allowlist)
    "account": "https://huggingface.co/join",
    "terms": "https://huggingface.co/pyannote/speaker-diarization-community-1",
    "tokens": "https://huggingface.co/settings/tokens",
}

CARDS = [
    {"name": asr_engines.RECOMMENDED_PARAKEET, "role": "speech", "recommended": True,
     "title": "Speech to text: Parakeet v2 (recommended)",
     "about": "Fast and accurate for English. Runs on this computer's processor."},
    {"name": "parakeet-tdt-0.6b-v3", "role": "speech", "recommended": False,
     "title": "Speech to text: Parakeet v3",
     "about": "Newer multilingual version. In our tests it was less accurate for English."},
    {"name": "large-v3-turbo", "role": "speech", "recommended": False,
     "title": "Speech to text: Whisper large-v3 turbo",
     "about": "Alternative engine; slower on a processor."},
    {"name": asr_engines.SPEAKER_MODEL.name, "role": "speakers", "recommended": True,
     "title": "Speaker identification (optional)",
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


def cards(cache_dir):
    out = []
    for c in CARDS:
        state, plain, _ = status(c["name"], cache_dir)
        lic, source = asr_engines.LICENCES[c["name"]]
        spec = asr_engines.MANAGED_MODELS[c["name"]]
        out.append({**c, "state": state, "status": plain, "licence": lic, "source": source,
                    "size_gb": round(_size(c["name"]) / 1e9, 2), "revision": spec.revision[:7],
                    "needs_token": spec.gated})
    return out


# -- the download process ------------------------------------------------------------

def _friendly(exc):
    """Download errors in plain language. Never includes the token."""
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if "gated" in low or "403" in low or "awaiting a review" in low:
        return ("Your Hugging Face account has not accepted the speaker model's terms yet. "
                "Open the model page, accept the terms, then try again.")
    if "401" in low or "unauthorized" in low or ("invalid" in low and "token" in low):
        return "Hugging Face did not accept the token. Check it, or create a new read token."
    if any(k in low for k in ("connection", "timeout", "temporarily", "resolve", "offline")):
        return "Could not reach Hugging Face. Check the internet connection and try again."
    if isinstance(exc, asr_engines.ModelLoadError):
        return str(exc).split(" (")[0] + ". Try again."
    return "The download did not finish. Nothing was changed; try again."


def _download_main(conn, name, cache_dir):
    """Runs in the download process. Network is allowed here only."""
    import offline_guard
    offline_guard.disable_telemetry()
    os.environ["HF_HUB_OFFLINE"] = "0"          # this process exists to download
    token = conn.recv()                          # via the pipe, never argv/env
    from workspace.worker import safe_message

    def download(spec, dest):
        from huggingface_hub import snapshot_download
        snapshot_download(spec.repo, revision=spec.revision, local_dir=str(dest),
                          allow_patterns=[f.name for f in spec.files],
                          token=(token or False) if spec.gated else False)

    def log(message):
        conn.send(("log", safe_message(message)[:300]))
    try:
        if asr_engines.MANAGED_MODELS[name].gated and not token:
            raise SetupError("The speaker model needs your Hugging Face token to download.")
        result = asr_engines.install(name, cache_dir, log, download)
        conn.send(("done", result))
    except SetupError as exc:
        conn.send(("error", str(exc)))
    except Exception as exc:
        conn.send(("error", _friendly(exc.__cause__ or exc)))


class Downloads:
    """One download at a time, in its own process; progress for the UI."""

    def __init__(self, cache_dir, before_install=None, target=None):
        self.cache_dir = str(cache_dir)
        self.target = target or _download_main   # tests: a fake download process
        self.before_install = before_install     # e.g. unload the inference worker
        self.state = {}                          # name -> {"state", "message", "log"}
        self._lock = threading.Lock()
        self._running = None

    def start(self, name, token=None):
        if name not in asr_engines.MANAGED_MODELS:
            raise SetupError("Unknown model.")
        with self._lock:
            if self._running:
                raise SetupError("Another download is in progress. Wait for it to finish.")
            self._running = name
            self.state[name] = {"state": "running", "message": "Starting the download…",
                                "log": []}
        threading.Thread(target=self._run, args=(name, token), daemon=True,
                         name="model-download").start()

    def _run(self, name, token):
        entry = self.state[name]
        final = {"state": "failed", "message": "The download stopped unexpectedly. Try again."}
        try:
            if self.before_install:
                self.before_install()
            ctx = mp.get_context("spawn")
            parent, child = ctx.Pipe()
            proc = ctx.Process(target=self.target, args=(child, name, self.cache_dir),
                               daemon=True, name="model-download")
            proc.start()
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
                else:
                    final = {"state": "failed", "message": payload}
                break
            proc.join(10)
        except (EOFError, OSError):
            pass                              # reported as "stopped unexpectedly"
        finally:
            # Free the slot first, so the finished state never refuses a retry.
            with self._lock:
                self._running = None
            entry.update(final)

    def busy(self):
        return self._running is not None
