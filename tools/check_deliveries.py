#!/usr/bin/env python3
"""Look up Royal Mail delivery status for dispatches in data/dispatches.db.

Opens an ordinary Chrome window (its own profile under data/), types each
tracking number into the public "Track your item" form, and records the
status the page itself receives from Royal Mail. It never hides that it is
automated and never answers captcha puzzles: if one appears, the operator
solves it; if Royal Mail refuses access, the run stops. Results are saved after every lookup, so the run can be
stopped and restarted at any point.

Items already recorded as Delivered are never looked up again. Everything
else is re-checked on each run (at most once every 20 hours).

After the run, every checked item that is not delivered is written to
data/undelivered.csv.

Usage:
    python tools/check_deliveries.py              # items still inside the 80-day claim window
    python tools/check_deliveries.py --all        # the whole history
    python tools/check_deliveries.py --limit 20   # stop after 20 lookups
    python tools/check_deliveries.py --report     # rewrite the CSV only, no lookups
"""

from __future__ import annotations

import argparse
import csv
import random
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "dispatches.db"
REPORT_PATH = REPO_ROOT / "data" / "undelivered.csv"
CHECKLIST_PATH = REPO_ROOT / "data" / "checklist.html"
CHROME_PROFILE = REPO_ROOT / "data" / "chrome-profile"
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

TRACK_PAGE = "https://www.royalmail.com/track-your-item"
TRACK_URL = TRACK_PAGE + "#/tracking-results/{tracking}"
SUMMARY_PATH = "/mailpieces/microsummary/v1/summary/"

# Royal Mail's loss-claim rules: a claim must be made within 80 calendar days
# of posting, and is not considered until 10 working days after the due date.
CLAIM_WINDOW_DAYS = 80
RECHECK_AFTER_HOURS = 20
# Long enough for the operator to complete a captcha puzzle if one appears.
LOOKUP_TIMEOUT_MS = 120_000
MAX_CONSECUTIVE_FAILURES = 3

DELIVERED = "Delivered"


@dataclass(frozen=True)
class TrackingResult:
    status: str             # Royal Mail's status category, e.g. "Delivered", "In transit"
    delivered_on: str       # YYYY-MM-DD when delivered, else ""
    detail: str             # one-line description for the report


class LookupFailed(Exception):
    """The page did not produce a usable tracking answer for this item."""


class AccessDenied(Exception):
    """Royal Mail refused to serve the tracking page to this browser."""


def _plain(text: str) -> str:
    """Royal Mail wraps emphasis in ** markers; strip them."""
    return text.replace("**", "").strip()


def parse_summary(payload: object) -> TrackingResult:
    """Turn the tracking page's summary response into a TrackingResult.

    Raises LookupFailed when the payload has no summary (unknown number,
    error envelope, or a shape we do not recognise).
    """
    if not isinstance(payload, dict):
        raise LookupFailed("unexpected response shape")
    mail_pieces = payload.get("mailPieces")
    if not isinstance(mail_pieces, dict):
        raise LookupFailed(_describe_error(payload))
    summary = mail_pieces.get("summary")
    if not isinstance(summary, dict):
        raise LookupFailed(_describe_error(mail_pieces))

    status = str(summary.get("statusCategory") or _plain(str(summary.get("statusDescription", ""))))
    if not status:
        raise LookupFailed("response had no status")

    delivered_on = ""
    if status == DELIVERED:
        delivered_on = str(summary.get("lastEventDateTime", ""))[:10]

    parts = [
        _plain(str(summary.get("summaryLine", ""))),
        _plain(str(summary.get("lastEventName", ""))),
        str(summary.get("lastEventLocationName", "")),
        str(summary.get("lastEventDateTime", ""))[:16].replace("T", " "),
    ]
    detail = " | ".join(p for p in parts if p)
    return TrackingResult(status=status, delivered_on=delivered_on, detail=detail)


def _describe_error(node: dict[str, object]) -> str:
    """Best-effort one-line description of an error envelope."""
    for key in ("error", "errors", "moreInformation", "httpMessage"):
        if key in node:
            return f"{key}: {str(node[key])[:200]}"
    return "no tracking summary in response"


def working_days_between(start: date, end: date) -> int:
    """Count Mon–Fri days after `start` up to and including `end`.

    Bank holidays are not excluded, so this can overstate by a day or two
    around public holidays.
    """
    if end <= start:
        return 0
    days = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days += 1
    return days


def select_pending(
    conn: sqlite3.Connection, since: str | None, limit: int | None, now: datetime
) -> list[tuple[str, str]]:
    """Return (tracking_number, despatch_date) rows due a lookup, oldest first."""
    recheck_before = (now - timedelta(hours=RECHECK_AFTER_HOURS)).isoformat(timespec="seconds")
    sql = """
        SELECT tracking_number, despatch_date FROM dispatches
        WHERE (delivery_status IS NULL OR delivery_status != ?)
          AND (last_checked_at IS NULL OR last_checked_at < ?)
    """
    params: list[str | int] = [DELIVERED, recheck_before]
    if since:
        sql += " AND despatch_date >= ?"
        params.append(since)
    sql += " ORDER BY despatch_date, tracking_number"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def record_result(conn: sqlite3.Connection, tracking: str, result: TrackingResult, now: datetime) -> None:
    conn.execute(
        """
        UPDATE dispatches
        SET delivery_status = ?, delivered_on = ?, status_detail = ?,
            last_checked_at = ?, check_error = NULL
        WHERE tracking_number = ?
        """,
        (result.status, result.delivered_on or None, result.detail,
         now.isoformat(timespec="seconds"), tracking),
    )
    conn.commit()


