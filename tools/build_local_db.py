#!/usr/bin/env python3
"""Build the local dispatch history database from every label PDF on this Mac.

Parses Click & Drop label PDFs from the processed folder, Downloads and the
second Batcher folder in Dropbox, keeps one row per tracking number, and
writes them to data/dispatches.db. Untracked (1st Class) orders are skipped:
they have no tracking number.

Safe to re-run: existing rows (and any delivery status already recorded on
them) are left untouched; only tracking numbers not yet in the database are
added.

Usage:
    python tools/build_local_db.py
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pdfplumber  # noqa: E402

import process_batches as pb  # noqa: E402

DB_PATH = REPO_ROOT / "data" / "dispatches.db"

# Listed in priority order: when the same tracking number appears in several
# places, the first source wins.
SOURCES: list[tuple[str, Path]] = [
    ("processed", pb.PROCESSED_DIR),
    (
        "dropbox_general",
        Path.home() / "Library/CloudStorage/Dropbox/ID5 General/Batches/batches/processed",
    ),
    ("downloads", Path.home() / "Downloads"),
]

LABEL_PDF_RE = re.compile(r"^orders?-.*label.*\.pdf$", re.IGNORECASE)
POST_BY_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatches (
    tracking_number   TEXT PRIMARY KEY,   -- no spaces, e.g. VU086023505GB
    tracking_display  TEXT NOT NULL,      -- as printed, e.g. VU 0860 2350 5GB
    order_number      TEXT NOT NULL,
    customer_name     TEXT NOT NULL,
    service_type      TEXT NOT NULL,
    despatch_date     TEXT NOT NULL,      -- YYYY-MM-DD
    source            TEXT NOT NULL,
    source_file       TEXT NOT NULL,
    added_at          TEXT NOT NULL,
    -- Filled in by tools/check_deliveries.py
    delivery_status   TEXT,
    delivered_on      TEXT,
    status_detail     TEXT,
    last_checked_at   TEXT,
    check_error       TEXT
);
CREATE INDEX IF NOT EXISTS dispatches_despatch_idx ON dispatches(despatch_date);
CREATE INDEX IF NOT EXISTS dispatches_status_idx ON dispatches(delivery_status);
"""


class Dispatch(NamedTuple):
    tracking_number: str
    tracking_display: str
    order_number: str
    customer_name: str
    service_type: str
    despatch_date: str
    source: str
    source_file: str


def extract_label_only_info(text: str) -> dict[str, str]:
    """Pull order number, name and post-by date off a label page.

    Used for PDFs that hold a label with no shipping-address page. The label
    prints "Customer Ref:" followed by the order number, "Post by end of day"
    followed by DD.MM.YYYY, and the address block (name first) after the
    tracking number. Lines of the rotated side text are single tokens, so the
    first multi-word line after the tracking number is the customer's name.
    """
    lines = [line.strip() for line in text.split("\n")]
    order_number = ""
    despatch_date = ""
    full_name = ""
    tracking_idx = next(
        (i for i, line in enumerate(lines) if pb.TRACKING_RE.fullmatch(line)), None
    )
    for i, line in enumerate(lines):
        if line == "Customer Ref:" and i + 1 < len(lines):
            order_number = lines[i + 1]
        elif POST_BY_RE.match(line) and not despatch_date:
            despatch_date = datetime.strptime(line, "%d.%m.%Y").strftime("%Y-%m-%d")
    if tracking_idx is not None:
        for line in lines[tracking_idx + 1:]:
            if line == "Customer Ref:":
                break
            if " " in line:
                full_name = line
                break
    return {
        "order_number": order_number,
        "customer_name": pb.strip_title(full_name),
        "despatch_date": despatch_date,
    }


def parse_pdf(path: Path, source: str) -> tuple[list[Dispatch], int]:
    """Return (tracked dispatches, count of untracked orders skipped) for one PDF."""
    found: list[Dispatch] = []
    untracked = 0
    with pdfplumber.open(path) as pdf:
        pages = pdf.pages
        if len(pages) == 1:
            pairs: list[tuple[str | None, str]] = [(None, pages[0].extract_text() or "")]
        else:
            pairs = [
                (pages[i].extract_text() or "", pages[i + 1].extract_text() or "")
                for i in range(0, len(pages) - 1, 2)
            ]
    for shipping_text, stamp_text in pairs:
        stamp = pb.extract_stamp_info(stamp_text)
        if not stamp["tracking_number"]:
            untracked += 1
            continue
        if shipping_text is None:
            info = extract_label_only_info(stamp_text)
        else:
            info = pb.extract_shipping_info(shipping_text)
        display = stamp["tracking_number"]
        found.append(
            Dispatch(
                tracking_number=display.replace(" ", ""),
                tracking_display=display,
                order_number=info["order_number"],
                customer_name=info["customer_name"],
                service_type=stamp["service_type"],
                despatch_date=info["despatch_date"],
                source=source,
                source_file=path.name,
            )
        )
    return found, untracked


def collect() -> tuple[dict[str, Dispatch], int, list[str]]:
    """Parse every source folder. Returns (by tracking number, untracked, unreadable files)."""
    by_tracking: dict[str, Dispatch] = {}
    untracked_total = 0
    unreadable: list[str] = []
    for source, folder in SOURCES:
        if not folder.exists():
            print(f"  skipping {source}: {folder} not found")
            continue
        files = sorted(p for p in folder.iterdir() if LABEL_PDF_RE.match(p.name))
        print(f"  {source}: {len(files)} PDFs")
        for path in files:
            try:
                dispatches, untracked = parse_pdf(path, source)
            except Exception as e:  # one corrupt PDF must not stop the run
                unreadable.append(f"{source}/{path.name}: {e}")
                continue
            untracked_total += untracked
            for d in dispatches:
                by_tracking.setdefault(d.tracking_number, d)
    return by_tracking, untracked_total, unreadable


def main() -> None:
    # pdfminer logs a warning per malformed font; thousands of PDFs drown the output.
    logging.getLogger("pdfminer").setLevel(logging.ERROR)

    print("Parsing label PDFs...")
    by_tracking, untracked, unreadable = collect()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        before = conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
        now = datetime.now().isoformat(timespec="seconds")
        conn.executemany(
            """
            INSERT OR IGNORE INTO dispatches (
                tracking_number, tracking_display, order_number, customer_name,
                service_type, despatch_date, source, source_file, added_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(*d, now) for d in by_tracking.values()],
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
        incomplete = conn.execute(
            """
            SELECT COUNT(*) FROM dispatches
            WHERE order_number = '' OR customer_name = ''
               OR service_type = '' OR despatch_date = ''
            """
        ).fetchone()[0]
    finally:
        conn.close()

    print(f"\nTracked dispatches found: {len(by_tracking)}")
    print(f"Untracked orders skipped: {untracked}")
    print(f"Unreadable PDFs: {len(unreadable)}")
    for line in unreadable:
        print(f"  {line}")
    print(f"Database: {DB_PATH}")
    print(f"  rows before: {before}, added: {after - before}, total: {after}")
    print(f"  rows with a blank field: {incomplete}")


if __name__ == "__main__":
    main()
