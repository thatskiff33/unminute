# Managed policy (#111 slice A5): parsing, fail-safe handling of bad values,
# and refresh. The registry itself is read only on Windows; the parsing rules
# are tested with winreg's type constants.
#   python -m unittest discover -s tests -v

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402
import profile_elsewhere  # noqa: E402

from workspace import policy  # noqa: E402


sys.path.append(str(Path(__file__).resolve().parents[1] / "tools"))
import decoder_runtime  # noqa: E402  (tools/decoder_runtime.py)
import page_scripts  # noqa: E402


def setUpModule():
    # #299: the decoder runs in an AppContainer that cannot read this Python:
    # decodes here use a scratch runtime it can read and a throwaway profile.
    decoder_runtime.use()


def tearDownModule():
    decoder_runtime.stop()


class PolicyTests(unittest.TestCase):
    def test_unmanaged_values_use_the_app_default(self):
        p = policy.Policy()
        self.assertTrue(p.get("AllowModelDownloads"))
        self.assertFalse(p.get("AllowNetworkSources"))
        self.assertEqual(p.get("ClipboardClearMinutes"), 0)
        self.assertFalse(p.managed("AllowModelDownloads"))

    def test_set_values_win(self):
        p = policy.Policy({"AllowModelDownloads": False, "AllowNetworkSources": True,
                           "ClipboardClearMinutes": 5})
        self.assertFalse(p.get("AllowModelDownloads"))
        self.assertTrue(p.get("AllowNetworkSources"))
        self.assertEqual(p.get("ClipboardClearMinutes"), 5)
        self.assertTrue(p.managed("ClipboardClearMinutes"))

    def test_a_bad_value_never_grants_a_permission(self):
        p = policy.Policy(invalid=("AllowModelDownloads", "AllowNetworkSources",
                                   "ClipboardClearMinutes"))
        self.assertFalse(p.get("AllowModelDownloads"))     # default True, withheld
        self.assertFalse(p.get("AllowNetworkSources"))
        self.assertEqual(p.get("ClipboardClearMinutes"), 0)  # not a permission: default
        self.assertEqual(p.summary()["invalid"],
                         ["AllowModelDownloads", "AllowNetworkSources",
                          "ClipboardClearMinutes"])

    def test_a_bad_value_keeps_the_recording_confirmation(self):
        self.assertTrue(policy.Policy().get("RequireRecordingConsent"))
        self.assertTrue(policy.Policy(invalid=("RequireRecordingConsent",))
                        .get("RequireRecordingConsent"))
        self.assertTrue(policy.Policy({}, policy.UNREADABLE).get("RequireRecordingConsent"))
        self.assertFalse(policy.Policy({"RequireRecordingConsent": False})
                         .get("RequireRecordingConsent"))

    def test_source_caches_and_refreshes(self):
        calls, now = [], [0.0]

        def reader():
            calls.append(1)
            return {"AllowModelDownloads": len(calls) == 1}, ()
        src = policy.PolicySource(reader=reader, refresh_s=60, clock=lambda: now[0])
        self.assertTrue(src.current().get("AllowModelDownloads"))
        now[0] = 30
        self.assertTrue(src.current().get("AllowModelDownloads"))   # cached
        now[0] = 61
        self.assertFalse(src.current().get("AllowModelDownloads"))  # read again
        self.assertEqual(len(calls), 2)
        src.refresh()
        self.assertEqual(len(calls), 3)

    def test_an_unreadable_key_is_read_again_soon(self):
        calls, now = [], [0.0]

        def reader():
            calls.append(1)
            return ({}, policy.UNREADABLE) if len(calls) == 1 else ({}, ())
        src = policy.PolicySource(reader=reader, refresh_s=900, clock=lambda: now[0])
        self.assertTrue(src.current().unreadable)
        self.assertFalse(src.current().get("AllowModelDownloads"))   # withheld meanwhile
        now[0] = policy.UNREADABLE_RETRY_S
        self.assertTrue(src.current().get("AllowModelDownloads"))
        self.assertEqual(len(calls), 2)


class AssistantValueTests(unittest.TestCase):
    """The Hand off assistant's name and address (#109): only a plain https
    address with a host name reaches Windows' "open", never another kind of
    link that Windows would hand to some other program."""

    def test_addresses_accepted(self):
        for url in ("https://chatgpt.com/", "https://chatgpt.com", "https://claude.ai/new",
                    "https://m365.cloud.microsoft/chat?auth=2", "https://chatgpt.com:443/",
                    "https://xn--mori-qsa.nz/", "https://a-b.example.org/x/y_z~1.2-3",
                    "https://example.org/?q=%20ok", "https://1.2.3.4.example.org/"):
            self.assertTrue(policy.valid_url(url), url)

    def test_addresses_refused(self):
        for url in ("javascript:alert(1)", "file:///C:/Windows/notepad.exe", "ms-settings:",
                    "\\\\server\\share\\x", "http://chatgpt.com/", "HTTPS://chatgpt.com/",
                    "https://ChatGPT.com/", "https://user@chatgpt.com/",
                    "https://user:pw@chatgpt.com/", "https://127.0.0.1/", "https://[::1]/",
                    "https://localhost/", "https://chatgpt.com:8443/", "https://chatgpt.com:/",
                    "https://chatgpt.com:x/", "https://chаtgpt.com/",   # Cyrillic a
                    "https://chatgpt.com/\nx", "https://chatgpt.com/\tx", " https://chatgpt.com/",
                    "https://chatgpt.com/ ", "https://chatgpt.com/a b", 'https://chatgpt.com/"x',
                    "https://chatgpt.com/'x", "https://chatgpt.com/<x>", "https://chatgpt.com/a\\b",
                    "https://chatgpt.com/%00", "https://chatgpt.com/%1f", "https://chatgpt.com/%7F",
                    "https://chatgpt.com/%", "https://chatgpt.com/#frag", "https://chatgpt.com.",
                    "https://-bad.com/", "https://chatgpt.com/\x7f", "https:///chatgpt.com",
                    "https:chatgpt.com", "https://chatgpt.com?", "https://chatgpt.com/" + "a" * 281,
                    "", None, 5, b"https://chatgpt.com/"):
            self.assertFalse(policy.valid_url(url), repr(url))
        self.assertTrue(policy.valid_url("https://chatgpt.com/" + "a" * 280))   # 300 exactly

    def test_names(self):
        for name in ("ChatGPT", "Copilot", "Microsoft 365 Copilot", "Kōrero AI", "R&D (internal)",
                     "Claude", "A.I. Helper+", "O'Brien-2", "x" * 30):
            self.assertTrue(policy.valid_name(name), name)
        for name in ("", " ChatGPT", "ChatGPT ", "Chat  GPT", "Chat\nGPT", "Chat\tGPT",
                     "Chat\u200bGPT", "Chat\u202eGPT", "Chat<b>", "Chat\"GPT", "Chat/GPT",
                     "Ko\u0304rero", "x" * 31, None, 5):
            self.assertFalse(policy.valid_name(name), repr(name))

    def test_the_default_assistant_meets_the_same_rules(self):
        # The ChatGPT default is held to the rules an IT-set value must meet,
        # so an edit to it cannot open something valid_url would refuse.
        from workspace import handoff
        name, url = handoff.DEFAULT_ASSISTANT
        self.assertTrue(policy.valid_name(name))
        self.assertTrue(policy.valid_url(url))

    def test_the_assistant_pause_matches_the_bridge_pause(self):
        # The design reuses api.DECLINE_PAUSE_S; app keeps its own copy only
        # because api imports app.
        from workspace import api, app
        self.assertEqual(app.ASSISTANT_PAUSE_S, api.DECLINE_PAUSE_S)