def record_failure(conn: sqlite3.Connection, tracking: str, message: str, now: datetime) -> None:
    # last_checked_at is left alone so the item is retried on the next run.
    conn.execute(
        "UPDATE dispatches SET check_error = ? WHERE tracking_number = ?",
        (f"{now.isoformat(timespec='seconds')} {message}"[:500], tracking),
    )
    conn.commit()


def write_report(conn: sqlite3.Connection, today: date) -> int:
    """Write every checked, not-delivered item to REPORT_PATH. Returns the row count."""
    rows = conn.execute(
        """
        SELECT despatch_date, order_number, customer_name, service_type,
               tracking_display, delivery_status, status_detail, last_checked_at
        FROM dispatches
        WHERE delivery_status IS NOT NULL AND delivery_status != ?
        ORDER BY despatch_date
        """,
        (DELIVERED,),
    ).fetchall()
    with REPORT_PATH.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "despatch_date", "order_number", "customer_name", "service_type",
            "tracking_number", "status", "detail", "last_checked",
            "working_days_since_despatch", "claim_deadline", "days_left_to_claim",
        ])
        for despatch, order, name, service, tracking, status, detail, checked in rows:
            posted = date.fromisoformat(despatch)
            deadline = posted + timedelta(days=CLAIM_WINDOW_DAYS)
            writer.writerow([
                despatch, order, name, service, tracking, status, detail, checked,
                working_days_between(posted, today), deadline.isoformat(),
                (deadline - today).days,
            ])
    return len(rows)


def write_checklist(conn: sqlite3.Connection, since: str | None) -> int:
    """Write an HTML page of not-yet-delivered items with links to Royal Mail tracking."""
    from html import escape

    sql = """
        SELECT despatch_date, order_number, customer_name, service_type, tracking_number
        FROM dispatches
        WHERE (delivery_status IS NULL OR delivery_status != ?)
    """
    params: list[str] = [DELIVERED]
    if since:
        sql += " AND despatch_date >= ?"
        params.append(since)
    rows = conn.execute(sql + " ORDER BY despatch_date, tracking_number", params).fetchall()
    body = "\n".join(
        "<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in (despatch, order, name, service))
        + f'<td><a href="{TRACK_URL.format(tracking=tracking)}" target="_blank">{tracking}</a></td></tr>'
        for despatch, order, name, service, tracking in rows
    )
    CHECKLIST_PATH.write_text(
        "<!doctype html><meta charset='utf-8'><title>Deliveries to check</title>"
        "<style>body{font:14px sans-serif}td,th{padding:4px 10px;text-align:left}"
        "a:visited{color:#999}</style>"
        f"<h1>{len(rows)} items to check, oldest first</h1>"
        "<table><tr><th>Despatched</th><th>Order</th><th>Customer</th><th>Service</th>"
        f"<th>Tracking</th></tr>\n{body}\n</table>"
    )
    return len(rows)


def _ensure_form(page) -> None:
    """Make sure the tracking form (reference box) is on screen."""
    if "royalmail.com/track-your-item" not in page.url:
        page.goto(TRACK_PAGE)
    _accept_cookies(page)
    box = page.locator("#barcode-input")
    if box.count() and box.first.is_visible():
        return
    again = page.get_by_role("button", name="Track another item")
    if again.count():
        again.first.click()
    else:
        page.goto(TRACK_PAGE)
        _accept_cookies(page)
    box.first.wait_for(state="visible", timeout=LOOKUP_TIMEOUT_MS)


def _accept_cookies(page) -> None:
    """Dismiss the cookie banner if it is showing."""
    consent = page.locator("#truste-consent-button")
    try:
        consent.first.wait_for(state="visible", timeout=3000)
    except Exception:  # no banner this time
        return
    consent.first.click()
    try:
        consent.first.wait_for(state="hidden", timeout=5000)
    except Exception:
        pass


