"""Limited Tkinter smoke + auth flow tests for app.py.

Tkinter is awkward to drive — we run only enough to confirm the module
imports cleanly under a fake DISPLAY and that the helper methods we added
(_prompt_login, _refresh_auth_label) behave correctly. We construct the
app in a real Tk root because Tkinter widgets need a master, then close
it cleanly after each test.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses


def _have_tk():
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _have_tk(), reason="Tk not available")


@pytest.fixture
def app_mod(batcher_env, tmp_home, fake_keyring, monkeypatch):
    """Import app.py with env scrubbed and keyring stubbed."""
    for name in ("app", "process_batches"):
        sys.modules.pop(name, None)
    import app  # noqa: E402
    return app


@pytest.fixture
def app_instance(app_mod):
    """Build a BatchProcessorApp inside a withdrawn Tk root."""
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    try:
        instance = app_mod.BatchProcessorApp(root)
        yield instance
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_app_module_imports_cleanly(app_mod):
    # If we got here, app.py imported without exploding under our test env.
    assert hasattr(app_mod, "BatchProcessorApp")


def test_app_constructs_without_raising(app_instance):
    assert app_instance is not None


# ---------------------------------------------------------------------------
# _prompt_login
# ---------------------------------------------------------------------------


@responses.activate
def test_prompt_login_success_stores_token_and_account_id(app_instance, app_mod, fake_keyring):
    responses.add(
        responses.POST,
        "https://server.example/v1/auth/token",
        json={"token": "TT", "account_id": "ACC", "email": "o@e.com"},
        status=201,
    )

    with patch.object(app_mod.simpledialog, "askstring", side_effect=["o@e.com", "longpw"]):
        # Don't call _refresh_auth_label twice — patch it to avoid the network call.
        with patch.object(app_instance, "_refresh_auth_label"):
            app_instance._prompt_login()

    assert app_mod.auth.get_token() == "TT"
    assert app_mod.auth.get_account_id() == "ACC"


def test_prompt_login_cancel_does_not_post(app_instance, app_mod):
    posted = []

    def fake_post(*a, **kw):
        posted.append(True)
        return MagicMock(status_code=201)

    with patch.object(app_mod.simpledialog, "askstring", return_value=None):
        with patch.object(app_mod.requests, "post", fake_post):
            app_instance._prompt_login()
    assert posted == []


@responses.activate
def test_prompt_login_bad_credentials_shows_error_box_and_no_token(app_instance, app_mod, fake_keyring):
    responses.add(
        responses.POST,
        "https://server.example/v1/auth/token",
        json={"error": "invalid credentials"},
        status=401,
    )
    with patch.object(app_mod.simpledialog, "askstring", side_effect=["o@e.com", "wrong"]):
        with patch.object(app_mod.messagebox, "showerror") as showerror:
            with patch.object(app_instance, "_refresh_auth_label"):
                app_instance._prompt_login()
    showerror.assert_called_once()
    assert app_mod.auth.get_token() is None


# ---------------------------------------------------------------------------
# _refresh_auth_label
# ---------------------------------------------------------------------------


@responses.activate
def test_refresh_auth_label_signed_in_shows_balance(app_instance, app_mod, fake_keyring):
    app_mod.auth.set_token("TOK")
    responses.add(
        responses.GET,
        "https://server.example/v1/account",
        json={"id": "ACC", "email": "o@e.com", "balance": 42},
        status=200,
    )
    app_instance._refresh_auth_label()
    text = app_instance.auth_label.cget("text")
    assert "42" in text
    assert "o@e.com" in text


@responses.activate
def test_refresh_auth_label_401_clears_token(app_instance, app_mod, fake_keyring):
    app_mod.auth.set_token("STALE")
    responses.add(
        responses.GET,
        "https://server.example/v1/account",
        json={"error": "invalid"},
        status=401,
    )
    app_instance._refresh_auth_label()
    assert app_mod.auth.get_token() is None
    text = app_instance.auth_label.cget("text")
    assert "expired" in text.lower() or "sign in" in text.lower()


def test_refresh_auth_label_offline_shows_offline(app_instance, app_mod, fake_keyring, monkeypatch):
    app_mod.auth.set_token("TOK")

    def boom(*a, **kw):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(app_mod.requests, "get", boom)
    app_instance._refresh_auth_label()
    text = app_instance.auth_label.cget("text").lower()
    assert "offline" in text


def test_refresh_auth_label_not_signed_in(app_instance, app_mod):
    app_mod.auth.clear_token()
    app_instance._refresh_auth_label()
    text = app_instance.auth_label.cget("text").lower()
    assert "not signed in" in text
