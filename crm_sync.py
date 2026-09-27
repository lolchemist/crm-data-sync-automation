"""Offline CRM-to-CSV synchronization using only the standard library."""

import argparse
import csv
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime


ROOT = Path(__file__).resolve().parent
FIELDS = ("id", "name", "email", "status", "updated_at")
MAX_FIELD_LENGTH = 131_072


class SyncError(Exception):
    """An actionable input, destination, or retry error."""


class TransientError(SyncError):
    """A simulated temporary CRM read failure."""


def workspace_path(value):
    """Resolve paths and disallow file access outside this project."""
    try:
        path = Path(value).resolve()
    except (RuntimeError, OSError) as exc:
        raise SyncError(f"Cannot resolve path {value}: {exc}") from exc
    if path != ROOT and ROOT not in path.parents:
        raise SyncError(f"Path must be inside {ROOT}: {value}")
    return path


def validate_records(records, source):
    if not isinstance(records, list):
        raise SyncError(f"{source}: expected a JSON array of records")
    result = []
    seen = set()
    for index, record in enumerate(records, 1):
        label = f"{source} record {index}"
        if not isinstance(record, dict):
            raise SyncError(f"{label}: expected an object")
        for field in FIELDS:
            value = record.get(field)
            if not isinstance(value, str) or not value.strip():
                raise SyncError(f"{label}: {field} must be a nonempty string")
            if len(value) > MAX_FIELD_LENGTH:
                raise SyncError(f"{label}: {field} exceeds {MAX_FIELD_LENGTH} characters")
            if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise SyncError(f"{label}: {field} contains surrounding whitespace or control characters")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", record["email"]):
            raise SyncError(f"{label}: invalid email")
        timestamp = record["updated_at"]
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", timestamp):
                raise ValueError()
            if not timestamp.endswith("Z"):
                if int(timestamp[-5:-3]) > 23 or int(timestamp[-2:]) > 59:
                    raise ValueError()
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError()
        except ValueError:
            raise SyncError(f"{label}: updated_at must be a valid timezone-aware ISO 8601 timestamp") from None
        if record["id"] in seen:
            raise SyncError(f"{label}: duplicate CRM id {record['id']!r}")
        seen.add(record["id"])
        result.append({field: record[field] for field in FIELDS})
    return result


def load_crm(path):
    try:
        with path.open(encoding="utf-8") as handle:
            return validate_records(json.load(handle), "CRM input")
    except json.JSONDecodeError as exc:
        raise SyncError(f"CRM input: malformed JSON at line {exc.lineno}, column {exc.colno}") from exc
    except RecursionError as exc:
        raise SyncError("CRM input: JSON nesting exceeds the decoder's supported depth") from exc


def load_destination(path):
    if not path.exists():
        return []
    previous_limit = csv.field_size_limit(MAX_FIELD_LENGTH)
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            if reader.fieldnames != list(FIELDS):
                raise SyncError(f"Destination CSV header must be: {','.join(FIELDS)}")
            records = list(reader)
            if any(None in record for record in records):
                raise SyncError("Destination CSV contains a row with extra columns")
            return validate_records(records, "Destination CSV")
    except csv.Error as exc:
        raise SyncError(f"Destination CSV is malformed: {exc}") from exc
    finally:
        csv.field_size_limit(previous_limit)


def write_destination(path, records):
    """Stage a complete CSV beside its destination, then atomically replace it."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=path.parent, prefix=".crm-sync-",
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(records)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def synchronize(input_path, destination_path, *, transient_failures=0,
                max_attempts=3, retry_delay=0.0, sleeper=time.sleep):
    """Merge a validated CRM snapshot; only transient read errors are retried."""
    if not isinstance(max_attempts, int) or max_attempts < 1:
        raise SyncError("max_attempts must be an integer >= 1")
    if not isinstance(transient_failures, int) or transient_failures < 0:
        raise SyncError("transient_failures must be an integer >= 0")
    if not math.isfinite(retry_delay) or retry_delay < 0:
        raise SyncError("retry_delay must be finite and >= 0")
    source = workspace_path(input_path)
    destination = workspace_path(destination_path)
    if source == destination or (destination.exists() and source.exists()
                                 and os.path.samefile(source, destination)):
        raise SyncError("Input and destination must be different files")
    for attempt in range(1, max_attempts + 1):
        try:
            if attempt <= transient_failures:
                raise TransientError("simulated transient CRM read failure")
            incoming = load_crm(source)
            break
        except TransientError as exc:
            if attempt == max_attempts:
                raise SyncError(f"CRM retry limit exhausted after {attempt} attempts: {exc}") from exc
            try:
                sleeper(retry_delay)
            except OverflowError as exc:
                raise SyncError("retry_delay exceeds the platform's supported sleep range") from exc
    existing = load_destination(destination)
    merged = {record["id"]: record for record in existing}
    inserted = updated = unchanged = 0
    for record in incoming:
        previous = merged.get(record["id"])
        if previous is None:
            inserted += 1
        elif previous != record:
            updated += 1
        else:
            unchanged += 1
        merged[record["id"]] = record
    if inserted or updated or not destination.exists():
        write_destination(destination, merged.values())
    return {"inserted": inserted, "updated": updated, "unchanged": unchanged,
            "preserved": len(existing) - updated - unchanged,
            "total": len(merged), "attempts": attempt}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Local CRM JSON array")
    parser.add_argument("--destination", required=True, help="Local CSV destination")
    parser.add_argument("--transient-failures", type=int, default=0,
                        help="Simulate this many initial CRM read failures (default: 0)")
    parser.add_argument("--max-attempts", type=int, default=3,
                        help="Total attempts including the first (default: 3)")
    parser.add_argument("--retry-delay", type=float, default=0.0,
                        help="Fixed delay in seconds between attempts (default: 0)")
    args = parser.parse_args(argv)
    try:
        result = synchronize(args.input, args.destination,
                             transient_failures=args.transient_failures,
                             max_attempts=args.max_attempts, retry_delay=args.retry_delay)
    except (SyncError, OSError, UnicodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("Sync complete: " + ", ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
