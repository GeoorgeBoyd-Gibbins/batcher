"""Dispatch email: recipients (BCC list, Trustpilot invite) and body copy."""

from email import message_from_bytes
from unittest.mock import MagicMock, patch

import pytest

TRUSTPILOT = "id5.co.uk+5c585510e3@invite.trustpilot.com"


@pytest.fixture
def stamp_pdf(tmp_path):
    p = tmp_path / "stamp.pdf"
    p.write_bytes(b"%PDF-1.4\n%stub\n")
    return p


def _send(pb, stamp_pdf, **kwargs):
    """Call send_dispatch_email with SMTP + IMAP stubbed; return the built message."""
    smtp = MagicMock()
    with patch.object(pb.smtplib, "SMTP", return_value=smtp), patch.object(
        pb, "_save_to_sent"
    ):
        ok = pb.send_dispatch_email(
            to_email=kwargs.pop("to_email", "customer@example.com"),
            customer_name=kwargs.pop("customer_name", "Jane Smith"),
            tracking_number="AB123456789GB",
            service_type="Tracked 24",
            stamp_pdf_path=stamp_pdf,
        )
    assert ok is True
    smtp.send_message.assert_called_once()
    return smtp.send_message.call_args.args[0]


def test_bcc_includes_sender_contact_and_trustpilot(pb, stamp_pdf):
    pb.SENDER_EMAIL = "george@id5.co.uk"
    msg = _send(pb, stamp_pdf)
    bcc = [a.strip() for a in msg["Bcc"].split(",")]
    assert bcc == ["george@id5.co.uk", "contact@id5.co.uk", TRUSTPILOT]


def test_to_header_carries_customer_name_for_trustpilot(pb, stamp_pdf):
    pb.SENDER_EMAIL = "george@id5.co.uk"
    msg = _send(pb, stamp_pdf, customer_name="Jane Smith")
    assert msg["To"] == "Jane Smith <customer@example.com>"


def test_to_header_without_name_is_bare_address(pb, stamp_pdf):
    pb.SENDER_EMAIL = "george@id5.co.uk"
    msg = _send(pb, stamp_pdf, customer_name="")
    assert msg["To"] == "customer@example.com"


def test_test_mode_redirects_and_skips_trustpilot(pb, stamp_pdf, capsys):
    pb.SENDER_EMAIL = "george@id5.co.uk"
    pb.TEST_EMAIL_OVERRIDE = "me@test.example"
    msg = _send(pb, stamp_pdf)
    assert msg["To"] == "Jane Smith <me@test.example>"
    bcc = [a.strip() for a in msg["Bcc"].split(",")]
    assert TRUSTPILOT not in bcc
    assert bcc == ["george@id5.co.uk", "contact@id5.co.uk"]
    assert "Skipping Trustpilot invite BCC" in capsys.readouterr().out


def test_bcc_list_dedupes_and_drops_blanks(pb):
    pb.SENDER_EMAIL = "Contact@id5.co.uk"
    pb.DISPATCH_BCC = ["contact@id5.co.uk", "", " ops@id5.co.uk "]
    pb.TRUSTPILOT_BCC = ""
    assert pb._dispatch_bcc_list() == ["Contact@id5.co.uk", "ops@id5.co.uk"]


def test_trustpilot_bcc_can_be_disabled_via_env(monkeypatch, batcher_env, tmp_home):
    import sys

    monkeypatch.setenv("TRUSTPILOT_BCC", "")
    monkeypatch.setenv("DISPATCH_BCC", "a@id5.co.uk, b@id5.co.uk")
    monkeypatch.setenv("SENDER_EMAIL", "george@id5.co.uk")
    sys.modules.pop("process_batches", None)
    import process_batches

    assert process_batches.TRUSTPILOT_BCC == ""
    assert process_batches._dispatch_bcc_list() == [
        "george@id5.co.uk",
        "a@id5.co.uk",
        "b@id5.co.uk",
    ]


def test_smtplib_delivers_to_bcc_but_strips_header(pb, stamp_pdf):
    """Sanity check on the stdlib contract we rely on."""
    pb.SENDER_EMAIL = "george@id5.co.uk"
    msg = _send(pb, stamp_pdf)

    captured = {}

    class FakeSMTP:
        def ehlo_or_helo_if_needed(self):
            pass

        def has_extn(self, name):
            return False

        def sendmail(self, from_addr, to_addrs, flat, *a, **k):
            captured["to"] = list(to_addrs)
            captured["flat"] = flat
            return {}

    import smtplib

    smtplib.SMTP.send_message(FakeSMTP(), msg)  # type: ignore[arg-type]
    assert set(captured["to"]) == {
        "customer@example.com",
        "george@id5.co.uk",
        "contact@id5.co.uk",
        TRUSTPILOT,
    }
    wire = message_from_bytes(captured["flat"])
    assert wire["Bcc"] is None


