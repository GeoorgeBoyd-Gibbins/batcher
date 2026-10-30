#!/Users/georgeboyd-gibbins/Projects/batcher/.venv/bin/python3
"""
Batch Order Label Processor for ID5 Certified Ltd.

Processes Royal Mail Click & Drop batch PDFs:
1. Splits each PDF into individual stamp labels per customer
2. Copies stamp PDFs into Dropbox customer folders
3. Posts dispatch tracking to web app API
4. Emails customers their tracking info with stamp attached
5. Moves processed PDFs to batches/processed/
"""

import glob
import hashlib
import imaplib
import os
import re
import shutil
import smtplib
import socket
import ssl
import sys
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

import pdfplumber
import requests
from dotenv import load_dotenv
from pypdf import PdfReader, PdfWriter

import auth

SCRIPT_DIR = Path(__file__).resolve().parent

load_dotenv(SCRIPT_DIR / ".env")

# BATCHER_API_URL points at the *root* of the service (no /tracking suffix).
# The client appends /v1/dispatches itself.
BATCHER_API_URL = os.getenv("BATCHER_API_URL", "https://id5data2.onrender.com").rstrip("/")
# When true, skip the server dispatch POST entirely — runs in pre-Phase-0
# "local-only" mode (PDFs split, Dropbox copied, emails sent, no ledger).
BYPASS_AUTH = os.getenv("BATCHER_BYPASS_AUTH", "").lower() in ("1", "true", "yes", "on")
# Legacy single-tenant endpoint (the only one deployed while Phase 0 is
# parked). When both are set, dispatches are recorded via POST /tracking with
# the shared key instead of /v1/dispatches.
TRACKING_API_URL = os.getenv("TRACKING_API_URL", "").strip()
TRACKING_API_KEY = os.getenv("TRACKING_API_KEY", "").strip()
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
# Optional Reply-To so customer replies go somewhere other than SENDER_EMAIL.
REPLY_TO = os.getenv("REPLY_TO", "").strip()
IMAP_HOST = os.getenv("IMAP_HOST", "imap.stackmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
TEST_EMAIL_OVERRIDE = os.getenv("TEST_EMAIL_OVERRIDE", "")
# Extra BCC recipients on every dispatch email (comma-separated).
DISPATCH_BCC = [
    a.strip() for a in os.getenv("DISPATCH_BCC", "contact@id5.co.uk").split(",") if a.strip()
]
# Trustpilot Automatic Feedback Service address. BCC'ing it makes Trustpilot
# send the customer (the To: address) a verified review invitation.
# Set to an empty string to disable.
TRUSTPILOT_BCC = os.getenv(
    "TRUSTPILOT_BCC", "id5.co.uk+5c585510e3@invite.trustpilot.com"
).strip()
# How the dispatch email asks for a Trustpilot review:
#   "bcc"  - BCC the Automatic Feedback Service address above (uses an
#            automated invite; the body tells the customer to expect it)
#   "link" - no BCC; the body carries a hyperlink to the public review page
#   "off"  - no BCC and no review paragraph at all
TRUSTPILOT_MODES = ("bcc", "link", "off")
TRUSTPILOT_MODE = os.getenv("TRUSTPILOT_MODE", "bcc").strip().lower() or "bcc"
if TRUSTPILOT_MODE not in TRUSTPILOT_MODES:
    print(f"WARNING: TRUSTPILOT_MODE={TRUSTPILOT_MODE!r} not recognised; using 'bcc'")
    TRUSTPILOT_MODE = "bcc"
# Public "write a review" page used in "link" mode.
TRUSTPILOT_REVIEW_URL = (
    os.getenv("TRUSTPILOT_REVIEW_URL", "").strip()
    or "https://www.trustpilot.com/evaluate/id5.co.uk"
)
DROPBOX_BASE = Path(os.getenv("DROPBOX_BASE", "~/Library/CloudStorage/Dropbox/ID5 matters")).expanduser()
_batches_env = os.getenv("BATCHES_DIR", "./batches")
BATCHES_DIR = Path(_batches_env) if os.path.isabs(_batches_env) else SCRIPT_DIR / _batches_env
PROCESSED_DIR = BATCHES_DIR / "processed"

TITLE_PREFIXES = re.compile(
    r"^(Mr|Mrs|Miss|Ms|Dr|Prof|Rev|Sir|Lady|Lord|Dame)\s+", re.IGNORECASE
)
TRACKING_RE = re.compile(r"[A-Z]{2}\s?\d{4}\s?\d{4}\s?\d?[A-Z]{2,3}")


def strip_title(name: str) -> str:
    """Remove title prefix (Mr/Mrs/etc.) from a name."""
    return TITLE_PREFIXES.sub("", name).strip()


def extract_shipping_info(text: str) -> dict[str, str]:
    """Extract customer name, order number, and despatch date from a shipping address page."""
    lines = text.split("\n")

    # Find name: line immediately after "Shipping Address"
    full_name = ""
    for i, line in enumerate(lines):
        if line.strip() == "Shipping Address" and i + 1 < len(lines):
            full_name = lines[i + 1].strip()
            break

    customer_name = strip_title(full_name)

    # Order number
    order_number = ""
    for line in lines:
        if line.startswith("Order Number:"):
            order_number = line.split(":", 1)[1].strip()
            break

    # Despatch date
    despatch_date = ""
    for line in lines:
        if line.startswith("Despatch Date:"):
            raw_date = line.split(":", 1)[1].strip()
            try:
                parsed = datetime.strptime(raw_date, "%d/%m/%Y")
                despatch_date = parsed.strftime("%Y-%m-%d")
            except ValueError:
                despatch_date = raw_date
            break

    return {
        "full_name": full_name,
        "customer_name": customer_name,
        "order_number": order_number,
        "despatch_date": despatch_date,
    }


def extract_stamp_info(text: str) -> dict[str, str]:
    """Extract tracking number and service type from a stamp/label page."""
    tracking_match = TRACKING_RE.search(text)
    tracking_number = tracking_match.group(0) if tracking_match else ""

    # Service type: "Delivered By\n24\nTracked" or "Delivered By\n48\nTracked"
    # or "Delivered By\n1\nSpecial Delivery"
    service_type = ""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "Delivered By" and i + 2 < len(lines):
            speed = lines[i + 1].strip()
            next_line = lines[i + 2].strip()
            if next_line == "Tracked":
                service_type = f"Tracked {speed}"
            elif next_line == "Special Delivery":
                service_type = "Special Delivery"
            break

    return {
        "tracking_number": tracking_number,
        "service_type": service_type,
    }


def save_stamp_pdf(source_pdf_path: str, page_index: int, output_path: Path) -> None:
    """Extract a single page from a PDF and save it as a new file."""
    reader = PdfReader(source_pdf_path)
    writer = PdfWriter()
    writer.add_page(reader.pages[page_index])
    with open(output_path, "wb") as f:
        writer.write(f)


def find_customer_folder(customer_name: str) -> Path | None:
    """Find the customer's Dropbox folder by matching name prefix.

    Folders are named like 'George Tinsley (ref/ 18644)'.
    Searches most recent 100 folders sorted by creation time descending.
    """
    if not DROPBOX_BASE.exists():
        print(f"  WARNING: Dropbox base path does not exist: {DROPBOX_BASE}")
        return None

    folders: list[tuple[float, Path]] = []
    try:
        for entry in DROPBOX_BASE.iterdir():
            if entry.is_dir():
                folders.append((entry.stat().st_birthtime, entry))
    except AttributeError:
        # st_birthtime not available on all platforms, fall back to st_mtime
        folders = []
        for entry in DROPBOX_BASE.iterdir():
            if entry.is_dir():
                folders.append((entry.stat().st_mtime, entry))

    # Sort by creation time, newest first
    folders.sort(key=lambda x: x[0], reverse=True)

    name_lower = customer_name.lower()
    for _, folder_path in folders[:200]:
        folder_name = folder_path.name.lower()
        if folder_name.startswith(name_lower):
            return folder_path

    return None


def read_customer_email(customer_folder: Path) -> str | None:
    """Read the customer's email address from .customer_email.txt in their folder.

    Raises TimeoutError if the file exists but cannot be read due to network timeout.
    """
    email_file = customer_folder / ".customer_email.txt"
    if not email_file.exists():
        return None
    return email_file.read_text().strip()


class AuthRequired(RuntimeError):
    """Raised when the desktop client has no API token configured."""


class OutOfCredits(RuntimeError):
    """Raised when the server returns 402 — operator needs to top up."""

    def __init__(self, balance: int, top_up_url: str) -> None:
        super().__init__(
            f"Out of credits (balance={balance}). Top up: {top_up_url}"
        )
        self.balance = balance
        self.top_up_url = top_up_url


def _client_dispatch_id(account_id: str, order_number: str, tracking_number: str) -> str:
    """Stable hash for idempotency. Must match the server-side shim formula."""
    raw = f"{account_id}|{order_number}|{tracking_number}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _post_with_cold_start_retry(
    url: str, payload: dict[str, str | bool], headers: dict[str, str]
) -> requests.Response | None:
    """POST JSON, retrying once on Timeout/ConnectionError. None on failure (logged)."""
    # Render free-tier dynos cold-start in ~30s. The first dispatch of the
    # day will time out; retry once with a longer timeout so the operator
    # doesn't have to babysit batches.
    try:
        return requests.post(url, json=payload, headers=headers, timeout=15)
    except (requests.Timeout, requests.ConnectionError) as e:
        print(f"  (cold start? retrying) {e}")
        try:
            return requests.post(url, json=payload, headers=headers, timeout=45)
        except requests.RequestException as e2:
            print(f"  ERROR posting to dispatches API after retry: {e2}")
            return None
    except requests.RequestException as e:
        print(f"  ERROR posting to dispatches API: {e}")
        return None


