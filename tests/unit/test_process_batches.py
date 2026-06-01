"""Unit tests for process_batches.py — POST, retries, error paths, step ordering."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest
import requests
import responses

# ---------------------------------------------------------------------------
# _client_dispatch_id
# ---------------------------------------------------------------------------


def test_client_dispatch_id_is_deterministic(pb):
    h1 = pb._client_dispatch_id("acc-1", "ORD-1", "TT 1234 5678 9GB")
    h2 = pb._client_dispatch_id("acc-1", "ORD-1", "TT 1234 5678 9GB")
    assert h1 == h2
    expected = hashlib.sha256(b"acc-1|ORD-1|TT 1234 5678 9GB").hexdigest()
    assert h1 == expected


def test_client_dispatch_id_distinct_for_different_inputs(pb):
    a = pb._client_dispatch_id("acc-1", "ORD-1", "TN")
    b = pb._client_dispatch_id("acc-2", "ORD-1", "TN")
    assert a != b


# ---------------------------------------------------------------------------
# post_tracking_record — happy + replay
# ---------------------------------------------------------------------------


def _stub_auth(monkeypatch, pb, token="tok", account_id="acc-1"):
    monkeypatch.setattr(pb.auth, "get_token", lambda: token)
    monkeypatch.setattr(pb.auth, "get_account_id", lambda: account_id)


DISPATCH_ARGS = dict(
    order_number="1",
    customer_name="A",
    tracking_number="TT 0000 0000 0GB",
    service_type="Tracked 24",
    despatch_date="2026-05-01",
    email_sent=False,
    email_address="c@example.com",
    source_file="orders-2026-05-01.pdf",
)


@responses.activate
def test_post_tracking_record_201_returns_true(pb, monkeypatch):
    _stub_auth(monkeypatch, pb)
    responses.add(
        responses.POST,
        "https://server.example/v1/dispatches",
        json={"status": "created", "dispatch_id": 42},
        status=201,
    )
    assert pb.post_tracking_record(**DISPATCH_ARGS) is True


@responses.activate
def test_post_tracking_record_200_replay_returns_true(pb, monkeypatch):
    _stub_auth(monkeypatch, pb)
    responses.add(
        responses.POST,
        "https://server.example/v1/dispatches",
        json={"status": "replay", "dispatch_id": 42},
        status=200,
    )
    assert pb.post_tracking_record(**DISPATCH_ARGS) is True


@responses.activate
def test_post_tracking_record_sends_bearer_token_and_correct_payload(pb, monkeypatch):
    _stub_auth(monkeypatch, pb, token="my-bearer", account_id="acc-X")
    rsp = responses.add(
        responses.POST,
        "https://server.example/v1/dispatches",
        json={"status": "created", "dispatch_id": 1},
        status=201,
    )
    pb.post_tracking_record(**DISPATCH_ARGS)
    call = rsp.calls[0]
    assert call.request.headers["Authorization"] == "Bearer my-bearer"
    body = call.request.body
    import json as _json
    payload = _json.loads(body)
    assert payload["client_dispatch_id"] == hashlib.sha256(
        b"acc-X|1|TT 0000 0000 0GB"
    ).hexdigest()
    assert "client_dispatch_id" in payload
    assert payload["order_number"] == "1"
    assert payload["source_file"] == "orders-2026-05-01.pdf"


# ---------------------------------------------------------------------------
# 402 / 401 / 500
# ---------------------------------------------------------------------------


@responses.activate
def test_post_tracking_record_402_raises_out_of_credits(pb, monkeypatch):
    _stub_auth(monkeypatch, pb)
    responses.add(
        responses.POST,
        "https://server.example/v1/dispatches",
        json={"error": "insufficient_credits", "balance": 0, "top_up_url": "https://server.example/account"},
        status=402,
    )
    with pytest.raises(pb.OutOfCredits) as excinfo:
        pb.post_tracking_record(**DISPATCH_ARGS)
    assert excinfo.value.balance == 0
    assert excinfo.value.top_up_url == "https://server.example/account"


@responses.activate
def test_post_tracking_record_401_clears_token_and_raises_auth_required(pb, monkeypatch):
    _stub_auth(monkeypatch, pb)
    cleared = []
    monkeypatch.setattr(pb.auth, "clear_token", lambda: cleared.append(True))

    responses.add(
        responses.POST,
        "https://server.example/v1/dispatches",
        json={"error": "invalid token"},
        status=401,
    )
    with pytest.raises(pb.AuthRequired):
        pb.post_tracking_record(**DISPATCH_ARGS)
    assert cleared == [True]


@responses.activate
def test_post_tracking_record_500_returns_false(pb, monkeypatch, capsys):
    _stub_auth(monkeypatch, pb)
    responses.add(
        responses.POST,
        "https://server.example/v1/dispatches",
        json={"error": "boom"},
        status=500,
    )
    assert pb.post_tracking_record(**DISPATCH_ARGS) is False
    captured = capsys.readouterr()
    assert "WARNING" in captured.out


def test_post_tracking_record_no_token_raises_auth_required(pb, monkeypatch):
    monkeypatch.setattr(pb.auth, "get_token", lambda: None)
    monkeypatch.setattr(pb.auth, "get_account_id", lambda: None)
    with pytest.raises(pb.AuthRequired):
        pb.post_tracking_record(**DISPATCH_ARGS)


# ---------------------------------------------------------------------------
# Timeout retry
# ---------------------------------------------------------------------------


def test_post_tracking_record_retries_once_on_timeout(pb, monkeypatch):
    _stub_auth(monkeypatch, pb)
    fake_response = MagicMock(status_code=201)
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise requests.Timeout("cold start")
        return fake_response

    monkeypatch.setattr(pb.requests, "post", fake_post)

    assert pb.post_tracking_record(**DISPATCH_ARGS) is True
    assert calls == [15, 45]  # first attempt short timeout, retry longer


def test_post_tracking_record_returns_false_on_double_timeout(pb, monkeypatch):
    _stub_auth(monkeypatch, pb)

    def fake_post(url, json=None, headers=None, timeout=None):
        raise requests.Timeout("still cold")

    monkeypatch.setattr(pb.requests, "post", fake_post)
    assert pb.post_tracking_record(**DISPATCH_ARGS) is False


def test_post_tracking_record_retries_on_connection_error(pb, monkeypatch):
    _stub_auth(monkeypatch, pb)
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise requests.ConnectionError("net flap")
        return MagicMock(status_code=201)

    monkeypatch.setattr(pb.requests, "post", fake_post)
    assert pb.post_tracking_record(**DISPATCH_ARGS) is True
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# process_pdf step ordering
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_pdf(tmp_path):
    """Build a minimal 2-page PDF with shipping + stamp pages.

    pdfplumber needs real PDF bytes. We use pypdf to assemble blank pages
    and inject text via reportlab if it's available; otherwise we monkeypatch
    pdfplumber.open and skip the real PDF parsing entirely.
    """
    # Easier: monkeypatch pdfplumber.open below.
    p = tmp_path / "orders-2026-05-01.pdf"
    p.write_bytes(b"%PDF-1.4 stub\n%%EOF\n")
    return p


def _fake_pdf_context(pdf_pages):
    """Return a fake pdfplumber.open context manager yielding the supplied pages."""

    class _Page:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _Pdf:
        def __init__(self):
            self.pages = [_Page(t) for t in pdf_pages]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Pdf()


def test_process_pdf_post_before_dropbox_and_email(pb, monkeypatch, fake_pdf, tmp_path):
    """The critical step-order property: stamp is copied to Dropbox and email
    sent only AFTER the dispatch is recorded server-side."""

    shipping_text = (
        "Shipping Address\nJane Doe\nOrder Number: 18888\nDespatch Date: 19/05/2026\n"
    )
    stamp_text = (
        "Delivered By\n24\nTracked\nGB 1234 5678 9GB\n"
    )
    monkeypatch.setattr(pb.pdfplumber, "open", lambda p: _fake_pdf_context([shipping_text, stamp_text]))

    # Customer folder lookup returns a tmp dir with a .customer_email.txt
    customer_folder = tmp_path / "Jane Doe (ref 18888)"
    customer_folder.mkdir(parents=True)
    (customer_folder / ".customer_email.txt").write_text("jane@example.com")
    monkeypatch.setattr(pb, "find_customer_folder", lambda name: customer_folder)

    call_order = []

    def fake_save_stamp(src, idx, out_path):
        call_order.append("save_stamp")
        out_path.write_bytes(b"stamp pdf")

    monkeypatch.setattr(pb, "save_stamp_pdf", fake_save_stamp)

    def fake_post(**kwargs):
        call_order.append("post")
        return True

    monkeypatch.setattr(pb, "post_tracking_record", fake_post)

    real_copy = pb.shutil.copy2

    def fake_copy2(src, dst):
        call_order.append("dropbox_copy")
        return real_copy(src, dst)

    monkeypatch.setattr(pb.shutil, "copy2", fake_copy2)

    # No SMTP -> email send skipped (we already verify ordering for post vs copy)
    monkeypatch.setattr(pb, "SMTP_HOST", "")
    pb.SMTP_HOST = ""

    processed, errors = pb.process_pdf(fake_pdf)
    assert processed == 1
    assert errors == 0
    # The step order must be: save_stamp -> post -> dropbox_copy
    save_idx = call_order.index("save_stamp")
    post_idx = call_order.index("post")
    copy_idx = call_order.index("dropbox_copy")
    assert save_idx < post_idx < copy_idx, f"bad order: {call_order}"


def test_process_pdf_failed_post_skips_dropbox_and_deletes_stamp(pb, monkeypatch, fake_pdf, tmp_path):
    """If the POST fails (returns False), no Dropbox copy happens AND the
    local stamp file is cleaned up so the operator's batches/ dir doesn't fill
    with orphans."""

    shipping_text = "Shipping Address\nJohn Doe\nOrder Number: 18889\nDespatch Date: 19/05/2026\n"
    stamp_text = "Delivered By\n48\nTracked\nGB 1111 2222 3GB\n"
    monkeypatch.setattr(pb.pdfplumber, "open", lambda p: _fake_pdf_context([shipping_text, stamp_text]))

    customer_folder = tmp_path / "John Doe (ref 18889)"
    customer_folder.mkdir(parents=True)
    (customer_folder / ".customer_email.txt").write_text("john@example.com")
    monkeypatch.setattr(pb, "find_customer_folder", lambda name: customer_folder)

    saved_paths = []

    def fake_save_stamp(src, idx, out_path):
        out_path.write_bytes(b"stamp pdf")
        saved_paths.append(out_path)

    monkeypatch.setattr(pb, "save_stamp_pdf", fake_save_stamp)
    monkeypatch.setattr(pb, "post_tracking_record", lambda **k: False)

    copies = []
    monkeypatch.setattr(pb.shutil, "copy2", lambda s, d: copies.append((s, d)))

    processed, errors = pb.process_pdf(fake_pdf)
    assert processed == 0
    assert errors == 1
    assert copies == [], "no Dropbox copy when post fails"
    # Stamp file was cleaned up (unlink missing_ok)
    assert all(not p.exists() for p in saved_paths), "stamp file should be removed"


def test_process_pdf_out_of_credits_bubbles_up(pb, monkeypatch, fake_pdf, tmp_path):
    shipping_text = "Shipping Address\nA\nOrder Number: 1\nDespatch Date: 19/05/2026\n"
    stamp_text = "Delivered By\n24\nTracked\nGB 0 0 0GB\n"
    monkeypatch.setattr(pb.pdfplumber, "open", lambda p: _fake_pdf_context([shipping_text, stamp_text]))
    monkeypatch.setattr(pb, "find_customer_folder", lambda name: None)
    monkeypatch.setattr(pb, "save_stamp_pdf", lambda *a: None)

    def fake_post(**kwargs):
        raise pb.OutOfCredits(balance=0, top_up_url="https://top-up")

    monkeypatch.setattr(pb, "post_tracking_record", fake_post)
    with pytest.raises(pb.OutOfCredits):
        pb.process_pdf(fake_pdf)


# ---------------------------------------------------------------------------
# main() exit codes
# ---------------------------------------------------------------------------


def test_main_exits_2_on_out_of_credits(pb, monkeypatch, tmp_path):
    pdf_path = pb.BATCHES_DIR / "orders-x.pdf"
    pdf_path.write_bytes(b"stub")

    def boom(p):
        raise pb.OutOfCredits(balance=0, top_up_url="https://top-up")

    monkeypatch.setattr(pb, "process_pdf", boom)

    with pytest.raises(SystemExit) as excinfo:
        pb.main()
    assert excinfo.value.code == 2


def test_main_exits_3_on_auth_required(pb, monkeypatch, tmp_path):
    pdf_path = pb.BATCHES_DIR / "orders-y.pdf"
    pdf_path.write_bytes(b"stub")

    def boom(p):
        raise pb.AuthRequired("nope")

    monkeypatch.setattr(pb, "process_pdf", boom)

    with pytest.raises(SystemExit) as excinfo:
        pb.main()
    assert excinfo.value.code == 3


def test_main_no_pdfs_returns_cleanly(pb, capsys):
    # Empty batches dir
    pb.main()
    out = capsys.readouterr().out
    assert "No new PDFs found" in out
