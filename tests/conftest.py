"""Top-level conftest for the batcher (desktop client) test suite.

Key fixtures:

* `tmp_home`: redirects `~/.batcher` to a temp directory so we never
  trample real credentials on the host machine.
* `fake_keyring`: patches the in-process keyring backend with a dict-backed
  one. The batcher's auth module dynamically imports `keyring`, so we
  need to install a `keyring` module before any test imports `auth`.
* `clean_auth_module`: forces re-import of `auth` per test, picking up
  a fresh `_BATCHER_DIR` per `tmp_home`.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Fake keyring backend
# ---------------------------------------------------------------------------


class _DictKeyring:
    """In-memory keyring replacement, just enough for the batcher's auth helper."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, key):
        return self.store.get((service, key))

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def delete_password(self, service, key):
        self.store.pop((service, key), None)


@pytest.fixture
def fake_keyring(monkeypatch):
    """Install a fake `keyring` module into sys.modules."""
    fk = _DictKeyring()
    mod = types.ModuleType("keyring")

    mod.get_password = fk.get_password
    mod.set_password = fk.set_password
    mod.delete_password = fk.delete_password
    mod.get_keyring = lambda: fk

    monkeypatch.setitem(sys.modules, "keyring", mod)
    return fk


@pytest.fixture
def no_keyring(monkeypatch):
    """Pretend keyring is unavailable — _try_keyring returns None."""
    monkeypatch.setitem(sys.modules, "keyring", None)


# ---------------------------------------------------------------------------
# Home directory redirect
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Make ~/.batcher resolve into a temp directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # On macOS, Path.home() reads HOME by default. Ensure the auth module's
    # constants get rebuilt with the new HOME.
    yield tmp_path


@pytest.fixture
def auth_mod(tmp_home, monkeypatch):
    """Fresh import of `auth` with ~/.batcher pointing at tmp_home."""
    if "auth" in sys.modules:
        del sys.modules["auth"]
    import auth  # noqa: E402

    # Rebind module-level constants in case the module was already cached
    # with the previous HOME by another import chain.
    auth._BATCHER_DIR = tmp_home / ".batcher"
    auth._TOKEN_FILE = auth._BATCHER_DIR / "token"
    auth._ACCOUNT_ID_FILE = auth._BATCHER_DIR / "account_id"
    return auth


# ---------------------------------------------------------------------------
# Env scrubbing so process_batches doesn't read a real .env file
# ---------------------------------------------------------------------------


@pytest.fixture
def batcher_env(monkeypatch, tmp_path):
    """Set the Batcher env vars the process_batches module reads at import."""
    monkeypatch.setenv("BATCHER_API_URL", "https://server.example")
    monkeypatch.setenv("BATCHES_DIR", str(tmp_path / "batches"))
    monkeypatch.setenv("DROPBOX_BASE", str(tmp_path / "dropbox"))
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    monkeypatch.setenv("SENDER_EMAIL", "")
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", "")
    # load_dotenv never overrides a var that is already set, so blanking
    # these stops the operator's real .env switching tests into bypass or
    # legacy-tracking mode.
    monkeypatch.setenv("BATCHER_BYPASS_AUTH", "")
    monkeypatch.setenv("TRACKING_API_URL", "")
    monkeypatch.setenv("TRACKING_API_KEY", "")
    # Same for the Trustpilot toggle: blank means the module default ("bcc").
    monkeypatch.setenv("TRUSTPILOT_MODE", "")
    monkeypatch.setenv("TRUSTPILOT_REVIEW_URL", "")
    (tmp_path / "batches").mkdir(exist_ok=True)
    (tmp_path / "batches" / "processed").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def pb(batcher_env, tmp_home, monkeypatch):
    """Fresh import of process_batches with env scrubbed."""
    for name in ("process_batches",):
        sys.modules.pop(name, None)
    import process_batches  # noqa: E402

    # Re-bind config values so any module reads we did at import time use the
    # test paths (defensive — should already be set by env).
    process_batches.BATCHER_API_URL = "https://server.example"
    process_batches.BATCHES_DIR = batcher_env / "batches"
    process_batches.PROCESSED_DIR = batcher_env / "batches" / "processed"
    process_batches.DROPBOX_BASE = batcher_env / "dropbox"
    return process_batches
