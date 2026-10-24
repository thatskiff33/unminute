# tests/test_offboard.py
# Offboarding (#111 B8a; the owner's decisions B8-1 to B8-8, 21 Oct 2026):
# python -m workspace --offboard-report and --offboard, run as the user.
#   - The report: counts and yes or no only, for no workspace, an empty one,
#     an empty locked one, each blocker, a refused location and an open app.
#   - Removal refused for every blocker, with every file byte for byte as it
#     was (C1, C2, C4: nothing deletes meeting content or expires a hold).
#   - The decoder's AppContainer profile (#299) reported and removed after
#     the token, with a stand-in, and once with a real throwaway profile.
#   - An empty workspace, the models and the tokens removed; the lid put back
#     first; the offboarded audit record written and mirrored before anything
#     is removed; the root key erased before the rename.
#   - Links: at Unminute or Unminute\workspace refused; inside the workspace
#     and at Unminute\models removed as links, their targets kept.
#   - Run as Local System, or as an account Windows cannot name: refused.
#   - A file in use stops the run (exit 1); a rerun finishes, and a folder
#     left part-removed is finished first.
#   - Nothing outside %LOCALAPPDATA%\Unminute changes. Other entries in it
#     (a workspace IT renamed, say) are left alone and counted (others_left,
#     exit 4); the earlier version's data is never looked at (22 Oct 2026).
#   - A copy of the offboarded record still being reported at close (a
#     mirror_gap) stops the run before anything is removed.
#   - The workspace lock (B5a, instance.acquire) is held from the lid to
#     after the root key's erase: another process cannot take it meanwhile.
# Synthetic workspaces in temporary folders with the test key protector, fake
# credentials, a fake lid and a fake Event Log; the one Credential Manager
# test uses scratch names only.
#   python -m unittest tests.test_offboard -v

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import keyprotect  # noqa: E402

from workspace import audit, diag, instance, offboard, store  # noqa: E402

MARKER = "Zqxvmarker"                     # synthetic text that must never reach the output


class FakeMirror:
    """The Event Log copy; `events` is shared with the other fakes so the
    order of what happened can be checked."""

    def __init__(self, events, failing=False):
        self.events, self.failing = events, failing

    def report(self, event_id, level, text):
        if self.failing:
            raise OSError("the event log refused it")
        self.events.append(("mirror", text.split(" kind=")[1].split(" ")[0]))


def setUpModule():
    # A run that is given no profiles stand-in uses workspace.appcontainer:
    # point it at a throwaway name that is never made, so no test here looks
    # at or removes the decoder's real profile for the user running it (#299).
    from workspace import appcontainer
    patch = mock.patch.object(appcontainer, "PROFILE", f"Unminute.Test.Offboard.{os.getpid()}")
    patch.start()
    _patches.append(patch)


def tearDownModule():
    while _patches:
        _patches.pop().stop()


_patches = []


class FakeProfiles:
    """The decoder's AppContainer profile (#299; workspace.appcontainer)."""

    def __init__(self, events, present=True, failing=False):
        self.events, self.present, self.failing = events, present, failing

    def profile_exists(self):
        return self.present

    def delete_profile(self):
        self.events.append(("decoder_profile",))
        if self.failing:
            raise OSError("Windows kept it")
        was, self.present = self.present, False
        return was


class FakeCredentials:
    def __init__(self, events, stored):
        self.events, self.stored = events, set(stored)

    def has_token(self, target):
        return target in self.stored

    def remove_token(self, target):
        self.events.append(("token", target))
        if target in self.stored:
            self.stored.discard(target)
            return True
        return False


def tree(folder):
    """{relative path: bytes, or "<dir>"} for everything under folder,
    without following links."""
    out = {}
    folder = Path(folder)
    if not os.path.lexists(folder):
        return out
    for here, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs:
            p = Path(here) / name
            out[p.relative_to(folder).as_posix()] = "<link>" if offboard.is_link(p) else "<dir>"
        for name in files:
            p = Path(here) / name
            try:
                data = p.read_bytes()
            except PermissionError:             # a held lock's byte (instance.lock)
                data = ("<locked>", p.stat().st_size)
            # A read-only SQLite connection to a database in WAL mode (the
            # gate's, --status's and the report's) may leave an empty log and
            # its index: no row changes.
            if name.endswith("-shm") or (name.endswith("-wal") and data == b""):
                continue
            out[p.relative_to(folder).as_posix()] = data
    return out


