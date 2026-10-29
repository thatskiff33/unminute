# workspace/settings_ws.py
# The app's settings: the settings file (Settings, R11) with its defaults and
# checks, the speech-model note Settings shows (#244), and what Settings and
# Setup show from disk (R7b), moved unchanged out of workspace/app.py (#414).
# Workspace inherits SettingsMixin, so the bridge (workspace/api.py) calls the
# same methods on the Workspace as before. The mixin keeps no state of its
# own: every attribute and lock it uses is the Workspace's, set in
# Workspace.__init__. What stays in app.py: get_settings and set_settings,
# which read COMPUTER_AUDIO_STARTS (tests patch
# workspace.app.COMPUTER_AUDIO_STARTS) and whose graphics card fields a test
# reads from app.py's source (tests/test_single_source.py).

import json
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from workspace import asr_engines, drafts, engines, resources, setup
from workspace.app_common import WorkspaceError
from workspace.diag import SETTINGS_UNREADABLE_FILE

# Settings and Setup are polled while open; what they show from disk (model
# and component files) or Credential Manager is worked out at most this often,
# and again at once after a download, removal, token or settings change (R7b).
DISPLAY_CACHE_S = 60.0

SETTINGS_VERSION = 1                # settings.json format (R11): raise if a key's meaning changes
SETTINGS_MAX_BYTES = 64 * 1024      # a larger settings.json is not one the app wrote

# The speech models Settings offers, in the order it lists them.
SPEECH_MODELS = (*asr_engines.PARAKEET_MODELS, *asr_engines.WHISPER_MODELS)

DEFAULT_SETTINGS = {
    "mode": resources.QUIET,                       # quiet | faster
    "model": asr_engines.RECOMMENDED_PARAKEET,
    "speakers": True,                              # separate speakers after transcription
    "battery_override": False,                     # process automatically on battery
    "onboarded": False,                            # guided setup finished or skipped (#19)
    "microphone": False,                           # microphone recording (#30), off by default
    "computer_audio": False,                       # computer audio as a second track (#108)
    "gpu_speakers": False,                         # NVIDIA for speaker identification (Faster)
    "recording_display": "flow",                   # flow | spectrum | wave, while recording
    "lid_override": False,                         # lid "Do nothing" while recording (#172)
    "live_drafts": False,                          # draft text while recording (#104), off
    # What the draft follows with computer audio on (#104, the owner's
    # decision of 22 Oct 2026): microphone | mix (both tracks, mixed live).
    "live_drafts_audio": "microphone",
}
RECORDING_DISPLAYS = ("flow", "spectrum", "wave")


def _model_note(name, state, downloads_allowed=True, only_it_models=False):
    """What Settings says beside the picker about a speech model that is not
    ready, and why Transcribe again refuses it (#244); None when it is ready.
    Manage downloads, which offers every speech model (#246), is named only
    when managed policy allows downloads (K3). only_it_models: downloads are
    allowed, but this machine install uses only IT's models (AllowUserModels
    at 0, #111 B7a), so the note says that rather than that downloads are off
    (#451)."""
    if state == "ready":
        return None
    label = asr_engines.label(name)
    what = "damaged" if state == "invalid" else "not downloaded"
    if only_it_models and not downloads_allowed:
        what = "damaged" if state == "invalid" else "not installed"
        return (f"{label} is {what}, and your organisation uses only the models your IT team "
                "installs on this computer. Choose another speech model, or ask your IT "
                "team to install or repair it.")
    if not downloads_allowed:
        return (f"{label} is {what}, and downloads are turned off by your organisation. "
                "Choose another speech model, or ask your IT team to install it.")
    if state == "invalid":
        return (f"{label} is damaged. Repair it in Settings > Manage downloads, or choose "
                "another speech model.")
    return (f"{label} is not downloaded. Download it in Settings > Manage downloads, or "
            "choose another speech model.")


