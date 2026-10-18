# Keeping the computer awake while recording (workspace/power.py).
#   python -m unittest discover -s tests -v

import ctypes
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace import power  # noqa: E402  (stdlib only)


class FakeKernel32:
    """Counts power requests the way Windows would."""

    def __init__(self, create=42, set_ok=1):
        self.handle, self.set_ok = create, set_ok
        self.active, self.open, self.reasons = 0, 0, []

    def create(self, reason):
        self.reasons.append(reason._obj.Reason.SimpleReasonString)
        if self.handle and self.handle != power.INVALID_HANDLE_VALUE:
            self.open += 1
        return self.handle

    def set(self, handle, kind):
        assert kind == power.POWER_REQUEST_SYSTEM_REQUIRED
        self.active += bool(self.set_ok)
        return self.set_ok

    def clear(self, handle, kind):
        self.active -= 1
        return 1

    def close(self, handle):
        self.open -= 1
        return 1


class KeepAwakeTests(unittest.TestCase):
    def test_held_once_and_released_once(self):
        api = FakeKernel32()
        k = power.KeepAwake(api)
        self.assertTrue(k.hold())
        self.assertTrue(k.hold())                       # already held: no second request
        self.assertEqual((k.held, api.active, api.open), (True, 1, 1))
        self.assertEqual(api.reasons, [power.REASON])
        k.release()
        k.release()                                     # repeat is harmless
        self.assertEqual((k.held, api.active, api.open), (False, 0, 0))

    def test_a_refused_request_leaves_nothing_open(self):
        for api in (FakeKernel32(create=None), FakeKernel32(create=power.INVALID_HANDLE_VALUE),
                    FakeKernel32(set_ok=0)):
            k = power.KeepAwake(api)
            self.assertFalse(k.hold())
            self.assertEqual((k.held, api.active, api.open), (False, 0, 0))
            k.release()

    def test_an_unavailable_api_is_not_fatal(self):
        class Broken:
            def create(self, reason):
                raise OSError("no power API")
        k = power.KeepAwake(Broken())
        self.assertFalse(k.hold())
        self.assertFalse(k.held)

    def test_any_error_is_contained_and_no_handle_is_left_open(self):
        # A recording must start and end whatever the power calls do.
        def failing(real, close_first):
            def boom(*args):
                if close_first:
                    real(*args)              # closed, then reports an error
                raise ValueError("unexpected")
            return boom

        for step in ("create", "set", "clear", "close"):
            api = FakeKernel32()
            setattr(api, step, failing(getattr(api, step), step == "close"))
            k = power.KeepAwake(api)
            with self.subTest(step):
                held = k.hold()
                self.assertEqual(held, step in ("clear", "close"))
                k.release()
                self.assertFalse(k.held)
                self.assertEqual(api.open, 0)

    @unittest.skipUnless(os.name == "nt", "Windows power requests")
    def test_windows_accepts_the_request(self):
        # The real calls: checks the ctypes signatures and REASON_CONTEXT layout.
        # The request lasts only for this test (Windows drops it at exit anyway).
        k = power.KeepAwake()
        try:
            self.assertTrue(k.hold())
        finally:
            k.release()
        self.assertFalse(k.held)


class FakeScheme:
    """A power scheme's lid action, plugged in (True) and on battery (False)."""

    SCHEME = "6cb4fe25-1ec4-4e4a-a4a5-24e60a60b5d3"

    def __init__(self, ac=1, dc=1, write_rc=0, apply_rc=0, take=True, watch=None):
        self.values = {True: ac, False: dc}
        self.write_rc, self.apply_rc, self.take = write_rc, apply_rc, take
        self.writes, self.watch = [], watch      # watch: a file that must exist before writing
        self.active = self.SCHEME
        self.targets, self.applied = [], []      # the scheme each write and apply went to
        self.fail_side = None                    # a side whose writes fail (True or False)

    def active_scheme(self):
        return self.active

    def read(self, scheme, plugged_in):
        return self.values[plugged_in]

    def write(self, scheme, plugged_in, value):
        if self.watch is not None and value == power.DO_NOTHING:
            assert self.watch.exists(), "the restore file must be saved before any change"
        self.writes.append((plugged_in, value))
        self.targets.append(scheme)
        if self.write_rc or plugged_in == self.fail_side:
            return self.write_rc or 5
        if self.take:
            self.values[plugged_in] = value
        return 0

    def apply(self, scheme):
        self.applied.append(scheme)
        return self.apply_rc


class LidSwitchTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "power_restore.json"

    def tearDown(self):
        self.tmp.cleanup()

    def switch(self, api, managed=lambda: False):
        return power.LidSwitch(self.path, api=api, managed=managed)

    def test_sets_do_nothing_and_puts_it_back(self):
        api = FakeScheme(ac=1, dc=2, watch=self.path)
        sw = self.switch(api)
        self.assertEqual(sw.override(), (True, "set"))
        self.assertEqual(api.values, {True: 0, False: 0})
        self.assertTrue(sw.active)
        sw.restore()
        self.assertEqual(api.values, {True: 1, False: 2})
        self.assertFalse(sw.active)
        sw.restore()                                     # repeat is harmless
        self.assertEqual(api.values, {True: 1, False: 2})

    def test_a_crash_is_put_right_at_the_next_start(self):
        api = FakeScheme(ac=1, dc=1)
        self.switch(api).override()                      # the app dies here
        self.switch(api).restore()                       # the next start
        self.assertEqual(api.values, {True: 1, False: 1})
        self.assertFalse(self.path.exists())

    def test_a_change_the_user_made_meanwhile_is_left_alone(self):
        api = FakeScheme(ac=1, dc=1)
        sw = self.switch(api)
        sw.override()
        api.values[True] = 3                             # the user chose "Shut down"
        sw.restore()
        self.assertEqual(api.values, {True: 3, False: 1})

    def test_stands_aside_when_the_organisation_manages_the_lid(self):
        def broken():
            raise OSError("registry unreadable")
        for managed in (lambda: True, broken):           # cannot tell: also stands aside
            api = FakeScheme()
            self.assertEqual(self.switch(api, managed).override(), (False, "managed"))
            self.assertEqual(api.writes, [])
            self.assertFalse(self.path.exists())

    def test_already_do_nothing_changes_nothing(self):
        api = FakeScheme(ac=0, dc=0)
        self.assertEqual(self.switch(api).override(), (True, "already"))
        self.assertEqual(api.writes, [])
        self.assertFalse(self.path.exists())

    def test_a_refused_or_ineffective_change_is_undone(self):
        for api in (FakeScheme(write_rc=5), FakeScheme(apply_rc=5), FakeScheme(take=False)):
            sw = self.switch(api)
            self.assertEqual(sw.override(), (False, "refused"))
            self.assertEqual(api.values, {True: 1, False: 1})
            self.assertFalse(self.path.exists())

    def test_nothing_changes_if_the_restore_file_cannot_be_saved(self):
        api = FakeScheme()
        sw = power.LidSwitch(Path(self.tmp.name) / "missing" / "r.json", api=api,
                             managed=lambda: False)
        self.assertEqual(sw.override(), (False, "refused"))
        self.assertEqual(api.writes, [])

    def test_a_damaged_restore_file_is_removed_without_changes(self):
        for text in ("not json", '{"scheme": "s", "ac": 9, "dc": 1}', '{"ac": true}'):
            self.path.write_text(text, encoding="utf-8")
            api = FakeScheme(ac=0, dc=0)
            self.switch(api).restore()
            self.assertEqual(api.writes, [])
            self.assertFalse(self.path.exists())

    def test_a_failed_restore_keeps_the_file_for_the_next_start(self):
        api = FakeScheme()
        sw = self.switch(api)
        sw.override()
        api.write_rc = 5
        sw.restore()
        self.assertTrue(self.path.exists())
        api.write_rc = 0
        sw.restore()
        self.assertEqual(api.values, {True: 1, False: 1})

    def test_managed_reads_group_policy_and_intune_values_only(self):
        def reader(values):
            return lambda key, name: values.get((key, name))
        self.assertFalse(power.lid_managed(reader({})))
        self.assertTrue(power.lid_managed(reader({(power.GP_LID_KEY, "DCSettingIndex"): 1})))
        self.assertTrue(power.lid_managed(reader(
            {(power.MDM_POWER_KEY, "SelectLidCloseActionOnBattery"): 1})))
        self.assertFalse(power.lid_managed(reader(           # Intune's leftover marker
            {(power.MDM_POWER_KEY, "SelectLidCloseActionOnBattery_LastWrite"): 1})))
        self.assertEqual(power.GP_LID_KEY.count(chr(92)), 5)  # a real registry path

    @unittest.skipUnless(os.name == "nt", "Windows power scheme")
    def test_windows_reads_the_lid_action(self):
        # Read-only: never writes the real power scheme.
        api = power._PowrProf()
        scheme = api.active_scheme()
        self.assertRegex(scheme, r"^[0-9a-f-]{36}$")
        for plugged_in in (True, False):
            self.assertIn(api.read(scheme, plugged_in), (0, 1, 2, 3))


