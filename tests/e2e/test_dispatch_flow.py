"""End-to-end client flows against a mocked HTTP server (responses).

These tests exercise the whole desktop client path:
* token loaded via auth.get_token (keyring or file)
* PDF parsed via process_pdf
* HTTP POST mocked at the requests boundary
* Side effects (Dropbox copy, stamp clean-up) observed on the filesystem

Run with: pytest -m e2e
"""

from __future__ import annotations

import hashlib
import json

import pytest
import responses

pytestmark = pytest.mark.e2e


class _Page:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _Pdf:
    def __init__(self, texts):
        self.pages = [_Page(t) for t in texts]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SHIPPING_TEXT = (
    "Shipping Address\nJane Doe\nOrder Number: 18888\nDespatch Date: 19/05/2026\n"
)
STAMP_TEXT = (
    "Delivered By\n24\nTracked\nGB 1234 5678 9GB\n"
)


@pytest.fixture
def signed_in(auth_mod, fake_keyring):
    auth_mod.set_token("real-token")
    auth_mod.set_account_id("acc-real")
    yield


@responses.activate
def test_full_dispatch_succeeds_copies_stamp_to_dropbox(
    pb, batcher_env, signed_in, monkeypatch, tmp_path
):
    """End-to-end: signed-in client -> POST 201 -> Dropbox copy happens."""
    # Build a fake PDF on disk and tell pdfplumber to yield our pages.
    pdf_path = pb.BATCHES_DIR / "orders-2026-05-19.pdf"
    pdf_path.write_bytes(b"stub")
    monkeypatch.setattr(pb.pdfplumber, "open", lambda p: _Pdf([SHIPPING_TEXT, STAMP_TEXT]))

    # save_stamp_pdf would need a real PDF — stub to write a marker file.
    def fake_save(src, idx, out_path):
        out_path.write_bytes(b"stamp")

    monkeypatch.setattr(pb, "save_stamp_pdf", fake_save)

    # Customer folder exists on disk with an email file.
    customer_folder = pb.DROPBOX_BASE / "Jane Doe (ref 18888)"
    customer_folder.mkdir(parents=True)
    (customer_folder / ".customer_email.txt").write_text("jane@example.com")

    # Mock the server response.
    responses.add(
        responses.POST,
        "https://server.example/v1/dispatches",
        json={"status": "created", "dispatch_id": 42},
        status=201,
    )

    processed, errors = pb.process_pdf(pdf_path)
    assert processed == 1
    assert errors == 0

    # The stamp PDF was copied into the customer's Dropbox folder.
    copied = list(customer_folder.glob("order-*-Jane Doe.pdf"))
    assert copied, "expected stamp file copied into customer folder"

    # And the request that was made carried the right client_dispatch_id.
    posted = json.loads(responses.calls[0].request.body)
    assert posted["client_dispatch_id"] == hashlib.sha256(
        b"acc-real|18888|GB 1234 5678 9GB"
    ).hexdigest()
    assert posted["customer_name"] == "Jane Doe"
    assert posted["service_type"] == "Tracked 24"


@responses.activate
def test_full_dispatch_402_halts_via_main_exit_code_2(
    pb, batcher_env, signed_in, monkeypatch
):
    pdf_path = pb.BATCHES_DIR / "orders-x.pdf"
    pdf_path.write_bytes(b"stub")
    monkeypatch.setattr(pb.pdfplumber, "open", lambda p: _Pdf([SHIPPING_TEXT, STAMP_TEXT]))
    monkeypatch.setattr(pb, "save_stamp_pdf", lambda s, i, o: o.write_bytes(b"stamp"))
    monkeypatch.setattr(pb, "find_customer_folder", lambda n: None)

    responses.add(
        responses.POST,
        "https://server.example/v1/dispatches",
        json={"error": "insufficient_credits", "balance": 0, "top_up_url": "https://server.example/account"},
        status=402,
    )

    with pytest.raises(SystemExit) as excinfo:
        pb.main()
    assert excinfo.value.code == 2

    # The PDF is left in place because we exited mid-batch.
    assert pdf_path.exists()


@responses.activate
def test_full_dispatch_401_clears_token_and_exits_3(
    pb, batcher_env, signed_in, auth_mod, monkeypatch
):
    pdf_path = pb.BATCHES_DIR / "orders-y.pdf"
    pdf_path.write_bytes(b"stub")
    monkeypatch.setattr(pb.pdfplumber, "open", lambda p: _Pdf([SHIPPING_TEXT, STAMP_TEXT]))
    monkeypatch.setattr(pb, "save_stamp_pdf", lambda s, i, o: o.write_bytes(b"stamp"))
    monkeypatch.setattr(pb, "find_customer_folder", lambda n: None)

    responses.add(
        responses.POST,
        "https://server.example/v1/dispatches",
        json={"error": "invalid token"},
        status=401,
    )

    with pytest.raises(SystemExit) as excinfo:
        pb.main()
    assert excinfo.value.code == 3

    # Token was cleared so a re-launch will prompt for sign-in.
    assert auth_mod.get_token() is None


@responses.activate
def test_replay_returns_200_treated_as_success(pb, batcher_env, signed_in, monkeypatch):
    """A repeat run of the same batch should be safe — server returns 200 replay."""
    pdf_path = pb.BATCHES_DIR / "orders-replay.pdf"
    pdf_path.write_bytes(b"stub")
    monkeypatch.setattr(pb.pdfplumber, "open", lambda p: _Pdf([SHIPPING_TEXT, STAMP_TEXT]))
    monkeypatch.setattr(pb, "save_stamp_pdf", lambda s, i, o: o.write_bytes(b"stamp"))
    monkeypatch.setattr(pb, "find_customer_folder", lambda n: None)

    responses.add(
        responses.POST,
        "https://server.example/v1/dispatches",
        json={"status": "replay", "dispatch_id": 99},
        status=200,
    )
    processed, errors = pb.process_pdf(pdf_path)
    assert processed == 1
    assert errors == 0
