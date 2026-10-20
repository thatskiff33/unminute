# Guided model setup and token storage (#19). No network, no real downloads:
# a fake download process stands in for Hugging Face.
#   python -m unittest discover -s tests -v

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))

if HAVE_DEPS:
    from workspace import asr_engines
    import fake_worker_engines
    from workspace import credentials, setup
    from workspace.app import Workspace, WorkspaceError

SECRET = "hf_TESTtokenNOTreal0123456789"


def wait_for(downloads, name, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = downloads.state.get(name, {}).get("state")
        if st in ("ready", "failed"):
            return downloads.state[name]
        time.sleep(0.1)
    raise AssertionError("download did not finish")


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = Workspace(self.root / "ws", self.root / "models", start=False,
                            allow_test_engines=True, battery=False)
        # Fake download process and an in-memory credential store.
        self.ws.downloads.target = fake_worker_engines.fake_download
        self.vault = {}
        self.saved = (credentials.save_token, credentials.load_token, credentials.has_token,
                      credentials.remove_token)
        credentials.save_token = lambda t, target=None: self.vault.__setitem__("t", t)
        credentials.load_token = lambda target=None: self.vault.get("t")
        credentials.has_token = lambda target=None: "t" in self.vault
        credentials.remove_token = lambda target=None: self.vault.pop("t", None) is not None

    def tearDown(self):
        (credentials.save_token, credentials.load_token, credentials.has_token,
         credentials.remove_token) = self.saved
        self.ws.close()
        self.tmp.cleanup()

    def test_cards_describe_every_model_without_downloading(self):
        s = self.ws.setup_state()
        names = [c["name"] for c in s["cards"]]
        self.assertIn(asr_engines.RECOMMENDED_PARAKEET, names)
        self.assertIn(asr_engines.SPEAKER_MODEL.name, names)
        for c in s["cards"]:
            self.assertEqual(c["state"], "missing")
            self.assertGreater(c["size_gb"], 0)
            self.assertTrue(c["licence"])
        self.assertFalse(s["speech_ready"])
        self.assertFalse(s["onboarded"])

    def test_whisper_large_v3_is_offered_like_turbo(self):
        # #246 (owner's decision, 20 Oct 2026): Setup's other options and
        # Manage downloads offer Whisper large-v3, sized from its pinned files.
        # Turbo stays the suggested Whisper model and Parakeet v2 the
        # recommended one. It downloads, checks, repairs and removes as turbo does.
        spec = asr_engines.WHISPER_MODELS[asr_engines.WHISPER_LARGE]
        cards = self.ws.setup_state()["cards"]
        speech = [c["name"] for c in cards if c["role"] == "speech"]
        large = next(c for c in cards if c["name"] == asr_engines.WHISPER_LARGE)
        self.assertEqual(large["size_gb"], round(spec.download_bytes / 1e9, 2))
        self.assertEqual(large["size_gb"], 3.09)
        self.assertEqual((large["role"], large["label"], large["state"], large["licence"],
                          large["revision"], large["needs_token"], large["recommended"]),
                         ("speech", "Whisper large-v3", "missing", "MIT", "edaa852", False,
                          False))
        # Round 1 review: the card says what docs/ENGINE_EVALUATION.md found
        # (no more accurate than turbo), never that it was ahead of turbo;
        # "most accurate" is OpenAI's claim, attributed. Round 3: those tests
        # ran on Intel Arc graphics, so the card says so and drops the memory
        # figure; speed on a processor is reasoned from size, not measured.
        self.assertEqual(large["about"],
                         "OpenAI's most accurate Whisper model. In our tests on Intel Arc "
                         "graphics it was no more accurate than turbo. It is the largest "
                         "model, so expect it to be the slowest on a processor.")
        self.assertNotIn("ahead of turbo", large["about"])
        self.assertLess(speech.index(asr_engines.WHISPER_TURBO),
                        speech.index(asr_engines.WHISPER_LARGE))
        self.assertEqual([c["name"] for c in cards if c["role"] == "speech" and c["recommended"]],
                         [asr_engines.RECOMMENDED_PARAKEET])
        for name in (asr_engines.WHISPER_TURBO, asr_engines.WHISPER_LARGE):
            with self.subTest(name=name):
                self.ws.download_model(name)                       # Download (the fake)
                self.assertEqual(wait_for(self.ws.downloads, name)["state"], "ready")
                self.ws.verify_model(name)                         # Check every file
                st = wait_for(self.ws.downloads, name)
                self.assertEqual(st["state"], "failed")            # nothing really installed
                self.assertIn("Repair", st["message"])
                # A damaged copy (synthetic): the card says so, and offers Remove.
                folder = asr_engines.model_dir(asr_engines.WHISPER_MODELS[name],
                                               self.ws.cache_dir)
                folder.mkdir(parents=True)
                (folder / "model.bin").write_bytes(b"not a model")
                self.ws._forget_display()
                card = next(c for c in self.ws.setup_state()["cards"] if c["name"] == name)
                self.assertEqual((card["state"], card["removable"]), ("invalid", True))
                self.ws.download_model(name)                       # Repair: download again
                self.assertEqual(wait_for(self.ws.downloads, name)["state"], "ready")
                self.ws.remove_model(name)                         # Remove...
                self.assertFalse(folder.exists())
                self.ws._forget_display()
                card = next(c for c in self.ws.setup_state()["cards"] if c["name"] == name)
                self.assertEqual((card["state"], card["removable"]), ("missing", False))

    def test_settings_mark_speech_models_as_the_downloads_page_does(self):
        # #244: each choice carries the state the Downloads page shows, and a
        # model that is not ready carries the note shown beside the picker.
        states = {asr_engines.PARAKEET_V2: "ready", asr_engines.PARAKEET_V3: "invalid",
                  asr_engines.WHISPER_TURBO: "missing", asr_engines.WHISPER_LARGE: "missing"}
        calls = []

        def status(name, cache_dir):
            calls.append(name)
            return states.get(name, "missing"), "synthetic", ""
        with mock.patch.object(setup, "status", side_effect=status):
            s = self.ws.get_settings()
            cards = self.ws.setup_state()["cards"]
            choices = {c["name"]: c for c in s["choices"]}
            self.assertEqual(list(choices), [*asr_engines.PARAKEET_MODELS,
                                             *asr_engines.WHISPER_MODELS])
            speech = [c for c in cards if c["role"] == "speech"]
            self.assertTrue(speech)
            for card in speech:                      # the same state as Downloads
                self.assertEqual(choices[card["name"]]["state"], card["state"])
                self.assertTrue(choices[card["name"]]["offered"])
            for name, state in states.items():
                self.assertEqual(choices[name]["state"], state)
            self.assertIsNone(choices[asr_engines.PARAKEET_V2]["note"])
            self.assertEqual(choices[asr_engines.PARAKEET_V3]["note"],
                             "Parakeet v3 is damaged. Repair it in Settings > Manage downloads, "
                             "or choose another speech model.")
            self.assertEqual(choices[asr_engines.WHISPER_TURBO]["note"],
                             "Whisper large-v3 turbo is not downloaded. Download it in Settings > "
                             "Manage downloads, or choose another speech model.")
            # #246: Whisper large-v3 is offered too, so its note names Manage
            # downloads like any other model, never IT.
            large = choices[asr_engines.WHISPER_LARGE]
            self.assertTrue(large["offered"])
            self.assertEqual(large["note"],
                             "Whisper large-v3 is not downloaded. Download it in Settings > "
                             "Manage downloads, or choose another speech model.")

            # Settings is polled while open: the files are checked at most once
            # a minute, and again at once after a download or check ends.
            checked = len(calls)
            self.ws.get_settings()
            self.ws.get_settings()
            self.assertEqual(len(calls), checked)
            states[asr_engines.WHISPER_TURBO] = "ready"
            self.ws._after_download()
            turbo = next(c for c in self.ws.get_settings()["choices"]
                         if c["name"] == asr_engines.WHISPER_TURBO)
            self.assertEqual((turbo["state"], turbo["note"]), ("ready", None))
            self.assertGreater(len(calls), checked)

    def test_settings_never_send_to_manage_downloads_when_policy_turns_downloads_off(self):
        # #244 review, K3: with AllowModelDownloads off, Manage downloads cannot
        # fix a model, so the note names IT and the page is not offered.
        from workspace import policy
        values = {"AllowModelDownloads": False}
        self.ws.policy = policy.PolicySource(reader=lambda: (values, ()))
        self.ws._forget_display()
        states = {asr_engines.PARAKEET_V2: "ready", asr_engines.PARAKEET_V3: "missing",
                  asr_engines.WHISPER_TURBO: "invalid", asr_engines.WHISPER_LARGE: "missing"}
        with mock.patch.object(setup, "status",
                               side_effect=lambda n, c: (states.get(n, "missing"), "x", "")):
            choices = {c["name"]: c for c in self.ws.get_settings()["choices"]}
            self.assertFalse(any(c["offered"] for c in choices.values()))
            self.assertIsNone(choices[asr_engines.PARAKEET_V2]["note"])
            self.assertEqual(choices[asr_engines.PARAKEET_V3]["note"],
                             "Parakeet v3 is not downloaded, and downloads are turned off by "
                             "your organisation. Choose another speech model, or ask your IT "
                             "team to install it.")
            self.assertTrue(choices[asr_engines.WHISPER_TURBO]["note"].startswith(
                "Whisper large-v3 turbo is damaged, and downloads are turned off"))
            # #246: large-v3 is offered now, but policy still wins (K3).
            self.assertEqual(choices[asr_engines.WHISPER_LARGE]["note"],
                             "Whisper large-v3 is not downloaded, and downloads are turned off "
                             "by your organisation. Choose another speech model, or ask your "
                             "IT team to install it.")
            for c in choices.values():
                self.assertNotIn("Manage downloads", c["note"] or "")
            # Policy is re-read on each poll: allowed again, the page offers it.
            values["AllowModelDownloads"] = True
            choices = {c["name"]: c for c in self.ws.get_settings()["choices"]}
            self.assertTrue(choices[asr_engines.PARAKEET_V3]["offered"])
            self.assertIn("Manage downloads", choices[asr_engines.PARAKEET_V3]["note"])

    def test_public_model_never_receives_a_token(self):
        self.vault["t"] = SECRET
        self.ws.download_model(asr_engines.RECOMMENDED_PARAKEET)
        st = wait_for(self.ws.downloads, asr_engines.RECOMMENDED_PARAKEET)
        self.assertEqual(st["state"], "ready")
        self.assertFalse(st["result"]["token_received"])

    def test_speaker_token_is_remembered_and_passed_by_pipe_only(self):
        name = asr_engines.SPEAKER_MODEL.name
        with self.assertRaisesRegex(WorkspaceError, "token"):
            self.ws.download_model(name)
        received = {}
        real = setup.Downloads._run

        def spy(downloads, n, token, on_success=None):
            received["token"] = token
            return real(downloads, n, token, on_success)
        setup.Downloads._run = spy
        try:
            self.ws.download_model(name, SECRET, remember=True)
            wait_for(self.ws.downloads, name)
        finally:
            setup.Downloads._run = real
        self.assertEqual(received["token"], SECRET)
        self.assertEqual(self.vault["t"], SECRET)                 # remembered by default
        # Never exposed: not in setup state, settings or download messages.
        settings = self.ws.root / "settings.json"
        blob = json.dumps(self.ws.setup_state()) + (settings.read_text() if settings.exists()
                                                     else "")
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(SECRET, os.environ.get("HF_TOKEN", ""))
        self.assertTrue(self.ws.setup_state()["token_remembered"])

    def test_whisper_gpu_component_downloads_without_a_token_and_stops_only_its_worker(self):
        # #248: a public download; replacing it stops only the Whisper GPU
        # worker (before_gpu_install gets the component's name).
        from workspace import gpu
        self.vault["t"] = SECRET
        hooks = []
        self.ws.downloads.before_gpu_install = hooks.append
        self.ws.downloads.before_install = lambda: hooks.append("models")
        self.ws.download_model(gpu.WHISPER_COMPONENT)
        st = wait_for(self.ws.downloads, gpu.WHISPER_COMPONENT)
        self.assertEqual(st["state"], "ready")
        self.assertFalse(st["result"]["token_received"])
        self.assertEqual(hooks, [gpu.WHISPER_COMPONENT])

    def test_whisper_gpu_component_progress_and_check(self):
        from unittest import mock

        from workspace import gpu
        d = self.ws.downloads
        d.state[gpu.WHISPER_COMPONENT] = {"state": "running", "message": "x", "log": []}
        wheels = gpu.WHISPER.staging_dir(d.cache_dir) / "wheels"
        wheels.mkdir(parents=True)
        self.assertIsNone(d.percent(gpu.WHISPER_COMPONENT))             # none started yet
        (wheels / (gpu.WHISPER_WHEELS[0]["name"] + ".whl")).write_bytes(b"x")
        with mock.patch.object(setup, "_file_size",
                               return_value=gpu.WHISPER_DOWNLOAD_BYTES // 2):
            self.assertEqual(d.percent(gpu.WHISPER_COMPONENT), 50)
        d.state.clear()
        self.assertTrue(setup.known(gpu.WHISPER_COMPONENT))
        self.assertFalse(setup.known(gpu.WHISPER_COMPONENT + "x"))
        self.ws.verify_model(gpu.WHISPER_COMPONENT)                     # Check every file
        self.assertEqual(wait_for(d, gpu.WHISPER_COMPONENT)["state"], "failed")  # not installed

    def test_child_gets_the_token_but_not_on_argv_or_env(self):
        result = {}
        real = fake_worker_engines.fake_download
        d = setup.Downloads(self.root / "models", target=real)
        d.start(asr_engines.SPEAKER_MODEL.name, SECRET)
        st = wait_for(d, asr_engines.SPEAKER_MODEL.name)
        result.update(st["result"])
        self.assertEqual(result, {"token_received": True, "in_argv": False, "in_env": False})

    def test_token_is_remembered_only_after_a_successful_download(self):
        self.ws.downloads.target = fake_worker_engines.fake_download_fails
        name = asr_engines.SPEAKER_MODEL.name
        self.ws.download_model(name, SECRET, remember=True)
        self.assertEqual(wait_for(self.ws.downloads, name)["state"], "failed")
        self.assertEqual(self.vault, {})                      # a mistyped token is not kept

    def test_gated_install_needs_no_environment_token(self):
        saved = os.environ.pop("HF_TOKEN", None)
        try:
            calls = []
            spec = asr_engines.SPEAKER_MODEL
            with self.assertRaises(asr_engines.ModelLoadError):
                asr_engines.install_model(spec, self.root / "m", log=lambda m: None,
                                          download=lambda sp, d: calls.append(d),
                                          has_token=True)      # fails verification, but...
            self.assertEqual(len(calls), 1)                    # ...it did try to download
            with self.assertRaisesRegex(asr_engines.ModelLoadError, "token"):
                asr_engines.install_model(spec, self.root / "m2", log=lambda m: None,
                                          download=lambda sp, d: calls.append(d),
                                          has_token=False)
            self.assertEqual(len(calls), 1)
        finally:
            if saved is not None:
                os.environ["HF_TOKEN"] = saved

    def test_processing_waits_while_a_model_is_replaced(self):
        import wave

        import numpy as np
        rec = self.root / "q.wav"
        with wave.open(str(rec), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(np.zeros(16000 * 10, dtype="<i2").tobytes())
        self.ws.settings.update(model="test:fake_worker_engines:sentences", speakers=False)
        self.ws._import_one(rec)
        self.ws.downloads._running = "x"                      # a download in progress
        self.ws.start()
        time.sleep(1.5)
        self.assertEqual(self.ws.meetings()[0]["progress"], 0)   # queued, not started
        self.ws.downloads._running = None
        self.ws.downloads.after()                             # the download ends
        self.assertTrue(self.ws.wait_idle(60))
        self.assertEqual(self.ws.meetings()[0]["progress"], 100)

    def test_cancel_and_full_check(self):
        name = asr_engines.RECOMMENDED_PARAKEET
        self.assertFalse(self.ws.cancel_download())
        self.ws.verify_model(name)
        st = wait_for(self.ws.downloads, name)
        self.assertEqual(st["state"], "failed")               # nothing installed here
        self.assertIn("Repair", st["message"])

    def test_not_remembering_uses_the_token_once(self):
        self.ws.download_model(asr_engines.SPEAKER_MODEL.name, SECRET, remember=False)
        wait_for(self.ws.downloads, asr_engines.SPEAKER_MODEL.name)
        self.assertEqual(self.vault, {})

    def test_remove_token_keeps_working_offline(self):
        self.vault["t"] = SECRET
        self.assertTrue(self.ws.remove_token())
        self.assertFalse(self.ws.setup_state()["token_remembered"])
        self.assertFalse(self.ws.remove_token())

    def test_failed_download_explains_and_can_be_retried(self):
        self.ws.downloads.target = fake_worker_engines.fake_download_fails
        name = asr_engines.RECOMMENDED_PARAKEET
        self.ws.download_model(name)
        st = wait_for(self.ws.downloads, name)
        self.assertEqual(st["state"], "failed")
        self.assertIn("internet connection", st["message"])
        self.ws.downloads.target = fake_worker_engines.fake_download
        self.ws.download_model(name)                              # retry is just "again"
        self.assertEqual(wait_for(self.ws.downloads, name)["state"], "ready")

    def test_one_download_at_a_time_and_not_while_processing(self):
        self.ws._busy = ("m" * 32, "transcribe")
        with self.assertRaisesRegex(WorkspaceError, "Pause processing"):
            self.ws.download_model(asr_engines.RECOMMENDED_PARAKEET)
        self.ws._busy = None
        with self.assertRaises(WorkspaceError):
            self.ws.download_model("not-a-model")

    def test_finish_setup_and_help_links_are_fixed(self):
        self.ws.finish_setup(False)
        s = self.ws.setup_state()
        self.assertTrue(s["onboarded"])
        self.assertFalse(s["speakers"])
        with self.assertRaises(WorkspaceError):
            self.ws.open_help("https://evil.example/")
        # Exactly these pages (an allowlist): the Hugging Face pages for the
        # token, and the user guide's recovery steps (R11, the user's choice).
        self.assertEqual(setup.HELP_LINKS, {
            "account": "https://huggingface.co/join",
            "terms": "https://huggingface.co/pyannote/speaker-diarization-community-1",
            "tokens": "https://huggingface.co/settings/tokens",
            "recovery": "https://github.com/thatskiff33/unminute/blob/main/docs/"
                        "WORKSPACE_GUIDE.md#if-unminute-finds-damage",
        })

    def test_unfinished_work_never_switches_model_revision(self):
        import wave

        import numpy as np
        rec = self.root / "a.wav"
        with wave.open(str(rec), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.zeros(16000 * 5)).astype("<i2").tobytes())
        self.ws.settings.update(model=asr_engines.RECOMMENDED_PARAKEET)
        self.ws._import_one(rec)
        mid = self.ws.meetings()[0]["id"]
        with self.ws.store.tx() as db:            # as if started by an older app version
            db.execute("UPDATE versions SET model_revision = 'old'")
        self.ws._run("transcribe", mid, self.ws.store.active_version(mid)["id"])
        self.assertIn("model was updated", self.ws.meetings()[0]["error"])


@unittest.skipUnless(sys.platform == "win32" and HAVE_DEPS, "Windows Credential Manager")
class CredentialManagerTests(unittest.TestCase):
    TARGET = "Unminute/UnitTest-" + str(os.getpid())

    def test_round_trip_and_remove(self):
        try:
            credentials.save_token(SECRET, target=self.TARGET)
        except credentials.CredentialError as exc:
            self.skipTest(f"Credential Manager not available here: {exc}")
        try:
            self.assertEqual(credentials.load_token(self.TARGET), SECRET)
            self.assertTrue(credentials.has_token(self.TARGET))
        finally:
            self.assertTrue(credentials.remove_token(self.TARGET))
        self.assertIsNone(credentials.load_token(self.TARGET))
        self.assertFalse(credentials.remove_token(self.TARGET))

    def test_rejects_things_that_are_not_tokens(self):
        for bad in ("", "   ", "two words", "x" * 500):
            with self.assertRaises(credentials.CredentialError):
                credentials.save_token(bad, target=self.TARGET)


if __name__ == "__main__":
    unittest.main()
