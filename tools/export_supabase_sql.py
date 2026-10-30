#!/usr/bin/env python3
"""Write SQL files for loading the local dispatch history into Supabase.

Produces two files to paste into the Supabase SQL editor:

    data/supabase_check.sql      read-only: what is in dispatch_tracking now
    data/supabase_dedupe.sql     removes duplicate rows already in the table (optional)
    data/supabase_backfill.sql   inserts every tracking number not already there

The backfill runs in one transaction and compares tracking numbers with
spaces removed, so it can be run more than once without creating duplicates
and it never touches existing rows.

Usage:
    python tools/export_supabase_sql.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "dispatches.db"
CHECK_PATH = REPO_ROOT / "data" / "supabase_check.sql"
BACKFILL_PATH = REPO_ROOT / "data" / "supabase_backfill.sql"
DEDUPE_PATH = REPO_ROOT / "data" / "supabase_dedupe.sql"

CHECK_SQL = """\
-- Read-only. Run each statement and note the results.

-- 1. Column names and types
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'dispatch_tracking'
ORDER BY ordinal_position;

-- 2. Size and date range
SELECT count(*) AS total_rows,
       count(DISTINCT replace(tracking_number, ' ', '')) AS distinct_tracking_numbers,
       min(created_at) AS first_row,
       max(created_at) AS latest_row
FROM dispatch_tracking;

-- 3. Tracking numbers stored more than once
SELECT replace(tracking_number, ' ', '') AS tracking_number, count(*) AS copies
FROM dispatch_tracking
GROUP BY 1
HAVING count(*) > 1
ORDER BY copies DESC
LIMIT 50;
"""


DEDUPE_SQL = """\
-- Removes duplicate rows in dispatch_tracking (same tracking number stored
-- more than once), keeping the earliest row for each. Run the SELECT first
-- to see what would go; the DELETE is wrapped in a transaction.

-- Preview: rows that would be deleted
SELECT d.id, d.created_at, d.order_number, d.tracking_number, d.source_file
FROM dispatch_tracking d
WHERE d.id <> (
  SELECT k.id FROM dispatch_tracking k
  WHERE replace(k.tracking_number, ' ', '') = replace(d.tracking_number, ' ', '')
  ORDER BY k.created_at, k.id
  LIMIT 1
)
ORDER BY d.tracking_number, d.created_at;

BEGIN;
DELETE FROM dispatch_tracking d
WHERE d.id <> (
  SELECT k.id FROM dispatch_tracking k
  WHERE replace(k.tracking_number, ' ', '') = replace(d.tracking_number, ' ', '')
  ORDER BY k.created_at, k.id
  LIMIT 1
);
COMMIT;

-- Optional, after the delete: stop it happening again.
-- CREATE UNIQUE INDEX dispatch_tracking_tracking_unique
--   ON dispatch_tracking ((replace(tracking_number, ' ', '')));
"""


def sql_literal(value: str) -> str:
    """Quote a string for a Postgres VALUES list."""
    return "'" + value.replace("'", "''") + "'"


def build_backfill_sql(rows: list[tuple[str, str, str, str, str, str]]) -> str:
    values = ",\n".join(
        "  (" + ", ".join(sql_literal(field) for field in row) + ")" for row in rows
    )
    return f"""\
-- Backfill of {len(rows)} tracked dispatches parsed from label PDFs.
-- Inserts only tracking numbers not already in dispatch_tracking.
-- Existing rows are never changed. Safe to run more than once.

BEGIN;

WITH backfill (order_number, customer_name, tracking_number, service_type, despatch_date, source_file) AS (
VALUES
{values}
)
INSERT INTO dispatch_tracking
  (order_number, customer_name, tracking_number, service_type, despatch_date,
   email_sent, email_address, source_file)
SELECT b.order_number, b.customer_name, b.tracking_number, b.service_type,
       b.despatch_date::date, false, '', b.source_file
FROM backfill b
WHERE NOT EXISTS (
  SELECT 1 FROM dispatch_tracking d
  WHERE replace(d.tracking_number, ' ', '') = replace(b.tracking_number, ' ', '')
);

-- The editor reports "INSERT 0 N": N is the number of rows added.
COMMIT;
"""


def main() -> None:
    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH} not found. Run tools/build_local_db.py first.")
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT order_number, customer_name, tracking_display, service_type,
                   despatch_date, source_file
            FROM dispatches
            ORDER BY despatch_date, tracking_number
            """
        ).fetchall()
    finally:
        conn.close()

    CHECK_PATH.write_text(CHECK_SQL)
    DEDUPE_PATH.write_text(DEDUPE_SQL)
    BACKFILL_PATH.write_text(build_backfill_sql(rows))
    size_kb = BACKFILL_PATH.stat().st_size / 1024
    print(f"Wrote {CHECK_PATH}")
    print(f"Wrote {DEDUPE_PATH}")
    print(f"Wrote {BACKFILL_PATH} ({len(rows)} rows, {size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
