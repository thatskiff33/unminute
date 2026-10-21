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
import shutil
import sqlite3
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
# 0x8009000B (NTE_BAD_KEY_STATE) as the signed number Windows reports (#111 C4a).
BAD_KEY_STATE = -2146893813


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


def event_xml(texts, provider=audit.SOURCE, sid=None):
    """Entries as `wevtutil qe /f:xml /e:Events` prints them (with the
    writer's SID when sid is given)."""
    ns = "http://schemas.microsoft.com/win/2004/08/events/event"
    user = f"<Security UserID='{sid}'/>" if sid else ""
    body = "".join(f"<Event xmlns='{ns}'><System><Provider Name='{provider}'/>"
                   f"<EventID Qualifiers='0'>1000</EventID>{user}</System>"
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

    def test_verify_says_why_each_chain_started(self):
        """Deleting the whole folder verifies, but says the history before
        it is gone (the security review): starts counts each reason."""
        self.fill(3, self.log(new_workspace=True)).close()
        self.assertEqual(audit.verify(self.folder)["starts"],
                         {"first_use": 1, "missing": 0, "unreadable": 0})
        shutil.rmtree(self.folder)
        self.fill(2).close()
        r = audit.verify(self.folder)
        self.assertEqual((r["result"], r["chains"], r["starts"]),
                         ("ok", 1, {"first_use": 0, "missing": 1, "unreadable": 0}))


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

    def capped(self):
        return mock.patch.multiple(audit, SEGMENT_BYTES=2000, MAX_BYTES=6000)

    def test_a_hand_deletion_still_shows_after_the_next_cap_drop(self):
        """The app's next drop names the segment after the deleted one: the
        drop records' numbers then skip one (C5-4, the crypto review)."""
        with self.capped():
            self.fill(200).close()
            deleted = audit.segments(self.folder)[0][0]
            audit.segments(self.folder)[0][1].unlink()
            log = self.log()
            while audit.segments(self.folder)[0][0] == deleted + 1:
                log.record("app_start", version=None, schema=1)   # until the cap drops it
            log.close()
        dropped = [r["segment"] for r in records(self.folder) if r["kind"] == "segment_dropped"]
        self.assertEqual(dropped[-2:], [deleted - 1, deleted + 1])
        r = audit.verify(self.folder)
        self.assertEqual((r["result"], r["reason"]), ("broken", "front_truncated"))

    def test_deleting_everything_before_a_later_chain_is_front_truncation(self):
        with mock.patch.object(audit, "SEGMENT_BYTES", 2000):
            self.fill(30).close()
        newest = audit.segments(self.folder)[-1][1]
        newest.write_bytes(newest.read_bytes() + b"not a record\n")
        self.fill(5).close()                         # a new chain, unreadable, in a new segment
        segs = audit.segments(self.folder)
        self.assertEqual(audit.verify(self.folder)["reason"], "format")   # the bad line
        for _, path in segs[:-1]:
            path.unlink()
        r = audit.verify(self.folder)
        self.assertEqual((r["result"], r["reason"]), ("broken", "front_truncated"))

    def test_a_segment_with_only_a_cut_short_line_is_dropped_at_the_cap(self):
        """A cut-short first write after a rotation leaves a segment with no
        whole line. Once it is the oldest, the cap still drops it, and the
        log stays within the cap and verifies (the code review's probe)."""
        with self.capped():
            self.fill(20).close()
            number = audit.segments(self.folder)[-1][0] + 1
            planted = self.folder / audit._segment_name(number)
            planted.write_bytes(b'{"at":17')       # as a cut-short first line leaves it
            log = self.fill(300)
            log.close()
        self.assertFalse(planted.exists())
        segs = audit.segments(self.folder)
        self.assertLessEqual(sum(p.stat().st_size for _, p in segs), 6000 + 2000)
        self.assertEqual(log.stats["dropped_segments"], segs[0][0] - 1)
        result = audit.verify(self.folder)
        self.assertEqual(result["result"], "ok", result)

    def test_a_drop_record_that_cannot_be_written_yet_is_owed(self):
        with self.capped():
            log = self.fill(40)
            real = log._append
            failed = []

            def append(kind, fields, mirror=True):
                if kind == "segment_dropped" and not failed:
                    failed.append(kind)
                    raise OSError(28, "No space left")
                return real(kind, fields, mirror)
            log._append = append
            while not failed:
                log.record("app_start", version=None, schema=1)
            self.fill(5, log)                        # the owed record comes first
            log.close()
        drops = [r["segment"] for r in records(self.folder) if r["kind"] == "segment_dropped"]
        self.assertEqual(drops, list(range(drops[0], drops[0] + len(drops))))
        self.assertEqual(audit.verify(self.folder)["result"], "ok")

    def test_segment_numbers_past_six_digits(self):
        self.folder.mkdir()
        for name in (audit._segment_name(999_999), audit._segment_name(1_000_000),
                     "audit-0000001.jsonl", "audit-1.jsonl"):
            (self.folder / name).write_bytes(b"")
        self.assertEqual([n for n, _ in audit.segments(self.folder)], [999_999, 1_000_000])


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
        ("suppressed", {"of_kind": "cleanup_started", "count": 1}),   # never limited
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

    def test_imports_and_deletions_are_never_reduced_to_a_count(self):
        """The owner's decision of 21 Oct 2026: the records the app writes
        for imports and deletions are always written one by one, also once
        a page flood has spent the hour and beyond 20 in a minute."""
        log = self.log()
        for _minute in range(10):                            # the page spends the hour
            self.timer.t += 61
            for _ in range(25):
                log.record("handoff_copied", meeting=MEETING, what="combined",
                           template="built_in", names_replaced=False, omitted=0,
                           redactions=0)
        self.assertFalse(log.record("damage_continued"))     # the hour is spent
        for _ in range(30):                                  # in one minute
            self.assertTrue(log.record("meeting_imported", meeting=MEETING, source="folder",
                                       folder=OTHER, recording=None, seconds=5,
                                       delete_original=True))
            self.assertTrue(log.record("recording_ended", recording=OTHER, result="saved",
                                       seconds=5))
            self.assertTrue(log.record("cleanup_started", meeting=MEETING, phase="audio",
                                       job=JOB))
            self.assertTrue(log.record("cleanup_done", meeting=MEETING, phase="audio", job=JOB,
                                       originals_deleted=1, originals_kept=0))
        log.close()
        got = kinds(self.folder)
        for kind in sorted(audit.APP_WRITTEN):
            self.assertEqual(got.count(kind), 30, kind)
        held = {r["of_kind"] for r in records(self.folder) if r["kind"] == "suppressed"}
        self.assertEqual(held, {"handoff_copied", "damage_continued"})
        self.assertEqual(audit.APP_WRITTEN & set(audit.LIMITED), set())


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
        self.assertEqual([e[1] for e in mirror.sent], [audit.WARNING, audit.INFO, audit.INFO])

    def test_chain_start_is_a_warning_unless_it_is_first_use(self):
        """A chain restarted because the folder was missing or unreadable is
        the tamper signal IT alerts on: a warning, as the docs say."""
        found = {}
        for previous in ("first_use", "missing", "unreadable"):
            with self.subTest(previous=previous):
                shutil.rmtree(self.folder, ignore_errors=True)
                if previous == "unreadable":
                    self.fill(2).close()
                    [(_, path)] = audit.segments(self.folder)
                    path.write_bytes(path.read_bytes() + b"not a record\n")
                mirror = FakeMirror()
                log = self.log(mirror=mirror, new_workspace=previous == "first_use")
                log.record("damage_continued")
                log.close()
                [start] = [e for e in mirror.sent if e[0] == 1000]
                found[previous] = start[1]
        self.assertEqual(found, {"first_use": audit.INFO, "missing": audit.WARNING,
                                 "unreadable": audit.WARNING})

    def test_a_failed_fsync_after_a_whole_write_is_a_written_record(self):
        """Only forcing the line to disk failed: it is a written record, with
        its Event Log copy, never counted as a failed write."""
        mirror = FakeMirror()
        log = self.log(mirror=mirror)
        log.record("damage_continued")
        with mock.patch.object(audit.os, "fsync", side_effect=OSError(5, "I/O")):
            self.assertTrue(log.record("meeting_discarded", meeting=MEETING))
        log.record("damage_continued")
        log.close()
        self.assertNotIn("write_failed", kinds(self.folder))
        self.assertEqual(log.stats["failed_writes"], 0)
        texts = [e[2] for e in mirror.sent]
        self.assertEqual(len(texts), len(lines(self.folder)))
        report = audit.compare_event_log(self.folder, read=lambda s, p: event_xml(texts))
        self.assertEqual(report["result"], "match", report)
        self.assertEqual(audit.verify(self.folder)["result"], "ok")

    def test_the_close_gap_covers_a_record_owed_before_it(self):
        """A failing mirror and a failed write at close: write_failed is
        written first, and the gap still ends at its own record."""
        mirror = FakeMirror()
        log = self.log(mirror=mirror)
        log.record("damage_continued")                       # seq 1, 2: mirrored
        self.wait(lambda: len(mirror.sent) == 2)
        mirror.failing = True
        log.record("damage_continued")                       # seq 3: no copy
        self.wait(lambda: log.stats["unmirrored"] == 1)
        with mock.patch("builtins.open", side_effect=OSError(28, "No space left")):
            self.assertFalse(log.record("damage_continued"))  # not written: owed
        log.close()
        rs = records(self.folder)
        self.assertEqual([r["kind"] for r in rs[-2:]], ["write_failed", "mirror_gap"])
        gap = rs[-1]
        self.assertEqual((gap["first_seq"], gap["last_seq"], gap["seq"]), (3, 5, 5))
        texts = [e[2] for e in mirror.sent]
        report = audit.compare_event_log(self.folder, read=lambda s, p: event_xml(texts))
        self.assertEqual(report["result"], "match", report)

    def test_a_copy_still_being_reported_at_close_is_in_the_gap(self):
        release = threading.Event()

        class Stuck(FakeMirror):
            def report(self, event_id, level, text):
                release.wait(5)
                raise OSError("the event log refused it")
        log = self.log(mirror=Stuck())
        for _ in range(3):
            log.record("damage_continued")                   # seq 1 in flight, 2 to 4 queued
        with mock.patch.object(audit, "CLOSE_DRAIN_S", 0.2):
            log.close()
        release.set()
        [gap] = [r for r in records(self.folder) if r["kind"] == "mirror_gap"]
        self.assertEqual((gap["first_seq"], gap["last_seq"]), (1, gap["seq"]))

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

    def test_records_made_up_under_a_new_chain_differ(self):
        """A chain that is only in the file, after records the Event Log
        holds, is missing from the log (the crypto review's probe)."""
        chain = "e" * 32
        made_up = [{"v": 1, "chain": chain, "seq": 1, "at": 1, "kind": "chain_start",
                    "prev": audit.ZERO, "previous": "unreadable"},
                   {"v": 1, "chain": chain, "seq": 2, "at": 1, "kind": "transcript_reviewed",
                    "prev": None, "meeting": MEETING, "job": JOB}]
        out, prev = [], audit.ZERO
        for record in made_up:
            record["prev"] = prev
            line = audit._canonical(record).encode()
            prev = audit.line_hash(line)
            out.append(line)
        number = audit.segments(self.folder)[-1][0] + 1
        (self.folder / audit._segment_name(number)).write_bytes(b"\n".join(out) + b"\n")
        self.assertEqual(audit.verify(self.folder)["result"], "ok")
        r = self.compare(self.texts)
        self.assertEqual((r["result"], r["missing_in_log"]), ("differs", 2))

    def test_an_entry_someone_else_wrote_is_unmatched(self):
        r = self.compare(self.texts + ["Unminute audit v1 nothing to see"])
        self.assertEqual((r["result"], r["unmatched"]), ("differs", 1))

    def test_another_chain_is_counted_apart_and_does_not_differ(self):
        """Another workspace's chain (an exported file from several devices,
        or an earlier chain dropped at the cap) is reported, not a difference."""
        other = (f"Unminute audit v1 chain={'f' * 32} seq=1 kind=chain_start "
                 f"sha256={'0' * 64}")
        r = self.compare(self.texts + [other, "Unminute audit v1 nothing to see"])
        self.assertEqual((r["result"], r["other_chains"], r["unmatched"]), ("differs", 1, 1))
        r = self.compare(self.texts + [other])
        self.assertEqual((r["result"], r["other_chains"], r["unmatched"]), ("match", 1, 0))
        r = self.compare([other])                           # nothing of this file's chains
        self.assertEqual((r["result"], r["other_chains"]), ("no_log", 1))

    def test_only_the_users_own_entries_are_compared(self):
        """The local Application log holds every account's entries: with the
        user's SID, other accounts' (and entries with no SID) are left out
        and counted, never a difference."""
        mine, theirs = "S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002"
        stranger = ["Unminute audit v1 nothing to see"]
        xml = event_xml(self.texts, sid=mine)[:-len("</Events>")] \
            + event_xml(self.texts[:3] + stranger, sid=theirs)[len("<Events>"):-len("</Events>")] \
            + event_xml(stranger)[len("<Events>"):]
        r = audit.compare_event_log(self.folder, read=lambda s, p: xml, sid=mine)
        self.assertEqual((r["result"], r["entries"], r["other_accounts"], r["unmatched"]),
                         ("match", 11, 5, 0), r)
        r = audit.compare_event_log(self.folder, read=lambda s, p: xml)   # an exported file
        self.assertEqual((r["result"], r["other_accounts"], r["unmatched"]), ("differs", 0, 2))

    def replace_with_new_chain(self, previous):
        """Delete the folder and start a fresh chain (`previous` its start),
        then let the app write one mirrored record that continues it (the
        round 2 reviewer's probe). Returns the Event Log texts: the real
        chain's and the new record's."""
        shutil.rmtree(self.folder)
        if previous == "first_use":
            forged = self.log(new_workspace=True)     # made by hand, never mirrored
            forged.record("app_start", version=None, schema=1)
            forged.close()
        m2 = FakeMirror()                             # missing: the app's own restart
        app = self.log(mirror=m2)
        app.record("app_start", version=None, schema=2)
        app.close()
        self.assertEqual(audit.verify(self.folder)["starts"][previous], 1)
        return self.texts + [e[2] for e in m2.sent]

    def compare_mine(self, texts, theirs=()):
        """The local log, filtered by the user's SID (as --verify-audit does)."""
        mine, other = "S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002"
        xml = event_xml(texts, sid=mine)
        if theirs:
            xml = xml[:-len("</Events>")] + event_xml(theirs, sid=other)[len("<Events>"):]
        return audit.compare_event_log(self.folder, read=lambda s, p: xml, sid=mine)

    def test_a_replaced_log_with_a_first_use_chain_differs(self):
        """Round 2 finding: deleting audit\\ and starting a fresh first_use
        chain must not get past the local comparison. The user's own entries
        of a chain the file does not have make it differ, counted apart."""
        texts = self.replace_with_new_chain("first_use")
        self.assertEqual(audit.verify(self.folder)["result"], "ok")
        r = self.compare_mine(texts)
        self.assertEqual(r["result"], "differs", r)
        self.assertEqual((r["result"], r["unexplained_chains"], r["other_chains"],
                          r["missing_in_log"], r["mismatched"]), ("differs", 11, 11, 0, 0), r)

    def test_a_deleted_log_with_nothing_since_differs(self):
        shutil.rmtree(self.folder)
        r = self.compare_mine(self.texts)
        self.assertEqual(r["result"], "differs", r)
        self.assertEqual((r["result"], r["unexplained_chains"]), ("differs", 11), r)

    def test_a_chain_restarted_for_a_recorded_reason_does_not_differ(self):
        """The app's own restart after the folder went missing records why
        (chain_start previous=missing, which verify reports in starts): the
        earlier chain's entries are other_chains, not a difference."""
        texts = self.replace_with_new_chain("missing")
        r = self.compare_mine(texts)
        self.assertEqual((r["result"], r["unexplained_chains"], r["other_chains"]),
                         ("match", 0, 11), r)

    def test_another_accounts_chains_stay_excluded(self):
        other = (f"Unminute audit v1 chain={'f' * 32} seq=1 kind=chain_start "
                 f"sha256={'0' * 64}")
        r = self.compare_mine(self.texts, theirs=[other])
        self.assertEqual((r["result"], r["other_accounts"], r["other_chains"],
                          r["unexplained_chains"]), ("match", 1, 0, 0), r)

    def test_an_exported_file_reports_other_chains_without_differing(self):
        """Without a SID (an exported file, which may hold other devices'
        chains) another chain is reported, never a difference."""
        texts = self.replace_with_new_chain("first_use")
        r = self.compare(texts)
        self.assertEqual((r["result"], r["other_chains"], r["unexplained_chains"]),
                         ("match", 11, 0), r)

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
            sid = "S-1-5-21-1-2-3-1001"
            # The signed-in user's entries only (another account's are left out).
            xml = event_xml(texts[:-1], sid=sid)[:-len("</Events>")] \
                + event_xml(texts, sid="S-1-5-21-1-2-3-1002")[len("<Events>"):]
            with mock.patch.object(audit, "read_event_log", lambda s, p: xml), \
                    mock.patch.object(audit, "token_user", return_value=(b"", sid)):
                result, code = main.verify_audit(["--verify-audit", "--event-log"],
                                                 root=self.folder.parent)
            self.assertEqual((result["event_log"]["missing_in_log"],
                              result["event_log"]["other_accounts"], code), (1, 3, 1))
            result, code = main.verify_audit(["--verify-audit", "--event-log-file", "nowhere"],
                                             root=self.folder.parent)
            self.assertEqual((result["event_log"]["result"], code), ("no_file", 1))
        self.assertNotIn(MEETING, json.dumps(result))

    def test_the_account_is_read_once(self):
        """Round 3 nit: --verify-audit --event-log read the token's user twice.
        If only the second read failed, the comparison ran without the SID and
        the user's own entries of another chain were never counted. It reads
        the user once and filters with that SID."""
        from workspace import __main__ as main
        mirror = FakeMirror()
        self.fill(2, self.log(mirror=mirror, new_workspace=True)).close()   # first_use
        sid = "S-1-5-21-1-2-3-1001"
        gone = (f"Unminute audit v1 chain={'f' * 32} seq=1 kind=chain_start "
                f"sha256={'0' * 64}")                 # the user's own, of a deleted chain
        xml = event_xml([e[2] for e in mirror.sent] + [gone], sid=sid)
        with mock.patch.object(audit, "read_event_log", lambda s, p: xml), \
                mock.patch.object(audit, "token_user",
                                  side_effect=[(b"", sid), None]) as user:
            result, code = main.verify_audit(["--verify-audit", "--event-log"],
                                             root=self.folder.parent)
        self.assertEqual((result["event_log"]["result"],
                          result["event_log"]["unexplained_chains"], code),
                         ("differs", 1, 1), result)
        self.assertEqual(user.call_count, 1)
        with mock.patch.object(audit, "token_user",
                               side_effect=[None, (b"", sid)]) as user:   # cannot tell
            self.assertEqual(main.verify_audit(["--verify-audit", "--event-log"],
                                               root=self.folder.parent),
                             ({"result": "account_unknown"}, 1))
        self.assertEqual(user.call_count, 1)

    def test_no_account_never_compares_the_local_log_unfiltered(self):
        """Off Windows the token's user is False: nothing to filter the local
        log by, so it is account_unknown, never a match of every account."""
        from workspace import __main__ as main
        self.fill(2).close()

        def refuse(*a, **k):
            raise AssertionError("read the event log without a SID")
        with mock.patch.object(audit, "read_event_log", refuse), \
                mock.patch.object(audit, "token_user", return_value=False):
            result, code = main.verify_audit(["--verify-audit", "--event-log"],
                                             root=self.folder.parent)
        self.assertEqual((result["result"], result["event_log"], code),
                         ("ok", {"result": "account_unknown"}, 1))

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

    def test_no_key_or_store_module_is_imported(self):
        # The log is plain metadata (P3) written while the workspace is
        # locked (#111 C4a): it seals and opens nothing, so importing it
        # loads none of the key, encryption or store modules.
        names = ("workspace.keystore", "workspace.vault", "workspace.dpapi",
                 "workspace.audiocrypt", "workspace.store", "cryptography")
        probe = subprocess.run(
            [sys.executable, "-B", "-c", "import sys; import workspace.audit; "
             f"print(sorted(m for m in sys.modules if m.startswith({names!r})))"],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(probe.stdout.strip(), "[]", probe.stderr[-500:])


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
        report = audit.compare_event_log(self.folder, provider="Unminute-Test",
                                         sid=audit.token_user()[1])
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

    def test_a_failing_cleanup_retried_by_the_loop_is_recorded_once(self):
        """The processing loop retries a failed job (every minute; at once
        here): one cleanup_started for the first attempt, one more after Try
        again, and cleanup_done when it finally works (#289 review)."""
        from workspace import app as app_module
        from workspace import cleanup
        ws = self.open()
        mid = self.imported(ws)
        with ws.store.tx() as db:                           # a reviewed meeting's audio job
            db.execute("UPDATE meetings SET reviewed_at = 1 WHERE id = ?", (mid,))
            jid = cleanup._add_job(db, mid, cleanup.AUDIO)

        def run_due():
            work = ws._next_work()
            self.assertEqual(work, ("cleanup", mid, jid))
            ws._run(*work)
        with mock.patch.object(app_module, "FAILED_RETRY_S", 0):
            with mock.patch.object(cleanup, "_remove_audio", side_effect=OSError(13, "in use")):
                for _ in range(5):
                    run_due()
                ws.retry_cleanup(jid)
                for _ in range(3):
                    run_due()
            run_due()                                       # it works now
        self.assertIsNone(ws._job_row(jid))                 # the receipt replaced it
        rs = records(ws.root / "audit")
        self.assertEqual([r["kind"] for r in rs if r["kind"].startswith("cleanup_")],
                         ["cleanup_started", "cleanup_retry", "cleanup_started",
                          "cleanup_done"])
        self.assertEqual(ws.audit.stats["suppressed"], 0)

    def locked_run(self, protector):
        """A locked start as the app makes one (#111 C4a: on_unavailable="lock",
        the start-up threads running, work held), Copy diagnostics, then close."""
        ws = self.open(protector=protector, on_unavailable="lock")
        ws.start()
        self.assertIsNotNone(ws.locked)
        text = ws.diagnostics()
        ws.close()
        self.ws = None
        return ws, text

    def locked_records(self):
        return [r for r in records(self.root / "ws" / "audit") if r["kind"] == "locked"]

    def test_a_locked_start_records_locked_and_leaves_the_database_alone(self):
        ws = self.open()
        ws.close()
        self.ws = None
        db = self.root / "ws" / "workspace.db"
        before = db.read_bytes()
        ws, _ = self.locked_run(keyprotect.TestProtector(fail=True, code=BAD_KEY_STATE))
        self.assertEqual(db.read_bytes(), before)
        rs = records(self.root / "ws" / "audit")
        self.assertEqual([r["kind"] for r in rs], ["chain_start", "app_start", "locked"])
        self.assertEqual((rs[-1]["reason"], rs[-1]["winerror"]), ("windows", BAD_KEY_STATE))
        self.assertEqual((ws.locked.reason, ws.locked.code), ("windows", BAD_KEY_STATE))
        self.assertEqual(rs[0]["previous"], "first_use")

    def test_each_kind_of_locked_start_is_recorded(self):
        # Workspace.locked's reason, whichever way the store opened locked:
        # another protector (the key check), and rows with no schema version
        # (found before the key is read, "missing").
        ws = self.open()
        ws.store.create_meeting("Synthetic meeting")      # rows: "missing" locks, never rebuilds
        ws.close()
        self.ws = None
        other = keyprotect.TestProtector()
        other.name = "other"
        first, _ = self.locked_run(other)
        db = self.root / "ws" / "workspace.db"
        con = sqlite3.connect(db)
        try:
            con.execute("DELETE FROM schema_version")
            con.commit()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()
        second, _ = self.locked_run(keyprotect.TestProtector())
        self.assertEqual([(r["reason"], r["winerror"]) for r in self.locked_records()],
                         [(first.locked.reason, None), ("missing", None)])
        self.assertEqual(first.locked.reason, "protector")
        self.assertNotIn("app_start", kinds(self.root / "ws" / "audit")[2:])
        self.assertEqual(audit.verify(self.root / "ws" / "audit")["result"], "ok")

    def test_a_locked_start_takes_the_windows_error_number_as_keys_json_does(self):
        # diag.windows_error's rule (#111 C4a; keys.json, Copy diagnostics):
        # a 32-bit number, signed or not, else none. A number outside it is
        # recorded as none, never refused, so a locked start is always in
        # the log with the number IT is shown.
        from workspace import diag
        ws = self.open()
        ws.close()
        self.ws = None
        for code in (2 ** 32 - 1, 10 ** 12):
            ws, _ = self.locked_run(keyprotect.TestProtector(fail=True, code=code))
            self.assertEqual(ws.audit.stats["refused"], 0)
        self.assertEqual([r["winerror"] for r in self.locked_records()], [2 ** 32 - 1, None])
        self.assertIsNone(diag.keys_state(ws.log.folder)["winerror"])

    def test_no_audit_write_needs_the_key_while_locked(self):
        # The audit log is plain metadata (P3) in its own file, not the
        # database: a locked run (start, Copy diagnostics, close) asks Windows
        # for the key once, the store's own attempt, and every record of the
        # run is written and chained.
        ws = self.open()
        ws.close()
        self.ws = None
        asked = []

        class Counting(keyprotect.TestProtector):
            def unprotect(self, blob):
                asked.append(len(blob))
                return super().unprotect(blob)

        ws, text = self.locked_run(Counting(fail=True))
        self.assertEqual(len(asked), 1)
        self.assertIsNone(ws.store.keyring)
        summary = ws.audit.summary()
        self.assertGreaterEqual(summary["written"], 1)
        self.assertEqual((summary["refused"], summary["failed_writes"], summary["unmirrored"]),
                         (0, 0, 0))
        self.assertEqual(kinds(self.root / "ws" / "audit")[-1], "locked")
        self.assertEqual(audit.verify(self.root / "ws" / "audit")["result"], "ok")
        self.assertIn("Audit log: newest seq 3;", text)

    def test_a_watched_folder_import_names_its_folder(self):
        ws = self.open()
        watch = self.root / "watch"
        watch.mkdir()
        fid = ws.add_watched_folder(str(watch), False)
        ws.set_folder_authority(fid, delete_originals=True, confirmed=True)
        mid = ws._import_one(write_wav(watch / "a.wav", 5), folder_id=fid)
        [r] = [r for r in records(ws.root / "audit") if r["kind"] == "meeting_imported"]
        self.assertEqual((r["meeting"], r["source"], r["folder"], r["recording"],
                          r["seconds"], r["delete_original"]),
                         (mid, "folder", fid, None, 5, True))

    def crashed_recording(self, ws, holder, seconds):
        """A recording the app did not stop (as test_recorder's crash tests)."""
        from workspace import consent
        ws.start_recording(3, consent=consent.recording_record())
        holder["stream"].feed(seconds)
        rec = ws._recording
        time.sleep(0.6)
        ws._recording = None
        rec._stop.set()
        rec._writer.join(5)
        journal = json.loads((rec.folder / "journal.json").read_text())
        journal["state"] = "recording"
        (rec.folder / "journal.json").write_text(json.dumps(journal))
        return rec.folder.name

    def reopen(self, holder):
        sys.path.insert(0, str(ROOT / "tests"))
        from test_recorder import opener
        if self.ws is not None:
            self.ws.close()
        ws = self.open()
        ws.settings.update(microphone=True)
        ws.open_stream = opener(holder)
        ws.start()
        self.assertTrue(ws.wait_idle(120))
        return ws

    def test_a_recovered_recording_and_a_deleted_one_are_recorded(self):
        holder = {}
        ws = self.reopen(holder)
        kept = self.crashed_recording(ws, holder, 3)
        ws = self.reopen(holder)                            # the next start finds it
        waiting = ws.recording_status()["reports"][-1]
        self.assertTrue(waiting.get("recovered"), waiting)
        ws.retry_recording_import(waiting["id"])            # Keep and transcribe
        self.assertTrue(ws.wait_idle(120))
        deleted = self.crashed_recording(ws, holder, 2)
        ws = self.reopen(holder)
        waiting = ws.recording_status()["reports"][-1]
        ws.delete_pending_recording(waiting["id"])
        ws.close()
        self.ws = None
        rs = records(self.root / "ws" / "audit")
        ended = [(r["recording"], r["result"]) for r in rs if r["kind"] == "recording_ended"]
        self.assertEqual(ended, [(kept, "recovered")])
        [imported] = [r for r in rs if r["kind"] == "meeting_imported"]
        self.assertEqual((imported["source"], imported["recording"]), ("recording", kept))
        self.assertEqual([r["recording"] for r in rs if r["kind"] == "recording_deleted"],
                         [deleted])
        self.assertEqual([r["recording"] for r in rs if r["kind"] == "recording_started"],
                         [kept, deleted])
        self.assertEqual(audit.verify(self.root / "ws" / "audit")["result"], "ok")

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