class FakeNotify:
    def __init__(self, rc=0):
        self.rc, self.callback, self.unregistered = rc, None, 0

    def PowerSettingRegisterNotification(self, guid, flags, params, handle):
        assert flags == power.DEVICE_NOTIFY_CALLBACK
        self.callback = params._obj.Callback
        handle._obj.value = 1234
        return self.rc

    def PowerSettingUnregisterNotification(self, handle):
        self.unregistered += 1
        return 0


def lid_setting(open_):
    s = power._PowerSetting()
    s.PowerSetting = power.GUID.of(power.LIDSWITCH_STATE)
    s.DataLength = 4
    s.Data[0] = 1 if open_ else 0
    return s


@unittest.skipUnless(os.name == "nt", "Windows power notifications")
class LidWatchTests(unittest.TestCase):
    def test_reports_open_and_closed_until_stopped(self):
        seen, api = [], FakeNotify()
        w = power.LidWatch(seen.append, api=api)
        self.assertTrue(w.start())
        for state in (True, False, True):
            s = lid_setting(state)
            w._notified(None, power.PBT_POWERSETTINGCHANGE, ctypes.addressof(s))
        w._notified(None, 0x0004, None)                  # something else: ignored
        self.assertEqual(seen, [True, False, True])
        w.stop()
        w.stop()
        self.assertEqual(api.unregistered, 1)

    def test_a_refused_registration_or_failing_handler_is_not_fatal(self):
        self.assertFalse(power.LidWatch(print, api=FakeNotify(rc=5)).start())

        def boom(open_):
            raise ValueError("handler failed")
        w = power.LidWatch(boom, api=FakeNotify())
        w.start()
        s = lid_setting(False)
        self.assertEqual(w._notified(None, power.PBT_POWERSETTINGCHANGE, ctypes.addressof(s)), 0)

    def test_windows_accepts_the_registration(self):
        w = power.LidWatch(lambda open_: None)
        started = w.start()                              # a laptop reports the lid at once
        w.stop()
        self.assertIsInstance(started, bool)


