# tests/test_audit.py
# The content-free, hash-chained audit log and its Event Log copy (#111 C5;
# workspace/audit.py, docs/WORKSPACE_DESIGN.md "Audit log"). Synthetic data
# only. The Windows test writes to the Application log under the source
# "Unminute-Test", never "Unminute".
#   python tools/run_tests.py

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

from workspace import audit  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
MEETING, OTHER, JOB, HOLD = "a" * 32, "b" * 32, "c" * 32, "d" * 32
MARKER = "Zqxvmarker"                      # one word: a filter that refuses spaces misses it
SR = 16000


class FakeMirror:
    """Collects what would go to the Event Log; fails while `failing`."""

    def __init__(self):
        self.sent, self.failing, self.calls = [], False, 0

    def report(self, event_id, level, text):
        self.calls += 1
        if self.failing:
            raise OSError("the event log refused it")
        self.sent.append((event_id, level, text))


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def lines(folder):
    out = []
    for _, path in audit.segments(folder):
        out += [line for line in path.read_bytes().split(b"\n") if line]
    return out


def records(folder):
    return [json.loads(line) for line in lines(folder)]


def kinds(folder):
    return [r["kind"] for r in records(folder)]


def event_xml(texts, provider=audit.SOURCE):
    """Entries as `wevtutil qe /f:xml /e:Events` prints them."""
    ns = "http://schemas.microsoft.com/win/2004/08/events/event"
    body = "".join(f"<Event xmlns='{ns}'><System><Provider Name='{provider}'/>"
                   f"<EventID Qualifiers='0'>1000</EventID></System>"
                   f"<EventData><Data>{t}</Data></EventData></Event>" for t in texts)
    return f"<Events>{body}</Events>"