def lookup(page, tracking: str) -> TrackingResult:  # page: playwright.sync_api.Page
    """Type one tracking number into the form and read the answer the page receives."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        _ensure_form(page)
        box = page.locator("#barcode-input").first
        box.click()
        box.fill("")
        box.type(tracking, delay=random.uniform(40, 120))
        with page.expect_response(
            lambda r: SUMMARY_PATH in r.url and tracking in r.url,
            timeout=LOOKUP_TIMEOUT_MS,
        ) as response_info:
            page.locator("button#submit").first.click()
        response = response_info.value
    except PlaywrightTimeout as e:
        # An unsolved captcha puzzle ends with the site replacing the page
        # with its Access Denied notice.
        if "Access Denied" in page.inner_text("body"):
            raise AccessDenied("Royal Mail returned Access Denied after its captcha check") from e
        raise LookupFailed("no tracking response (captcha puzzle not completed?)") from e

    try:
        payload: object = response.json()
    except Exception as e:  # non-JSON body, e.g. an HTML block page
        raise LookupFailed(f"HTTP {response.status}, unreadable response") from e
    if response.status != 200 and not isinstance(payload, dict):
        raise LookupFailed(f"HTTP {response.status}")
    return parse_summary(payload)


def _chrome_running() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def start_chrome() -> None:
    """Launch an ordinary Chrome (not a Playwright-driven one) that the script can attach to.

    Chrome will not accept a debugging connection on its everyday profile, so
    this uses a profile folder of its own under data/. Cookies and the
    captcha's memory of past visits persist there between runs.
    """
    if _chrome_running():
        return
    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "open", "-na", "Google Chrome", "--args",
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={CHROME_PROFILE}",
            TRACK_PAGE,
        ],
        check=True,
    )
    for _ in range(60):
        if _chrome_running():
            return
        time.sleep(0.5)
    sys.exit(
        f"Chrome did not open a debugging port on {CDP_PORT}. Quit all Chrome "
        "windows that use the Batcher profile and try again."
    )


def run_lookups(conn: sqlite3.Connection, pending: list[tuple[str, str]], min_delay: float, max_delay: float) -> None:
    from playwright.sync_api import sync_playwright

    start_chrome()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = next((pg for pg in context.pages if "royalmail.com" in pg.url), None)
        if page is None:
            page = context.new_page()
            page.goto(TRACK_PAGE)
        page.bring_to_front()
        consecutive_failures = 0
        try:
            for n, (tracking, despatch) in enumerate(pending, start=1):
                prefix = f"[{n}/{len(pending)}] {despatch} {tracking}"
                try:
                    result = lookup(page, tracking)
                except AccessDenied as e:
                    record_failure(conn, tracking, str(e), datetime.now())
                    print(f"{prefix}  BLOCKED: {e}")
                    print("\nStopping: Royal Mail is refusing automated lookups from this browser.")
                    break
                except LookupFailed as e:
                    consecutive_failures += 1
                    record_failure(conn, tracking, str(e), datetime.now())
                    print(f"{prefix}  FAILED: {e}")
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        print(
                            f"\n{MAX_CONSECUTIVE_FAILURES} lookups in a row failed. "
                            "If the Chrome window is showing a verification "
                            "challenge, complete it there."
                        )
                        try:
                            answer = input("Press Enter to carry on, or type q then Enter to stop: ")
                        except EOFError:  # not running in a terminal
                            answer = "q"
                        if answer.strip().lower().startswith("q"):
                            break
                        consecutive_failures = 0
                else:
                    consecutive_failures = 0
                    record_result(conn, tracking, result, datetime.now())
                    print(f"{prefix}  {result.status}  {result.delivered_on}")
                if n < len(pending):
                    time.sleep(random.uniform(min_delay, max_delay))
        finally:
            # Leave Chrome open: it is the operator's window, not ours.
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="check the whole history, not just the claim window")
    parser.add_argument("--since", help="only items despatched on or after this date (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, help="stop after this many lookups")
    parser.add_argument("--min-delay", type=float, default=6.0, help="shortest pause between lookups, seconds")
    parser.add_argument("--max-delay", type=float, default=12.0, help="longest pause between lookups, seconds")
    parser.add_argument("--report", action="store_true", help="rewrite the undelivered CSV without doing lookups")
    parser.add_argument("--checklist", action="store_true", help="write a click-through HTML list instead of doing lookups")
    args = parser.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH} not found. Run tools/build_local_db.py first.")

    today = date.today()
    if args.since:
        since: str | None = args.since
    elif args.all:
        since = None
    else:
        since = (today - timedelta(days=CLAIM_WINDOW_DAYS)).isoformat()

    conn = sqlite3.connect(DB_PATH)
    try:
        if args.checklist:
            count = write_checklist(conn, since)
            print(f"{count} items written to {CHECKLIST_PATH}")
            return
        if not args.report:
            pending = select_pending(conn, since, args.limit, datetime.now())
            scope = f"despatched since {since}" if since else "whole history"
            print(f"{len(pending)} items to look up ({scope}).")
            if pending:
                print(
                    "A Chrome window will open. Leave it alone unless Royal Mail "
                    "shows a picture puzzle: solve that there; the script waits "
                    "up to two minutes."
                )
                run_lookups(conn, pending, args.min_delay, args.max_delay)

        count = write_report(conn, today)
        totals = conn.execute(
            "SELECT COALESCE(delivery_status, 'not checked yet'), COUNT(*) FROM dispatches GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
    finally:
        conn.close()

    print("\nStatus across the database:")
    for status, n in totals:
        print(f"  {n:5d}  {status}")
    print(f"\n{count} not-delivered items written to {REPORT_PATH}")


if __name__ == "__main__":
    main()