def _post_legacy_tracking_record(record: dict[str, str | bool]) -> bool:
    """POST a dispatch record to the legacy /tracking endpoint.

    The server rejects a record with any blank required field (400), so an
    order whose tracking number could not be parsed returns False here.
    """
    resp = _post_with_cold_start_retry(
        TRACKING_API_URL, record, {"Authorization": f"Bearer {TRACKING_API_KEY}"}
    )
    if resp is None:
        return False
    if resp.status_code in (200, 201):
        return True
    print(f"  WARNING: Tracking API returned status {resp.status_code}: {resp.text[:200]}")
    return False


def post_tracking_record(
    order_number: str,
    customer_name: str,
    tracking_number: str,
    service_type: str,
    despatch_date: str,
    email_sent: bool,
    email_address: str,
    source_file: str,
) -> bool:
    """POST a dispatch record to /v1/dispatches.

    Returns True on success (201 created OR 200 idempotent replay).
    Raises OutOfCredits on 402. Raises AuthRequired if no token is configured.
    Returns False on other transport/server errors (logged).

    When TRACKING_API_URL and TRACKING_API_KEY are set, the record goes to
    the legacy /tracking endpoint instead (no account, no credits, no
    idempotency) and the result is simply True/False.

    Otherwise, when BATCHER_BYPASS_AUTH is set, returns True without
    contacting the server — local-only "old desktop app" mode.
    """
    record = {
        "order_number": order_number,
        "customer_name": customer_name,
        "tracking_number": tracking_number,
        "service_type": service_type,
        "despatch_date": despatch_date,
        "email_sent": email_sent,
        "email_address": email_address,
        "source_file": source_file,
    }

    if TRACKING_API_URL and TRACKING_API_KEY:
        return _post_legacy_tracking_record(record)

    if BYPASS_AUTH:
        return True

    token = auth.get_token()
    account_id = auth.get_account_id()
    if not token or not account_id:
        raise AuthRequired(
            "No API token. Run the app and sign in, or use the CLI login flow."
        )

    payload = {
        "client_dispatch_id": _client_dispatch_id(account_id, order_number, tracking_number),
        **record,
    }
    resp = _post_with_cold_start_retry(
        f"{BATCHER_API_URL}/v1/dispatches",
        payload,
        {"Authorization": f"Bearer {token}"},
    )
    if resp is None:
        return False

    if resp.status_code in (200, 201):
        return True
    if resp.status_code == 402:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        raise OutOfCredits(
            balance=int(body.get("balance", 0)),
            top_up_url=body.get("top_up_url", f"{BATCHER_API_URL}/account"),
        )
    if resp.status_code == 401:
        # Token revoked or invalid. Clear it so the next launch re-prompts.
        auth.clear_token()
        raise AuthRequired("Server rejected the API token; please sign in again.")
    print(f"  WARNING: Dispatches API returned status {resp.status_code}: {resp.text[:200]}")
    return False