class LidSwitchReviewTests(unittest.TestCase):
    """The #188 security review (F1 to F7 and nits)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "power_restore.json"

    def tearDown(self):
        self.tmp.cleanup()

    def switch(self, api, managed=lambda: False):
        return power.LidSwitch(self.path, api=api, managed=managed)

    def test_a_file_that_cannot_be_deleted_never_raises(self):
        # F1: the values go back; the file stays for the next try.
        from unittest import mock
        api = FakeScheme(ac=1, dc=2)
        sw = self.switch(api)
        sw.override()
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            sw.restore()
        self.assertEqual(api.values, {True: 1, False: 2})
        self.assertTrue(self.path.exists())
        self.assertEqual(sw.problem, "failed")
        sw.restore()                                     # unlocked later
        self.assertFalse(self.path.exists())
        self.assertIsNone(sw.problem)

    def test_a_brief_read_error_keeps_the_record(self):
        # F2: an unreadable file is not a damaged one.
        from unittest import mock
        api = FakeScheme(ac=1, dc=2)
        sw = self.switch(api)
        sw.override()
        with mock.patch("workspace.power.open", side_effect=PermissionError("locked"),
                        create=True):
            sw.restore()
        self.assertTrue(self.path.exists())
        self.assertEqual(api.values, {True: 0, False: 0})
        self.assertEqual(sw.problem, "failed")
        sw.restore()
        self.assertEqual(api.values, {True: 1, False: 2})

    def test_a_waiting_restore_is_never_overwritten(self):
        # F3: AC went back, DC failed; the next recording must not save
        # {ac 1, dc 0} over the real DC original.
        api = FakeScheme(ac=1, dc=2)
        sw = self.switch(api)
        sw.override()
        api.fail_side = False
        sw.restore()
        self.assertEqual(api.values, {True: 1, False: 0})
        saved = self.path.read_text(encoding="utf-8")
        self.assertEqual(sw.override(), (False, "refused"))
        self.assertEqual(self.path.read_text(encoding="utf-8"), saved)
        api.fail_side = None
        self.assertEqual(sw.override(), (True, "set"))   # put back first, then set again
        sw.restore()
        self.assertEqual(api.values, {True: 1, False: 2})

    def test_an_unreadable_registry_counts_as_managed(self):
        # F5: only "not there" means not managed.
        def denied(key, name):
            raise PermissionError("access denied")
        api = FakeScheme()
        sw = self.switch(api, managed=lambda: power.lid_managed(denied))
        self.assertEqual(sw.override(), (False, "managed"))
        self.assertEqual(api.writes, [])

    @unittest.skipUnless(os.name == "nt", "Windows registry")
    def test_registry_reads_say_not_set_only_when_missing(self):
        from unittest import mock
        self.assertIsNone(power._registry_value(power.GP_LID_KEY + "-absent", "X"))
        with mock.patch("winreg.OpenKey", side_effect=PermissionError("access denied")):
            with self.assertRaises(PermissionError):
                power._registry_value(power.GP_LID_KEY, "ACSettingIndex")

    def test_restore_targets_the_saved_scheme_and_applies_only_if_active(self):
        api = FakeScheme(ac=1, dc=2)
        sw = self.switch(api)
        sw.override()
        api.active = "11111111-2222-3333-4444-555555555555"   # the user switched plans
        api.targets.clear()
        api.applied.clear()
        sw.restore()
        self.assertEqual(set(api.targets), {FakeScheme.SCHEME})
        self.assertEqual(api.applied, [])
        api2 = FakeScheme(ac=1, dc=2)
        sw2 = power.LidSwitch(Path(self.tmp.name) / "r2.json", api=api2, managed=lambda: False)
        sw2.override()
        api2.applied.clear()
        sw2.restore()
        self.assertEqual(api2.applied, [FakeScheme.SCHEME])

    def test_a_scheme_that_is_not_a_guid_is_a_lost_record(self):
        self.path.write_text('{"scheme": "not-a-guid", "ac": 1, "dc": 1}', encoding="utf-8")
        api = FakeScheme(ac=0, dc=0)
        sw = self.switch(api)
        sw.restore()
        self.assertEqual((api.writes, self.path.exists(), sw.problem), ([], False, "lost"))

    def test_an_oversized_or_deeply_nested_record_is_lost_without_raising(self):
        # R2: "never raises" holds for any file content.
        for text in ("[" * 100000 + "]" * 100000, " " * 5000 + "{}"):
            self.path.write_text(text, encoding="utf-8")
            api = FakeScheme(ac=0, dc=0)
            sw = self.switch(api)
            sw.restore()
            self.assertEqual((api.writes, self.path.exists(), sw.problem), ([], False, "lost"))

    def test_a_record_deleted_meanwhile_is_put_back_from_memory(self):
        api = FakeScheme(ac=1, dc=2)
        sw = self.switch(api)
        sw.override()
        self.path.unlink()                               # removed by something else
        self.assertTrue(sw.active)
        sw.restore()
        self.assertEqual(api.values, {True: 1, False: 2})
        self.assertFalse(sw.active)

    def test_the_copy_in_memory_also_blocks_a_new_override(self):
        # N1: record deleted, a half-failed restore from memory, a new recording.
        api = FakeScheme(ac=1, dc=2)
        sw = self.switch(api)
        sw.override()
        self.path.unlink()
        api.fail_side = False
        sw.restore()                                     # {1, 0}: battery still waiting
        self.assertEqual(sw.override(), (False, "refused"))
        api.fail_side = None
        sw.restore()
        self.assertEqual(api.values, {True: 1, False: 2})

    def test_a_damaged_file_gives_way_to_the_copy_in_memory(self):
        # N2: the right values at once, and no "could not put back" notice.
        api = FakeScheme(ac=1, dc=2)
        sw = self.switch(api)
        sw.override()
        self.path.write_text("{damaged", encoding="utf-8")
        sw.restore()
        self.assertEqual((api.values, sw.problem, sw.active), ({True: 1, False: 2}, None, False))

    def test_an_original_out_of_range_is_never_changed(self):
        api = FakeScheme(ac=7, dc=1)
        self.assertEqual(self.switch(api).override(), (False, "refused"))
        self.assertEqual((api.writes, self.path.exists()), ([], False))


@unittest.skipUnless(os.name == "nt", "Windows power notifications")
class LidWatchReviewTests(unittest.TestCase):
    def test_the_callback_outlives_the_subscription(self):
        # F7: Windows might still call it as the subscription ends.
        api = FakeNotify()
        w = power.LidWatch(lambda open_: None, api=api)
        w.start()
        keep = w._keep
        self.assertTrue(w.stop())
        self.assertIs(w._keep, keep)
        self.assertIn(keep, power._ALIVE)
        w.start()
        self.assertIs(w._keep, keep)                     # made once, reused

    def test_a_refused_unsubscribe_is_reported(self):
        class Refusing(FakeNotify):
            def PowerSettingUnregisterNotification(self, handle):
                return 5
        w = power.LidWatch(lambda open_: None, api=Refusing())
        w.start()
        self.assertFalse(w.stop())


class OtherSchemes(FakeScheme):
    """A computer with one power scheme: any other GUID is not found."""

    def read(self, scheme, plugged_in):
        if scheme != self.SCHEME:
            raise OSError(2, "PowerRead")
        return super().read(scheme, plugged_in)

    def write(self, scheme, plugged_in, value):
        if scheme != self.SCHEME:
            self.writes.append((plugged_in, value))
            return 2
        return super().write(scheme, plugged_in, value)


class RestoreOnlyTests(unittest.TestCase):
    """#191: the uninstaller's restore (python -m workspace --restore-lid) is
    LidSwitch.restore() once, with the same rules, on a fake power scheme."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "power_restore.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_restore(self, api, running=lambda: False):
        return power.restore_only(self.path, running, api=api)

    def record(self, ac=1, dc=2, scheme=FakeScheme.SCHEME):
        import json
        self.path.write_text(json.dumps({"scheme": scheme, "ac": ac, "dc": dc}),
                             encoding="utf-8")

    def test_puts_back_what_a_crash_left(self):
        api = OtherSchemes(ac=1, dc=2)
        power.LidSwitch(self.path, api=api, managed=lambda: False).override()  # then a crash
        self.assertEqual(api.values, {True: 0, False: 0})
        self.assertEqual(self.run_restore(api), "restored")
        self.assertEqual(api.values, {True: 1, False: 2})
        self.assertEqual(api.applied[-1], FakeScheme.SCHEME)
        self.assertFalse(self.path.exists())
        self.assertEqual(power.RESTORE_CODES["restored"], 0)

    def test_nothing_waiting_touches_nothing(self):
        api = OtherSchemes(ac=0, dc=0)
        self.assertEqual(self.run_restore(api), "none")
        self.assertEqual((api.writes, api.applied), ([], []))
        self.assertEqual(power.RESTORE_CODES["none"], 0)

    def test_a_side_the_user_changed_is_left_alone(self):
        self.record(ac=1, dc=2)
        api = OtherSchemes(ac=3, dc=0)                   # the user chose "Shut down" plugged in
        self.assertEqual(self.run_restore(api), "restored")
        self.assertEqual(api.values, {True: 3, False: 2})
        self.assertEqual(api.writes, [(False, 2)])

    def test_the_open_app_owns_the_lid_switch(self):
        self.record()
        api = OtherSchemes(ac=0, dc=0)
        self.assertEqual(self.run_restore(api, running=lambda: True), "running")
        self.assertEqual((api.writes, api.values), ([], {True: 0, False: 0}))
        self.assertTrue(self.path.exists())
        self.assertEqual(power.RESTORE_CODES["running"], 3)

    def test_a_failed_restore_keeps_the_record_for_the_next_start(self):
        self.record()
        api = OtherSchemes(ac=0, dc=0, write_rc=5)
        self.assertEqual(self.run_restore(api), "failed")
        self.assertTrue(self.path.exists())
        self.assertEqual(power.RESTORE_CODES["failed"], 1)

        def broken():
            raise OSError("cannot tell")
        api = OtherSchemes(ac=0, dc=0)
        self.assertEqual(self.run_restore(api, running=broken), "failed")
        self.assertEqual(api.writes, [])
        self.assertTrue(self.path.exists())

    def test_damaged_or_malicious_records_leave_the_setting_alone(self):
        # Wrong types, values the app never saves, a scheme that is not a GUID,
        # oversized or deeply nested files and bytes that are not text: nothing
        # is written, and the record is removed (the user's decision on #188).
        import json

        def rec(**values):
            return json.dumps({"scheme": FakeScheme.SCHEME, **values}).encode()
        cases = {
            "not json": b"{damaged",
            "not utf-8": b"\xff\xfe\x00{",
            "a list": b"[1, 2]",
            "null": b"null",
            "no scheme": b'{"ac": 1, "dc": 1}',
            "scheme a number": b'{"scheme": 7, "ac": 1, "dc": 1}',
            "scheme a path": b'{"scheme": "..\\\\..\\\\x", "ac": 1, "dc": 1}',
            "scheme empty": b'{"scheme": "", "ac": 1, "dc": 1}',
            "value true": rec(ac=True, dc=1),
            "value 1.0": rec(ac=1.0, dc=1),
            "value text": rec(ac="1", dc=1),
            "value null": rec(ac=None, dc=1),
            "value -1": rec(ac=-1, dc=1),
            "value 4": rec(ac=1, dc=4),
            "value huge": rec(ac=2 ** 64, dc=1),
            "value missing": rec(ac=1),
            "too large": b" " * (power.MAX_RESTORE_BYTES + 1) + rec(ac=1, dc=1),
            "too deep": b"[" * 100000 + b"]" * 100000,
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.path.write_bytes(raw)
                api = OtherSchemes(ac=0, dc=0)
                self.assertEqual(self.run_restore(api), "damaged")
                self.assertEqual((api.writes, api.applied), ([], []))
                self.assertEqual(api.values, {True: 0, False: 0})
                self.assertFalse(self.path.exists())
        self.assertEqual(power.RESTORE_CODES["damaged"], 2)

    def test_a_scheme_this_computer_does_not_have_changes_nothing(self):
        # A well-formed GUID that names no scheme here: nothing is written to
        # the active scheme, and the record is kept, as at the app's own start.
        self.record(scheme="11111111-2222-3333-4444-555555555555")
        api = OtherSchemes(ac=0, dc=0)
        self.assertEqual(self.run_restore(api), "failed")
        self.assertEqual((api.writes, api.applied), ([], []))
        self.assertEqual(api.values, {True: 0, False: 0})
        self.assertTrue(self.path.exists())

    def test_a_record_for_another_existing_scheme_writes_only_that_scheme(self):
        # The saved scheme is no longer the active one: its "Do nothing" sides
        # are put back, and the active scheme is neither written nor re-applied.
        self.record(ac=1, dc=2)
        api = FakeScheme(ac=0, dc=0)
        api.active = "11111111-2222-3333-4444-555555555555"
        self.assertEqual(self.run_restore(api), "restored")
        self.assertEqual(set(api.targets), {FakeScheme.SCHEME})
        self.assertEqual(api.applied, [])

    def test_another_spelling_of_the_active_scheme_is_still_re_applied(self):
        # The app writes the standard form, but uuid.UUID (and so the record
        # check) also accepts braces and upper case: the values are written,
        # and the scheme is re-applied so they take effect.
        self.record(ac=1, dc=2, scheme="{" + FakeScheme.SCHEME.upper() + "}")
        api = FakeScheme(ac=0, dc=0)
        self.assertEqual(self.run_restore(api), "restored")
        self.assertEqual(api.values, {True: 1, False: 2})
        self.assertEqual(len(api.applied), 1)
        self.assertFalse(self.path.exists())

    def test_no_power_scheme_never_raises(self):
        # Off Windows (or if powrprof will not load): failed, and the record is kept.
        from unittest import mock
        self.record()
        with mock.patch.object(power.sys, "platform", "linux"):
            self.assertEqual(power.restore_only(self.path, lambda: False), "failed")
        self.assertTrue(self.path.exists())

    def test_every_state_has_a_documented_code(self):
        self.assertEqual(power.RESTORE_CODES, {"none": 0, "restored": 0, "failed": 1,
                                               "location": 1, "damaged": 2, "running": 3})


if __name__ == "__main__":
    unittest.main()