def test_body_explains_trustpilot_invite_instead_of_link(pb):
    html = pb.build_email_html("AB123456789GB", "Tracked 24")
    assert "trustpilot.com/evaluate" not in html
    assert "separate invitation from Trustpilot" in html
    assert "AB123456789GB" in html


# ---------------------------------------------------------------------------
# TRUSTPILOT_MODE toggle: bcc (default) / link / off
# ---------------------------------------------------------------------------

REVIEW_URL = "https://www.trustpilot.com/evaluate/id5.co.uk"


def test_trustpilot_mode_defaults_to_bcc(pb):
    assert pb.TRUSTPILOT_MODE == "bcc"
    assert pb.TRUSTPILOT_REVIEW_URL == REVIEW_URL


def test_link_mode_drops_bcc_and_adds_hyperlink(pb, stamp_pdf):
    pb.SENDER_EMAIL = "george@id5.co.uk"
    pb.TRUSTPILOT_MODE = "link"
    msg = _send(pb, stamp_pdf)
    bcc = [a.strip() for a in msg["Bcc"].split(",")]
    assert bcc == ["george@id5.co.uk", "contact@id5.co.uk"]
    html = msg.get_payload()[0].get_payload(decode=True).decode()
    assert f'<a href="{REVIEW_URL}" style="color: #0563C1;">Trustpilot Review</a>' in html
    assert "Would you mind leaving us a" in html
    assert "It takes literally 2 minutes to do and massively helps us." in html
    assert "separate invitation from Trustpilot" not in html


def test_link_mode_uses_configured_review_url(pb):
    pb.TRUSTPILOT_MODE = "link"
    pb.TRUSTPILOT_REVIEW_URL = "https://uk.trustpilot.com/evaluate/example.com"
    html = pb.build_email_html("AB123456789GB", "Tracked 24")
    assert 'href="https://uk.trustpilot.com/evaluate/example.com"' in html


def test_off_mode_drops_bcc_and_review_paragraph(pb, stamp_pdf):
    pb.SENDER_EMAIL = "george@id5.co.uk"
    pb.TRUSTPILOT_MODE = "off"
    msg = _send(pb, stamp_pdf)
    bcc = [a.strip() for a in msg["Bcc"].split(",")]
    assert TRUSTPILOT not in bcc
    html = msg.get_payload()[0].get_payload(decode=True).decode()
    assert "Trustpilot" not in html
    assert "favour" not in html
    assert "Let me know if you have any queries." in html
    assert "Kind regards" in html


def test_build_email_html_accepts_explicit_mode(pb):
    pb.TRUSTPILOT_MODE = "bcc"
    assert "trustpilot.com/evaluate" in pb.build_email_html("X", "Tracked 24", "link")
    assert "Trustpilot" not in pb.build_email_html("X", "Tracked 24", "off")


def test_link_mode_in_test_mode_does_not_print_bcc_skip(pb, capsys):
    pb.SENDER_EMAIL = "george@id5.co.uk"
    pb.TRUSTPILOT_MODE = "link"
    assert pb._dispatch_bcc_list(test_mode=True) == ["george@id5.co.uk", "contact@id5.co.uk"]
    assert "Skipping Trustpilot" not in capsys.readouterr().out


def test_trustpilot_mode_read_from_env(monkeypatch, batcher_env, tmp_home):
    import sys

    monkeypatch.setenv("TRUSTPILOT_MODE", " Link ")
    monkeypatch.setenv("TRUSTPILOT_REVIEW_URL", "https://www.trustpilot.com/evaluate/x.co.uk")
    sys.modules.pop("process_batches", None)
    import process_batches

    assert process_batches.TRUSTPILOT_MODE == "link"
    assert process_batches.TRUSTPILOT_REVIEW_URL == "https://www.trustpilot.com/evaluate/x.co.uk"


def test_unknown_trustpilot_mode_falls_back_to_bcc(monkeypatch, batcher_env, tmp_home, capsys):
    import sys

    monkeypatch.setenv("TRUSTPILOT_MODE", "banana")
    sys.modules.pop("process_batches", None)
    import process_batches

    assert process_batches.TRUSTPILOT_MODE == "bcc"
    assert "TRUSTPILOT_MODE" in capsys.readouterr().out


def test_reply_to_header_set_when_configured(pb, stamp_pdf):
    pb.SENDER_EMAIL = "contact@id5.co.uk"
    pb.REPLY_TO = "george@id5.co.uk"
    msg = _send(pb, stamp_pdf)
    assert msg["From"] == "contact@id5.co.uk"
    assert msg["Reply-To"] == "george@id5.co.uk"


def test_no_reply_to_header_by_default(pb, stamp_pdf):
    pb.SENDER_EMAIL = "george@id5.co.uk"
    pb.REPLY_TO = ""
    msg = _send(pb, stamp_pdf)
    assert msg["Reply-To"] is None