def _trustpilot_paragraph(mode: str | None = None) -> str:
    """The review-request paragraph for the dispatch email, per TRUSTPILOT_MODE."""
    mode = (mode or TRUSTPILOT_MODE).lower()
    if mode == "bcc":
        return """<p><em>Lastly, I wonder if I could ask you a favour?
We're trying to improve the visibility of our business online. Shortly after this email
you'll receive a separate invitation from Trustpilot asking you to review your experience
with ID5. It's a genuine, verified invite sent on our behalf, so any review you leave
through it is marked as verified. It takes literally 2 minutes to do and massively
helps us.</em></p>
"""
    if mode == "link":
        return f"""<p><em>Lastly, I wonder if I could ask you a favour?
We're trying to improve the visibility of our business online. Would you mind leaving us a
<a href="{TRUSTPILOT_REVIEW_URL}" style="color: #0563C1;">Trustpilot Review</a>
detailing your experience? It takes literally 2 minutes to do and massively helps us.</em></p>
"""
    return ""


def build_email_html(
    tracking_number: str, service_type: str, trustpilot_mode: str | None = None
) -> str:
    """Build the HTML dispatch notice email body.

    `trustpilot_mode` defaults to the module-level TRUSTPILOT_MODE.
    """
    service_upper = service_type.upper()
    trustpilot_paragraph = _trustpilot_paragraph(trustpilot_mode)
    return f"""\
<html>
<body style="font-family: Calibri, Arial, sans-serif; font-size: 14px; color: #333;">

<p>Hi</p>

<p><strong>DISPATCH NOTICE - {service_upper}</strong></p>

<p>Thanks for the docs and for your payment.</p>

<p>I've signed the forms and will send them out in tonight's post run.
The letter should be delivered in the next few days.</p>

<p>I attach Postage details for {service_type} Post. Your tracking number is
{tracking_number} and the letter can be tracked using that reference at the link below:-</p>

<p><a href="https://www.royalmail.com/track-your-item" style="color: #0563C1;">
https://www.royalmail.com/track-your-item</a></p>

<p>Let me know if you have any queries.</p>

{trustpilot_paragraph}
<p>Kind regards</p>

<p><strong>George Boyd-Gibbins</strong></p>

<p>For and on behalf of<br>
<strong>ID5 Certified Ltd</strong> t/a <strong>ID5 Identification Verification</strong></p>

<p><img src="https://www.id5.co.uk/wp-content/uploads/apple-icon-152x152-2.png"
  alt="ID5 Logo" width="80" style="display:block; margin: 10px 0;"></p>

<p>Tel. : <a href="tel:03303904000" style="color: #0563C1;">0330 390 4000</a><br>
Email : <a href="mailto:george@id5.co.uk" style="color: #0563C1;">george@id5.co.uk</a><br>
Website : <a href="https://www.id5.co.uk/" style="color: #0563C1;">https://www.id5.co.uk/</a></p>

<p style="color: #0563C1;"><strong>Doing your own property transfer?</strong><br>
Get your Land Registry forms completed for you, with tailored, step-by-step guidance, for &pound;69.<br>
<a href="https://www.diy-conveyancing.co.uk" style="color: #0563C1;">www.diy-conveyancing.co.uk</a></p>

<hr style="border: none; border-top: 1px solid #ccc; margin: 20px 0;">

<p style="font-size: 10px; color: #999;">
This message is confidential and/or may be privileged or protected by other legal rules
and is intended for a particular recipient only. If you have received this message by
mistake, please notify us by return email. Do not copy this email message or disclose its
contents to anyone else. Please then delete it and any attachments from your system. This
message has not been encrypted. It may therefore be liable to compromise. It is your
responsibility to scan this message for viruses. Viruses and compromises of security are
inherent risks in relation to email. To the extent permitted by law, we do not accept any
liability for any virus infection and/or external compromise of security in relation to
transmissions by email. Any personal views expressed in this message are not necessarily the
views of ID5 Identification Verification, its directors, officers, employees or
subsidiaries. Please note that contracts may NOT be concluded on behalf of ID5
Identification Verification by email. Activity and use of ID5 Identification
Verification's message systems is monitored to secure their effective operation and for
other lawful business purposes.
</p>

<p style="font-size: 10px; color: #999;">
Beware fraud and be alert. If you receive an email from ID5 Identification Verification
about paying funds or requesting your bank details or purporting to amend our bank details,
please contact us by telephone immediately to check it is genuine. We will never advise a
change of bank details by email.
</p>

<p style="font-size: 10px; color: #999;">
ID5 Identification Verification, ID5 Identification Certification and ID5 Certified are
trading names of ID5 Certified Ltd which is incorporated in England and Wales with
Registered Number: 13035709 and Registered Office: Sophia House, 28 Cathedral Road, Cardiff
CF11 9LJ. Our VAT number is 438 2790 70.
</p>

</body>
</html>"""


