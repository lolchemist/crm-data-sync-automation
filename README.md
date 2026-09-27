# CRM sync demo

Offline one-way CRM JSON to CSV simulator. Python 3.9+ and the standard
library only. No network calls, dependencies, credentials, or real customer data.

## Run

From the repository root:

```sh
python3 -B projects/crm-sync-demo/crm_sync.py --input projects/crm-sync-demo/fixtures/initial.json --destination projects/crm-sync-demo/destination.csv
# Repeat: unchanged=2, with no duplicate rows.
python3 -B projects/crm-sync-demo/crm_sync.py --input projects/crm-sync-demo/fixtures/initial.json --destination projects/crm-sync-demo/destination.csv
# Update crm-001, insert crm-003, preserve crm-002.
python3 -B projects/crm-sync-demo/crm_sync.py --input projects/crm-sync-demo/fixtures/updated.json --destination projects/crm-sync-demo/destination.csv
# Two simulated failures followed by success on attempt three.
python3 -B projects/crm-sync-demo/crm_sync.py --input projects/crm-sync-demo/fixtures/initial.json --destination projects/crm-sync-demo/retry-demo.csv --transient-failures 2 --max-attempts 3 --retry-delay 0.1
# Exhaust retries; exits 1 without modifying the destination.
python3 -B projects/crm-sync-demo/crm_sync.py --input projects/crm-sync-demo/fixtures/initial.json --destination projects/crm-sync-demo/retry-demo.csv --transient-failures 3 --max-attempts 3
# Validation error; exits 1 without modifying the destination.
python3 -B projects/crm-sync-demo/crm_sync.py --input projects/crm-sync-demo/fixtures/invalid.json --destination projects/crm-sync-demo/destination.csv
```

`--help` describes all options. Relative paths use the current working directory;
all input/output paths must resolve inside this project, including symlink targets.
The destination's parent directory must already exist. A missing destination is
created. Success prints inserted, updated, unchanged, preserved, total and attempts
counts. Runtime/input errors exit 1; argparse usage errors exit 2.

## Data contract and merge rules

Input is a UTF-8 JSON array of objects. Required fields:

| Field | Validation |
| --- | --- |
| `id` | Nonempty string; unique, case-sensitive CRM identifier |
| `name` | Nonempty string |
| `email` | Basic `local@domain.suffix` shape; no whitespace |
| `status` | Nonempty string; no restricted enumeration |
| `updated_at` | Valid ISO 8601 date/time with seconds and `Z` or `±HH:MM` timezone; optional fractional seconds |

All fields reject surrounding whitespace and ASCII control characters. Numeric
IDs are rejected rather than silently converted. Extra JSON fields are ignored.
Email validation is deliberately basic, not a deliverability or full RFC check.
Each required field is limited to 131,072 Unicode characters (inclusive), matching
the CSV parser limit used when reading the destination. Oversized input or
destination fields are rejected before writing. Offset hours must be 00–23 and
minutes 00–59; invalid offsets are rejected rather than normalized. Excessively
nested JSON and path resolution failures are reported as input errors.
Accepted fractional-second precision follows the running Python version's
`datetime.fromisoformat` parser.

CSV columns must be exactly `id,name,email,status,updated_at` in that order.
Existing rows receive the same validation as incoming rows. Duplicate IDs in
either file are rejected, including identical duplicates. Malformed JSON/CSV,
missing fields, invalid timestamps and invalid UTF-8 cause failure before writing.

CRM fields replace all five destination fields for a matching ID if any changes.
`updated_at` is validated but is not a freshness gate: the supplied snapshot wins,
even if older. Existing row order is retained, new IDs append in input order, and
rows absent from the snapshot remain. An empty input preserves existing data;
with a missing destination it creates a header-only CSV. Unchanged syncs do not
rewrite the destination. CSV quoting supports commas, quotes and Unicode.

Writes use a temporary file in the destination directory, flush/fsync, and atomic
replacement. Validation and retry failures leave the destination unchanged;
staging files are cleaned up if writing/replacement fails.

## Retry simulation

`--transient-failures N` fails the first N CRM read attempts on each invocation.
`--max-attempts` (default 3) includes the first attempt, and must be at least 1.
`--retry-delay` is a finite nonnegative fixed delay in seconds (default 0).
If the platform cannot represent the requested sleep duration, the retry fails
with a clear error and CLI exit 1, without modifying the destination.
Only simulated transient read errors are retried. Permanent validation and file
errors are not retried. Retries finish before any destination write.

## Tests

From the repository root:

```sh
python3 -B -m unittest discover -s projects/crm-sync-demo/tests -v
```

Or from this directory: `python3 -B -m unittest discover -s tests -v`.
Tests use temporary directories inside this project and clean them up. They cover
the acceptance scenarios, validation, CLI behavior, path restrictions, atomic
replacement failure, sample fixtures, and retry bounds without real delays.

## Limitations and review

This is a local simulator: no API integration or authentication. All records are
loaded into memory. Run one writer at a time; there is no concurrent-writer lock
or hostile-filesystem race protection. Atomic replacement does not preserve
existing file metadata, and directory entries are not fsynced for power-loss
recovery. CSV values are stored verbatim; spreadsheet formula-like strings are
not neutralized. Use a text CSV viewer for untrusted data.

Human review should confirm the documented field formats, unrestricted statuses,
source-wins conflict policy, and suitability of the sample CLI workflow before
adapting this demo to a real integration.