def another_process_takes_the_lock(ws):
    """Whether another process can take the workspace lock now, as the app
    does at its start in any Windows session (instance.acquire): "took" or
    "held". A process of its own, since POSIX record locks belong to the
    process."""
    code = ("import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from workspace import instance\n"
            "try:\n"
            "    instance.acquire(sys.argv[2]).release()\n"
            "except instance.Held:\n"
            "    print('held')\n"
            "else:\n"
            "    print('took')\n")
    done = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT), str(ws)],
                          capture_output=True, text=True, timeout=60)
    return done.stdout.strip() or done.returncode


def make_link(link, target):
    """A junction on Windows (no rights needed), a symbolic link elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.appdata = Path(self._tmp.name) / "AppData" / "Local"
        self.appdata.mkdir(parents=True)
        self.where = offboard.Where(self.appdata)
        self.ws = self.where.workspace
        self.events = []
        self.mirror = FakeMirror(self.events)
        self.creds = FakeCredentials(self.events, {"Scratch/Current"})
        self.profiles = FakeProfiles(self.events)
        self.lid_result = "none"
        # Beside Unminute: must never change.
        (self.appdata / "Programs" / "Unminute").mkdir(parents=True)
        (self.appdata / "Programs" / "Unminute" / "keep.txt").write_bytes(b"program")
        (self.appdata / "Other").mkdir()
        (self.appdata / "Other" / "keep.txt").write_bytes(b"other")
        # Shorter than the app's 30 s, long enough for a scanner that opens a
        # new file for a moment.
        waiting = mock.patch.object(offboard, "WAIT_S", 10.0)
        waiting.start()
        self.addCleanup(waiting.stop)

    def lid(self, path, running):
        self.events.append(("lid", Path(path).name, running()))
        return self.lid_result

    def run_(self, remove=True, account=False, running=False, mirror="fake"):
        return offboard.run(self.where, remove, account=account,
                            session_running=lambda: running,
                            mirror=self.mirror if mirror == "fake" else mirror,
                            tokens=("Scratch/Current",), creds=self.creds, lid=self.lid,
                            profiles=self.profiles)

    def make_workspace(self, models=True):
        """An empty workspace with every kind of file a used one has."""
        st = keyprotect.open_store(self.ws / "workspace.db")
        st.close()
        logs = self.ws / "logs"
        logs.mkdir()
        (logs / "app.log").write_bytes(b'{"event":"start"}\n')
        (logs / "integrity.json").write_bytes(b'{"result":"ok"}')
        diag.record_keys(logs, "ok")
        (self.ws / "webview" / "a" / "b").mkdir(parents=True)
        (self.ws / "webview" / "a" / "b" / "Cookies").write_bytes(b"x" * 10)
        (self.ws / "settings.json").write_text("{}", encoding="ascii")
        (self.ws / "templates.sealed").write_bytes(b"\x01sealed")
        (self.ws / "media").mkdir()
        (self.ws / "recordings").mkdir()
        (self.ws / "power_restore.json").write_text("{}", encoding="ascii")
        log = audit.AuditLog(self.ws / "audit", strict=True)
        log.record("damage_continued")
        log.close()
        if models:
            (self.where.models / "speaker").mkdir(parents=True)
            (self.where.models / "speaker" / "model.bin").write_bytes(b"m" * 3000)

    def outside(self):
        return {k: v for k, v in tree(self.appdata).items() if not k.startswith("Unminute")}


class ReportTests(Base):
    def test_nothing_to_offboard(self):
        out, code = self.run_(remove=False)
        self.assertEqual(code, 0)
        self.assertEqual(out["result"], "ok")
        self.assertEqual(out["workspace"], "none")
        self.assertFalse(os.path.lexists(self.where.base))       # the report creates nothing
        self.assertTrue(out["token"])
        self.assertEqual(out["models_mb"], 0)

    def test_an_empty_workspace_is_reported_and_left_as_it_was(self):
        self.make_workspace()
        before = tree(self.where.base)
        out, code = self.run_(remove=False)
        self.assertEqual((out["result"], out["workspace"], code), ("ok", "empty", 0))
        self.assertEqual(out["blockers"], {})
        self.assertTrue(out["lid_record"])
        self.assertEqual(out["models_mb"], 1)
        self.assertEqual(tree(self.where.base), before)
        self.assertEqual(self.events, [])                         # nothing removed or recorded

    def test_an_empty_locked_workspace(self):
        self.make_workspace()
        diag.record_keys(self.ws / "logs", "locked", "windows", 13, "U-ABCDEF")
        out, code = self.run_(remove=False)
        self.assertEqual((out["workspace"], code), ("locked_empty", 0))

    def test_each_blocker_is_counted(self):
        self.make_workspace()
        st = keyprotect.open_store(self.ws / "workspace.db")
        mid = st.create_meeting(MARKER)
        st.add_hold(store.new_id(), mid, "all", MARKER, 1.0)
        st.close()
        (self.ws / "recordings" / uuid.uuid4().hex).mkdir()
        (self.ws / "media" / uuid.uuid4().hex).mkdir()
        (self.ws / "media" / mid).mkdir()
        (self.ws / "media" / mid / "audio.bin").write_bytes(b"a")
        out, code = self.run_(remove=False)
        self.assertEqual(code, offboard.BLOCKED)
        self.assertEqual((out["result"], out["workspace"]), ("blocked", "blocked"))
        self.assertEqual(out["blockers"], {"holds": 1, "meetings": 1, "recordings": 1,
                                           "media": 1})
        text = json.dumps(out)
        self.assertNotIn(MARKER, text)
        self.assertNotIn(self._tmp.name, text)
        self.assertNotIn(self._tmp.name.replace("\\", "\\\\"), text)

    def test_a_damaged_database_blocks_as_unreadable(self):
        self.make_workspace()
        (self.ws / "workspace.db").write_bytes(b"not a database" * 100)
        out, code = self.run_(remove=False)
        self.assertEqual((out["blockers"], code), ({"unreadable": 1}, offboard.BLOCKED))

    def test_media_without_a_database_blocks(self):
        (self.ws / "media" / "x").mkdir(parents=True)
        (self.ws / "media" / "x" / "audio.bin").write_bytes(b"a")
        (self.ws / "recordings" / "y").mkdir(parents=True)
        out, code = self.run_(remove=False)
        self.assertEqual((out["blockers"], code), ({"media": 1, "recordings": 1},
                                                    offboard.BLOCKED))

    def test_an_open_app_is_reported(self):
        self.make_workspace()
        out, code = self.run_(remove=False, running=True)
        self.assertEqual((out["result"], out["running"], code), ("running", True,
                                                                 offboard.RUNNING))

    @unittest.skipUnless(os.name == "nt", "the run marker names a Windows process")
    def test_an_app_open_in_another_session_is_seen_by_its_run_marker(self):
        self.make_workspace()
        (self.ws / "logs" / "app.running").write_text(json.dumps(
            {"pid": os.getpid(), "created": diag.process_created()}), encoding="ascii")
        out, code = self.run_(remove=False)
        self.assertEqual((out["running"], code), (True, offboard.RUNNING))
        # A marker a crash left (its process has ended) is not an open app.
        (self.ws / "logs" / "app.running").write_text(json.dumps(
            {"pid": os.getpid(), "created": 1}), encoding="ascii")
        out, code = self.run_(remove=False)
        self.assertEqual((out["running"], code), (False, 0))

    def test_a_held_workspace_lock_is_an_open_app(self):
        # B5's workspace lock (#111 B5-10): the first byte of instance.lock,
        # held by another handle (another process or Windows session).
        self.make_workspace()
        path = self.ws / "instance.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        self.addCleanup(os.close, fd)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            import subprocess
            # POSIX record locks belong to the process: hold it in another.
            child = subprocess.Popen(
                [sys.executable, "-c", "import fcntl, os, sys, time; "
                 "fd = os.open(sys.argv[1], os.O_RDWR); "
                 "fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0); print('held', "
                 "flush=True); time.sleep(30)", str(path)], stdout=subprocess.PIPE, text=True)
            self.addCleanup(child.kill)
            self.assertEqual(child.stdout.readline().strip(), "held")
            del fcntl
        out, code = self.run_(remove=False)
        self.assertEqual((out["running"], code), (True, offboard.RUNNING))
        before = tree(self.appdata)
        out, code = self.run_()
        self.assertEqual((out["running"], code), (True, offboard.RUNNING))
        # Taken between the first look and the removal: the removal's own
        # hold of the lock fails, and nothing is done.
        with mock.patch.object(offboard, "_lock_held", return_value=False):
            out, code = self.run_()
        self.assertEqual((out["running"], code), (True, offboard.RUNNING))
        self.assertEqual(tree(self.appdata), before)
        self.assertEqual(self.events, [])


class RefusalTests(Base):
    def test_local_system_and_an_unknown_account_read_nothing(self):
        self.make_workspace()
        before = tree(self.appdata)
        for account, result in ((True, "system_account"), (None, "account_unknown")):
            for remove in (False, True):
                with mock.patch.object(offboard, "assess",
                                       side_effect=AssertionError("read")):
                    out, code = self.run_(remove=remove, account=account)
                self.assertEqual((out, code), ({"result": result}, offboard.ACCOUNT))
        self.assertEqual(tree(self.appdata), before)
        self.assertEqual(self.events, [])

    def test_a_link_at_unminute_or_workspace_is_refused(self):
        real = Path(self._tmp.name) / "elsewhere"
        for link in ("base", "workspace"):
            with self.subTest(link=link):
                if real.exists():
                    import shutil
                    shutil.rmtree(real)
                if os.path.lexists(self.where.base):
                    offboard.remove_tree(self.where.base)
                (real / "workspace").mkdir(parents=True)
                (real / "workspace" / "keep.txt").write_bytes(b"keep")
                if link == "base":
                    make_link(self.where.base, real)
                else:
                    self.where.base.mkdir()
                    make_link(self.ws, real / "workspace")
                for remove in (False, True):
                    out, code = self.run_(remove=remove)
                    self.assertEqual((out["result"], code), ("refused", offboard.REFUSED))
                self.assertEqual((real / "workspace" / "keep.txt").read_bytes(), b"keep")
                self.assertTrue(os.path.lexists(self.ws))
        self.assertEqual(self.events, [])

    def test_removal_is_refused_for_every_blocker_and_nothing_changes(self):
        self.make_workspace()
        st = keyprotect.open_store(self.ws / "workspace.db")
        mid = st.create_meeting(MARKER)
        st.close()
        cases = {
            "meetings": lambda: None,
            "recordings": lambda: (self.ws / "recordings" / "r1").mkdir(),
            "media": lambda: ((self.ws / "media" / "m1").mkdir(),
                              (self.ws / "media" / "m1" / "a.bin").write_bytes(b"a")),
        }
        for kind, add in cases.items():
            with self.subTest(kind=kind):
                add()
                before = tree(self.appdata)
                out, code = self.run_()
                self.assertEqual(code, offboard.BLOCKED)
                self.assertIn(kind, out["blockers"])
                self.assertEqual(tree(self.appdata), before)
        st = keyprotect.open_store(self.ws / "workspace.db")
        st.add_hold(store.new_id(), mid, "all", MARKER, 1.0)
        st.close()
        before = tree(self.appdata)
        out, code = self.run_()
        self.assertEqual((out["blockers"]["holds"], code), (1, offboard.BLOCKED))
        self.assertEqual(tree(self.appdata), before)
        self.assertEqual(self.events, [])                 # no lid, record, key or token

    def test_a_locked_workspace_with_meetings_is_blocked(self):
        self.make_workspace()
        st = keyprotect.open_store(self.ws / "workspace.db")
        st.create_meeting(MARKER)
        st.close()
        diag.record_keys(self.ws / "logs", "locked", "windows", 13, "U-ABCDEF")
        before = tree(self.appdata)
        # The blockers need no key: a protector that refuses everything changes nothing.
        out, code = self.run_()
        self.assertEqual((out["workspace"], code), ("blocked", offboard.BLOCKED))
        self.assertEqual(tree(self.appdata), before)

    def test_an_open_app_stops_removal(self):
        self.make_workspace()
        before = tree(self.appdata)
        out, code = self.run_(running=True)
        self.assertEqual(code, offboard.RUNNING)
        self.assertEqual(tree(self.appdata), before)
        self.assertEqual(self.events, [])

    def test_the_lid_owned_by_an_open_app_stops_removal(self):
        self.make_workspace()
        self.lid_result = "running"
        before = tree(self.appdata)
        out, code = self.run_()
        self.assertEqual((out["result"], code), ("running", offboard.RUNNING))
        self.assertEqual(tree(self.appdata), before)

    def test_a_lid_that_cannot_be_put_back_now_stops_removal(self):
        self.make_workspace()
        self.lid_result = "failed"
        before = tree(self.appdata)
        out, code = self.run_()
        self.assertEqual((out["result"], out["step"], code), ("failed", "lid", offboard.FAILED))
        self.assertEqual(tree(self.appdata), before)

    def test_a_meeting_added_after_the_first_look_stops_the_erase(self):
        # An app that does not take B5's lock (a version from before B5a),
        # started meanwhile in another session, imports a meeting: the last
        # look before the erase sees it.
        self.make_workspace()

        def lid(path, running):
            st = keyprotect.open_store(self.ws / "workspace.db")
            st.create_meeting(MARKER)
            st.close()
            return "none"
        out, code = offboard.run(self.where, True, account=False,
                                 session_running=lambda: False, mirror=self.mirror,
                                 tokens=("Scratch/Current",), creds=self.creds, lid=lid)
        self.assertEqual((out["result"], out["blockers"], code),
                         ("blocked", {"meetings": 1}, offboard.BLOCKED))
        con = sqlite3.connect(self.ws / "workspace.db")
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM crypto_meta").fetchone()[0], 1)
        finally:
            con.close()
        self.assertFalse((self.ws / "instance.lock").exists())   # as it found it
        self.assertTrue(self.creds.stored)

    def test_the_app_cannot_open_the_workspace_from_the_lid_to_the_erase(self):
        # B5a (#111 B5-10): the app takes instance.lock at its start, in any
        # Windows session, before it opens the store (instance.acquire).
        # Offboarding holds the same lock from the lid to after the root
        # key's erase, so no other session can open the workspace and add a
        # meeting between the last look and the erase (B8a review round 1).
        self.assertEqual(offboard.LOCK, instance.NAME)
        self.make_workspace()
        self.assertEqual(another_process_takes_the_lock(self.ws), "took")   # free before
        seen = []
        real_erase = store.erase_root_key

        def lid(path, running):
            seen.append(("lid", another_process_takes_the_lock(self.ws)))
            return "none"

        def erase(db):
            seen.append(("erase", another_process_takes_the_lock(self.ws)))
            done = real_erase(db)
            seen.append(("erased", another_process_takes_the_lock(self.ws)))
            return done
        with mock.patch.object(store, "erase_root_key", side_effect=erase):
            out, code = offboard.run(self.where, True, account=False,
                                     session_running=lambda: False, mirror=self.mirror,
                                     tokens=("Scratch/Current",), creds=self.creds, lid=lid)
        self.assertEqual((out["result"], code), ("offboarded", 0))
        self.assertEqual(seen, [("lid", "held"), ("erase", "held"), ("erased", "held")])

    def test_a_record_that_does_not_reach_the_event_log_stops_removal(self):
        self.make_workspace()
        out, code = self.run_(mirror=FakeMirror(self.events, failing=True))
        self.assertEqual((out["result"], out["step"], code), ("failed", "audit",
                                                              offboard.FAILED))
        self.assertTrue((self.ws / "workspace.db").exists())
        con = sqlite3.connect(self.ws / "workspace.db")
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM crypto_meta").fetchone()[0], 1)
        finally:
            con.close()
        self.assertTrue(self.creds.stored)


class RemovalTests(Base):
    def test_an_empty_workspace_models_and_tokens_are_removed_in_order(self):
        self.make_workspace()
        outside = self.outside()
        order = []
        real_erase = store.erase_root_key

        def erase(db):
            order.append("erase")
            self.assertTrue(db.exists())
            return real_erase(db)
        with mock.patch.object(store, "erase_root_key", side_effect=erase):
            out, code = self.run_()
        self.assertEqual((out["result"], out.get("step"), code), ("offboarded", None, 0))
        self.assertFalse(os.path.lexists(self.where.base))       # Unminute itself, now empty
        self.assertEqual(self.outside(), outside)                # beside it: as it was
        kinds = [e[0] for e in self.events]
        self.assertEqual(kinds[0], "lid")                        # the lid first
        self.assertEqual(self.events[0][2], False)               # asked with the app closed
        self.assertIn(("mirror", "offboarded"), self.events)
        self.assertLess(kinds.index("mirror"), kinds.index("token"))
        self.assertEqual(order, ["erase"])
        self.assertEqual(self.creds.stored, set())
        self.assertEqual(out["removed"], {"workspace": True, "models": True, "token": True,
                                          "decoder_profile": True})
        self.assertLess(kinds.index("token"), kinds.index("decoder_profile"))   # #299
        self.assertFalse(out["decoder_profile"])                 # gone, as the report after says
        self.assertEqual((out["workspace"], out["others"]), ("none", 0))

    def test_the_decoders_profile_is_reported_and_removed_on_its_own(self):
        # #299: the profile goes even when there is no workspace; none there
        # is not an error; one Windows keeps stops the run at its own step.
        out, code = self.run_(remove=False)
        self.assertEqual((out["decoder_profile"], code), (True, 0))
        out, code = self.run_()
        self.assertEqual((out["result"], code), ("offboarded", 0))
        self.assertEqual(out["removed"]["decoder_profile"], True)
        out, code = self.run_()
        self.assertEqual((out["removed"]["decoder_profile"], code), (False, 0))
        self.profiles = FakeProfiles(self.events, failing=True)
        out, code = self.run_()
        self.assertEqual((out["result"], out["step"], code), ("failed", "decoder_profile",
                                                              offboard.FAILED))
        self.profiles.profile_exists = mock.Mock(side_effect=OSError("cannot say"))
        out, _ = self.run_(remove=False)
        self.assertIsNone(out["decoder_profile"])                # never "not there" on a guess

    @unittest.skipUnless(sys.platform == "win32", "Windows AppContainer profiles")
    def test_a_real_throwaway_profile_is_removed(self):
        from workspace import appcontainer
        name = f"Unminute.Test.Offboard.{uuid.uuid4().hex[:12]}"
        self.addCleanup(appcontainer.delete_profile, name)
        appcontainer.ensure_profile(name).close()
        with mock.patch.object(appcontainer, "PROFILE", name):
            out, code = offboard.run(self.where, True, account=False,
                                     session_running=lambda: False, mirror=self.mirror,
                                     tokens=("Scratch/Current",), creds=self.creds, lid=self.lid)
        self.assertEqual((out["result"], code), ("offboarded", 0))
        self.assertTrue(out["removed"]["decoder_profile"])
        self.assertFalse(appcontainer.profile_exists(name))
        self.assertTrue(out["residuals"])
        self.assertTrue(all(isinstance(r, str) for r in out["residuals"]))

    def test_the_record_is_written_and_mirrored_before_anything_is_removed(self):
        self.make_workspace()
        seen = {}

        class Watching(FakeMirror):
            def report(inner, event_id, level, text):
                super().report(event_id, level, text)
                if "kind=offboarded" in text:
                    seen["db"] = (self.ws / "workspace.db").exists()
                    seen["records"] = [json.loads(line)["kind"] for p in sorted(
                        (self.ws / "audit").iterdir()) for line in p.read_text().splitlines()]
        out, code = self.run_(mirror=Watching(self.events))
        self.assertEqual(code, 0)
        self.assertTrue(seen["db"])
        self.assertEqual(seen["records"][-1], "offboarded")
        self.assertEqual(seen["records"][:2], ["chain_start", "damage_continued"])

    def test_a_locked_empty_workspace_is_removed_without_its_key(self):
        self.make_workspace()
        diag.record_keys(self.ws / "logs", "locked", "windows", 13, "U-ABCDEF")
        with mock.patch.object(keyprotect.TestProtector, "unprotect",
                               side_effect=AssertionError("the key was asked for")):
            out, code = self.run_()
        self.assertEqual((out["result"], out.get("step"), code), ("offboarded", None, 0))
        self.assertFalse(os.path.lexists(self.ws))

    def test_a_crash_after_the_key_erase_leaves_nothing_readable_and_a_rerun_finishes(self):
        self.make_workspace()
        with mock.patch.object(offboard, "_rename_aside", side_effect=OSError("crash")), \
                mock.patch.object(offboard, "WAIT_S", 0.0):
            out, code = self.run_()
        self.assertEqual((out["result"], out["step"], code), ("failed", "rename",
                                                              offboard.FAILED))
        db = self.ws / "workspace.db"
        for name in (db, Path(str(db) + "-wal")):
            if name.exists():
                self.assertNotIn(keyprotect.PREFIX, name.read_bytes())
        # The workspace now opens locked: its root key is gone.
        st = keyprotect.open_store(db, on_unavailable="lock")
        try:
            self.assertEqual(st.locked.reason, "missing")
        finally:
            st.close()
        out, code = self.run_()
        self.assertEqual((out["result"], out.get("step"), code), ("offboarded", None, 0))
        self.assertFalse(os.path.lexists(self.where.base))

    def test_links_inside_are_removed_as_links_and_their_targets_kept(self):
        self.make_workspace(models=False)
        target = Path(self._tmp.name) / "target"
        target.mkdir()
        (target / "keep.txt").write_bytes(b"keep")
        make_link(self.ws / "logs" / "linked", target)
        models = Path(self._tmp.name) / "redirected-models"
        (models / "speaker").mkdir(parents=True)
        (models / "speaker" / "model.bin").write_bytes(b"m")
        self.where.base.mkdir(exist_ok=True)
        make_link(self.where.models, models)
        out, code = self.run_(remove=False)
        self.assertEqual((out["models_link"], out["models_mb"]), (True, 0))
        out, code = self.run_()
        self.assertEqual((out["result"], out.get("step"), code), ("offboarded", None, 0))
        self.assertEqual((target / "keep.txt").read_bytes(), b"keep")
        self.assertEqual((models / "speaker" / "model.bin").read_bytes(), b"m")
        self.assertFalse(os.path.lexists(self.where.models))
        self.assertTrue(out["models_link"])

    def test_a_leftover_part_removed_workspace_is_finished_first(self):
        self.make_workspace()
        aside = self.where.base / f"workspace.offboarding-{uuid.uuid4().hex}"
        (aside / "logs").mkdir(parents=True)
        (aside / "logs" / "app.log").write_bytes(b"x")
        other = self.where.base / "workspace.offboarding-not-ours"
        other.mkdir()
        out, code = self.run_(remove=False)
        self.assertEqual((out["leftover"], out.get("others")), (1, 1))
        out, code = self.run_()
        self.assertEqual((out["result"], out.get("others"), code), ("others_left", 1, 4))
        self.assertFalse(os.path.lexists(aside))
        self.assertTrue(other.exists())                          # only the app's own names
        self.assertTrue(self.where.base.exists())                # not empty: kept

    def test_other_entries_left_in_unminute_are_counted_and_never_a_clean_result(self):
        # A workspace IT renamed by the guide's steps ("workspace-damaged")
        # still holds meetings: offboarding leaves it alone (C1), and says how
        # many entries it left, without naming them (P3).
        self.make_workspace()
        renamed = self.where.base / "workspace-damaged"
        (renamed / "media").mkdir(parents=True)
        (renamed / "workspace.db").write_bytes(MARKER.encode())
        (self.where.base / "notes.txt").write_bytes(b"x")
        out, code = self.run_(remove=False)
        self.assertEqual((out["result"], out.get("others"), code), ("others_left", 2, 4))
        out, code = self.run_()
        self.assertEqual((out["result"], out.get("others"), code), ("others_left", 2, 4))
        self.assertTrue(out["removed"]["workspace"])
        self.assertFalse(os.path.lexists(self.ws))
        self.assertEqual((renamed / "workspace.db").read_bytes(), MARKER.encode())
        self.assertEqual((self.where.base / "notes.txt").read_bytes(), b"x")
        text = json.dumps(out)
        for name in ("workspace-damaged", "notes", MARKER):
            self.assertNotIn(name, text)
        # Once IT has dealt with them, a rerun is clean and removes Unminute.
        offboard.remove_tree(renamed)
        (self.where.base / "notes.txt").unlink()
        out, code = self.run_()
        self.assertEqual((out["result"], out.get("others"), code), ("offboarded", 0, 0))
        self.assertFalse(os.path.lexists(self.where.base))

    def test_a_copy_still_being_reported_at_close_is_never_counted_as_copied(self):
        # The Event Log is slow: close() stops waiting and records the
        # offboarded record's copy as a mirror_gap. Nothing may be removed.
        import threading
        self.make_workspace()
        release = threading.Event()
        self.addCleanup(release.set)

        class Slow(FakeMirror):
            def report(inner, event_id, level, text):
                if "kind=offboarded" in text:
                    release.wait(30)
                super().report(event_id, level, text)
        with mock.patch.object(audit, "CLOSE_DRAIN_S", 0.2):
            out, code = self.run_(mirror=Slow(self.events))
        self.assertEqual((out["result"], out.get("step"), code), ("failed", "audit",
                                                                   offboard.FAILED))
        kinds = [json.loads(line)["kind"] for p in sorted((self.ws / "audit").iterdir())
                 for line in p.read_text().splitlines()]
        self.assertEqual(kinds[-2:], ["offboarded", "mirror_gap"])
        con = sqlite3.connect(self.ws / "workspace.db")
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM crypto_meta").fetchone()[0], 1)
        finally:
            con.close()
        self.assertTrue(self.creds.stored)

    @unittest.skipUnless(os.name == "nt", "Windows keeps an open file from being removed")
    def test_a_file_in_use_stops_the_run_and_a_rerun_finishes(self):
        self.make_workspace()
        held = open(self.ws / "logs" / "app.log", "rb")
        try:
            with mock.patch.object(offboard, "WAIT_S", 0.5):
                out, code = self.run_()
            self.assertEqual((out["result"], code), ("failed", offboard.FAILED))
            self.assertIn(out["step"], ("rename", "workspace"))
        finally:
            held.close()
        out, code = self.run_()
        self.assertEqual((out["result"], out.get("step"), code), ("offboarded", None, 0))
        self.assertFalse(os.path.lexists(self.where.base))

    def test_no_workspace_still_removes_models_and_tokens(self):
        (self.where.models / "x").mkdir(parents=True)
        out, code = self.run_()
        self.assertEqual((out["result"], out.get("step"), code), ("offboarded", None, 0))
        self.assertFalse(os.path.lexists(self.where.base))
        self.assertEqual(self.creds.stored, set())
        self.assertNotIn("mirror", [e[0] for e in self.events])  # nothing to record in

    def test_the_output_names_no_path_or_content(self):
        self.make_workspace()
        out, code = self.run_()
        text = json.dumps(out)
        self.assertNotIn(self._tmp.name, text)
        self.assertNotIn(self._tmp.name.replace("\\", "\\\\"), text)
        self.assertNotIn("Scratch", text)


class RootKeyEraseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "workspace.db"

    def test_the_root_key_is_deleted_and_the_log_emptied(self):
        st = keyprotect.open_store(self.db)
        st.create_meeting("Synthetic")
        st.delete_new_meeting(st.meetings()[0]["id"])
        reader = sqlite3.connect(self.db)              # keeps the log in use while the store writes
        reader.close()
        self.assertTrue(store.erase_root_key(self.db))
        st.close()
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM crypto_meta").fetchone()[0], 0)
        finally:
            con.close()
        self.assertNotIn(keyprotect.PREFIX, self.db.read_bytes())
        wal = Path(str(self.db) + "-wal")
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)

    def test_a_reader_that_keeps_the_log_busy_is_reported(self):
        st = keyprotect.open_store(self.db)
        st.close()
        with mock.patch.object(store, "_truncate_log", return_value=False):
            self.assertFalse(store.erase_root_key(self.db, retries=()))

    def test_it_never_creates_a_database(self):
        with self.assertRaises(sqlite3.Error):
            store.erase_root_key(self.db)
        self.assertFalse(self.db.exists())

    def test_an_earlier_schema_without_a_key_table_is_nothing_to_erase(self):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        con.execute("INSERT INTO schema_version VALUES (1)")
        con.commit()
        con.close()
        self.assertTrue(store.erase_root_key(self.db))


@unittest.skipUnless(sys.platform == "win32", "Windows Credential Manager")
class CredentialManagerTests(Base):
    def test_scratch_tokens_are_removed_from_credential_manager(self):
        from workspace import credentials
        names = (f"UnminuteTest/B8a-{uuid.uuid4().hex}",)
        for name in names:
            credentials.save_token("hf_synthetic_test_value", target=name)
        try:
            out, code = offboard.run(self.where, True, account=False,
                                     session_running=lambda: False, mirror=self.mirror,
                                     tokens=names, creds=credentials, lid=self.lid)
            self.assertEqual(code, 0)
            self.assertFalse(any(credentials.has_token(n) for n in names))
            self.assertEqual(out["removed"]["token"], True)
        finally:
            for name in names:
                credentials.remove_token(name)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        import workspace.__main__ as main
        self.main = main

    def test_local_system_is_refused_before_anything_is_read(self):
        for flag in ("--offboard-report", "--offboard"):
            with mock.patch.object(self.main, "_system_account", return_value=True), \
                    mock.patch("workspace.store.default_root",
                               side_effect=AssertionError("read")):
                out, code = self.main.offboard([flag])
            self.assertEqual((out, code), ({"result": "system_account"}, 2))

    def test_a_refused_location_is_exit_6(self):
        with mock.patch.object(self.main, "_system_account", return_value=False), \
                mock.patch("workspace.store.default_root",
                           side_effect=store.StoreError("refused")):
            out, code = self.main.offboard(["--offboard-report"])
        self.assertEqual((out, code), ({"result": "refused"}, 6))

    def test_the_options_print_one_line_and_return_its_code(self):
        import contextlib
        import io
        for flag in ("--offboard-report", "--offboard"):
            for given, printed, code in (
                    (({"result": "blocked"}, 5), {"result": "blocked"}, 5),
                    (OSError("C:\\Users\\someone"), {"result": "failed", "step": "unexpected"},
                     1)):
                kwargs = ({"side_effect": given} if isinstance(given, Exception)
                          else {"return_value": given})
                out = io.StringIO()
                with mock.patch.object(sys, "argv", ["workspace", flag]), \
                        mock.patch.object(self.main, "offboard", **kwargs) as called, \
                        contextlib.redirect_stdout(out):
                    self.assertEqual(self.main.main(), code)
                self.assertEqual(json.loads(out.getvalue()), printed)
                self.assertEqual(called.call_args.args[0], ["workspace", flag])

    def test_the_real_names(self):
        from workspace import credentials
        self.assertEqual(offboard.TOKENS, (credentials.TARGET,))
        self.assertEqual(offboard.OTHERS_LEFT, 4)
        # The earlier version's data is not looked at (the owner's decision of
        # 22 Oct 2026, replacing B8-3): no field, record or token for it.
        self.assertEqual(set(audit.KINDS["offboarded"][2]), {"locked", "models", "token"})
        self.assertFalse([n for n in vars(offboard) if "EARLIER" in n.upper()])

    def test_the_report_has_no_earlier_version_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            creds = FakeCredentials([], set())
            out, code = offboard.run(offboard.Where(Path(tmp)), False, account=False,
                                     session_running=lambda: False, creds=creds,
                                     lid=lambda p, r: "none")
        self.assertEqual((out["result"], code), ("ok", 0))
        self.assertNotIn("earlier_version", out)


if __name__ == "__main__":
    unittest.main()