class _IMAP4_SSL_IPv4(imaplib.IMAP4_SSL):
    """IMAP4_SSL that forces IPv4 connections (avoids macOS Errno 49)."""

    def _create_socket(self, timeout: float | None = None) -> ssl.SSLSocket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if timeout is not None:
            sock.settimeout(timeout)
        sock.connect((self.host, self.port))
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


def _save_to_sent(msg_bytes: bytes) -> None:
    """Append an email to the Sent folder via IMAP (IPv4)."""
    # Read IMAP config directly from env with hardcoded defaults.
    # This avoids any issues with module-level variables being patched.
    imap_host = os.getenv("IMAP_HOST", "imap.stackmail.com")
    imap_port = int(os.getenv("IMAP_PORT", "993"))
    imap_user = os.getenv("SMTP_USER", SMTP_USER)
    imap_pass = os.getenv("SMTP_PASSWORD", SMTP_PASSWORD)

    with _IMAP4_SSL_IPv4(imap_host, imap_port) as imap:
        imap.login(imap_user, imap_pass)
        status, result = imap.append("Sent", "\\Seen", None, msg_bytes)
        if status != "OK":
            print(f"  WARNING: IMAP append returned {status}: {result}")


def _dispatch_bcc_list(test_mode: bool = False) -> list[str]:
    """BCC recipients for a dispatch email, deduplicated and in order.

    The Trustpilot invite address is only added in "bcc" mode, and is dropped
    in test mode so redirected test emails never trigger a real review
    invitation.
    """
    candidates = [SENDER_EMAIL, *DISPATCH_BCC]
    if TRUSTPILOT_BCC and TRUSTPILOT_MODE == "bcc":
        if test_mode:
            print("  TEST MODE: Skipping Trustpilot invite BCC")
        else:
            candidates.append(TRUSTPILOT_BCC)
    seen: set[str] = set()
    result: list[str] = []
    for addr in candidates:
        key = addr.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(addr.strip())
    return result


