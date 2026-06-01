"""Credential storage for the Batcher desktop client.

The bearer token is stored in the OS keyring (macOS Keychain, Windows
Credential Manager, Linux Secret Service). If keyring is unavailable —
which happens on headless macOS sessions and some CI environments — we
fall back to a 0600 file at ~/.batcher/token.

The account_id is cached separately at ~/.batcher/account_id. It is not
secret, just a value we want to avoid round-tripping to the server on
every dispatch to compute the client_dispatch_id.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

_BATCHER_DIR = Path.home() / ".batcher"
_TOKEN_FILE = _BATCHER_DIR / "token"
_ACCOUNT_ID_FILE = _BATCHER_DIR / "account_id"

_KEYRING_SERVICE = "batcher"
_KEYRING_KEY = "api_token"


def _try_keyring():
    """Return the keyring module if available and functional, else None."""
    try:
        import keyring  # type: ignore
        # Trigger backend discovery; raises on misconfigured keyring.
        keyring.get_keyring()
        return keyring
    except Exception:
        return None


def _ensure_dir() -> None:
    _BATCHER_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_BATCHER_DIR, 0o700)


def _write_secret_file(path: Path, value: str) -> None:
    _ensure_dir()
    path.write_text(value)
    os.chmod(path, 0o600)


def _read_secret_file(path: Path) -> str | None:
    if not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    # Refuse to read if file permissions are too permissive — protects against
    # an admin tool accidentally creating the file world-readable.
    if mode & 0o077:
        raise PermissionError(
            f"{path} has insecure permissions {oct(mode)}; expected 0600. "
            f"Delete the file and log in again."
        )
    return path.read_text().strip() or None


def get_token() -> str | None:
    keyring = _try_keyring()
    if keyring is not None:
        try:
            value = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
            if value:
                return value
        except Exception:
            pass
    return _read_secret_file(_TOKEN_FILE)


def set_token(plaintext: str) -> None:
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, plaintext)
            # Best-effort: clear any old file fallback so we don't carry two copies.
            if _TOKEN_FILE.exists():
                _TOKEN_FILE.unlink()
            return
        except Exception:
            pass
    _write_secret_file(_TOKEN_FILE, plaintext)


def clear_token() -> None:
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_KEY)
        except Exception:
            pass
    if _TOKEN_FILE.exists():
        _TOKEN_FILE.unlink()
    if _ACCOUNT_ID_FILE.exists():
        _ACCOUNT_ID_FILE.unlink()


def get_account_id() -> str | None:
    if not _ACCOUNT_ID_FILE.exists():
        return None
    return _ACCOUNT_ID_FILE.read_text().strip() or None


def set_account_id(account_id: str) -> None:
    _ensure_dir()
    _ACCOUNT_ID_FILE.write_text(account_id)
    os.chmod(_ACCOUNT_ID_FILE, 0o600)
