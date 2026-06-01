"""Unit tests for batcher.auth — credential storage with keyring + file fallback."""

from __future__ import annotations

import os
import stat

import pytest

# ---------------------------------------------------------------------------
# Keyring path
# ---------------------------------------------------------------------------


def test_set_and_get_token_via_keyring(auth_mod, fake_keyring):
    auth_mod.set_token("hunter2")
    assert fake_keyring.store[("batcher", "api_token")] == "hunter2"
    assert auth_mod.get_token() == "hunter2"


def test_clear_token_removes_keyring_entry(auth_mod, fake_keyring):
    auth_mod.set_token("hunter2")
    auth_mod.clear_token()
    assert fake_keyring.store == {}
    assert auth_mod.get_token() is None


def test_set_token_clears_any_existing_file_fallback(auth_mod, fake_keyring, tmp_home):
    # Pre-create the file fallback as if we had previously written it
    auth_mod._BATCHER_DIR.mkdir(parents=True, exist_ok=True)
    auth_mod._TOKEN_FILE.write_text("stale-token")
    os.chmod(auth_mod._TOKEN_FILE, 0o600)

    auth_mod.set_token("new-token")
    assert not auth_mod._TOKEN_FILE.exists(), "stale file copy should be cleared after keyring write"
    assert auth_mod.get_token() == "new-token"


# ---------------------------------------------------------------------------
# File fallback
# ---------------------------------------------------------------------------


def test_set_token_writes_0600_file_when_keyring_unavailable(auth_mod, no_keyring, tmp_home):
    auth_mod.set_token("filetoken")
    assert auth_mod._TOKEN_FILE.exists()
    mode = stat.S_IMODE(auth_mod._TOKEN_FILE.stat().st_mode)
    assert mode == 0o600
    assert auth_mod.get_token() == "filetoken"


def test_get_token_refuses_permissive_file(auth_mod, no_keyring, tmp_home):
    auth_mod._BATCHER_DIR.mkdir(parents=True, exist_ok=True)
    auth_mod._TOKEN_FILE.write_text("oops")
    os.chmod(auth_mod._TOKEN_FILE, 0o644)
    with pytest.raises(PermissionError) as excinfo:
        auth_mod.get_token()
    assert "0o644" in str(excinfo.value)


def test_clear_token_removes_file_copy(auth_mod, no_keyring, tmp_home):
    auth_mod.set_token("filetoken")
    auth_mod.clear_token()
    assert not auth_mod._TOKEN_FILE.exists()


def test_get_token_returns_none_when_nothing_stored(auth_mod, no_keyring, tmp_home):
    assert auth_mod.get_token() is None


# ---------------------------------------------------------------------------
# account_id
# ---------------------------------------------------------------------------


def test_set_and_get_account_id(auth_mod, tmp_home):
    auth_mod.set_account_id("acc-123")
    assert auth_mod.get_account_id() == "acc-123"
    mode = stat.S_IMODE(auth_mod._ACCOUNT_ID_FILE.stat().st_mode)
    assert mode == 0o600


def test_clear_token_also_clears_account_id(auth_mod, fake_keyring, tmp_home):
    auth_mod.set_token("t")
    auth_mod.set_account_id("acc-1")
    auth_mod.clear_token()
    assert auth_mod.get_account_id() is None