def send_dispatch_email(
    to_email: str,
    customer_name: str,
    tracking_number: str,
    service_type: str,
    stamp_pdf_path: Path,
) -> bool:
    """Send a dispatch notification email with the stamp PDF attached."""
    actual_recipient = TEST_EMAIL_OVERRIDE if TEST_EMAIL_OVERRIDE else to_email
    if TEST_EMAIL_OVERRIDE:
        print(f"  TEST MODE: Redirecting email from {to_email} to {TEST_EMAIL_OVERRIDE}")

    bcc = _dispatch_bcc_list(test_mode=bool(TEST_EMAIL_OVERRIDE))

    msg = MIMEMultipart("mixed")
    msg["From"] = SENDER_EMAIL
    # Include the customer's name so the Trustpilot invitation is personalised.
    msg["To"] = formataddr((customer_name, actual_recipient)) if customer_name else actual_recipient
    if bcc:
        # smtplib.send_message() delivers to every Bcc address and strips the
        # header from the copy it puts on the wire.
        msg["Bcc"] = ", ".join(bcc)
    if REPLY_TO:
        msg["Reply-To"] = REPLY_TO
    msg["Subject"] = f"Dispatch Notice - {service_type}"

    html_body = build_email_html(tracking_number, service_type)
    msg.attach(MIMEText(html_body, "html"))

    # Attach stamp PDF
    with open(stamp_pdf_path, "rb") as f:
        attachment = MIMEBase("application", "pdf")
        attachment.set_payload(f.read())
    encoders.encode_base64(attachment)
    attachment.add_header(
        "Content-Disposition",
        "attachment",
        filename=stamp_pdf_path.name,
    )
    msg.attach(attachment)

    try:
        print(f"  DEBUG: SMTP connecting to {SMTP_HOST}:{SMTP_PORT}")
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        print("  DEBUG: SMTP send complete, quitting SMTP")
        server.quit()
        print("  DEBUG: SMTP quit done, now saving to Sent")

        msg_bytes = msg.as_bytes()
        try:
            _save_to_sent(msg_bytes)
        except Exception as e:
            import traceback
            print(f"  WARNING: Could not save to Sent folder: {e}")
            traceback.print_exc()

        return True
    except Exception as e:
        print(f"  ERROR sending email to {to_email}: {e}")
        return False


