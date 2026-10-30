"""Unit tests for the pure helpers in tools/ (no browser, no PDFs)."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"


@pytest.fixture
def cd(pb):
    """tools/check_deliveries, imported after `pb` has scrubbed the env."""
    sys.path.insert(0, str(TOOLS_DIR))
    try:
        sys.modules.pop("check_deliveries", None)
        import check_deliveries
        yield check_deliveries
    finally:
        sys.path.remove(str(TOOLS_DIR))


@pytest.fixture
def tool_modules(pb):
    sys.path.insert(0, str(TOOLS_DIR))
    try:
        for name in ("build_local_db", "export_supabase_sql"):
            sys.modules.pop(name, None)
        import build_local_db
        import export_supabase_sql
        yield build_local_db, export_supabase_sql
    finally:
        sys.path.remove(str(TOOLS_DIR))


DELIVERED_PAYLOAD = {
    "mailPieces": {
        "mailPieceId": "VU578574195GB",
        "summary": {
            "productName": "Royal Mail Tracked 48™",
            "lastEventName": "**Delivered by**",
            "lastEventDateTime": "2026-09-12T09:49:19+01:00",
            "lastEventLocationName": "Quinton DO",
            "statusDescription": "**Delivered**",
            "statusCategory": "Delivered",
            "summaryLine": "Your item was delivered on **12-09-2026**.",
        },
    }
}


def test_parse_summary_delivered(cd):
    result = cd.parse_summary(DELIVERED_PAYLOAD)
    assert result.status == "Delivered"
    assert result.delivered_on == "2026-09-12"
    assert "**" not in result.detail
    assert "Quinton DO" in result.detail


def test_parse_summary_not_delivered_has_no_delivery_date(cd):
    payload = {"mailPieces": {"summary": {
        "statusCategory": "In transit",
        "lastEventDateTime": "2026-09-30T08:00:00+01:00",
        "summaryLine": "We have your item.",
    }}}
    result = cd.parse_summary(payload)
    assert result.status == "In transit"
    assert result.delivered_on == ""


@pytest.mark.parametrize("payload", [
    None,
    [],
    {"httpCode": "404", "moreInformation": "not found"},
    {"mailPieces": {"error": {"errorCode": "E1142"}}},
    {"mailPieces": {"summary": {}}},
])
def test_parse_summary_rejects_payloads_without_a_status(cd, payload):
    with pytest.raises(cd.LookupFailed):
        cd.parse_summary(payload)


def test_working_days_skip_weekends(cd):
    # Fri 2 Oct 2026 -> Mon 5 Oct is one working day; -> Fri 16 Oct is ten.
    assert cd.working_days_between(date(2026, 10, 2), date(2026, 10, 5)) == 1
    assert cd.working_days_between(date(2026, 10, 2), date(2026, 10, 16)) == 10
    assert cd.working_days_between(date(2026, 10, 2), date(2026, 10, 2)) == 0


def _db(build_local_db):
    conn = sqlite3.connect(":memory:")
    conn.executescript(build_local_db.SCHEMA)
    return conn


def _add(conn, tracking, despatch, status=None, checked=None):
    conn.execute(
        """INSERT INTO dispatches (tracking_number, tracking_display, order_number,
           customer_name, service_type, despatch_date, source, source_file, added_at,
           delivery_status, last_checked_at)
           VALUES (?, ?, '1', 'A', 'Tracked 48', ?, 's', 'f.pdf', 'now', ?, ?)""",
        (tracking, tracking, despatch, status, checked),
    )


def test_select_pending_skips_delivered_and_recently_checked(cd, tool_modules):
    build_local_db, _ = tool_modules
    conn = _db(build_local_db)
    _add(conn, "NEW", "2026-09-01")
    _add(conn, "OLD", "2026-01-01")
    _add(conn, "DONE", "2026-09-02", status="Delivered", checked="2026-09-05T10:00:00")
    _add(conn, "FRESH", "2026-09-03", status="In transit", checked="2026-10-02T09:00:00")
    _add(conn, "STALE", "2026-09-04", status="In transit", checked="2026-09-20T09:00:00")
    now = datetime(2026, 10, 2, 12, 0, 0)

    in_window = cd.select_pending(conn, "2026-07-14", None, now)
    assert [t for t, _ in in_window] == ["NEW", "STALE"]

    everything = cd.select_pending(conn, None, 2, now)
    assert [t for t, _ in everything] == ["OLD", "NEW"]


def test_label_only_page_fields(tool_modules):
    build_local_db, _ = tool_modules
    text = "\n".join([
        "Delivered By", "24", "Tracked", "MZ 1234 5678 9GB", "sideA", "sideB",
        "Mr John O'Brien", "1 High Street", "AB1 2CD",
        "Customer Ref:", "19808", "Post by end of day", "03.06.2026",
    ])
    info = build_local_db.extract_label_only_info(text)
    assert info == {
        "order_number": "19808",
        "customer_name": "John O'Brien",
        "despatch_date": "2026-06-03",
    }


def test_backfill_sql_escapes_quotes_and_dedupes_on_unspaced_number(tool_modules):
    _, export = tool_modules
    sql = export.build_backfill_sql([
        ("19808", "John O'Brien", "MZ 1234 5678 9GB", "Tracked 24", "2026-06-03", "order-19808-label.pdf"),
    ])
    assert "'John O''Brien'" in sql
    assert "replace(d.tracking_number, ' ', '') = replace(b.tracking_number, ' ', '')" in sql
    assert sql.strip().startswith("--") and "BEGIN;" in sql and "COMMIT;" in sql