def rechain(folder, change):
    """Rewrite every line through change(record) and rebuild the hashes, as
    someone with write access to the folder could (a rebuilt chain)."""
    prev = None
    for _, path in audit.segments(folder):
        out = []
        for line in path.read_bytes().split(b"\n"):
            if not line:
                continue
            record = change(json.loads(line))
            if prev is not None:
                record["prev"] = prev
            new = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            prev = hashlib.sha256(new).hexdigest()
            out.append(new)
        path.write_bytes(b"\n".join(out) + b"\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "audit"
        self.clock, self.timer = Clock(), Clock(1000.0)
        # One fsync a record is real in the app; here it only slows the
        # thousand-record tests down.
        patcher = mock.patch.object(audit.os, "fsync")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def log(self, **kw):
        kw.setdefault("strict", True)
        return audit.AuditLog(self.folder, clock=self.clock, timer=self.timer, **kw)

    def fill(self, n, log=None):
        log = log or self.log()
        for i in range(n):
            self.assertTrue(log.record("app_start", version=None, schema=i))
        return log


class ChainTests(Base):
    def test_a_thousand_records_verify(self):
        self.fill(1000).close()
        result = audit.verify(self.folder)
        self.assertEqual((result["result"], result["records"], result["chains"],
                          result["first_seq"], result["last_seq"]), ("ok", 1001, 1, 1, 1001))
        self.assertEqual(result["head"], hashlib.sha256(lines(self.folder)[-1]).hexdigest())

    def edit(self, change):
        self.fill(20).close()
        [(_, path)] = audit.segments(self.folder)
        parts = path.read_bytes().split(b"\n")[:-1]
        path.write_bytes(b"\n".join(change(parts)) + b"\n")
        return audit.verify(self.folder)

    def test_one_changed_byte_breaks_at_the_next_record(self):
        def change(parts):
            parts[9] = parts[9].replace(b'"schema":8', b'"schema":9')
            return parts
        r = self.edit(change)
        self.assertEqual((r["result"], r["broken_at"], r["reason"]), ("broken", 11, "link"))

    def test_a_byte_that_breaks_the_format_breaks_at_that_record(self):
        def change(parts):
            parts[9] = parts[9].replace(b'"kind"', b'"kind" ')
            return parts
        r = self.edit(change)
        self.assertEqual((r["result"], r["broken_at"], r["reason"]), ("broken", 10, "format"))

    def test_a_removed_line_breaks_the_sequence(self):
        r = self.edit(lambda parts: parts[:5] + parts[6:])
        self.assertEqual((r["result"], r["broken_at"], r["reason"]), ("broken", 6, "sequence"))

    def test_swapped_lines_break_the_sequence(self):
        r = self.edit(lambda parts: parts[:5] + [parts[6], parts[5]] + parts[7:])
        self.assertEqual((r["result"], r["broken_at"], r["reason"]), ("broken", 6, "sequence"))

    def test_a_reused_seq_breaks_the_sequence(self):
        r = self.edit(lambda parts: parts[:6] + [parts[5]] + parts[6:])
        self.assertEqual((r["result"], r["broken_at"], r["reason"]), ("broken", 7, "sequence"))

    def test_a_record_written_after_close_carries_on_the_chain(self):
        self.fill(3).close()
        log = self.log()
        log.record("damage_continued")
        log.close()
        self.assertEqual(kinds(self.folder), ["chain_start"] + ["app_start"] * 3
                         + ["damage_continued"])
        self.assertEqual(audit.verify(self.folder)["result"], "ok")
        self.assertEqual(audit.tail_seq(self.folder), 5)

    def test_threads_give_contiguous_sequence_numbers(self):
        log = self.log()

        def write():
            for i in range(250):
                log.record("app_start", version=None, schema=i)
        threads = [threading.Thread(target=write) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        log.close()
        self.assertEqual([r["seq"] for r in records(self.folder)], list(range(1, 1002)))
        self.assertEqual(audit.verify(self.folder)["result"], "ok")


class StartTests(Base):
    def test_a_new_workspace_starts_with_first_use(self):
        log = self.log(new_workspace=True)
        log.record("app_start", version="0.5.0", schema=2)
        log.close()
        first = records(self.folder)[0]
        self.assertEqual((first["kind"], first["previous"], first["seq"], first["prev"]),
                         ("chain_start", "first_use", 1, audit.ZERO))

    def test_a_missing_folder_otherwise_starts_with_missing(self):
        self.log().record("app_start", version=None, schema=2)
        self.assertEqual(records(self.folder)[0]["previous"], "missing")

    def test_a_folder_with_no_segment_counts_as_missing(self):
        self.folder.mkdir()
        self.log(new_workspace=True).record("app_start", version=None, schema=2)
        self.assertEqual(records(self.folder)[0]["previous"], "first_use")

    def test_an_unreadable_end_starts_a_new_chain_in_a_new_segment(self):
        self.fill(2).close()
        [(_, path)] = audit.segments(self.folder)
        path.write_bytes(path.read_bytes() + b"not a record\n")
        log = self.log()
        log.record("damage_continued")
        log.close()
        self.assertEqual([n for n, _ in audit.segments(self.folder)], [1, 2])
        new = [json.loads(x) for x in (self.folder / "audit-000002.jsonl").read_bytes().split()]
        self.assertEqual((new[0]["kind"], new[0]["previous"]), ("chain_start", "unreadable"))
        self.assertEqual(audit.verify(self.folder)["result"], "broken")   # the bad line stays


class TornTailTests(Base):
    def torn(self, extra=b'{"at":17'):
        self.fill(3).close()
        [(_, path)] = audit.segments(self.folder)
        path.write_bytes(path.read_bytes() + extra)
        return path

    def test_a_cut_short_line_starts_a_new_segment_with_tail_damaged(self):
        path = self.torn()
        before = path.read_bytes()
        result = audit.verify(self.folder)
        self.assertEqual((result["result"], result["tail"]), ("ok", "unfinished"))
        log = self.log()
        log.record("damage_continued")
        log.close()
        self.assertEqual(path.read_bytes(), before)               # left as it is
        new = [json.loads(x) for x in (self.folder / "audit-000002.jsonl").read_bytes().split()]
        self.assertEqual([(r["kind"], r["seq"]) for r in new],
                         [("tail_damaged", 5), ("damage_continued", 6)])
        self.assertEqual(new[0]["bytes"], 8)
        self.assertEqual(new[0]["prev"], hashlib.sha256(before.split(b"\n")[-2]).hexdigest())
        result = audit.verify(self.folder)
        self.assertEqual((result["result"], result["tail"], result["records"]), ("ok", None, 6))

    def test_tail_damaged_with_the_wrong_length_is_broken(self):
        path = self.torn()
        self.log().record("damage_continued")
        path.write_bytes(path.read_bytes() + b"9")                # now 9 bytes cut short
        r = audit.verify(self.folder)
        self.assertEqual((r["result"], r["reason"], r["broken_at"]), ("broken", "torn", 5))

    def test_a_cut_short_line_not_followed_by_tail_damaged_is_broken(self):
        with mock.patch.object(audit, "SEGMENT_BYTES", 900):
            self.fill(10).close()
        segs = audit.segments(self.folder)
        self.assertGreater(len(segs), 2)
        segs[0][1].write_bytes(segs[0][1].read_bytes() + b'{"at"')
        self.assertEqual(audit.verify(self.folder)["reason"], "torn")


class RotationTests(Base):
    def test_rotation_and_the_cap_keep_the_log_verifiable(self):
        with mock.patch.object(audit, "SEGMENT_BYTES", 2000), \
                mock.patch.object(audit, "MAX_BYTES", 6000):
            log = self.fill(200)
            log.close()
        segs = audit.segments(self.folder)
        self.assertNotEqual(segs[0][0], 1)                        # the oldest were dropped
        self.assertLessEqual(sum(p.stat().st_size for _, p in segs), 6000 + 2000)
        dropped = [r for r in records(self.folder) if r["kind"] == "segment_dropped"]
        self.assertTrue(dropped)
        self.assertEqual(log.stats["dropped_segments"], segs[0][0] - 1)
        result = audit.verify(self.folder)
        self.assertEqual(result["result"], "ok", result)
        self.assertEqual(result["first_seq"], dropped[-1]["last_seq"] + 1)

    def test_deleting_the_oldest_kept_segment_by_hand_is_front_truncation(self):
        with mock.patch.object(audit, "SEGMENT_BYTES", 2000), \
                mock.patch.object(audit, "MAX_BYTES", 6000):
            self.fill(200).close()
        audit.segments(self.folder)[0][1].unlink()
        r = audit.verify(self.folder)
        self.assertEqual((r["result"], r["reason"]), ("broken", "front_truncated"))

    def test_deleting_the_first_segment_without_a_cap_is_front_truncation(self):
        with mock.patch.object(audit, "SEGMENT_BYTES", 2000):
            self.fill(30).close()
        audit.segments(self.folder)[0][1].unlink()
        self.assertEqual(audit.verify(self.folder)["reason"], "front_truncated")


class FieldRuleTests(Base):
    BAD = [
        ("no_such_kind", {}),
        ("meeting_discarded", {"meeting": MEETING, "title": "Synthetic"}),     # extra field
        ("meeting_discarded", {}),                                              # missing field
        ("meeting_discarded", {"meeting": MEETING.upper()}),                    # not lower hex
        ("meeting_discarded", {"meeting": "x" * 32}),                           # not hex
        ("hold_placed", {"meeting": MEETING, "hold": HOLD, "scope": "a reason"}),
        ("cleanup_done", {"meeting": MEETING, "phase": "audio", "job": JOB,
                          "originals_deleted": -1, "originals_kept": 0}),       # negative
        ("cleanup_done", {"meeting": MEETING, "phase": "audio", "job": JOB,
                          "originals_deleted": "1", "originals_kept": 0}),      # a string
        ("handoff_copied", {"meeting": MEETING, "what": "combined", "template": "built_in",
                            "names_replaced": 1, "omitted": 0, "redactions": 0}),
        ("app_start", {"version": "C:\\Users\\someone", "schema": 2}),
        ("suppressed", {"of_kind": "app_start", "count": 1}),    # only limited kinds
    ]

    def test_a_record_that_breaks_its_rules_is_refused_and_nothing_is_written(self):
        log = self.log()
        for kind, fields in self.BAD:
            with self.subTest(kind=kind, fields=fields):
                with self.assertRaises(audit.AuditError):
                    log.record(kind, **fields)
        self.assertFalse(self.folder.exists())

    def test_outside_tests_it_is_refused_and_logged_by_type_only(self):
        incidents = []
        log = self.log(strict=False, log=mock.Mock(
            incident=lambda area, exc: incidents.append((area, type(exc).__name__))))
        for kind, fields in self.BAD:
            self.assertFalse(log.record(kind, **fields))
        self.assertFalse(self.folder.exists())
        self.assertEqual(log.stats["refused"], len(self.BAD))
        self.assertEqual(set(incidents), {("audit", "AuditError")})

    def test_every_kind_has_a_distinct_event_id(self):
        ids = [v[0] for v in audit.KINDS.values()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_failed_write_never_raises_and_is_counted_at_the_next_write(self):
        log = self.fill(1)
        with mock.patch("builtins.open", side_effect=OSError(28, "No space left")):
            self.assertFalse(log.record("meeting_discarded", meeting=MEETING))
            self.assertFalse(log.record("meeting_discarded", meeting=OTHER))
        self.assertTrue(log.record("damage_continued"))
        log.close()
        written = records(self.folder)
        self.assertEqual([r["kind"] for r in written[-2:]], ["write_failed", "damage_continued"])
        self.assertEqual(written[-2]["count"], 2)
        self.assertEqual(audit.verify(self.folder)["result"], "ok")


class RateLimitTests(Base):
    def test_a_flood_from_the_page_is_counted_not_written(self):
        log = self.log()
        for _ in range(1000):
            log.record("handoff_copied", meeting=MEETING, what="combined", template="built_in",
                       names_replaced=False, omitted=0, redactions=0)
        self.assertEqual(kinds(self.folder).count("handoff_copied"), 20)
        self.assertNotIn("suppressed", kinds(self.folder))
        self.timer.t += 61                                   # the window ends
        log.record("damage_continued")
        [held] = [r for r in records(self.folder) if r["kind"] == "suppressed"]
        self.assertEqual((held["of_kind"], held["count"]), ("handoff_copied", 980))
        self.assertEqual(log.stats["suppressed"], 980)

    def test_held_back_counts_are_written_at_close(self):
        log = self.log()
        for _ in range(25):
            log.record("meeting_discarded", meeting=MEETING)
        log.close()
        [held] = [r for r in records(self.folder) if r["kind"] == "suppressed"]
        self.assertEqual(held["count"], 5)

    def test_the_hourly_total_holds_across_kinds(self):
        log = self.log()
        limited = [k for k in audit.LIMITED if not audit.KINDS[k][2]] or ["damage_continued"]
        self.assertIn("damage_continued", limited)
        for _minute in range(15):
            self.timer.t += 61
            for _ in range(20):
                log.record("damage_continued")
        self.assertEqual(kinds(self.folder).count("damage_continued"), 200)

    def test_internal_kinds_are_never_limited(self):
        self.fill(500).close()
        self.assertEqual(kinds(self.folder).count("app_start"), 500)


class MirrorTests(Base):
    def wait(self, check):
        deadline = time.monotonic() + 10
        while not check() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(check())

    def test_the_mirror_gets_each_record_in_order_without_ids(self):
        mirror = FakeMirror()
        log = self.log(mirror=mirror)
        log.record("meeting_discarded", meeting=MEETING)
        log.record("hold_placed", meeting=MEETING, hold=HOLD, scope="all")
        log.close()
        written = lines(self.folder)
        self.assertEqual(len(mirror.sent), len(written))
        for (event_id, _level, text), line in zip(mirror.sent, written, strict=True):
            record = json.loads(line)
            self.assertEqual(text, f"Unminute audit v1 chain={record['chain']} "
                                   f"seq={record['seq']} kind={record['kind']} "
                                   f"sha256={hashlib.sha256(line).hexdigest()}")
            self.assertEqual(event_id, audit.KINDS[record["kind"]][0])
            for value in (MEETING, HOLD):
                self.assertNotIn(value, text)
        self.assertEqual([e[0] for e in mirror.sent], [1000, 1210, 1400])
        self.assertEqual(mirror.sent[0][1], audit.INFO)

    def test_a_failing_mirror_never_stops_the_file_and_the_gap_is_recorded(self):
        mirror = FakeMirror()
        log = self.log(mirror=mirror)
        log.record("damage_continued")                       # seq 1, 2: mirrored
        self.wait(lambda: len(mirror.sent) == 2)
        mirror.failing = True
        for _ in range(3):                                   # seq 3 to 5: no copy
            self.assertTrue(log.record("damage_continued"))
        self.wait(lambda: log.stats["unmirrored"] == 3)
        self.assertEqual(mirror.calls, 3)                    # tried once, then waits a minute
        self.assertEqual(log.summary()["mirror"], "failing")
        mirror.failing = False
        self.timer.t += audit.MIRROR_RETRY_S + 1
        log.record("damage_continued")                       # seq 6: works again
        self.wait(lambda: any(e[0] == 1903 for e in mirror.sent))
        log.close()
        [gap] = [r for r in records(self.folder) if r["kind"] == "mirror_gap"]
        self.assertEqual((gap["first_seq"], gap["last_seq"], gap["seq"]), (3, 5, 7))
        self.assertEqual(audit.verify(self.folder)["result"], "ok")
        texts = [e[2] for e in mirror.sent]
        report = audit.compare_event_log(self.folder, read=lambda s, p: event_xml(texts))
        self.assertEqual(report["result"], "match", report)

    def test_a_mirror_that_cannot_keep_up_at_close_leaves_a_gap_record(self):
        release = threading.Event()

        class Slow(FakeMirror):
            def report(self, event_id, level, text):
                release.wait(5)
                super().report(event_id, level, text)
        mirror = Slow()
        log = self.log(mirror=mirror)
        for _ in range(3):
            log.record("damage_continued")
        with mock.patch.object(audit, "CLOSE_DRAIN_S", 0.2):
            log.close()
        release.set()
        [gap] = [r for r in records(self.folder) if r["kind"] == "mirror_gap"]
        self.assertEqual(gap["last_seq"], gap["seq"])          # covers itself
        self.assertLessEqual(gap["first_seq"], 3)


class CompareTests(Base):
    def setUp(self):
        super().setUp()
        self.mirror = FakeMirror()
        log = self.log(mirror=self.mirror)
        for i in range(10):
            log.record("meeting_discarded", meeting=MEETING if i % 2 else OTHER)
        log.close()
        self.texts = [e[2] for e in self.mirror.sent]

    def compare(self, texts, provider=audit.SOURCE):
        return audit.compare_event_log(self.folder, read=lambda s, p: event_xml(texts, provider))

    def test_the_same_log_matches(self):
        r = self.compare(self.texts)
        self.assertEqual((r["result"], r["entries"]), ("match", 11))

    def test_a_rebuilt_chain_verifies_but_differs_from_the_log(self):
        def change(record):
            if record.get("meeting") == MEETING:
                record["meeting"] = OTHER
            return record
        rechain(self.folder, change)
        self.assertEqual(audit.verify(self.folder)["result"], "ok")
        r = self.compare(self.texts)
        self.assertEqual(r["result"], "differs")
        self.assertGreaterEqual(r["mismatched"], 5)

    def test_a_tail_cut_by_three_lines(self):
        [(_, path)] = audit.segments(self.folder)
        kept = path.read_bytes().split(b"\n")[:-1][:-3]
        path.write_bytes(b"\n".join(kept) + b"\n")
        self.assertEqual(audit.verify(self.folder)["result"], "ok")
        r = self.compare(self.texts)
        self.assertEqual((r["result"], r["missing_in_file"], r["mismatched"]), ("differs", 3, 0))

    def test_an_entry_someone_else_wrote_is_unmatched(self):
        r = self.compare(self.texts + ["Unminute audit v1 nothing to see"])
        self.assertEqual((r["result"], r["unmatched"]), ("differs", 1))

    def test_a_missing_entry(self):
        r = self.compare(self.texts[:4] + self.texts[5:])
        self.assertEqual((r["result"], r["missing_in_log"]), ("differs", 1))

    def test_no_entries_is_no_log(self):
        self.assertEqual(self.compare([])["result"], "no_log")

    def test_a_document_type_declaration_is_refused(self):
        bad = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]>' + event_xml(self.texts)
        r = audit.compare_event_log(self.folder, read=lambda s, p: bad)
        self.assertEqual(r["result"], "unreadable")


class CommandLineTests(Base):
    def test_as_system_nothing_is_read(self):
        from workspace import __main__ as main
        self.fill(2).close()

        def refuse(*a, **k):
            raise AssertionError("read a file as SYSTEM")
        with mock.patch.object(main, "_system_account", return_value=True), \
                mock.patch("builtins.open", refuse), \
                mock.patch.object(Path, "read_bytes", refuse), \
                mock.patch.object(os, "listdir", refuse):
            result, code = main.verify_audit(["--verify-audit", "--event-log"],
                                             root=self.folder.parent)
        self.assertEqual((result, code), ({"result": "system_account"}, 1))

    def test_verify_and_compare_from_the_command_line(self):
        from workspace import __main__ as main
        mirror = FakeMirror()
        self.fill(2, self.log(mirror=mirror)).close()
        texts = [e[2] for e in mirror.sent]
        with mock.patch.object(main, "_system_account", return_value=False):
            result, code = main.verify_audit(["--verify-audit"], root=self.folder.parent)
            self.assertEqual((result["result"], result["records"], code), ("ok", 3, 0))
            with mock.patch.object(audit, "read_event_log",
                                   lambda s, p: event_xml(texts[:-1])):
                result, code = main.verify_audit(["--verify-audit", "--event-log"],
                                                 root=self.folder.parent)
            self.assertEqual((result["event_log"]["missing_in_log"], code), (1, 1))
            result, code = main.verify_audit(["--verify-audit", "--event-log-file", "nowhere"],
                                             root=self.folder.parent)
            self.assertEqual((result["event_log"]["result"], code), ("no_file", 1))
        self.assertNotIn(MEETING, json.dumps(result))

    def test_status_reports_the_newest_seq(self):
        from workspace import __main__ as main
        self.fill(4).close()
        self.assertEqual(main.status(self.folder.parent)["audit_seq"], 5)
        self.assertIsNone(main.status(Path(self.tmp.name) / "none")["audit_seq"])


class ImportRuleTests(unittest.TestCase):
    def test_no_ctypes_at_module_level(self):
        tree = ast.parse((ROOT / "workspace" / "audit.py").read_text(encoding="utf-8"))
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top |= {a.name.partition(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                top.add((node.module or "").partition(".")[0])
        self.assertNotIn("ctypes", top)
        probe = subprocess.run(
            [sys.executable, "-B", "-c", "import sys; import workspace.audit; "
             "print('ctypes' in sys.modules)"],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(probe.stdout.strip(), "False", probe.stderr[-500:])


class StoreNewRootTests(unittest.TestCase):
    def test_new_root_only_when_this_open_made_the_key(self):
        import schema_v1
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "workspace.db"
        old = Path(tmp.name) / "old" / "workspace.db"
        old.parent.mkdir()
        schema_v1.create(old).close()
        found = []
        for path in (db, db, old):        # new, an ordinary reopen, the 1 to 2 migration
            s = keyprotect.open_store(path)
            found.append(s.new_root)
            s.close()
        self.assertEqual(found, [True, False, True])


@unittest.skipUnless(sys.platform == "win32", "the Windows Event Log")
class WindowsEventLogTests(Base):
    """A real ReportEventW under the test source, read back with wevtutil.
    Earlier runs leave entries of other chains under the same source, so
    only this chain's counts are checked."""

    def test_a_real_entry_is_written_with_the_user_and_read_back(self):
        mirror = audit.EventLogMirror(source="Unminute-Test")
        log = self.log(mirror=mirror)
        log.record("damage_continued")
        log.close()
        self.assertEqual(log.summary()["mirror"], "ok")
        report = audit.compare_event_log(self.folder, provider="Unminute-Test")
        self.assertGreaterEqual(report["entries"], 2, report)
        self.assertEqual((report["missing_in_log"], report["missing_in_file"],
                          report["mismatched"]), (0, 0, 0), report)
        text = audit.read_event_log(None, "Unminute-Test")
        chain = records(self.folder)[0]["chain"]
        mine = [e for e in text.split("<Event ") if f"chain={chain}" in e]
        self.assertEqual(len(mine), 2)
        self.assertTrue(all("<Security UserID='S-1-" in e for e in mine))


# -- the app ------------------------------------------------------------------------------

def write_wav(path, seconds):
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(3)
    pcm = (rng.standard_normal(int(seconds * SR)) * 300).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


def fake(name):
    return f"test:fake_worker_engines:{name}"


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class WorkspaceAuditTests(unittest.TestCase):
    DEVICES = [{"index": 3, "name": f"{MARKER} microphone", "default": True, "rate": SR}]

    def setUp(self):
        from workspace import power, recorder
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mirror = FakeMirror()
        self.opened = []
        for patcher in (mock.patch.object(recorder, "list_devices", lambda: self.DEVICES),
                        mock.patch("workspace.clipboard.copy_text", return_value=1),
                        mock.patch("workspace.clipboard.clear_if_unchanged")):
            patcher.start()
            self.addCleanup(patcher.stop)

        def never(*args, **kwargs):
            raise AssertionError("a test reached the real power scheme")
        for name in ("write", "apply"):
            guard = mock.patch.object(power._PowrProf, name, never)
            guard.start()
            self.addCleanup(guard.stop)
        self.ws = None

    def tearDown(self):
        if self.ws is not None:
            self.ws.close()
        self.tmp.cleanup()

    def open(self, policy=None, **kw):
        from workspace.app import Workspace
        values = policy or {}
        ws = Workspace(self.root / "ws", self.root / "models", start=False,
                       allow_test_engines=True, battery=False,
                       speaker_model=fake("fake_diarizer"),
                       protector=kw.pop("protector", keyprotect.TestProtector()),
                       policy_reader=lambda: (values, ()), audit_mirror=self.mirror,
                       opener=self.opened.append, **kw)
        ws.settings.update(model=fake("sentences"), speakers=True)
        self.ws = ws
        return ws

    def audit_kinds(self):
        return kinds(self.ws.root / "audit")

    def test_markers_never_reach_the_audit_log(self):
        """Every recorded action, with marker words in the title, file and
        folder names, edits, speaker names, a hold reason, a watched folder,
        a template, redactions and the microphone's name: none reaches the
        audit file or its Event Log copy, and each action writes its record
        once (two for Copy and open)."""
        from workspace import api, consent, handoff, speaker_names
        sys.path.insert(0, str(ROOT / "tests"))
        from test_recorder import opener
        ws = self.open()
        ws.settings.update(microphone=True)
        holder = {}
        ws.open_stream = opener(holder)
        ws.start()
        rec = write_wav(self.root / MARKER / f"{MARKER}.wav", 30)
        self.assertEqual(ws.import_paths([str(rec)])["accepted"], 1)
        self.assertTrue(ws.wait_idle(180))
        [m] = ws.meetings()
        mid = m["id"]
        ws.rename(mid, f"{MARKER} meeting")
        first = ws.transcript(mid)["items"][0]
        ws.edit(mid, first["id"], f"{MARKER} said this")
        label = speaker_names.resolve(ws.store, ws.store.active_version(mid)["id"])["order"][0]
        ws.rename_speaker(mid, label, MARKER)
        hid = ws.add_hold(mid, "audio", f"{MARKER} reason")
        ws.keep_hold(hid)
        ws.release_hold(hid)
        watch = self.root / f"{MARKER}-watch"
        watch.mkdir()
        fid = ws.add_watched_folder(str(watch), True)
        page = api.make_api(ws, confirm=lambda q: True, decline_pause_s=0)
        self.assertTrue(page.set_folder_authority(fid, True)["ok"])
        ws.set_folder_authority(fid, delete_originals=True, confirmed=True)   # no change
        ws.set_folder_authority(fid, delete_originals=False)
        ws.remove_folder(fid)
        tid = ws.save_template(None, f"{MARKER} template", f"{MARKER} prompt")
        ws.confirm_reviewed(mid)
        ws.set_handoff_options(mid, [], [MARKER])
        preview = ws.handoff_preview(mid, tid, anonymise=True)
        ws.copy_handoff(mid, tid, "combined", preview["digests"]["combined"], anonymise=True)
        ws.save_handoff_to(mid, tid, str(self.root / f"{MARKER}.md"),
                           preview["digests"]["markdown"], anonymise=True)
        builtin = next(iter(handoff.BUILTIN))
        preview = ws.handoff_preview(mid, builtin)
        ws.copy_and_open_assistant(mid, builtin, preview["digests"]["combined"])
        self.assertTrue(ws.wait_idle(120))                  # the audio cleanup runs
        ws.confirm_final_saved(mid)
        self.assertTrue(ws.wait_idle(120))
        record_ = consent.recording_record()
        ws.start_recording(3, consent=record_)
        holder["stream"].feed(2)
        ws.stop_recording()
        self.assertTrue(ws.wait_idle(120))
        [recorded] = ws.meetings()
        ws.discard_meeting(recorded["id"])
        self.assertTrue(ws.wait_idle(120))
        ws.start_recording(3, consent=record_)
        ws.discard_recording()
        self.assertEqual(ws.audit.stats["refused"], 0)    # every call site keeps the rules
        ws.close()
        self.ws = None
        folder = self.root / "ws" / "audit"
        got = kinds(folder)
        expected = {"chain_start": 1, "app_start": 1, "meeting_imported": 2,
                    "hold_placed": 1, "hold_kept": 1, "hold_released": 1, "folder_added": 1,
                    "delete_authority": 2, "folder_removed": 1, "transcript_reviewed": 1,
                    "handoff_copied": 2, "handoff_saved": 1, "assistant_opened": 1,
                    "final_saved": 1, "cleanup_started": 4, "cleanup_done": 4,
                    "recording_started": 2, "recording_ended": 2, "meeting_discarded": 1}
        self.assertEqual({k: got.count(k) for k in set(got)}, expected)
        rs = records(folder)
        imported = [r for r in rs if r["kind"] == "meeting_imported"]
        self.assertEqual([(r["source"], r["meeting"] == mid) for r in imported],
                         [("file", True), ("recording", False)])
        started = next(r for r in rs if r["kind"] == "recording_started")
        self.assertEqual(imported[1]["recording"], started["recording"])   # consent to meeting
        self.assertEqual((started["consent"], started["wording"], started["method"]),
                         ("confirmed", consent.RECORDING_WORDING_VERSION, "app-window"))
        self.assertEqual([r["result"] for r in rs if r["kind"] == "recording_ended"],
                         ["saved", "discarded"])
        copied = [r for r in rs if r["kind"] == "handoff_copied"]
        self.assertEqual([(r["template"], r["names_replaced"], r["what"]) for r in copied],
                         [("custom", True, "combined"), ("built_in", False, "combined")])
        self.assertEqual([r["phase"] for r in rs if r["kind"] == "cleanup_started"],
                         ["audio", "transcript", "audio", "transcript"])
        for kind in ("cleanup_started", "cleanup_done"):     # started before done, per job
            self.assertEqual(len({r["job"] for r in rs if r["kind"] == kind}), 4)
        self.assertEqual(audit.verify(folder)["result"], "ok")
        # No marker, no folder of this run, anywhere it is kept or sent.
        texts = [e[2] for e in self.mirror.sent]
        self.assertEqual(len(texts), len(rs))
        everything = b"\n".join(p.read_bytes() for _, p in audit.segments(folder)) \
            + "\n".join(texts).encode()
        for needle in (MARKER, MARKER.lower(), self.root.name, "microphone"):
            self.assertNotIn(needle.encode(), everything, needle)
        report = audit.compare_event_log(folder, read=lambda s, p: event_xml(texts))
        self.assertEqual(report["result"], "match", report)

    def imported(self, ws, name="a.wav"):
        return ws._import_one(write_wav(self.root / name, 5))

    def test_refused_actions_write_nothing(self):
        from workspace import api, holds, policy
        from workspace.app import WorkspaceError
        from workspace.store import StoreError
        ws = self.open(policy={"AllowDeleteOriginals": False, "AllowWatchedFolders": True})
        mid = self.imported(ws)
        before = self.audit_kinds()
        self.assertEqual(before, ["chain_start", "app_start", "meeting_imported"])
        with self.assertRaises(WorkspaceError):
            ws.confirm_reviewed(mid)                        # not transcribed yet
        with self.assertRaises(WorkspaceError):
            ws.confirm_final_saved(mid)                     # not reviewed
        with self.assertRaises(WorkspaceError):
            ws.add_hold(mid, "audio", "")                   # no reason
        with mock.patch.object(holds, "add_hold", side_effect=StoreError("disk full")), \
                self.assertRaises(StoreError):
            ws.add_hold(mid, "audio", "Synthetic")
        watch = self.root / "watch"
        watch.mkdir()
        fid = ws.add_watched_folder(str(watch), False)
        with self.assertRaises(WorkspaceError):             # policy withholds deletion
            ws.set_folder_authority(fid, delete_originals=True, confirmed=True)
        ws.policy = policy.PolicySource(reader=lambda: ({}, ()))
        with self.assertRaises(WorkspaceError):             # no confirmation in Windows
            ws.set_folder_authority(fid, delete_originals=True)
        page = api.make_api(ws, confirm=lambda q: False, decline_pause_s=0)
        self.assertEqual(page.set_folder_authority(fid, True)["value"], api.CANCELLED)
        self.assertEqual(page.discard_meeting(mid)["value"], api.CANCELLED)
        ws.release_hold("e" * 32)                           # no such hold
        ws.keep_hold("e" * 32)
        ws.confirm_cleanup("e" * 32)
        ws.retry_cleanup("e" * 32)
        ws.remove_folder("e" * 32)
        self.assertEqual(self.audit_kinds(), before + ["folder_added"])
        self.assertEqual(ws.audit.stats["refused"], 0)

    def test_confirm_and_retry_cleanup_are_recorded_only_when_they_change_a_job(self):
        from workspace import cleanup
        ws = self.open()
        mid = self.imported(ws)
        with ws.store.tx() as db:                           # a reviewed meeting's audio job
            db.execute("UPDATE meetings SET reviewed_at = 1 WHERE id = ?", (mid,))
            jid = cleanup._add_job(db, mid, cleanup.AUDIO)
        ws.confirm_cleanup(jid)                             # not waiting: nothing changes
        ws.retry_cleanup(jid)                               # not failed: nothing changes
        with ws.store.tx() as db:
            db.execute("UPDATE cleanup_jobs SET state = 'awaiting_confirmation' WHERE id = ?",
                       (jid,))
        ws.confirm_cleanup(jid)
        with ws.store.tx() as db:
            db.execute("UPDATE cleanup_jobs SET state = 'failed' WHERE id = ?", (jid,))
        ws.retry_cleanup(jid)
        rs = records(ws.root / "audit")
        self.assertEqual([(r["kind"], r.get("job"), r.get("phase")) for r in rs[3:]],
                         [("cleanup_confirmed", jid, "audio"), ("cleanup_retry", jid, "audio")])

    def test_a_locked_start_records_locked_and_leaves_the_database_alone(self):
        from workspace.app import Workspace
        ws = self.open()
        ws.close()
        self.ws = None
        db = self.root / "ws" / "workspace.db"
        before = db.read_bytes()
        with mock.patch.object(Workspace, "_upkeep"):      # until C4a holds start-up work
            ws = self.open(protector=keyprotect.TestProtector(fail=True), on_unavailable="lock")
        self.assertIsNotNone(ws.store.locked)
        ws.close()
        self.ws = None
        self.assertEqual(db.read_bytes(), before)
        rs = records(self.root / "ws" / "audit")
        self.assertEqual([r["kind"] for r in rs], ["chain_start", "app_start", "locked"])
        self.assertEqual((rs[-1]["reason"], rs[-1]["winerror"]), ("windows", 13))
        self.assertEqual(rs[0]["previous"], "first_use")

    def test_damage_continued_is_recorded(self):
        ws = self.open()
        ws.continue_despite_damage()                        # nothing held: no record
        ws._integrity = {"state": "failed", "code": "U-000000"}
        ws._integrity_ok.clear()
        ws.continue_despite_damage()
        self.assertEqual(self.audit_kinds()[-1], "damage_continued")
        self.assertEqual(self.audit_kinds().count("damage_continued"), 1)

    def test_copy_diagnostics_shows_counts_only(self):
        ws = self.open()
        self.imported(ws)
        line = next(x for x in ws.diagnostics().splitlines() if x.startswith("Audit log:"))
        self.assertEqual(line, "Audit log: newest seq 3; 3 written this run; Event Log copy ok "
                               "(0 without a copy); failed writes 0; segments dropped 0; "
                               "suppressed 0; refused 0")

    def test_only_the_real_workspace_copies_to_the_event_log(self):
        """The app's protector (by name) on a folder outside the workspace's
        own place: no Event Log copy, and a refused record is logged, not
        raised. In its own place the copy is made (the decision only: the
        test protector is refused there, by design)."""
        from workspace import keystore
        from workspace.app import Workspace
        like_real = keyprotect.TestProtector()
        like_real.name = keystore.APP_PROTECTOR
        other = Workspace(self.root / "ws2", self.root / "models", start=False,
                          allow_test_engines=True, battery=False, protector=like_real)
        try:
            self.assertIsNone(other.audit.mirror)
            self.assertFalse(other.audit.strict)          # refused and logged, never raised
            with mock.patch("workspace.store.under_workspace_root", return_value=True), \
                    mock.patch.object(audit, "EventLogMirror", return_value="windows"), \
                    mock.patch.object(audit, "AuditLog") as made:
                other._open_audit(None)
            self.assertEqual(made.call_args.kwargs["mirror"], "windows")
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