def process_pdf(pdf_path: Path) -> tuple[int, int]:
    """Process a single batch PDF. Returns (processed_count, error_count)."""
    source_filename = pdf_path.name
    print(f"\nProcessing: {source_filename}")

    processed = 0
    errors = 0

    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        if total_pages % 2 != 0:
            print(f"  WARNING: Odd number of pages ({total_pages}), last page will be skipped")

        for i in range(0, total_pages - 1, 2):
            shipping_page = pdf.pages[i]
            stamp_page = pdf.pages[i + 1]

            shipping_text = shipping_page.extract_text() or ""
            stamp_text = stamp_page.extract_text() or ""

            # Extract info
            shipping_info = extract_shipping_info(shipping_text)
            stamp_info = extract_stamp_info(stamp_text)

            customer_name = shipping_info["customer_name"]
            order_number = shipping_info["order_number"]
            despatch_date = shipping_info["despatch_date"]
            tracking_number = stamp_info["tracking_number"]
            service_type = stamp_info["service_type"]

            print(f"  Order {order_number}: {customer_name} — {tracking_number} ({service_type})")

            if not customer_name:
                print(f"  ERROR: Could not extract customer name from page {i}")
                errors += 1
                continue

            # Save stamp as standalone PDF
            stamp_filename = f"order-{despatch_date}-{customer_name}.pdf"
            stamp_pdf_path = BATCHES_DIR / stamp_filename
            save_stamp_pdf(str(pdf_path), i + 1, stamp_pdf_path)

            # Look up the customer's email *without* writing the stamp PDF
            # to their Dropbox folder yet — we don't want to leave files in
            # customer folders for dispatches we couldn't record server-side.
            email_address = ""
            customer_folder = find_customer_folder(customer_name)
            if customer_folder:
                email = read_customer_email(customer_folder)
                if email:
                    email_address = email
                else:
                    print(f"  WARNING: No .customer_email.txt found in {customer_folder.name}")
            else:
                print(f"  WARNING: No Dropbox folder found for '{customer_name}'")

            # POST-before-email: authorise the dispatch (decrements credit)
            # BEFORE sending the customer email or copying anything to the
            # customer's folder. Idempotency makes retries free: a repeat
            # run gets a 200 replay and we can re-send the email + re-copy
            # without re-charging.
            dispatch_ok = post_tracking_record(
                order_number=order_number,
                customer_name=customer_name,
                tracking_number=tracking_number,
                service_type=service_type,
                despatch_date=despatch_date,
                email_sent=False,
                email_address=email_address,
                source_file=source_filename,
            )
            if dispatch_ok:
                print(f"  Tracking posted to API")
            else:
                print(f"  Skipping — server did not record dispatch")
                errors += 1
                stamp_pdf_path.unlink(missing_ok=True)
                continue

            # Now that we're committed, copy the stamp into the customer's
            # Dropbox folder.
            if customer_folder:
                dest = customer_folder / stamp_filename
                shutil.copy2(stamp_pdf_path, dest)
                print(f"  Copied stamp to: {dest}")

            # Send dispatch email
            email_sent = False
            if email_address and SMTP_HOST:
                email_sent = send_dispatch_email(
                    to_email=email_address,
                    customer_name=customer_name,
                    tracking_number=tracking_number,
                    service_type=service_type,
                    stamp_pdf_path=stamp_pdf_path,
                )
                if email_sent:
                    print(f"  Email sent to {email_address}")
                else:
                    errors += 1
            elif not email_address:
                print(f"  Skipping email (no address found)")

            # Clean up temp stamp PDF from batches dir
            stamp_pdf_path.unlink(missing_ok=True)

            processed += 1

    return processed, errors