class Settings:
    """Configuration only (no content), kept beside the database.

    Values read from settings.json pass the same checks as values set from
    the page: the file is writable by anything running as the user, so an
    invalid value falls back to its default instead of being trusted. Test
    engines ("test:module:function") are accepted only when the tests ask for
    them (allow_test_engines); they would otherwise let a settings value
    import any module in the worker."""

    def __init__(self, path, allow_test_engines=False):
        self.path = Path(path)
        self.allow_test_engines = allow_test_engines
        self.values = dict(DEFAULT_SETTINGS)
        self.unreadable = False            # the file exists but could not be read: all defaults
        self._locked = False               # ...because it could not be opened (in use)
        self._save_lock = threading.Lock()
        self._load(self._read())

    def _load(self, saved):
        """Apply saved settings over the defaults."""
        self.values = dict(DEFAULT_SETTINGS)
        self.repaired = []                 # keys on their default: the saved value was refused
        self.version = SETTINGS_VERSION
        # Saved values this version does not use, written back unchanged (R11):
        # settings only a newer version knows, and a newer version's value for
        # a setting this one knows but cannot use. A rollback then loses none.
        self._kept = {}
        version = saved.pop("settings_version", 0)
        newer = isinstance(version, int) and not isinstance(version, bool) \
            and SETTINGS_VERSION < version < 10 ** 6
        if newer:
            self.version = version
        for key, value in saved.items():
            if key not in DEFAULT_SETTINGS:
                self._kept[key] = value
                continue
            try:
                self._check(key, value)
                self.values[key] = value
            except WorkspaceError:
                self.repaired.append(key)
                if newer:
                    self._kept[key] = value

    def _read(self):
        """The saved settings as a dict ({} if there is no file yet). Sets
        unreadable (and _locked, if the file could not even be opened)."""
        self.unreadable = self._locked = False
        data = None
        for attempt in range(2):                # once more: an antivirus scan may hold it
            try:
                with open(self.path, "rb") as f:
                    data = f.read(SETTINGS_MAX_BYTES + 1)
                break
            except FileNotFoundError:
                return {}
            except OSError:
                time.sleep(0.2 * (1 - attempt))
        if data is None:                        # in use: unknown, not damaged
            self.unreadable = self._locked = True
            return {}
        try:
            saved = json.loads(data.decode("utf-8-sig")) \
                if len(data) <= SETTINGS_MAX_BYTES else None
            json.dumps(saved, indent=2)         # too deeply nested to write back: unreadable
        except (ValueError, RecursionError):
            saved = None
        if not isinstance(saved, dict):
            self.unreadable = True
            return {}
        return saved

    def __getitem__(self, key):
        return self.values[key]

    def _check(self, key, value):
        if key not in DEFAULT_SETTINGS:
            raise WorkspaceError("Unknown setting.")
        if key == "mode" and value not in (resources.QUIET, resources.FASTER):
            raise WorkspaceError("Choose Quiet or Faster.")
        if key == "recording_display" and value not in RECORDING_DISPLAYS:
            raise WorkspaceError("Choose Flowing wave, Spectrum or Sound wave.")
        if key == "live_drafts_audio" and value not in drafts.AUDIO_CHOICES:
            raise WorkspaceError("Choose the microphone, or the microphone and computer audio.")
        if key == "model" and not (isinstance(value, str) and (
                asr_engines.is_parakeet(value) or asr_engines.is_whisper(value)
                or (self.allow_test_engines and value.startswith(engines.TEST_PREFIX)))):
            raise WorkspaceError("Unknown speech model.")
        if (key in ("speakers", "battery_override", "onboarded", "microphone",
                    "computer_audio", "gpu_speakers", "lid_override", "live_drafts")
                and not isinstance(value, bool)):
            raise WorkspaceError("Expected on or off.")

    def update(self, **changes):
        for key, value in changes.items():
            self._check(key, value)
        with self._save_lock:                      # page calls run on their own threads
            if self._locked:
                # It could not be opened at start: read it now, rather than
                # replace settings that may be good with the defaults.
                self._load(self._read())
                if self._locked:
                    raise WorkspaceError("Unminute cannot open its settings file just now. "
                                         "Try again in a moment.")
            values = {**self.values, **changes}
            kept = {k: v for k, v in self._kept.items() if k not in changes}
            # settings_version is the newest version that wrote the file. A key's
            # meaning never changes between versions: a new meaning takes a new key.
            saved = {"settings_version": self.version, **values, **kept}
            text = json.dumps(saved, indent=2)
            if len(text.encode("utf-8")) > SETTINGS_MAX_BYTES:
                text = json.dumps(saved, separators=(",", ":"))
            if len(text.encode("utf-8")) > SETTINGS_MAX_BYTES:
                kept = {}                          # not a file the app wrote: drop the extras
                text = json.dumps({"settings_version": self.version, **values})
            if self.unreadable and self.path.exists():
                # Copied once, aside, before it is replaced: it may hold values
                # a newer version wrote, or the read failed only for a moment.
                # A copy, so a write that fails next still leaves it in place.
                import shutil
                try:
                    shutil.copyfile(self.path, self.path.with_name(SETTINGS_UNREADABLE_FILE))
                except OSError:
                    pass
            # Written in full or not at all: flushed to disk before the swap,
            # so a power cut leaves the old file or the new one, never a part.
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(3):
                try:
                    tmp.replace(self.path)
                    break
                except PermissionError:            # held open for a moment (an antivirus scan)
                    if attempt == 2:
                        raise
                    time.sleep(0.05)
            # Only once the file is saved does the app use the new values.
            self.values, self._kept = values, kept
            self.repaired = [k for k in self.repaired if k not in changes]
            self.unreadable = False


class SettingsMixin:
    """Settings and Setup data, for Workspace (workspace/app.py) only."""

    if TYPE_CHECKING:
        # For the type check only: what the methods below use of the
        # Workspace, typed as Workspace.__init__ sets it. Nothing here exists
        # when the app runs.
        _clock: Any
        _display: dict[Any, Any]
        _display_gen: int
        _display_lock: threading.Lock
        _wake: threading.Event
        cache_dir: Path

    # -- Settings and Setup data (R7b) -------------------------------------------------

    def _after_download(self):
        """A download or check ended: what Setup shows is read again, and
        processing that waited carries on."""
        self._forget_display()
        self._wake.set()

    def _display_value(self, key, compute, version=None):
        """A value Settings or Setup shows, worked out again only after
        DISPLAY_CACHE_S, when `version` changes, or after _forget_display()."""
        now = self._clock()
        with self._display_lock:
            hit, gen = self._display.get(key), self._display_gen
        if hit and hit[0] > now and hit[1] == version:
            return hit[2]
        value = compute()
        with self._display_lock:
            if self._display_gen == gen:     # not forgotten meanwhile: still current
                self._display[key] = (now + DISPLAY_CACHE_S, version, value)
        return value

    def _forget_display(self):
        with self._display_lock:
            self._display.clear()
            self._display_gen += 1

    def _speech_states(self):
        """Each speech model's state ("ready", "missing" or "invalid"), worked
        out as the Downloads page does (setup.status) and kept as long as its
        cards (DISPLAY_CACHE_S, or until a download, removal or check ends).
        Every speech model has a card there (#246; a test keeps it so)."""
        cards = self._display_value("cards", lambda: setup.cards(self.cache_dir))
        return {c["name"]: c["state"] for c in cards if c["role"] == "speech"}
