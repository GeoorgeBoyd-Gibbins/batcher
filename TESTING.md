# Testing — batcher desktop client

## Layout

```
tests/
├── conftest.py            # Fake keyring, ~/.batcher tmp redirect, env scrub
├── unit/
│   ├── test_auth.py             # Keyring + file-fallback credential store
│   ├── test_process_batches.py  # POST flow, retries, step ordering, main()
│   └── test_app.py              # Tk smoke + auth dialogs + balance refresh
└── e2e/
    └── test_dispatch_flow.py    # End-to-end runs against a mocked server
```

## Running locally

```bash
source .venv/bin/activate          # or use .venv/bin/python directly
pip install -r requirements.txt -r requirements-dev.txt

# Everything
pytest

# Without the Tk smoke tests (e.g. headless box without python3-tk)
pytest -k "not test_app"

# Only E2E
pytest -m e2e

# Coverage
pytest --cov
```

## What gets mocked

- **HTTP**: `responses` library patches `requests` at the `urllib3` layer
  so the real server is never reached.
- **Keyring**: a small `_DictKeyring` is installed into `sys.modules` as
  `keyring`, so `auth.set_token` / `get_token` round-trip without
  touching the macOS Keychain.
- **`~/.batcher`**: the `tmp_home` fixture redirects `HOME` to a temp
  directory for each test, so file-fallback paths are isolated.
- **PDF parsing**: `pdfplumber.open` is replaced with a tiny fake that
  yields the shipping + stamp page text inline.
- **SMTP/IMAP**: never invoked — `SMTP_HOST` is set to empty in the
  test env so `process_pdf` skips the email send path.

## What the tests cover

### `auth.py`
- Keyring round-trip + fallback to `~/.batcher/token` at 0600.
- Refuses to read a file with permissions `0o644` or wider.
- `clear_token` removes both keyring + file copies + account_id.

### `process_batches.py`
- `_client_dispatch_id` is deterministic and matches the server-side
  legacy shim formula.
- `post_tracking_record`: 201, 200 replay, 402 → `OutOfCredits`, 401 →
  clears token + `AuthRequired`, 500 → False with warning, two cold-
  start retries, double timeout → False.
- `process_pdf` step ordering: save_stamp → POST → Dropbox copy. A
  failed POST skips the Dropbox copy AND deletes the local stamp file.
- `main()` exits 2 on OutOfCredits, exits 3 on AuthRequired.

### `app.py`
- Module imports under the test env.
- `_prompt_login`: success stores token + account_id; cancel makes no
  HTTP call; 401 shows the error box and stores nothing.
- `_refresh_auth_label`: balance shown on 200; token cleared on 401;
  "offline" label on transport errors.

### E2E flow
- Full happy path: signed-in client + 201 → stamp copied to the
  customer's Dropbox folder, request body matches expectations.
- 402 → `sys.exit(2)` and the PDF stays in place.
- 401 → token cleared, `sys.exit(3)`.
- 200 replay treated as success.

## CI

`.github/workflows/ci.yml` runs the tests under `xvfb-run` so Tk
widget construction works in headless Linux. `py2app` is filtered out
of `requirements.txt` (mac-only) before installation.