def main() -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    # Find new PDFs (batch: orders-*.pdf, single: order-*-label.pdf)
    pdf_files = sorted(
        glob.glob(str(BATCHES_DIR / "orders-*.pdf"))
        + glob.glob(str(BATCHES_DIR / "order-*-label*.pdf"))
    )

    if not pdf_files:
        print("No new PDFs found in batches/. Nothing to do.")
        return

    print(f"Found {len(pdf_files)} PDF(s) to process.")

    total_processed = 0
    total_errors = 0

    for pdf_file in pdf_files:
        pdf_path = Path(pdf_file)
        try:
            processed, errs = process_pdf(pdf_path)
            total_processed += processed
            total_errors += errs

            # Move to processed
            dest = PROCESSED_DIR / pdf_path.name
            shutil.move(str(pdf_path), str(dest))
            print(f"  Moved to: {dest}")
        except OutOfCredits as e:
            print(f"\n{'='*40}")
            print(f"OUT OF CREDITS — batch halted.")
            print(f"Balance: {e.balance}")
            print(f"Top up:  {e.top_up_url}")
            print(f"PDF left in place: {pdf_path.name}")
            sys.exit(2)
        except AuthRequired as e:
            print(f"\nNot signed in: {e}")
            print("Run the GUI app and use Sign in, or top up via the web dashboard.")
            sys.exit(3)
        except Exception as e:
            print(f"  ERROR processing {pdf_path.name}: {e}")
            total_errors += 1

    # Summary
    print(f"\n{'='*40}")
    print(f"Done. {total_processed} orders processed, {total_errors} errors.")


if __name__ == "__main__":
    main()
