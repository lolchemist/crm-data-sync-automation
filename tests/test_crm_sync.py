"""Acceptance and failure-safety tests, entirely inside the project."""
import csv
import importlib.util
import json
import io
from contextlib import redirect_stderr
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("crm_sync", ROOT / "crm_sync.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def record(identifier="crm-001", **changes):
    value = dict(id=identifier, name="Demo Person", email="demo@example.test",
                 status="active", updated_at="2026-09-01T12:00:00Z")
    value.update(changes)
    return value


SCHEMA = ("id", "name", "email", "status", "updated_at")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "input.json"
        self.destination = self.folder / "destination.csv"
        self.put([record()])

    def put(self, data):
        self.source.write_text(json.dumps(data), encoding="utf-8")

    def run_sync(self, **kwargs):
        return sync.synchronize(self.source, self.destination, **kwargs)

    def rows(self):
        with self.destination.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(ROOT / "crm_sync.py"),
                               "--input", str(self.source), "--destination",
                               str(self.destination), *args], capture_output=True, text=True)

    def test_initial_multiple_and_repeat_without_duplicates(self):
        self.put([record(), record("crm-002")])
        self.assertEqual(self.run_sync()["inserted"], 2)
        before = self.destination.read_bytes()
        result = self.run_sync()
        self.assertEqual(result["unchanged"], 2)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(self.destination.read_bytes(), before)
        self.assertEqual(len(self.rows()), 2)

    def test_update_insert_and_preserve_absent_rows(self):
        absent = record("absent")
        self.put([record(), absent])
        self.run_sync()
        changed = record(name="Updated Name", status="inactive",
                         updated_at="2026-09-02T13:00:00+01:00")
        self.put([changed, record("new")])
        result = self.run_sync()
        self.assertEqual(result, dict(inserted=1, updated=1, unchanged=0,
                                     preserved=1, total=3, attempts=1))
        self.assertEqual(self.rows(), [changed, absent, record("new")])

    def test_empty_input_preserves_destination(self):
        self.run_sync()
        before = self.destination.read_bytes()
        self.put([])
        self.assertEqual(self.run_sync()["preserved"], 1)
        self.assertEqual(self.destination.read_bytes(), before)

    def test_empty_initial_input_creates_header(self):
        self.put([])
        self.assertEqual(self.run_sync()["total"], 0)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.destination.read_text().strip(), ",".join(SCHEMA))

    def test_malformed_json(self):
        self.source.write_text('[{', encoding="utf-8")
        with self.assertRaisesRegex(sync.SyncError, "malformed JSON at line"):
            self.run_sync()
        self.assertFalse(self.destination.exists())

    def test_wrong_json_shapes(self):
        for value in ({}, None, [1], [None]):
            with self.subTest(value=value):
                self.put(value)
                with self.assertRaises(sync.SyncError):
                    self.run_sync()

    def test_each_required_field_missing_or_invalid(self):
        for field in SCHEMA:
            for value in (None, "", "   ", 123, True, [], " leading", "bad\nvalue"):
                with self.subTest(field=field, value=value):
                    self.put([record(**{field: value})])
                    with self.assertRaisesRegex(sync.SyncError, field):
                        self.run_sync()
            missing = record()
            del missing[field]
            self.put([missing])
            with self.assertRaisesRegex(sync.SyncError, field):
                self.run_sync()

    def test_ascii_del_rejected_in_each_required_field(self):
        for field in SCHEMA:
            with self.subTest(field=field):
                value = record()
                value[field] += "\x7f"
                self.put([value])
                with self.assertRaisesRegex(sync.SyncError, f"{field}.*control characters"):
                    self.run_sync()
                self.assertFalse(self.destination.exists())

    def test_invalid_emails(self):
        for email in ("invalid", "a@@b.test", "a@b", "a b@c.test"):
            with self.subTest(email=email):
                self.put([record(email=email)])
                with self.assertRaisesRegex(sync.SyncError, "email"):
                    self.run_sync()

    def test_invalid_timestamps(self):
        for timestamp in ("yesterday", "2026-02-30T12:00:00Z", "2026-09-01",
                          "2026-09-01T12:00:00", "2026-09-01T25:00:00Z"):
            with self.subTest(timestamp=timestamp):
                self.put([record(updated_at=timestamp)])
                with self.assertRaisesRegex(sync.SyncError, "updated_at"):
                    self.run_sync()

    def test_duplicate_source_ids_rejected_without_mutation(self):
        self.run_sync()
        before = self.destination.read_bytes()
        self.put([record(), record(name="Conflict")])
        with self.assertRaisesRegex(sync.SyncError, "duplicate CRM id"):
            self.run_sync()
        self.assertEqual(self.destination.read_bytes(), before)

    def test_duplicate_destination_ids_rejected(self):
        sync.write_destination(self.destination, [record(), record()])
        before = self.destination.read_bytes()
        with self.assertRaisesRegex(sync.SyncError, "duplicate CRM id"):
            self.run_sync()
        self.assertEqual(self.destination.read_bytes(), before)

    def test_invalid_destination_csv_unchanged(self):
        header = ",".join(SCHEMA) + "\n"
        for content in ("", "id,name\n1,Demo\n", header + "x,Demo\n",
                        header + '"unterminated',
                        header + "x,Demo,d@example.test,active,2026-09-01T12:00:00Z,extra\n"):
            with self.subTest(content=content):
                self.destination.write_text(content, encoding="utf-8")
                with self.assertRaises(sync.SyncError):
                    self.run_sync()
                self.assertEqual(self.destination.read_text(), content)

    def test_transient_failures_then_success(self):
        sleeper = Mock()
        result = self.run_sync(transient_failures=2, max_attempts=3,
                               retry_delay=0.25, sleeper=sleeper)
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(sleeper.call_count, 2)
        sleeper.assert_called_with(0.25)
        self.assertEqual(self.rows(), [record()])

    def test_retry_exhaustion_preserves_destination(self):
        self.run_sync()
        before = self.destination.read_bytes()
        sleeper = Mock()
        with self.assertRaisesRegex(sync.SyncError, "exhausted after 3 attempts"):
            self.run_sync(transient_failures=5, max_attempts=3, sleeper=sleeper)
        self.assertEqual(sleeper.call_count, 2)
        self.assertEqual(self.destination.read_bytes(), before)

    def test_one_attempt_does_not_sleep(self):
        sleeper = Mock()
        with self.assertRaises(sync.SyncError):
            self.run_sync(transient_failures=1, max_attempts=1, sleeper=sleeper)
        sleeper.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_validation_errors_are_not_retried(self):
        self.put([record(email="bad")])
        sleeper = Mock()
        with self.assertRaises(sync.SyncError):
            self.run_sync(sleeper=sleeper)
        sleeper.assert_not_called()

    def test_invalid_retry_options(self):
        for kwargs in (dict(max_attempts=0), dict(transient_failures=-1),
                       dict(retry_delay=-1), dict(retry_delay=float("nan")),
                       dict(retry_delay=float("inf"))):
            with self.subTest(kwargs=kwargs), self.assertRaises(sync.SyncError):
                self.run_sync(**kwargs)

    def test_atomic_replace_failure_keeps_original_and_cleans_temp(self):
        self.run_sync()
        before = self.destination.read_bytes()
        self.put([record(name="Change")])
        with patch.object(sync.os, "replace", side_effect=OSError("simulated disk error")):
            with self.assertRaises(OSError):
                self.run_sync()
        self.assertEqual(self.destination.read_bytes(), before)
        self.assertEqual(list(self.folder.glob(".crm-sync-*")), [])

    def test_unicode_and_csv_quoting(self):
        expected = record(name='Demo, "Zoë"')
        self.put([expected])
        self.run_sync()
        self.assertEqual(self.rows(), [expected])

    def test_external_paths_and_symlinks_rejected(self):
        outside = ROOT.parent / "outside.csv"
        with self.assertRaisesRegex(sync.SyncError, "inside"):
            sync.synchronize(self.source, outside)
        link = self.folder / "link"
        link.symlink_to(ROOT.parent, target_is_directory=True)
        with self.assertRaisesRegex(sync.SyncError, "inside"):
            sync.synchronize(self.source, link / "outside.csv")

    def test_same_input_destination_rejected(self):
        before = self.source.read_bytes()
        with self.assertRaisesRegex(sync.SyncError, "different files"):
            sync.synchronize(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), before)

    def test_cli_success_and_retry(self):
        result = self.cli("--transient-failures", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("inserted=1", result.stdout)
        self.assertIn("attempts=2", result.stdout)

    def test_cli_failures_are_clear_without_tracebacks(self):
        for args in (("--max-attempts", "0"), ("--retry-delay", "nan"),
                     ("--transient-failures", "3")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Error:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
        self.source.unlink()
        self.assertEqual(self.cli().returncode, 1)

    def test_cli_help_and_required_arguments(self):
        command = [sys.executable, "-B", str(ROOT / "crm_sync.py")]
        help_result = subprocess.run(command + ["--help"], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--max-attempts", help_result.stdout)
        missing = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)

    def write_raw_destination(self, rows):
        with self.destination.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SCHEMA)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def boundary_record(field, length):
        if field == "email":
            value = "a" * (length - len("@example.test")) + "@example.test"
        elif field == "updated_at":
            prefix = "2026-09-01T12:00:00."
            value = prefix + "0" * (length - len(prefix) - 1) + "Z"
        else:
            value = "é" * length
        return record(**{field: value})

    def test_field_length_boundaries_in_crm(self):
        self.run_sync()
        for field in SCHEMA:
            # Timestamp syntax has additional interpreter-dependent precision limits.
            lengths = (131073,) if field == "updated_at" else (131071, 131072, 131073)
            for length in lengths:
                with self.subTest(field=field, length=length):
                    value = self.boundary_record(field, length)
                    self.put([value])
                    before = self.destination.read_bytes()
                    if length > 131072:
                        with self.assertRaisesRegex(sync.SyncError, "exceeds 131072 characters"):
                            self.run_sync()
                        self.assertEqual(self.destination.read_bytes(), before)
                    else:
                        self.run_sync()
                        self.assertIn(value, self.rows())

    def test_field_length_boundaries_in_destination(self):
        self.put([])
        for field in SCHEMA:
            # Timestamp syntax has additional interpreter-dependent precision limits.
            lengths = (131073,) if field == "updated_at" else (131071, 131072, 131073)
            for length in lengths:
                with self.subTest(field=field, length=length):
                    self.write_raw_destination([self.boundary_record(field, length)])
                    before = self.destination.read_bytes()
                    if length > 131072:
                        with self.assertRaisesRegex(sync.SyncError, "field larger than field limit"):
                            self.run_sync()
                    else:
                        self.assertEqual(self.run_sync()["preserved"], 1)
                    self.assertEqual(self.destination.read_bytes(), before)

    def test_max_length_write_then_repeat_round_trip(self):
        expected = record(name="é" * 131072)
        self.put([expected])
        self.assertEqual(self.run_sync()["inserted"], 1)
        before = self.destination.read_bytes()
        with patch.object(sync, "write_destination") as writer:
            self.assertEqual(self.run_sync()["unchanged"], 1)
            writer.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), before)
        self.assertEqual(self.rows(), [expected])

    def test_destination_parser_limit_is_explicit_and_restored(self):
        self.put([record(name="x" * 200)])
        self.run_sync()
        previous = csv.field_size_limit(100)
        try:
            self.assertEqual(self.run_sync()["unchanged"], 1)
            self.assertEqual(csv.field_size_limit(), 100)
        finally:
            csv.field_size_limit(previous)

    def test_invalid_timezone_offsets_in_crm_and_destination(self):
        for offset in ("+00:99", "+12:60", "-00:99", "-12:60", "+24:00", "-24:00"):
            for location in ("CRM", "destination"):
                with self.subTest(offset=offset, location=location):
                    invalid = record(updated_at="2026-09-01T12:00:00" + offset)
                    self.put([invalid] if location == "CRM" else [record()])
                    self.write_raw_destination([record()] if location == "CRM" else [invalid])
                    before = self.destination.read_bytes()
                    with self.assertRaisesRegex(sync.SyncError, "updated_at"):
                        self.run_sync()
                    self.assertEqual(self.destination.read_bytes(), before)

    def test_valid_timezone_offset_boundaries(self):
        for offset in ("+00:00", "-00:00", "+23:59", "-23:59"):
            with self.subTest(offset=offset):
                expected = record(updated_at="2026-09-01T12:00:00" + offset)
                self.put([expected])
                self.run_sync()
                self.assertEqual(self.rows(), [expected])

    def test_retry_delay_overflow_cli(self):
        self.run_sync()
        before = self.destination.read_bytes()
        result = self.cli("--transient-failures", "1", "--retry-delay", "1e300")
        self.assertEqual(result.returncode, 1)
        self.assertIn("retry_delay", result.stderr)
        self.assertIn("supported sleep range", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.destination.read_bytes(), before)

    def test_retry_delay_overflow_becomes_sync_error(self):
        with self.assertRaisesRegex(sync.SyncError, "retry_delay"):
            self.run_sync(transient_failures=1,
                          sleeper=Mock(side_effect=OverflowError("too large")))
        self.assertFalse(self.destination.exists())

    def test_deep_json_preserves_destination_and_cli_has_no_traceback(self):
        self.run_sync()
        before = self.destination.read_bytes()
        depth = max(10000, sys.getrecursionlimit() * 2)
        self.source.write_text("[" * depth + "0" + "]" * depth, encoding="utf-8")
        with self.assertRaisesRegex(sync.SyncError, "JSON nesting"):
            self.run_sync()
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("JSON nesting", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.destination.read_bytes(), before)

    def test_resolution_runtime_error_becomes_sync_error_and_cli_exit_one(self):
        self.run_sync()
        before = self.destination.read_bytes()
        error = io.StringIO()
        with patch.object(sync.Path, "resolve", side_effect=RuntimeError("Symlink loop")):
            with self.assertRaisesRegex(sync.SyncError, "Cannot resolve path"):
                self.run_sync()
            with redirect_stderr(error):
                code = sync.main(["--input", str(self.source),
                                  "--destination", str(self.destination)])
        self.assertEqual(code, 1)
        self.assertIn("Cannot resolve path", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())
        self.assertEqual(self.destination.read_bytes(), before)

    def test_isolated_field_updates(self):
        changes = dict(name="Another Name", email="other@example.test", status="inactive",
                       updated_at="2026-09-02T12:00:00Z")
        for field, value in changes.items():
            with self.subTest(field=field):
                self.write_raw_destination([record()])
                expected = record(**{field: value})
                self.put([expected])
                result = self.run_sync()
                self.assertEqual(result["updated"], 1)
                self.assertEqual(result["inserted"], 0)
                self.assertEqual(self.rows(), [expected])

    def test_older_snapshot_replaces_destination(self):
        self.run_sync()
        expected = record(name="Older snapshot", updated_at="2020-01-01T00:00:00Z")
        self.put([expected])
        self.assertEqual(self.run_sync()["updated"], 1)
        self.assertEqual(self.rows(), [expected])

    def test_literal_five_field_schema(self):
        self.put([dict(record(), extra="ignored")])
        self.run_sync()
        with self.destination.open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            self.assertEqual(next(reader), ["id", "name", "email", "status", "updated_at"])
            self.assertEqual(len(next(reader)), 5)
        self.assertEqual(self.rows(), [record()])

    def test_unchanged_repeat_does_not_call_writer(self):
        self.run_sync()
        with patch.object(sync, "write_destination") as writer:
            self.assertEqual(self.run_sync()["unchanged"], 1)
            writer.assert_not_called()

    def test_invalid_utf8_input_cli_preserves_destination(self):
        self.run_sync()
        before = self.destination.read_bytes()
        self.source.write_bytes(b'["\xff"]')
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Error:", result.stderr)
        self.assertIn("utf-8", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.destination.read_bytes(), before)

    def test_sample_fixtures(self):
        sync.synchronize(ROOT / "fixtures" / "initial.json", self.destination)
        result = sync.synchronize(ROOT / "fixtures" / "updated.json", self.destination)
        self.assertEqual((result["inserted"], result["updated"], result["preserved"]), (1, 1, 1))
        with self.assertRaises(sync.SyncError):
            sync.synchronize(ROOT / "fixtures" / "invalid.json", self.destination)


if __name__ == "__main__":
    unittest.main()
