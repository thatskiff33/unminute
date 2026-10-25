# Queued model downloads (#366, the owner's decisions of 25 October 2026):
# Download on another model while one runs queues it, in click order; one
# download process at a time; the queue is kept (model identifiers only) and
# carries on at the next start; policy that turns downloads off empties it.
# No network, no real downloads: a fake download process that the test
# releases stands in for Hugging Face.
#   python tools/run_tests.py test_download_queue

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))

if HAVE_DEPS:
    import fake_worker_engines
    from workspace import asr_engines, credentials, gpu, setup
    from workspace.app import Workspace, WorkspaceError

SECRET = "hf_TESTtokenNOTreal0123456789"


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class DownloadQueueTests(unittest.TestCase):
    def setUp(self):
        # Never the real download process: a Workspace that starts resumes the
        # saved queue before a test could swap its target, so the default is
        # replaced too.
        guard = mock.patch.object(setup, "_download_main", fake_worker_engines.fake_download_held)
        guard.start()
        self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.models = self.root / "models"
        self.policy = {}                     # unmanaged: every download allowed
        self.vault = {}
        self.saved = (credentials.save_token, credentials.load_token, credentials.has_token)
        credentials.save_token = lambda t, target=None: self.vault.__setitem__("t", t)
        credentials.load_token = lambda target=None: self.vault.get("t")
        credentials.has_token = lambda target=None: "t" in self.vault
        self.workspaces = []
        self.ws = self.open()
        self.a, self.b, self.c = (asr_engines.PARAKEET_V3, asr_engines.WHISPER_TURBO,
                                  asr_engines.WHISPER_LARGE)

    def tearDown(self):
        (credentials.save_token, credentials.load_token, credentials.has_token) = self.saved
        for ws in self.workspaces:
            ws.close()
        self.tmp.cleanup()

    def open(self, start=False):
        ws = Workspace(self.root / "ws", self.models, start=False, allow_test_engines=True,
                       battery=False, policy_reader=lambda: (self.policy, ()),
                       protector=keyprotect.TestProtector())
        self.assertIs(ws.downloads.target, fake_worker_engines.fake_download_held)
        self.workspaces.append(ws)
        if start:
            ws.start()                         # resumes the saved queue (#366)
        return ws

    # -- helpers ----------------------------------------------------------------------

    def release(self, name, how="ok"):
        self.models.mkdir(exist_ok=True)
        (self.models / ("release-" + name.replace("/", "_"))).write_text(how, encoding="utf-8")

    def state(self, name, ws=None):
        return ((ws or self.ws).downloads.state.get(name) or {}).get("state")

    def wait(self, name, *states, ws=None, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.state(name, ws) in states:
                return (ws or self.ws).downloads.state[name]
            time.sleep(0.05)
        raise AssertionError(f"{name} never reached {states}: {self.state(name, ws)}")

    def wait_started(self, name, timeout=60):
        """The fake process for name has started (it writes order.txt)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if name in self.order():
                return
            time.sleep(0.05)
        raise AssertionError(f"{name} never started")

    def order(self):
        f = self.models / "order.txt"
        return f.read_text(encoding="utf-8").split() if f.exists() else []

    def queue_file(self, ws=None):
        return (ws or self.ws).root / setup.QUEUE_FILE

    def saved_queue(self, ws=None):
        f = self.queue_file(ws)
        return json.loads(f.read_text(encoding="utf-8"))["queue"] if f.exists() else []

    def three(self):
        """a runs; b and c wait behind it, in click order."""
        self.assertEqual(self.ws.download_model(self.a), "started")
        self.wait_started(self.a)
        self.assertEqual(self.ws.download_model(self.b), "queued")
        self.assertEqual(self.ws.download_model(self.c), "queued")

    # -- order, cancel, failure (criteria 1 and 2) -------------------------------------

    def test_downloads_queue_in_click_order_and_run_one_at_a_time(self):
        self.three()
        self.assertEqual((self.state(self.a), self.state(self.b), self.state(self.c)),
                         ("running", "queued", "queued"))
        cards = {c["name"]: c for c in self.ws.setup_state()["cards"]}
        self.assertEqual(cards[self.b]["download"]["state"], "queued")
        self.assertIsNone(cards[self.b]["download"]["percent"])
        time.sleep(1)
        self.assertEqual(self.order(), [self.a])               # one download process at a time
        self.release(self.a)
        self.wait(self.a, "ready")
        self.wait_started(self.b)
        self.assertEqual(self.state(self.c), "queued")
        self.release(self.b)
        self.wait_started(self.c)
        self.release(self.c)
        self.wait(self.c, "ready")
        self.assertEqual(self.order(), [self.a, self.b, self.c])
        self.assertFalse(self.ws.downloads.busy())
        self.assertFalse(self.queue_file().exists())           # nothing left to carry on

    def test_cancelling_a_queued_entry_removes_only_that_one(self):
        self.three()
        self.assertTrue(self.ws.cancel_download(self.b))
        self.assertIsNone(self.state(self.b))                  # back to Download
        self.assertEqual((self.state(self.a), self.state(self.c)), ("running", "queued"))
        self.assertEqual(self.saved_queue(), [self.a, self.c])
        self.release(self.a)
        self.wait_started(self.c)
        self.release(self.c)
        self.wait(self.c, "ready")
        self.assertEqual(self.order(), [self.a, self.c])
        with self.assertRaisesRegex(WorkspaceError, "Unknown model"):
            self.ws.cancel_download("not-a-model")
        with self.assertRaisesRegex(WorkspaceError, "Unknown model"):
            self.ws.cancel_download(["list"])
        self.assertFalse(self.ws.cancel_download(self.b))      # not queued or running

    def test_the_page_cancels_a_queued_entry_by_name_and_only_a_known_one(self):
        from workspace import api
        page = api.make_api(self.ws)
        self.three()
        self.assertEqual(page.cancel_download(self.c), {"ok": True, "value": True})
        self.assertIsNone(self.state(self.c))
        for bad in ("../x", 5, {"name": self.b}, "x" * 10000):
            with self.subTest(bad=str(bad)[:20]):
                self.assertEqual(page.cancel_download(bad), {"ok": False,
                                                             "error": "Unknown model."})
        self.assertEqual(self.state(self.b), "queued")
        self.assertEqual(page.cancel_download(), {"ok": True, "value": True})   # the running one
        self.wait_started(self.b)

    def test_cancelling_the_running_one_starts_the_next(self):
        self.three()
        self.assertTrue(self.ws.cancel_download(self.a))
        self.assertIn("cancelled", self.wait(self.a, "failed")["message"])
        self.wait_started(self.b)
        self.assertEqual(self.state(self.c), "queued")
        self.assertEqual(self.saved_queue(), [self.b, self.c])
        self.assertTrue(self.ws.cancel_download())             # no name: the running one
        self.wait_started(self.c)

    def test_a_failure_does_not_drop_the_rest(self):
        self.three()
        self.release(self.a, "fail")
        self.assertIn("internet connection", self.wait(self.a, "failed")["message"])
        self.wait_started(self.b)
        self.release(self.b)
        self.wait(self.b, "ready")
        self.release(self.c)
        self.wait(self.c, "ready")
        self.assertEqual(self.order(), [self.a, self.b, self.c])

    def test_a_model_already_downloading_or_queued_is_not_queued_twice(self):
        self.three()
        for name in (self.a, self.b):
            with self.assertRaisesRegex(WorkspaceError, "already"):
                self.ws.download_model(name)
        self.assertEqual(self.saved_queue(), [self.a, self.b, self.c])

    def test_a_download_queues_behind_a_check(self):
        with self.ws.downloads._lock:
            self.ws.downloads._running = self.a                # Check every file is running
            self.ws.downloads.state[self.a] = {"state": "running", "message": "Checking",
                                               "log": [], "verify": True}
        self.assertEqual(self.ws.download_model(self.b), "queued")
        self.assertEqual(self.saved_queue(), [self.b])         # a check is never carried on
        with self.ws.downloads._lock:
            self.ws.downloads._running = None
        self.ws.downloads._next()                              # the check ends
        self.wait_started(self.b)
        self.release(self.b)
        self.wait(self.b, "ready")

    # -- persistence and restart (criterion 3) -----------------------------------------

    def test_the_queue_is_kept_with_model_identifiers_only(self):
        self.vault["t"] = SECRET
        self.three()
        speaker = asr_engines.SPEAKER_MODEL.name
        self.assertEqual(self.ws.download_model(speaker, SECRET, remember=False), "queued")
        self.assertEqual(self.saved_queue(), [self.a, self.b, self.c, speaker])
        text = self.queue_file().read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)
        self.assertEqual(set(json.loads(text)), {"queue"})
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(SECRET, json.dumps(self.ws.setup_state()))

    def test_queued_downloads_and_the_interrupted_one_carry_on_at_the_next_start(self):
        self.three()
        self.ws.close()                                        # a is cut off mid-download
        self.workspaces.remove(self.ws)
        self.assertEqual(self.saved_queue(), [self.a, self.b, self.c])
        (self.models / "order.txt").unlink()
        ws2 = self.open(start=True)
        self.wait_started(self.a)
        a = ws2.downloads.state[self.a]
        self.assertEqual(a["state"], "running")
        self.assertTrue(a["resumed"])                          # the page says it carried on
        cards = {c["name"]: c for c in ws2.setup_state()["cards"]}
        self.assertTrue(cards[self.b]["download"]["resumed"])
        self.assertEqual(cards[self.b]["download"]["state"], "queued")
        for name in (self.a, self.b, self.c):
            self.release(name)
        self.wait(self.c, "ready", ws=ws2)
        self.assertEqual(self.order(), [self.a, self.b, self.c])
        self.assertFalse(self.queue_file(ws2).exists())

    def test_processing_waits_for_the_whole_queue(self):
        self.three()
        self.release(self.a)
        self.wait(self.a, "ready")
        self.assertTrue(self.ws.downloads.busy())              # b and c still to come
        for name in (self.b, self.c):
            self.release(name)
        self.wait(self.c, "ready")
        self.assertFalse(self.ws.downloads.busy())

    def test_nothing_is_queued_without_a_click(self):
        ws = self.open(start=True)
        time.sleep(0.5)
        self.assertEqual(ws.downloads.state, {})
        self.assertFalse(self.queue_file(ws).exists())
        self.assertFalse(ws.downloads.busy())

    def test_a_damaged_or_foreign_queue_file_starts_nothing_it_does_not_know(self):
        self.ws.close()
        self.workspaces.remove(self.ws)
        f = self.queue_file()
        for text in ("not json", json.dumps(["list"]), json.dumps({"queue": "x"}),
                     json.dumps({"queue": ["C:\\Users\\someone\\evil", 7, None]}),
                     "{" * 100000, json.dumps({"queue": [self.a]}) + " " * 70000):
            with self.subTest(text=text[:30]):
                f.write_text(text, encoding="utf-8")
                ws = self.open(start=True)
                time.sleep(0.3)
                self.assertEqual(ws.downloads.state, {})
                self.assertFalse(f.exists())                   # tidied away
                ws.close()
                self.workspaces.remove(ws)
        # Unknown names are dropped, duplicates kept once, order kept.
        f.write_text(json.dumps({"queue": ["evil", self.b, self.b, self.a]}), encoding="utf-8")
        ws = self.open(start=True)
        self.wait_started(self.b)
        self.assertEqual(self.state(self.a, ws), "queued")
        self.assertNotIn("evil", ws.downloads.state)
        self.assertEqual(self.saved_queue(ws), [self.b, self.a])

    def test_a_locked_workspace_keeps_the_queue_for_a_later_start(self):
        self.ws.close()
        self.workspaces.remove(self.ws)
        self.queue_file().write_text(json.dumps({"queue": [self.a]}), encoding="utf-8")
        ws = self.open()
        ws.locked = "unavailable"                              # as a locked start (#111 C4a)
        ws.start()
        time.sleep(0.5)
        self.assertEqual(ws.downloads.state, {})
        self.assertEqual(self.saved_queue(ws), [self.a])

    # -- policy (criterion 4) ----------------------------------------------------------

    def test_policy_that_turns_downloads_off_empties_the_queue(self):
        self.three()
        self.policy["AllowModelDownloads"] = False
        self.ws.policy.refresh()
        self.ws.setup_state()                                  # the page's next poll
        for name in (self.b, self.c):
            st = self.ws.downloads.state[name]
            self.assertEqual(st["state"], "failed")
            self.assertIn("turned off by your organisation", st["message"])
        self.assertEqual(self.saved_queue(), [self.a])         # the running one ends as before
        self.release(self.a)
        self.wait(self.a, "ready")
        time.sleep(1)
        self.assertEqual(self.order(), [self.a])               # nothing more started

    def test_policy_read_when_the_next_one_would_start(self):
        self.three()
        self.policy["AllowModelDownloads"] = False
        self.ws.policy.refresh()
        self.release(self.a)                                   # before any poll
        self.wait(self.a, "ready")
        for name in (self.b, self.c):
            self.assertIn("turned off by your organisation",
                          self.wait(name, "failed")["message"])
        time.sleep(1)
        self.assertEqual(self.order(), [self.a])
        self.assertFalse(self.queue_file().exists())

    def test_an_unreadable_policy_grants_nothing_at_the_next_start(self):
        from workspace import policy
        self.three()
        self.ws.close()
        self.workspaces.remove(self.ws)
        ws = Workspace(self.root / "ws", self.models, start=False, allow_test_engines=True,
                       battery=False, policy_reader=lambda: ({}, policy.UNREADABLE),
                       protector=keyprotect.TestProtector())
        self.assertIs(ws.downloads.target, fake_worker_engines.fake_download_held)
        self.workspaces.append(ws)
        (self.models / "order.txt").unlink()
        ws.start()
        for name in (self.a, self.b, self.c):
            self.assertIn("turned off by your organisation",
                          self.wait(name, "failed", ws=ws)["message"])
        time.sleep(1)
        self.assertEqual(self.order(), [])
        self.assertFalse(self.queue_file(ws).exists())

    def test_a_queued_graphics_card_component_policy_turned_off_fails_alone(self):
        self.three()
        self.assertEqual(self.ws.download_model(gpu.COMPONENT), "queued")
        self.assertEqual(self.ws.download_model(asr_engines.PARAKEET_V2), "queued")
        self.policy["AllowGpuComponent"] = False
        self.ws.policy.refresh()
        self.ws.setup_state()
        self.assertIn("graphics card component is turned off",
                      self.ws.downloads.state[gpu.COMPONENT]["message"])
        for name in (self.a, self.b, self.c):
            self.release(name)
        self.release(asr_engines.PARAKEET_V2)
        self.wait(asr_engines.PARAKEET_V2, "ready")
        self.assertEqual(self.order(), [self.a, self.b, self.c, asr_engines.PARAKEET_V2])

    # -- the token (criterion 5) -------------------------------------------------------

    def test_a_queued_speaker_model_gets_its_token_by_pipe(self):
        speaker = asr_engines.SPEAKER_MODEL.name
        self.assertEqual(self.ws.download_model(self.a), "started")
        self.wait_started(self.a)
        self.assertEqual(self.ws.download_model(speaker, SECRET, remember=True), "queued")
        self.assertEqual(self.vault, {})                       # remembered only once it works
        self.release(self.a)
        self.release(speaker)
        st = self.wait(speaker, "ready")
        self.assertEqual(st["result"], {"token_received": True, "token_matches": True})
        self.assertEqual(self.vault["t"], SECRET)

    def test_a_carried_on_speaker_model_without_a_token_fails_and_the_rest_carry_on(self):
        speaker = asr_engines.SPEAKER_MODEL.name
        self.assertEqual(self.ws.download_model(self.a), "started")
        self.wait_started(self.a)
        self.ws.download_model(speaker, SECRET, remember=False)   # used once, never kept
        self.ws.download_model(self.b)
        self.ws.close()
        self.workspaces.remove(self.ws)
        self.assertEqual(self.saved_queue(), [self.a, speaker, self.b])
        (self.models / "order.txt").unlink()
        ws = self.open(start=True)
        self.release(self.a)
        self.assertEqual(self.wait(speaker, "failed", ws=ws)["message"],
                         "Enter your Hugging Face token to download the speaker model.")
        self.wait_started(self.b)
        self.release(self.b)
        self.wait(self.b, "ready", ws=ws)
        self.assertEqual(self.order(), [self.a, self.b])

    def test_a_carried_on_speaker_model_uses_the_remembered_token(self):
        speaker = asr_engines.SPEAKER_MODEL.name
        self.vault["t"] = SECRET
        self.assertEqual(self.ws.download_model(self.a), "started")
        self.wait_started(self.a)
        self.ws.download_model(speaker)
        self.ws.close()
        self.workspaces.remove(self.ws)
        ws = self.open(start=True)
        self.release(self.a)
        self.release(speaker)
        self.assertEqual(self.wait(speaker, "ready", ws=ws)["result"],
                         {"token_received": True, "token_matches": True})

    # -- removal and the graphics card (criterion 6) -----------------------------------

    def test_removal_and_checks_wait_for_the_queue(self):
        self.three()
        self.release(self.a)
        self.wait(self.a, "ready")
        self.wait_started(self.b)
        for refused in (lambda: self.ws.remove_model(self.a),
                        lambda: self.ws.verify_model(self.a)):
            with self.assertRaises(WorkspaceError):
                refused()

    def test_a_queued_graphics_card_component_is_not_used_meanwhile(self):
        self.three()
        self.ws.download_model(gpu.COMPONENT)
        self.ws.download_model(gpu.WHISPER_COMPONENT)
        self.ws.settings.update(gpu_speakers=True, mode="faster")
        with mock.patch.object(gpu, "verified_for_use", return_value=True), \
                mock.patch.object(gpu.WHISPER, "status", return_value=("ready", "")), \
                mock.patch.object(gpu.WHISPER, "verified_for_use", return_value=True):
            self.assertEqual(self.ws._speaker_device(), ("cpu", None))
            self.assertEqual(self.ws._whisper_device(asr_engines.WHISPER_TURBO), ("cpu", None))

    def test_a_queued_graphics_card_component_in_use_when_its_turn_comes_fails_alone(self):
        self.three()
        self.ws.download_model(gpu.COMPONENT)
        # A speaker pass on the card that was running before the first click
        # (only a graphics card component may be downloaded meanwhile).
        self.ws._gpu_pass = True
        self.ws._busy = ("m" * 32, "speakers")
        for name in (self.a, self.b, self.c):
            self.release(name)
        self.assertIn("Pause processing",
                      self.wait(gpu.COMPONENT, "failed")["message"])
        self.ws._busy, self.ws._gpu_pass = None, False


if __name__ == "__main__":
    unittest.main()