@unittest.skipUnless(sys.platform == "win32", "Windows registry types")
class ParseTests(unittest.TestCase):
    def test_assistant_values_are_plain_text_values_only(self):
        import winreg
        self.assertEqual(policy._parse("HandoffAssistantUrl", "https://claude.ai/",
                                       winreg.REG_SZ), "https://claude.ai/")
        self.assertEqual(policy._parse("HandoffAssistantName", "Claude", winreg.REG_SZ), "Claude")
        for name, value, kind in (("HandoffAssistantUrl", "https://claude.ai/",
                                   winreg.REG_EXPAND_SZ),
                                  ("HandoffAssistantUrl", "file:///C:/x", winreg.REG_SZ),
                                  ("HandoffAssistantUrl", 1, winreg.REG_DWORD),
                                  ("HandoffAssistantName", "Claude", winreg.REG_EXPAND_SZ),
                                  ("HandoffAssistantName", "Chat\u202eGPT", winreg.REG_SZ),
                                  ("AllowHandoffAssistant", 2, winreg.REG_DWORD)):
            with self.assertRaises(ValueError, msg=(name, value)):
                policy._parse(name, value, kind)

    def test_types_and_ranges(self):
        import winreg
        self.assertIs(policy._parse("AllowWatchedFolders", 0, winreg.REG_DWORD), False)
        self.assertIs(policy._parse("AllowWatchedFolders", 1, winreg.REG_DWORD), True)
        self.assertEqual(policy._parse("ClipboardClearMinutes", 15, winreg.REG_DWORD), 15)
        for name, value, kind in (("AllowWatchedFolders", 2, winreg.REG_DWORD),
                                  ("AllowWatchedFolders", "1", winreg.REG_SZ),
                                  ("ClipboardClearMinutes", 99999, winreg.REG_DWORD),
                                  ("HandoffDefaultFolder", "relative\\path", winreg.REG_SZ),
                                  ("HandoffDefaultFolder", "\\\\server\\share\\Clients",
                                   winreg.REG_SZ),
                                  ("HandoffDefaultFolder", "%NO_SUCH_VAR%\\x",
                                   winreg.REG_EXPAND_SZ),
                                  ("HandoffDefaultFolder", 5, winreg.REG_DWORD)):
            with self.assertRaises(ValueError, msg=(name, value)):
                policy._parse(name, value, kind)

    def test_retention_reminder_days(self):
        # #111 C6: whole days, 0 (off) to 365. Not permissions: a bad or
        # unreadable value gives the app's default (review due at 30 days,
        # action required off), never something stricter or looser.
        import winreg
        for name in ("ReviewReminderDays", "ActionRequiredDays"):
            for value in (0, 14, 365):
                self.assertEqual(policy._parse(name, value, winreg.REG_DWORD), value)
            for value, kind in ((366, winreg.REG_DWORD), (-1, winreg.REG_DWORD),
                                ("14", winreg.REG_SZ), (14, winreg.REG_QWORD)):
                with self.assertRaises(ValueError, msg=(name, value)):
                    policy._parse(name, value, kind)
        defaults = {"ReviewReminderDays": 30, "ActionRequiredDays": 0}
        for p in (policy.Policy(), policy.Policy({}, policy.UNREADABLE),
                  policy.Policy(invalid=tuple(defaults))):
            self.assertEqual({n: p.get(n) for n in defaults}, defaults)
        p = policy.Policy({"ReviewReminderDays": 7, "ActionRequiredDays": 14})
        self.assertEqual((p.get("ReviewReminderDays"), p.get("ActionRequiredDays")), (7, 14))

    def test_folder_expands_environment_variables(self):
        import os
        import winreg
        got = policy._parse("HandoffDefaultFolder", "%USERPROFILE%\\Handoffs",
                            winreg.REG_EXPAND_SZ)
        self.assertEqual(got, os.path.join(os.environ["USERPROFILE"], "Handoffs"))

    def test_folder_expands_only_what_windows_expands(self):
        # #111 D2 (red-team part 2, finding 7): os.path.expandvars also
        # expanded $NAME and ${NAME}, which Windows leaves as they are.
        import os
        import winreg
        env = {"UMV_TEST_BASE": "C:\\Data", "UMV_TEST_DOLLAR": "\\\\evil\\x"}
        with mock.patch.dict(os.environ, env):
            got = policy._parse("HandoffDefaultFolder",
                                "%UMV_TEST_BASE%\\$UMV_TEST_DOLLAR\\${UMV_TEST_DOLLAR}",
                                winreg.REG_EXPAND_SZ)
            self.assertEqual(got, "C:\\Data\\$UMV_TEST_DOLLAR\\${UMV_TEST_DOLLAR}")
            with self.assertRaises(ValueError):          # expands to a network path
                policy._parse("HandoffDefaultFolder", "%UMV_TEST_DOLLAR%", winreg.REG_EXPAND_SZ)

    def test_reading_without_a_policy_key_is_empty(self):
        # Development and CI machines have no policy key.
        self.assertEqual(policy.read_registry(), ({}, (), ()))

    def test_registry_reading_end_to_end(self):
        import winreg

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        done = OSError(None, "No more data is available", None, policy.ERROR_NO_MORE_ITEMS)
        rows = [("allowmodeldownloads", 0, winreg.REG_DWORD),        # any case
                ("AllowModelDownload", 1, winreg.REG_DWORD),         # misspelt: listed only
                ("AllowWatchedFolders", "0", winreg.REG_SZ),          # wrong type: withheld
                ("ClipboardClearMinutes", 5, winreg.REG_DWORD), done]
        with mock.patch.object(winreg, "OpenKey", return_value=Key()), \
                mock.patch.object(winreg, "EnumValue", side_effect=rows):
            values, invalid, unrecognised = policy.read_registry()
        self.assertEqual(values, {"AllowModelDownloads": False, "ClipboardClearMinutes": 5})
        self.assertEqual(invalid, ("AllowWatchedFolders",))
        self.assertEqual(unrecognised, ("AllowModelDownload",))

    def test_unrecognised_names_are_listed_and_change_nothing(self):
        # #111 D2 (red-team part 2, finding 8): a deny with a trailing space or
        # a lookalike letter was ignored with nothing to show it.
        import winreg

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        done = OSError(None, "No more data is available", None, policy.ERROR_NO_MORE_ITEMS)
        rows = [("AllowModelDownloads ", 0, winreg.REG_DWORD),
                ("AllowModelDownl\u043eads", 0, winreg.REG_DWORD),
                *((f"Other{i}", 1, winreg.REG_DWORD) for i in range(30)), done]
        with mock.patch.object(winreg, "OpenKey", return_value=Key()), \
                mock.patch.object(winreg, "EnumValue", side_effect=rows):
            values, invalid, unrecognised = policy.read_registry()
        self.assertEqual((values, invalid), ({}, ()))
        self.assertEqual(len(unrecognised), policy.UNRECOGNISED_MAX)        # bounded
        p = policy.PolicySource(reader=lambda: (values, invalid, unrecognised)).current()
        self.assertTrue(p.get("AllowModelDownloads"))      # still not managed: nothing changes
        self.assertFalse(p.managed("AllowModelDownloads"))
        shown = p.summary()["unrecognised"]
        self.assertIn("AllowModelDownloads\\x20", shown)
        self.assertIn("AllowModelDownl\\u043eads", shown)
        self.assertTrue(all(n.isascii() and n.isprintable() and " " not in n for n in shown))

    def test_a_policy_key_that_cannot_be_read_withholds_everything(self):
        import winreg

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        with mock.patch.object(winreg, "OpenKey", side_effect=PermissionError("denied")):
            self.assertEqual(policy.read_registry(), ({}, policy.UNREADABLE, ()))
        broken = [("AllowGpuComponent", 1, winreg.REG_DWORD),
                  OSError(None, "The key was changed", None, 1018)]   # not "no more items"
        with mock.patch.object(winreg, "OpenKey", return_value=Key()), \
                mock.patch.object(winreg, "EnumValue", side_effect=broken):
            values, invalid, unrecognised = policy.read_registry()
        self.assertEqual((values, invalid, unrecognised), ({}, policy.UNREADABLE, ()))
        p = policy.Policy(values, invalid)
        self.assertFalse(any(p.get(n) for n in policy.SPEC if policy.SPEC[n][2]))


