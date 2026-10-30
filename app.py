"""
Tkinter GUI for ID5 Batch Order Label Processor.

Provides a visual interface to scan batch PDFs, preview orders,
selectively process them, and view live progress logs.
"""

import glob
import os
import shutil
import sys
import tkinter as tk
import webbrowser
from tkinter import messagebox, simpledialog, ttk
from pathlib import Path

import pdfplumber
import requests

# Determine the real app directory before importing process_batches,
# so we can load .env from next to the .app (not from inside the bundle).
from dotenv import load_dotenv

if getattr(sys, "frozen", False):
    # .app/Contents/MacOS/exe  →  up 3 levels = directory containing the .app
    _app_dir = Path(sys.executable).resolve().parent.parent.parent.parent
else:
    _app_dir = Path(__file__).resolve().parent

# Load .env from the directory containing the .app (overrides any bundled copy)
load_dotenv(_app_dir / ".env", override=True)

import auth
import process_batches
from process_batches import (
    AuthRequired,
    OutOfCredits,
    extract_shipping_info,
    extract_stamp_info,
    find_customer_folder,
    post_tracking_record,
    read_customer_email,
    save_stamp_pdf,
    send_dispatch_email,
)

# Re-read config after our .env load (process_batches read them at import time
# from the bundled .env; we need the values from the real .env next to the app).
SMTP_HOST = os.getenv("SMTP_HOST", "")
BATCHER_API_URL = os.getenv("BATCHER_API_URL", "https://id5data2.onrender.com").rstrip("/")

# Patch config values in process_batches so its functions use the correct settings.
# Only override if the env var is actually set — otherwise keep process_batches defaults.
for _var in (
    "BATCHER_API_URL", "SMTP_HOST", "SMTP_PORT",
    "SMTP_USER", "SMTP_PASSWORD", "SENDER_EMAIL", "IMAP_HOST", "IMAP_PORT",
    "TEST_EMAIL_OVERRIDE", "DISPATCH_BCC", "TRUSTPILOT_BCC", "TRUSTPILOT_MODE",
    "TRUSTPILOT_REVIEW_URL", "REPLY_TO", "TRACKING_API_URL", "TRACKING_API_KEY",
):
    _val = os.environ.get(_var)
    if _val is not None:
        if _var in ("SMTP_PORT", "IMAP_PORT"):
            setattr(process_batches, _var, int(_val) if _val else 0)
        elif _var == "BATCHER_API_URL":
            setattr(process_batches, _var, _val.rstrip("/"))
        elif _var == "DISPATCH_BCC":
            setattr(process_batches, _var, [a.strip() for a in _val.split(",") if a.strip()])
        elif _var == "TRUSTPILOT_MODE":
            _mode = _val.strip().lower() or "bcc"
            if _mode not in process_batches.TRUSTPILOT_MODES:
                _mode = "bcc"
            process_batches.TRUSTPILOT_MODE = _mode
        elif _var == "TRUSTPILOT_REVIEW_URL":
            if _val.strip():
                process_batches.TRUSTPILOT_REVIEW_URL = _val.strip()
        elif _var in ("TRUSTPILOT_BCC", "TRACKING_API_URL", "TRACKING_API_KEY"):
            setattr(process_batches, _var, _val.strip())
        else:
            setattr(process_batches, _var, _val)

_batches_env = os.getenv("BATCHES_DIR", "./batches")
BATCHES_DIR = Path(_batches_env) if os.path.isabs(_batches_env) else _app_dir / _batches_env
PROCESSED_DIR = BATCHES_DIR / "processed"

process_batches.BATCHES_DIR = BATCHES_DIR
process_batches.PROCESSED_DIR = PROCESSED_DIR


class BatchProcessorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ID5 Batch Processor")
        self.root.minsize(620, 520)

        self.orders: list[dict] = []
        self.check_vars: list[tk.BooleanVar] = []

        self._build_menu()
        self._build_ui()
        self._log_config()
        self._refresh_auth_label()
        # Prompt for sign-in lazily on the first batch run so the app
        # always opens cleanly. Operators not yet migrated to the new
        # /v1 flow can still use the GUI to scan and triage PDFs.

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        account_menu = tk.Menu(menubar, tearoff=False)
        account_menu.add_command(label="Sign in…", command=self._prompt_login)
        account_menu.add_command(label="Sign out", command=self._sign_out)
        account_menu.add_separator()
        account_menu.add_command(label="Open billing", command=self._open_billing)
        menubar.add_cascade(label="Account", menu=account_menu)

        # Trustpilot review request: BCC invite vs. hyperlink vs. off.
        # Changes apply to this session; set TRUSTPILOT_MODE in .env to persist.
        self.trustpilot_mode = tk.StringVar(value=process_batches.TRUSTPILOT_MODE)
        email_menu = tk.Menu(menubar, tearoff=False)
        for value, label in (
            ("bcc", "Trustpilot: BCC automated invite"),
            ("link", "Trustpilot: review link in email"),
            ("off", "Trustpilot: no review request"),
        ):
            email_menu.add_radiobutton(
                label=label,
                value=value,
                variable=self.trustpilot_mode,
                command=self._set_trustpilot_mode,
            )
        menubar.add_cascade(label="Email", menu=email_menu)
        self.root.configure(menu=menubar)

    def _set_trustpilot_mode(self) -> None:
        mode = self.trustpilot_mode.get()
        if mode not in process_batches.TRUSTPILOT_MODES:
            mode = "bcc"
            self.trustpilot_mode.set(mode)
        process_batches.TRUSTPILOT_MODE = mode
        self._log(f"Trustpilot review request set to: {self._trustpilot_mode_text(mode)}")

    @staticmethod
    def _trustpilot_mode_text(mode: str) -> str:
        return {
            "bcc": "BCC automated invite",
            "link": f"review link ({process_batches.TRUSTPILOT_REVIEW_URL})",
            "off": "none",
        }.get(mode, mode)

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 5}

        # --- Scan button ---
        self.scan_btn = ttk.Button(
            self.root, text="Scan for Batches", command=self._scan
        )
        self.scan_btn.pack(**pad, anchor="w")

        # --- Order checklist (Treeview with checkboxes) ---
        list_frame = ttk.Frame(self.root)
        list_frame.pack(fill="both", expand=True, **pad)

        columns = ("order", "name", "tracking", "service")
        self.tree = ttk.Treeview(
            list_frame, columns=columns, show="tree headings", height=12
        )
        self.tree.heading("order", text="Order")
        self.tree.heading("name", text="Customer")
        self.tree.heading("tracking", text="Tracking")
        self.tree.heading("service", text="Service")

        self.tree.column("#0", width=30, stretch=False)
        self.tree.column("order", width=70, anchor="w")
        self.tree.column("name", width=180, anchor="w")
        self.tree.column("tracking", width=180, anchor="w")
        self.tree.column("service", width=90, anchor="w")

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Button-1>", self._on_tree_click)

        # --- Select All / Deselect All + count ---
        sel_frame = ttk.Frame(self.root)
        sel_frame.pack(fill="x", **pad)

        ttk.Button(sel_frame, text="Select All", command=self._select_all).pack(side="left")
        ttk.Button(sel_frame, text="Deselect All", command=self._deselect_all).pack(
            side="left", padx=(5, 0)
        )
        self.count_label = ttk.Label(sel_frame, text="")
        self.count_label.pack(side="right")

        # --- Process button + auth status ---
        action_frame = ttk.Frame(self.root)
        action_frame.pack(fill="x", **pad)
        self.process_btn = ttk.Button(
            action_frame, text="Process Selected", command=self._start_processing
        )
        self.process_btn.pack(side="left")
        self.process_btn.state(["disabled"])
        self.auth_label = ttk.Label(action_frame, text="", foreground="#555")
        self.auth_label.pack(side="right")

        # --- Log area ---
        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill="both", expand=True, **pad)

        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _log_config(self) -> None:
        """Show key config on startup so user can verify .env loaded."""
        env_path = _app_dir / ".env"
        self._log(f"Config: .env={'found' if env_path.exists() else 'MISSING'} at {_app_dir}")
        self._log(f"  SMTP: {'configured' if SMTP_HOST else 'NOT configured'}")
        self._log(f"  Batcher API: {BATCHER_API_URL}")
        self._log(f"  Batches dir: {BATCHES_DIR}")
        self._log(
            "  Trustpilot: "
            f"{self._trustpilot_mode_text(process_batches.TRUSTPILOT_MODE)} (Email menu to change)"
        )
        self._log("")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _refresh_auth_label(self) -> None:
        if process_batches.BYPASS_AUTH:
            legacy = process_batches.TRACKING_API_URL and process_batches.TRACKING_API_KEY
            self.auth_label.configure(
                text="Local mode (tracking saved via /tracking)"
                if legacy
                else "Local mode (auth bypassed — tracking NOT saved)",
                foreground="#555" if legacy else "#a00",
            )
            return
        token = auth.get_token()
        if not token:
            self.auth_label.configure(text="Not signed in", foreground="#a00")
            return
        # Fetch balance lazily; degrade gracefully on network errors.
        try:
            resp = requests.get(
                f"{BATCHER_API_URL}/v1/account",
                headers={"Authorization": f"Bearer {token}"},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                self.auth_label.configure(
                    text=f"{data.get('email', '')} — {data.get('balance', 0)} credits",
                    foreground="#333",
                )
                # Refresh cached account_id in case it changed (e.g. after sign-out/in).
                if data.get("id"):
                    auth.set_account_id(data["id"])
                return
            if resp.status_code == 401:
                auth.clear_token()
                self.auth_label.configure(text="Session expired — sign in again", foreground="#a00")
                return
        except requests.RequestException:
            pass
        self.auth_label.configure(text="Signed in (offline)", foreground="#555")

    def _prompt_login(self) -> None:
        email = simpledialog.askstring(
            "Sign in to Batcher",
            f"Email address for {BATCHER_API_URL}:",
            parent=self.root,
        )
        if not email:
            return
        password = simpledialog.askstring(
            "Sign in to Batcher",
            "Password:",
            show="*",
            parent=self.root,
        )
        if not password:
            return
        try:
            resp = requests.post(
                f"{BATCHER_API_URL}/v1/auth/token",
                json={"email": email.strip(), "password": password, "label": "desktop"},
                timeout=15,
            )
        except requests.RequestException as e:
            messagebox.showerror("Sign in failed", f"Could not reach server: {e}")
            return
        if resp.status_code != 201:
            try:
                msg = resp.json().get("error", resp.text[:200])
            except ValueError:
                msg = resp.text[:200]
            messagebox.showerror("Sign in failed", f"{resp.status_code}: {msg}")
            return
        data = resp.json()
        auth.set_token(data["token"])
        auth.set_account_id(data["account_id"])
        self._log_now(f"Signed in as {data.get('email', email)}.")
        self._refresh_auth_label()

    def _sign_out(self) -> None:
        if not auth.get_token():
            return
        if not messagebox.askokcancel("Sign out", "Sign out of Batcher on this device?"):
            return
        auth.clear_token()
        self._log_now("Signed out.")
        self._refresh_auth_label()

    def _open_billing(self) -> None:
        webbrowser.open(f"{BATCHER_API_URL}/account")

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        """Append a line to the log area (thread-safe via after)."""
        self.root.after(0, self._log_ui, message)

    def _log_now(self, message: str) -> None:
        """Append a line and immediately refresh the GUI (main thread only)."""
        self._log_ui(message)
        self.root.update()

    def _log_ui(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # Checkbox helpers
    # ------------------------------------------------------------------

    def _checked_tag(self) -> str:
        return "\u2611"

    def _unchecked_tag(self) -> str:
        return "\u2610"

    def _on_tree_click(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        region = self.tree.identify_region(event.x, event.y)
        if region == "tree":
            item = self.tree.identify_row(event.y)
            if item:
                idx = self.tree.index(item)
                self.check_vars[idx].set(not self.check_vars[idx].get())
                mark = self._checked_tag() if self.check_vars[idx].get() else self._unchecked_tag()
                self.tree.item(item, text=mark)
                self._update_count()

    def _select_all(self) -> None:
        for i, var in enumerate(self.check_vars):
            var.set(True)
        for item in self.tree.get_children():
            self.tree.item(item, text=self._checked_tag())
        self._update_count()

    def _deselect_all(self) -> None:
        for i, var in enumerate(self.check_vars):
            var.set(False)
        for item in self.tree.get_children():
            self.tree.item(item, text=self._unchecked_tag())
        self._update_count()

    def _update_count(self) -> None:
        n = sum(1 for v in self.check_vars if v.get())
        total = len(self.check_vars)
        self.count_label.configure(text=f"{n} of {total} selected")

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def _scan(self) -> None:
        self.orders.clear()
        self.check_vars.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        pdf_files = sorted(
            glob.glob(str(BATCHES_DIR / "orders-*.pdf"))
            + glob.glob(str(BATCHES_DIR / "order-*-label*.pdf"))
        )

        if not pdf_files:
            self._log("No PDFs found in batches/. Nothing to do.")
            self.process_btn.state(["disabled"])
            self._update_count()
            return

        self._log(f"Found {len(pdf_files)} PDF(s). Scanning...")

        seen_orders: set[tuple[str, str]] = set()
        for pdf_file in pdf_files:
            pdf_path = Path(pdf_file)
            try:
                with pdfplumber.open(pdf_path) as pdf:
                    total_pages = len(pdf.pages)
                    if total_pages % 2 != 0:
                        self._log(
                            f"  WARNING: {pdf_path.name} has odd page count ({total_pages})"
                        )

                    for i in range(0, total_pages - 1, 2):
                        shipping_text = pdf.pages[i].extract_text() or ""
                        stamp_text = pdf.pages[i + 1].extract_text() or ""

                        shipping_info = extract_shipping_info(shipping_text)
                        stamp_info = extract_stamp_info(stamp_text)

                        order = {
                            "source_pdf": str(pdf_path),
                            "page_index": i,
                            "order_number": shipping_info["order_number"],
                            "customer_name": shipping_info["customer_name"],
                            "despatch_date": shipping_info["despatch_date"],
                            "tracking_number": stamp_info["tracking_number"],
                            "service_type": stamp_info["service_type"],
                        }

                        # Skip duplicates (same order may appear in batch and individual PDFs)
                        seen_key = (order["order_number"], order["tracking_number"])
                        if seen_key in seen_orders:
                            continue
                        seen_orders.add(seen_key)

                        self.orders.append(order)

                        var = tk.BooleanVar(value=True)
                        self.check_vars.append(var)

                        trk_display = order["tracking_number"][:14] + ".." if len(order["tracking_number"]) > 14 else order["tracking_number"]
                        self.tree.insert(
                            "",
                            "end",
                            text=self._checked_tag(),
                            values=(
                                order["order_number"],
                                order["customer_name"],
                                trk_display,
                                order["service_type"],
                            ),
                        )
            except Exception as e:
                self._log(f"  ERROR reading {pdf_path.name}: {e}")

        self._log(f"Scanned {len(self.orders)} orders.")
        self._update_count()
        if self.orders:
            self.process_btn.state(["!disabled"])

    # ------------------------------------------------------------------
    # Process
    # ------------------------------------------------------------------

    def _start_processing(self) -> None:
        selected = [
            self.orders[i]
            for i, var in enumerate(self.check_vars)
            if var.get()
        ]
        if not selected:
            self._log("No orders selected.")
            return

        if not process_batches.BYPASS_AUTH and not auth.get_token():
            self._log_now("Sign in required before processing — opening login.")
            self._prompt_login()
            if not auth.get_token():
                return

        self.scan_btn.state(["disabled"])
        self.process_btn.state(["disabled"])

        # Process in main thread to avoid macOS tkinter+thread socket issues.
        # GUI stays updated via root.update() calls between orders.
        self._process_orders(selected)
        self._refresh_auth_label()

    def _process_orders(self, selected: list[dict]) -> None:
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

        processed = 0
        errors = 0

        # Track which source PDFs are fully handled so we can move them afterwards
        source_pdfs: set[str] = set()
        for order in selected:
            source_pdfs.add(order["source_pdf"])

        for order in selected:
            order_number = order["order_number"]
            customer_name = order["customer_name"]
            despatch_date = order["despatch_date"]
            tracking_number = order["tracking_number"]
            service_type = order["service_type"]
            source_pdf = order["source_pdf"]
            stamp_page_index = order["page_index"] + 1  # even page (0-indexed)
            source_filename = Path(source_pdf).name

            if not Path(source_pdf).exists():
                self._log_now(f"Order {order_number}: SKIPPED — source PDF already moved ({source_filename})")
                errors += 1
                continue

            self._log_now(f"Order {order_number}: {customer_name} \u2014 {tracking_number} ({service_type})")

            if not customer_name:
                self._log_now(f"  ERROR: No customer name for page {order['page_index']}")
                errors += 1
                continue

            # Save stamp PDF
            stamp_filename = f"order-{despatch_date}-{customer_name}.pdf"
            stamp_pdf_path = BATCHES_DIR / stamp_filename
            try:
                save_stamp_pdf(source_pdf, stamp_page_index, stamp_pdf_path)
            except Exception as e:
                self._log_now(f"  ERROR saving stamp PDF: {e}")
                errors += 1
                continue

            # Look up the customer's email *without* copying the stamp into
            # their Dropbox folder yet — that comes after the server has
            # successfully recorded the dispatch.
            email_address = ""
            customer_folder = find_customer_folder(customer_name)
            if customer_folder:
                try:
                    email = read_customer_email(customer_folder)
                except (TimeoutError, OSError) as e:
                    email = None
                    self._log_now(f"  ERROR: Timeout reading email for {customer_name} — check Dropbox is online")
                if email:
                    email_address = email
                else:
                    self._log_now(f"  WARNING: No email found for {customer_name}")
            else:
                self._log_now(f"  WARNING: No Dropbox folder for '{customer_name}'")

            # POST-before-email: authorise the dispatch (decrements credit)
            # before sending the customer email or copying anything to the
            # customer's folder. If the POST fails we skip both so we never
            # advertise a dispatch the server doesn't know about.
            try:
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
            except OutOfCredits as e:
                self._log_now(f"  OUT OF CREDITS \u2014 balance {e.balance}. Halting batch.")
                messagebox.showerror(
                    "Out of credits",
                    f"Your balance is {e.balance}. Top up to continue:\n\n{e.top_up_url}",
                )
                webbrowser.open(e.top_up_url)
                stamp_pdf_path.unlink(missing_ok=True)
                self._log_now("Batch halted. Top up and rerun \u2014 already-processed orders are idempotent.")
                self._processing_done()
                return
            except AuthRequired as e:
                self._log_now(f"  AUTH REQUIRED: {e}")
                messagebox.showerror("Sign in required", str(e))
                stamp_pdf_path.unlink(missing_ok=True)
                self._processing_done()
                return
            except Exception as e:
                self._log_now(f"  ERROR posting tracking: {e}")
                errors += 1
                stamp_pdf_path.unlink(missing_ok=True)
                continue

            if not dispatch_ok:
                self._log_now(f"  ERROR: server did not record dispatch; skipping email")
                errors += 1
                stamp_pdf_path.unlink(missing_ok=True)
                continue

            self._log_now(f"  Tracking posted")

            # Now that we're committed, copy the stamp into the customer's
            # Dropbox folder.
            if customer_folder:
                dest = customer_folder / stamp_filename
                shutil.copy2(stamp_pdf_path, dest)
                self._log_now(f"  Copied stamp to folder")

            # Send email
            email_sent = False
            if email_address and SMTP_HOST:
                try:
                    email_sent = send_dispatch_email(
                        to_email=email_address,
                        customer_name=customer_name,
                        tracking_number=tracking_number,
                        service_type=service_type,
                        stamp_pdf_path=stamp_pdf_path,
                    )
                    if email_sent:
                        self._log_now(f"  Email sent to {email_address}")
                    else:
                        self._log_now(f"  ERROR: send_dispatch_email returned False")
                        errors += 1
                except Exception as e:
                    self._log_now(f"  ERROR sending email: {e}")
                    errors += 1
            elif not email_address:
                self._log_now(f"  Skipped email (no address)")
            elif not SMTP_HOST:
                self._log_now(f"  Skipped email (SMTP not configured)")

            # Clean up temp stamp
            stamp_pdf_path.unlink(missing_ok=True)

            status = "sent \u2713" if email_sent else "done (no email)"
            self._log_now(f"  \u2192 {status}")
            processed += 1

        # Move processed PDFs
        for pdf_str in source_pdfs:
            pdf_path = Path(pdf_str)
            if pdf_path.exists():
                dest = PROCESSED_DIR / pdf_path.name
                shutil.move(str(pdf_path), str(dest))
                self._log_now(f"Moved {pdf_path.name} to processed/")

        self._log_now(f"\nDone. {processed} processed, {errors} errors.")
        self._processing_done()

    def _processing_done(self) -> None:
        self.scan_btn.state(["!disabled"])
        self.process_btn.state(["!disabled"])


def main() -> None:
    root = tk.Tk()
    BatchProcessorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