@unittest.skipUnless(sys.platform == "win32", "Windows clipboard")
class ClipboardClearTests(unittest.TestCase):
    def test_clears_only_the_apps_own_copy(self):
        from workspace import clipboard
        try:
            previous = clipboard.read_text()
        except clipboard.ClipboardError:
            self.skipTest("clipboard busy")
        try:
            ours = clipboard.copy_text("handoff text")
            self.assertTrue(clipboard.clear_if_unchanged(ours))
            self.assertEqual(clipboard.read_text(), "")
            ours = clipboard.copy_text("handoff text")
            clipboard.copy_text("something the user copied since")
            self.assertFalse(clipboard.clear_if_unchanged(ours))
            self.assertEqual(clipboard.read_text(), "something the user copied since")
        finally:
            clipboard.copy_text(previous or "")       # put the user's text back


class AdmxTests(unittest.TestCase):
    """The ADMX template IT imports into Intune matches the values the app reads."""

    def test_template_covers_every_policy_value_with_text(self):
        import re
        import xml.etree.ElementTree as ET
        root = Path(__file__).resolve().parents[1] / "packaging" / "policy"
        ns = "{http://schemas.microsoft.com/GroupPolicy/2006/07/PolicyDefinitions}"
        admx_text = (root / "Unminute.admx").read_text(encoding="utf-8")
        admx = ET.fromstring(admx_text.encode("utf-8"))
        adml = ET.parse(root / "en-US" / "Unminute.adml").getroot()
        names = {p.get("name") for p in admx.iter(ns + "policy")}
        self.assertEqual(names, set(policy.SPEC))
        for p in admx.iter(ns + "policy"):
            self.assertEqual(p.get("key"), policy.POLICY_KEY)
            self.assertEqual(p.get("class"), "Machine")
        strings = {s.get("id") for s in adml.iter(ns + "string")}
        presentations = {s.get("id") for s in adml.iter(ns + "presentation")}
        self.assertLessEqual(set(re.findall(r"\$\(string\.(\w+)\)", admx_text)), strings)
        self.assertLessEqual(set(re.findall(r"\$\(presentation\.(\w+)\)", admx_text)),
                             presentations)

    def test_it_guide_lists_every_policy_value(self):
        import re
        guide =(Path(__file__).resolve().parents[1] / "docs" / "PACKAGING.md").read_text(
            encoding="utf-8")
        section = guide.split("### Managed settings (policy)", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(set(re.findall(r"^\| `(\w+)` \|", section, re.M)), set(policy.SPEC))


import importlib.util as _ilu  # noqa: E402

HAVE_DEPS = all(_ilu.find_spec(m) for m in ("numpy", "av"))


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class WorkspacePolicyTests(unittest.TestCase):
    """Each policy value is enforced by the app, whatever the page shows."""

    def setUp(self):
        profile_elsewhere.start(self)       # folders in %TEMP%, inside AppData (#111 D2)

    def open(self, **values):
        import tempfile
        from workspace.app import Workspace
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = Workspace(root / "ws", root / "models", start=False, battery=False,
                            allow_test_engines=True, policy_reader=lambda: (values, ()),
                            protector=keyprotect.TestProtector())
        return self.ws

    def tearDown(self):
        self.ws.close()
        self.tmp.cleanup()

    def refused(self, fn, *args):
        from workspace.app import WorkspaceError
        with self.assertRaises(WorkspaceError) as ctx:
            fn(*args)
        return str(ctx.exception)

    def test_downloads_off(self):
        ws = self.open(AllowModelDownloads=False)
        self.assertIn("Downloads are turned off", self.refused(ws.download_model,
                                                              "parakeet-tdt-0.6b-v2"))
        self.assertFalse(ws.setup_state()["managed"]["allow"]["AllowModelDownloads"])

    def test_user_models_off_on_a_machine_install(self):
        # #111 B7a: only IT's models are used, so a download would never be
        # used: refused with the IT wording; the graphics card components are
        # not models and are unaffected.
        from workspace import app, asr_engines, gpu
        ws = self.open(AllowUserModels=False)
        with mock.patch.object(asr_engines, "_machine_root", [Path("C:/Program Files/Unminute")]):
            self.assertEqual(self.refused(ws.download_model, "parakeet-tdt-0.6b-v2"),
                             app.USER_MODELS_OFF)
            self.assertFalse(ws._model_downloads_allowed())
            with mock.patch.object(ws.downloads, "start", return_value="started") as start:
                ws.download_model(gpu.COMPONENT)
            start.assert_called_once()
            note = app._model_note("parakeet-tdt-0.6b-v2", "missing",
                                   ws._model_downloads_allowed())
            self.assertIn("ask your IT team", note)
        # A per-user install never reads it.
        with mock.patch.object(asr_engines, "_machine_root", [None]):
            self.assertTrue(ws._user_models_allowed())
            self.assertTrue(ws._model_downloads_allowed())

    def test_only_it_models_words_what_is_true(self):
        # #451 (review of 317a3d4, nit 2): downloads on, AllowUserModels at 0
        # on a machine install: a model that is not ready says only IT's models
        # are used, not that downloads are turned off. With downloads off too,
        # the downloads wording stands (it is true); a per-user install never
        # reads AllowUserModels.
        from workspace import asr_engines
        ws = self.open(AllowUserModels=False)
        it_root = Path(self.tmp.name) / "machine"          # synthetic: holds no models
        with mock.patch.object(asr_engines, "_machine_root", [it_root]):
            self.assertTrue(ws._only_it_models())
            ws._forget_display()
            notes = {c["name"]: c["note"] for c in ws.get_settings()["choices"]}
            self.assertTrue(notes[asr_engines.PARAKEET_V2].startswith(
                "Parakeet v2 is not installed, and your organisation uses only the models "
                "your IT team installs on this computer."), notes)
            self.assertNotIn("downloads are turned off", notes[asr_engines.PARAKEET_V2])
        with mock.patch.object(asr_engines, "_machine_root", [None]):
            self.assertFalse(ws._only_it_models())
        ws.close()
        ws = self.open(AllowUserModels=False, AllowModelDownloads=False)
        with mock.patch.object(asr_engines, "_machine_root", [it_root]):
            self.assertFalse(ws._only_it_models())
            ws._forget_display()
            notes = {c["name"]: c["note"] for c in ws.get_settings()["choices"]}
            self.assertIn("downloads are turned off", notes[asr_engines.PARAKEET_V2])

    def test_downloads_names_repair_only_where_it_is_offered(self):
        # #451: a damaged copy's Details said "Repair downloads a fresh, verified
        # copy" beside "Ask your IT team", where no Repair is offered.
        from workspace import asr_engines, setup
        cases = (({}, None, setup.INVALID),
                 ({"AllowModelDownloads": False}, None, setup.INVALID_ASK_IT),
                 ({"AllowUserModels": False}, "machine", setup.INVALID_ASK_IT),
                 ({"AllowUserModels": False}, None, setup.INVALID))
        for values, root, expected in cases:
            with self.subTest(values=values, machine=root is not None):
                ws = self.open(**values)
                damaged = {"name": asr_engines.PARAKEET_V2, "role": "speech",
                           "state": "invalid", "status": setup.INVALID}
                machine_root = Path(self.tmp.name) / root if root else None
                with mock.patch.object(setup, "cards", return_value=[damaged]), \
                        mock.patch.object(asr_engines, "_machine_root", [machine_root]):
                    [card] = ws.setup_state()["cards"]
                self.assertEqual(card["status"], expected)
                ws.close()

    def test_the_page_is_told_whether_user_models_apply(self):
        # #111 B7a, design 3.2: the page shows "Ask your IT team" for a model
        # when allow.AllowUserModels is false (the reviews of 317a3d4 and
        # 1ae77ee). It is the value in force: false on a machine install with
        # AllowUserModels off, true on a per-user install, which never reads it.
        # AllowModelDownloads stays raw: the graphics card components stay
        # per-user (B7-5, DEC-068) and follow only it and AllowGpuComponent.
        from workspace import asr_engines
        ws = self.open(AllowUserModels=False)
        it_root = Path(self.tmp.name) / "machine"      # synthetic: holds no models
        for root, expected in ((it_root, False), (None, True)):
            with self.subTest(machine=root is not None), \
                    mock.patch.object(asr_engines, "_machine_root", [root]):
                ws._forget_display()
                allow = ws.setup_state()["managed"]["allow"]
                self.assertIs(allow["AllowUserModels"], expected)
                self.assertIs(allow["AllowModelDownloads"], True)
                self.assertIs(ws.get_settings()["managed"]["allow"]["AllowUserModels"],
                              expected)

    def test_downloads_off_stay_off_on_both_install_modes(self):
        from workspace import asr_engines
        ws = self.open(AllowModelDownloads=False)
        for root in (Path(self.tmp.name) / "machine", None):
            with mock.patch.object(asr_engines, "_machine_root", [root]):
                allow = ws.managed()["allow"]
                self.assertFalse(allow["AllowModelDownloads"])
                self.assertTrue(allow["AllowUserModels"])

    def test_an_unreadable_policy_uses_only_it_models_on_a_machine_install(self):
        from workspace import asr_engines
        ws = self.open()
        ws.policy = policy.PolicySource(reader=lambda: ({}, policy.UNREADABLE))
        with mock.patch.object(asr_engines, "_machine_root", [Path("C:/Program Files/Unminute")]):
            self.assertFalse(ws._user_models_allowed())
        with mock.patch.object(asr_engines, "_machine_root", [None]):
            self.assertTrue(ws._user_models_allowed())

    def test_gpu_component_off(self):
        from workspace import gpu
        ws = self.open(AllowGpuComponent=False)
        self.assertIn("graphics card", self.refused(ws.download_model, gpu.COMPONENT))
        self.assertIn("organisation", self.refused(ws.set_settings, {"gpu_speakers": True}))
        ws.settings.update(gpu_speakers=True, mode="faster")
        with mock.patch.object(gpu, "verified_for_use", return_value=True):
            self.assertEqual(ws._speaker_device(), ("cpu", None))
        self.assertFalse(ws.get_settings()["gpu_speakers"])     # shown as off

    def test_gpu_component_off_covers_whisper_on_the_card(self):
        # #248: the same policy governs the Whisper component and its use.
        from workspace import gpu
        ws = self.open(AllowGpuComponent=False)
        self.assertIn("graphics card", self.refused(ws.download_model, gpu.WHISPER_COMPONENT))
        ws.settings.update(gpu_speakers=True, mode="faster")
        with mock.patch.object(gpu.WHISPER, "status", return_value=("ready", "")), \
                mock.patch.object(gpu.WHISPER, "verified_for_use", return_value=True):
            self.assertEqual(ws._whisper_device("large-v3-turbo"), ("cpu", None))

    def test_microphone_off(self):
        ws = self.open(AllowMicrophoneRecording=False)
        ws.settings.update(microphone=True)                      # saved before the policy
        self.assertIn("organisation", self.refused(ws.microphone_devices))
        self.assertIn("organisation", self.refused(ws.test_microphone, None))
        self.assertIn("organisation", self.refused(ws.start_recording, None))
        self.assertIn("organisation", self.refused(ws.set_settings, {"microphone": True}))
        self.assertFalse(ws.get_settings()["microphone"])
        self.assertFalse(ws.recording_status()["enabled"])       # the page shows no recorder

    def test_a_recording_in_progress_stops_when_policy_turns_the_microphone_off(self):
        values = {}
        ws = self.open()
        ws.policy = policy.PolicySource(reader=lambda: (values, ()))
        rec = mock.Mock()
        ws._recording = rec
        with mock.patch.object(ws, "_finish_recording") as finish:
            ws._policy_tick()
            finish.assert_not_called()                           # allowed: carries on
            values["AllowMicrophoneRecording"] = False
            ws.policy.refresh()
            self.assertIn("organisation", self.refused(ws.resume_recording))
            ws._policy_tick()
        finish.assert_called_once()
        self.assertIs(finish.call_args.args[0], rec)
        self.assertEqual(finish.call_args.kwargs["event"][0], "policy_stop")
        ws._recording = None

    def test_an_unreadable_policy_refuses_new_recordings_but_does_not_cut_one_short(self):
        ws = self.open()
        ws.policy = policy.PolicySource(reader=lambda: ({}, policy.UNREADABLE))
        ws.settings.update(microphone=True)
        self.assertIn("organisation", self.refused(ws.start_recording, None))
        ws._recording = mock.Mock()
        with mock.patch.object(ws, "_finish_recording") as finish:
            ws._policy_tick()
        finish.assert_not_called()
        ws._recording = None

    def test_only_the_path_that_finishes_a_recording_notes_why(self):
        ws = self.open()
        rec = mock.Mock()
        ws._recording = None                                     # the user's Stop got there first
        self.assertFalse(ws._finish_recording(rec, event=("policy_stop", "x")))
        rec._event.assert_not_called()
        ws._recording = rec
        with mock.patch.object(ws._imports, "put"):
            ws._finish_recording(rec, event=("policy_stop", "x"))
        rec._event.assert_called_once_with("policy_stop", note="x")

    def test_watched_folders_and_deletion_authority_off(self):
        ws = self.open(AllowWatchedFolders=False, AllowDeleteOriginals=False)
        self.assertIn("Watched folders", self.refused(ws.add_watched_folder, "C:\\x", False))
        self.assertIn("Deleting original", self.refused(
            ws.set_folder_authority, "a" * 32, True, None))
        seen = {}
        with mock.patch("workspace.cleanup.run_job",
                        side_effect=lambda *a, **kw: seen.update(kw)), \
                mock.patch("workspace.intake.deletion_roots", return_value=["C:\\rec"]):
            ws._run("cleanup", "m" * 32, "job")
        self.assertEqual(seen["original_roots"], [])             # originals are kept

    def test_cleanup_reads_the_policy_again_before_deleting(self):
        values = {}
        ws = self.open()
        ws.policy = policy.PolicySource(reader=lambda: (dict(values), ()))
        ws.policy.current()                                      # cached: deletion allowed
        values["AllowDeleteOriginals"] = False                   # IT changes it since
        seen = {}
        with mock.patch("workspace.cleanup.run_job",
                        side_effect=lambda *a, **kw: seen.update(kw)), \
                mock.patch("workspace.intake.deletion_roots", return_value=["C:\\rec"]):
            ws._run("cleanup", "m" * 32, "job")
        self.assertEqual(seen["original_roots"], [])

    def test_imports_get_no_deletion_authority_while_withheld(self):
        from workspace import media
        ws = self.open(AllowDeleteOriginals=False)
        mid = ws.store.create_meeting("Watched")
        sid = ws.store.add_source(mid, original_path="C:\\rec\\a.wav", original_sha256="0" * 64,
                                  original_size=1, original_mtime_ns=1, audio_id="0" * 64,
                                  samples=16000, sample_rate=16000)
        with mock.patch.object(media, "import_recording", return_value=(mid, sid)), \
                mock.patch("workspace.intake.authority_for", return_value=True), \
                mock.patch.object(ws, "_plan_version"):
            ws._import_one("C:\\rec\\a.wav", folder_id="f" * 32)
        row = ws.store.source(sid)
        self.assertEqual((row["delete_original"], row["original_path"]), (0, None))

    def test_watch_loop_reads_no_folder_while_turned_off(self):
        from workspace import intake
        ws = self.open(AllowWatchedFolders=False)
        # A fake clock whose waits move it on. It starts just after boot (12 s),
        # where the monotonic clock is still small: the launch check must still
        # happen (it once waited until 5 minutes of uptime).
        clock = [12.0]

        def wait(timeout):
            clock[0] += timeout
            if clock[0] > 12.0 + intake.LAUNCH_SCAN_DELAY_S:
                ws._closing.set()
            return False
        ws._clock = lambda: clock[0]
        with mock.patch("workspace.intake.scan") as scan, \
                mock.patch.object(ws._scan_wake, "wait", side_effect=wait):
            ws._watch_loop()
        scan.assert_not_called()
        self.assertEqual(ws.last_scan, {"managed_off": True})

    def test_lined_up_watched_imports_wait_while_paused_or_turned_off(self):
        # #152: from the pause (or the policy change) nothing more is imported
        # from the folder; what was lined up waits until it is on again.
        from workspace import intake
        values = {}
        ws = self.open()
        ws.policy = policy.PolicySource(reader=lambda: (dict(values), ()))
        folder = Path(self.tmp.name) / "Recordings"
        folder.mkdir()
        fid = intake.add_folder(ws.store, str(folder))
        path = str(folder / "lined up.wav")
        with ws.store.tx() as db:                                  # its row, as a scan left it
            db.execute("INSERT INTO intake_seen (folder_id, key, size, mtime_ns, file_id, state, "
                       "first_seen, last_seen, content_fp) VALUES (?, ?, 1, 1, 0, 'importing', "
                       "0, 0, ?)", (fid, "k" * 64, "c" * 64))

        def drain(*items):
            for item in (*items, None):
                ws._imports.put(item)
            with mock.patch.object(ws, "_import_one") as imported, \
                    mock.patch.object(intake, "import_held",
                                      wraps=intake.import_held) as held:
                ws._intake_loop()
            return imported, held
        intake.set_active(ws.store, fid, False)                    # paused
        imported, held = drain((path, fid, "k" * 64, "c" * 64))
        imported.assert_not_called()
        held.assert_called_once_with(ws.store, fid, "k" * 64)
        row = ws.store._one("SELECT state, content_fp FROM intake_seen WHERE key = ?",
                            ("k" * 64,))
        self.assertEqual((row["state"], row["content_fp"]), ("waiting", None))   # for the resume
        intake.set_active(ws.store, fid, True)
        values["AllowWatchedFolders"] = False                      # IT turns it off
        ws.policy.refresh()
        imported, held = drain((path, fid, "k" * 64, "c" * 64), (path, None))
        held.assert_called_once()
        imported.assert_called_once_with(path, None)               # a recording added by hand
        values["AllowWatchedFolders"] = True
        ws.policy.refresh()
        imported, held = drain((path, fid, "k" * 64, "c" * 64))
        imported.assert_called_once_with(path, fid)                # on again: imported
        held.assert_not_called()
        ws.policy = policy.PolicySource(reader=lambda: ({}, policy.UNREADABLE))
        imported, held = drain((path, fid, "k" * 64, "c" * 64))
        imported.assert_not_called()                               # unreadable: grants nothing
        held.assert_called_once()

    @unittest.skipUnless(sys.platform == "win32",
                         "imports use Windows path rules (the app is Windows-only)")
    def test_turning_watched_folders_off_stops_a_scan_part_way(self):
        import os
        from workspace import intake
        values = {}
        ws = self.open()
        ws.policy = policy.PolicySource(reader=lambda: (dict(values), ()))
        folder = Path(self.tmp.name) / "Recordings"
        folder.mkdir()
        intake.add_folder(ws.store, str(folder))
        hashed = []

        def hasher(path):               # IT turns watched folders off meanwhile
            hashed.append(path)
            values["AllowWatchedFolders"] = False
            ws.policy.refresh()
            return f"{len(hashed):064d}"
        saved = (intake.STABLE_S, intake.STABLE_RECENT_S)
        intake.STABLE_S = intake.STABLE_RECENT_S = 0
        try:
            ws._scan_folders()                                     # baseline: empty
            for i in range(3):
                p = folder / f"new {i}.wav"
                p.write_bytes(b"synthetic")
                os.utime(p, (1e9, 1e9))
            ws._scan_folders()                                     # seen: waiting
            with mock.patch("workspace.media.sha256_file", side_effect=hasher):
                ws._scan_folders()
        finally:
            intake.STABLE_S, intake.STABLE_RECENT_S = saved
        self.assertEqual(len(hashed), 1)                           # stopped after the first
        self.assertEqual(ws._imports.qsize(), 1)                   # held when it comes up
        self.assertEqual(ws.last_scan, {"managed_off": True})

    def test_closing_runs_a_pending_clipboard_clear(self):
        from workspace import clipboard
        ws = self.open(ClipboardClearMinutes=5)
        ws._schedule_clipboard_clear(44, 300)                     # due in 5 minutes
        with mock.patch.object(clipboard, "clear_if_unchanged") as clear:
            ws.close()
        clear.assert_called_once_with(44)
        ws.close = lambda: None                                  # tearDown: already closed

    def test_clipboard_is_cleared_later_only_by_policy(self):
        from workspace import clipboard
        ws = self.open(ClipboardClearMinutes=5)
        out = {"combined": "text", "long": False}
        with mock.patch.object(ws, "_checked", return_value=out), \
                mock.patch.object(clipboard, "copy_text", return_value=42), \
                mock.patch.object(clipboard, "clear_if_unchanged") as clear:
            self.assertEqual(ws.copy_handoff("m" * 32, "t", "combined", "x")["clears_in_minutes"],
                             5)
            ws._run_clipboard_clears()                           # not due yet
            clear.assert_not_called()
            ws._flush_clipboard_clears()                         # closing runs pending clears
            clear.assert_called_once_with(42)
            ws._schedule_clipboard_clear(43, 0)                  # due now: the next tick
            ws._policy_tick()
            clear.assert_called_with(43)
            self.assertEqual(ws._clipboard_clears, [])

    def test_handoff_file_name_and_folder(self):
        import tempfile
        from workspace import handoff
        ws = self.open(HandoffFilenameIncludesTitle=True,
                       HandoffDefaultFolder=tempfile.gettempdir())
        mid = ws.store.create_meeting("Client: budget review")
        built = {"filename": "Meeting transcript 2026-10-16 10-00.md"}
        with mock.patch.object(handoff, "build", return_value=built), \
                mock.patch.object(ws.templates, "get", return_value={}), \
                mock.patch.object(handoff, "options", return_value={}):
            self.assertEqual(ws.handoff_preview(mid, "t")["filename"],
                             "Client  budget review - transcript.md")
            self.assertEqual(ws.handoff_preview(mid, "t", True)["filename"],
                             "Meeting transcript 2026-10-16 10-00.md")   # anonymised: no title
        # Invisible format characters (a right-to-left override) cannot disguise the name.
        self.assertEqual(handoff.title_filename("Notes\u202egnp.exe\x7f\x85", "x"),
                         "Notes gnp.exe - transcript.md")
        self.assertEqual(ws.handoff_folder(), tempfile.gettempdir())

    # -- Copy and open the assistant (#109) ------------------------------------------

    def test_assistant_default_policy_and_fail_closed(self):
        from workspace import handoff
        url, name = "HandoffAssistantUrl", "HandoffAssistantName"
        cases = (({}, handoff.DEFAULT_ASSISTANT, None),
                 ({name: "Copilot", url: "https://copilot.example.org/chat"},
                  ("Copilot", "https://copilot.example.org/chat"),
                  {"state": "set", "name": "Copilot", "host": "copilot.example.org"}),
                 ({"AllowHandoffAssistant": False}, None, {"state": "off"}),
                 # One of the two alone: withheld, never the ChatGPT default.
                 ({name: "Copilot"}, None, {"state": "invalid"}),
                 ({url: "https://copilot.example.org/"}, None, {"state": "invalid"}))
        ws = self.open()
        for values, expected, setting in cases:
            ws.policy = policy.PolicySource(reader=lambda values=values: (values, ()))
            self.assertEqual(ws._assistant(), expected, values)
            self.assertEqual(ws.managed()["assistant"], setting, values)
        # A bad value in any of the three withholds it; so does an unreadable
        # key, which Settings words as unreadable, not as IT's choice or a bad value.
        both = {name: "Copilot", url: "https://copilot.example.org/"}
        for values, invalid, state in (({name: "Copilot"}, (url,), "invalid"),
                                       ({url: both[url]}, (name,), "invalid"),
                                       (both, ("AllowHandoffAssistant",), "invalid"),
                                       ({}, policy.UNREADABLE, "unreadable")):
            p = policy.Policy(values, invalid)
            self.assertIsNone(ws._assistant(p), invalid)
            self.assertEqual(ws._assistant_setting(p), {"state": state}, invalid)

    def test_the_preview_offers_no_assistant_when_policy_withholds_it(self):
        # What the page lays out by: None (the layout before #109) when policy
        # turns Copy and open off or sets it badly, else the name and host.
        from workspace import handoff
        ws = self.open()
        mid = ws.store.create_meeting("Synthetic meeting")
        for values, expected in (({}, {"name": "ChatGPT", "host": "chatgpt.com"}),
                                 ({"AllowHandoffAssistant": False}, None),
                                 ({"HandoffAssistantName": "Copilot"}, None)):
            ws.policy = policy.PolicySource(reader=lambda values=values: (values, ()))
            with mock.patch.object(handoff, "build", return_value={"filename": "x.md"}), \
                    mock.patch.object(ws.templates, "get", return_value={}), \
                    mock.patch.object(handoff, "options", return_value={}):
                self.assertEqual(ws.handoff_preview(mid, "t")["assistant"], expected, values)

    def copy_and_open(self, ws, **kw):
        from workspace import clipboard
        out = {"combined": "MARKER-TRANSCRIPT text", "long": False, **kw.pop("out", {})}
        with mock.patch.object(ws, "_checked", return_value=out) as checked, \
                mock.patch.object(clipboard, "copy_text", **kw) as copy:
            try:
                return ws.copy_and_open_assistant("m" * 32, "minutes", "digest"), copy, checked
            except Exception as exc:
                return exc, copy, checked

    def test_copy_then_open_the_fixed_address(self):
        ws = self.open(ClipboardClearMinutes=5)
        events = []
        ws._opener = lambda u: events.append(("open", u))
        r, copy, checked = self.copy_and_open(
            ws, side_effect=lambda text: events.append(("copy", text)) or 7)
        self.assertEqual(events, [("copy", "MARKER-TRANSCRIPT text"),
                                  ("open", "https://chatgpt.com/")])     # copy first, address as is
        checked.assert_called_once_with("m" * 32, "minutes", "combined", "digest", False)
        self.assertEqual(r, {"chars": 22, "long": False, "clears_in_minutes": 5, "opened": True,
                             "name": "ChatGPT", "host": "chatgpt.com"})
        self.assertEqual(ws._clipboard_clears[0][1], 7)                   # the clear still applies

    def test_nothing_opens_without_a_good_copy(self):
        from workspace.app import WorkspaceError
        from workspace import clipboard
        ws = self.open()
        opened = []
        ws._opener = opened.append
        with mock.patch.object(ws, "_checked", side_effect=WorkspaceError("changed")):
            self.assertIn("changed", self.refused(ws.copy_and_open_assistant, "m" * 32, "t", "x"))
        r, copy, _ = self.copy_and_open(ws, side_effect=clipboard.ClipboardError("busy"))
        self.assertIn("busy", str(r))
        self.assertEqual(opened, [])
        # A refused copy did not use up the 3 seconds: the next click works.
        r, copy, _ = self.copy_and_open(ws, return_value=1)
        self.assertEqual(opened, ["https://chatgpt.com/"])

    def test_policy_is_read_again_at_the_click(self):
        values = {"HandoffAssistantName": "Copilot",
                  "HandoffAssistantUrl": "https://copilot.example.org/"}
        ws = self.open()
        ws.policy = policy.PolicySource(reader=lambda: (dict(values), ()))
        self.assertEqual(ws._assistant()[0], "Copilot")                   # cached for 15 minutes
        opened = []
        ws._opener = opened.append
        values["AllowHandoffAssistant"] = False                          # IT turns it off since
        r, copy, _ = self.copy_and_open(ws, return_value=1)
        self.assertIn("turned this off", str(r))
        copy.assert_not_called()
        values["AllowHandoffAssistant"] = True
        del values["HandoffAssistantName"]                               # or sets it badly
        r, copy, _ = self.copy_and_open(ws, return_value=1)
        self.assertIn("is not valid", str(r))
        self.assertNotIn("turned this off", str(r))
        copy.assert_not_called()
        values["HandoffAssistantName"] = "Copilot"
        del values["AllowHandoffAssistant"]
        values["HandoffAssistantUrl"] = "https://other.example.org/"     # or points it elsewhere
        r, _, _ = self.copy_and_open(ws, return_value=1)
        self.assertEqual(opened, ["https://other.example.org/"])
        ws.policy = policy.PolicySource(reader=lambda: ({}, policy.UNREADABLE))
        ws._assistant_opened_at = None
        r, copy, _ = self.copy_and_open(ws, return_value=1)
        self.assertIn("could not be read", str(r))                        # unreadable: nothing
        self.assertNotIn("turned this off", str(r))
        copy.assert_not_called()
        self.assertEqual(len(opened), 1)

    def test_one_opening_every_three_seconds(self):
        from workspace import app as app_module
        ws = self.open()
        now = [100.0]
        ws._clock = lambda: now[0]
        opened = []
        ws._opener = opened.append
        self.copy_and_open(ws, return_value=1)
        now[0] += app_module.ASSISTANT_PAUSE_S - 0.1
        r, copy, _ = self.copy_and_open(ws, return_value=1)
        self.assertIn("Already opening", str(r))
        copy.assert_not_called()
        now[0] += 0.1
        self.copy_and_open(ws, return_value=1)
        self.assertEqual(len(opened), 2)

    def test_a_browser_that_cannot_open_keeps_the_copy(self):
        ws = self.open()

        def fail(url):
            raise OSError("no association")
        ws._opener = fail
        r, copy, _ = self.copy_and_open(ws, return_value=1)
        copy.assert_called_once()
        self.assertFalse(r["opened"])

    def test_without_an_opener_only_windows_opens(self):
        import types
        ws = self.open()
        windows = types.SimpleNamespace(startfile=mock.Mock())
        with mock.patch("workspace.app.os", windows):
            self.copy_and_open(ws, return_value=1)
        windows.startfile.assert_called_once_with("https://chatgpt.com/")
        ws._assistant_opened_at = None
        with mock.patch("workspace.app.os", types.SimpleNamespace()):    # no startfile
            r, copy, _ = self.copy_and_open(ws, return_value=1)
        self.assertIn("only on Windows", str(r))
        copy.assert_not_called()

    def test_diagnostics_show_assistant_values_only_as_set(self):
        ws = self.open(HandoffAssistantName="Markername", AllowHandoffAssistant=True,
                       HandoffAssistantUrl="https://markerhost.example.org/markerpath")
        text = ws.diagnostics()
        self.assertIn("HandoffAssistantName=set", text)
        self.assertIn("HandoffAssistantUrl=set", text)
        self.assertIn("AllowHandoffAssistant=True", text)
        self.assertNotIn("marker", text.lower())
        preview = ws._assistant_shown(ws._assistant())
        self.assertEqual(preview, {"name": "Markername", "host": "markerhost.example.org"})

    def test_network_sources_only_by_policy(self):
        from workspace import app as app_module
        for values, expected in (({}, False), ({"AllowNetworkSources": True}, True)):
            if expected:                                     # second pass: a fresh workspace
                self.ws.close()
                self.tmp.cleanup()
            ws = self.open(**values)
            with mock.patch.object(app_module, "validate_input_file",
                                   side_effect=app_module.UnsafePathError("no")) as check:
                ws.import_paths(["Z:\\meeting.wav"])
            self.assertIs(check.call_args.kwargs["allow_network"], expected)

    def test_without_policy_nothing_changes(self):
        ws = self.open()
        managed = ws.get_settings()["managed"]
        self.assertEqual(managed["managed"], [])
        self.assertTrue(all(v for k, v in managed["allow"].items()
                            if k not in ("AllowNetworkSources", "HandoffFilenameIncludesTitle")))


class PageModelGateTests(unittest.TestCase):
    """#111 B7a: what Setup and Downloads offer for each card under policy.
    The page helpers are taken from the page's scripts (workspace/ui) by name and run in
    Node.js, as tests/test_settings_policy_names.py does."""

    HELPERS = ("allowed", "modelDownloadsOff", "downloadOff")
    CASES = {
        "all allowed": {},
        "user models off": {"AllowUserModels": False},
        "downloads off": {"AllowModelDownloads": False},
        "gpu off": {"AllowGpuComponent": False},
        "user models and gpu off": {"AllowUserModels": False, "AllowGpuComponent": False},
    }

    def simulate(self):
        import json
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js not installed")
        app_js = page_scripts.source()
        parts = []
        for name in self.HELPERS:
            found = re.search(r"^function " + name + r"\(.*?^\}$", app_js, re.M | re.S)
            self.assertIsNotNone(found, f"{name} not found at the top level of the page scripts")
            parts.append(found.group(0))
        script = "\n".join(parts) + "\nconst cases = " + json.dumps(self.CASES) + r""";
const state = { managed: null };
const result = {};
for (const [k, allow] of Object.entries(cases)) {
  state.managed = { allow: Object.assign({ AllowModelDownloads: true, AllowUserModels: true,
                                           AllowGpuComponent: true }, allow) };
  result[k] = { speech: downloadOff({ role: "speech" }),
                speakers: downloadOff({ role: "speakers" }),
                gpu: downloadOff({ role: "gpu" }), token: modelDownloadsOff() };
}
console.log(JSON.stringify(result));
"""
        done = subprocess.run([node, "-"], input=script, capture_output=True, text=True,
                              timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_user_models_off_stops_model_downloads_but_not_the_gpu_components(self):
        r = self.simulate()
        off = {"speech": True, "speakers": True, "gpu": False, "token": True}
        self.assertEqual(r["all allowed"],
                         {"speech": False, "speakers": False, "gpu": False, "token": False})
        self.assertEqual(r["user models off"], off)
        self.assertEqual(r["downloads off"],
                         {"speech": True, "speakers": True, "gpu": True, "token": True})
        self.assertEqual(r["gpu off"],
                         {"speech": False, "speakers": False, "gpu": True, "token": False})
        self.assertEqual(r["user models and gpu off"], {**off, "gpu": True})

    def test_the_cards_and_the_token_field_use_the_helpers(self):
        app_js = page_scripts.source()
        self.assertEqual(app_js.count("else if (downloadOff(c))\n    action = itManaged(c);"), 2)
        self.assertIn('$("s-token").disabled = modelDownloadsOff();', app_js)
        self.assertIn("|| isQueued(speaker) || modelDownloadsOff();", app_js)
        self.assertEqual(app_js.count('allowed("AllowModelDownloads")'), 2,
                         "a page reader decides model downloads outside the two helpers")


if __name__ == "__main__":
    unittest.main()
