# Batch Order Label Processor

## Overview
Processes Royal Mail Click & Drop shipping PDFs. Splits batch PDFs into individual stamp labels, files them in Dropbox customer folders, posts dispatch tracking to a Render-hosted web service, and emails customers their tracking info.

Architecture is split between a **local desktop client** (runs against the operator's files and SMTP) and a **Render-hosted web service** (multi-tenant auth + Stripe gate + dispatch ledger; will own tracking polling + claims).

## READ FIRST — what is actually live (corrected 2026-10-02)

**The Phase 0 server was never deployed.** Everything below that says Phase 0 is "live" or "delivered" describes code that exists only as uncommitted work in `id5data2` and is parked (George intends to host it in a separate web app later; see the note at the top of `id5data2/CLAUDE.md`). `/v1/dispatches`, `/v1/account` and `/v1/auth/token` return 404 on Render.

What runs today:
- The client records dispatches through the **legacy `POST /tracking`** endpoint. This mode is on whenever `TRACKING_API_URL` and `TRACKING_API_KEY` are set in `.env` (`post_tracking_record` → `_post_legacy_tracking_record`). `BATCHER_BYPASS_AUTH=true` stays set so the GUI does not ask for a sign-in.
- Rows land in `dispatch_tracking` in the original (Acuity) Supabase project. No accounts, credits or idempotency: re-processing an order inserts a duplicate row.
- The POST still happens before the Dropbox copy and the email, and a failed POST still skips both. `email_sent` is therefore always stored as `false`.
- No dispatches were recorded between 2026-05-27 and 2026-10-02 (the client was calling the undeployed API with bypass on). Backfilling that gap from `batches/processed/` is planned.
- **Trustpilot review request** is controlled by `TRUSTPILOT_MODE` in `.env` (`bcc` = BCC the automated-invite address, `link` = hyperlink to `TRUSTPILOT_REVIEW_URL` in the email body, `off` = neither). The GUI's Email menu switches it for the current session. George's `.env` is on `link` since 2026-10-19 because the automated invite allowance ran out.
- `dist/ID5 Batch Processor.app` was built on 2026-09-14 and does not contain this change; rebuild with `./build_app.sh` before using the bundle.

## Current State — Phase 0 written, NOT deployed (code dated 2026-05-19)

Phase 0 (multi-tenant foundation + Stripe gate) is described below as designed. The Render service now authenticates every dispatch, debits credits atomically, and charges via Stripe. The desktop client has been rewritten to talk to the new authenticated API.

### Local desktop client (`/Users/georgeboyd-gibbins/Projects/batcher/`)
- `process_batches.py` — CLI/library: PDF parsing, stamp extraction, POSTs to `/v1/dispatches` with `client_dispatch_id = sha256(account_id|order_number|tracking_number)`, Dropbox file ops, SMTP send. Retries once on Timeout/ConnectionError. Raises `OutOfCredits` (HTTP 402) and `AuthRequired` (HTTP 401). Exit codes: 2 = out of credits, 3 = auth required.
- `auth.py` — credential store. Keyring (macOS Keychain) primary, `~/.batcher/token` 0600 file fallback. Also caches `account_id`.
- `app.py` — Tkinter GUI. Account menu (Sign in / Sign out / Open billing). 402 modal opens top-up URL in browser. Balance label refreshed via `GET /v1/account`.
- `build_app.sh` / `setup.py` — Build a `.app` bundle for macOS.
- `batches/` — Drop incoming `orders-*.pdf` files here; processed PDFs move to `batches/processed/`.
- `.env` — Local config (SMTP, `BATCHER_API_URL`, Dropbox base path). No more `TRACKING_API_KEY` — the per-account token lives in the keyring.
- **Step order in `process_pdf`:** save stamp locally → read customer email → POST `/v1/dispatches` → copy stamp to Dropbox folder → send email. A failed POST skips Dropbox copy AND deletes the local stamp file (no orphan rows, no double-charge on rerun thanks to idempotency).
- 40 tests under `tests/`, GitHub Actions CI runs them under `xvfb-run`.

### Web service (`/Users/georgeboyd-gibbins/Projects/id5data2/`, hosted on Render)
- Flask 3.1.0 + gunicorn. `main3.py` is the app entrypoint; auth/account/billing/API routes live in blueprints. The same process also hosts the pre-existing Acuity / Credas booking flow — they share a dyno but nothing else.
- **Two Supabase projects in one app.** `supabase` (Acuity / Credas — pre-Phase-0) and `batcher_supabase` (Phase 0, separate project). All Batcher code paths use `batcher_supabase`. Schema for the Batcher project lives in `migrations/0000_initial_dispatch_tracking.sql` (base table) + `0001_phase0_multitenant.sql` (Phase 0 additions), applied by hand to the Batcher project only — see `migrations/README.md`.
- **Tables in the Batcher project:** `dispatch_tracking`, `accounts`, `api_tokens`, `credit_ledger`. **View:** `account_credit_balance` (derived `SUM(delta)` — balance is never stored).
- **`fn_record_dispatch` RPC** — atomic dispatch + debit in a single transaction. Uses `pg_advisory_xact_lock` and a payload-key whitelist.
- **API routes (blueprints):**
  - `POST /v1/dispatches` — Bearer-authed, idempotent, returns 201 / 200 (replay) / 402 (`insufficient_credits` with `top_up_url`) / 401.
  - `POST /v1/auth/token` — email + password → mint long-lived bearer (returned plaintext once; `sha256` stored).
  - `GET /v1/account` — `{id, email, plan_type, balance, subscription_status}`.
- **Web routes (blueprints):** `/signup` (gated by `SIGNUPS_OPEN` env, default `false`; optional `INVITE_CODE`), `/login`, `/logout`, `/account` dashboard, `/account/tokens` (mint/revoke), `/account/billing` (Stripe Customer Portal), `/account/checkout`, `/stripe/webhook`.
- **Legacy `/tracking` shim** — kept alive, mapped to the operator's personal account via `LEGACY_OPERATOR_ACCOUNT_ID`. Uses the same `client_dispatch_id` hash as the desktop client so the operator can never be double-charged on cutover.
- **Stripe:** 6 SKUs — three credit packs (`pack_50`, `pack_200`, `pack_1000`) + three monthly tiers (`monthly_100`, `monthly_250`, `monthly_500`). Webhook handles `checkout.session.completed`, `invoice.paid`, `customer.subscription.updated`, `customer.subscription.deleted`. Idempotent via unique constraint on `credit_ledger.stripe_event_id`. Returns 503 on customer-not-yet-linked race so Stripe retries.
- **Free trial:** `FREE_TRIAL_CREDITS` env var (default 50) — seeded as a `+N free_trial` ledger row on signup.
- 84 tests under `tests/` (unit + integration against real Postgres + e2e). GitHub Actions CI runs them and fires a Render deploy hook on push to main.

### Security posture
Phase 0 ships to a **small private beta only** (≤10 users). A security review surfaced gaps that were NOT fully fixed in Phase 0: CSRF protection, session hardening, plaintext token in cookie, account enumeration, no rate limiting, no request-size limits. Only the four critical findings were patched in Phase 0 — the rest will be addressed in Phase 1 alongside the framework migration (FastAPI middleware is the right place). `SIGNUPS_OPEN=false` is the default so the URL can't be discovered while billing is settling.

## PDF Structure
Each batch PDF contains pairs of pages per customer:
- **Odd pages (0, 2, 4...)**: Shipping address — customer name, order number, despatch date
- **Even pages (1, 3, 5...)**: Royal Mail stamp/label — tracking number, service type (Tracked 24/48, Special Delivery)

## Tech
- Python 3, pdfplumber (text extraction), pypdf (page splitting), Tkinter (GUI), requests, keyring
- Server: Flask 3, gunicorn, Stripe SDK, supabase-py
- Supabase (auth + Postgres), SMTP via Stackmail (customer emails)

## Running locally

### Client
```bash
source .venv/bin/activate
python process_batches.py     # CLI (exits 2 if out of credits, 3 if auth required)
python app.py                 # GUI — Account menu has Sign in / Sign out / Open billing
```

First run: the GUI prompts for sign-in (email + password) and stores the long-lived token in the OS keyring. The CLI assumes a token is already present — sign in via the GUI once, or call `POST /v1/auth/token` by hand.

Client env vars (see `.env` template — minimal: `BATCHER_API_URL` defaults to the live service, `SMTP_*`, `DROPBOX_BASE`, `BATCHES_DIR`). `TRACKING_API_KEY` is gone.

### Server
See `/Users/georgeboyd-gibbins/Projects/id5data2/.env.example` for the full set. Phase 0 added: `FLASK_SECRET_KEY` (≥32 chars), `BATCHER_SUPABASE_URL`, `BATCHER_SUPABASE_KEY` (separate Supabase project from the Acuity one), `FREE_TRIAL_CREDITS`, `PUBLIC_BASE_URL`, `SIGNUPS_OPEN`, `INVITE_CODE` (optional), `LEGACY_OPERATOR_ACCOUNT_ID`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PACK_{50,200,1000}`, `STRIPE_PRICE_MONTHLY_{100,250,500}`. Stripe dashboard setup in `STRIPE_SETUP.md`.

## Testing
- Client: see `/Users/georgeboyd-gibbins/Projects/batcher/TESTING.md`.
- Server: see `/Users/georgeboyd-gibbins/Projects/id5data2/TESTING.md`.

Dependencies in `requirements.txt`, venv at `.venv/`.

---

## Direction (target product)

Batcher is being grown from a single-operator internal tool into a **distributable product** for small UK businesses that ship via Royal Mail Click & Drop. The desktop client stays a thin client; the server owns everything billable.

### Functional roadmap
1. **Tracking polling** — server periodically calls Royal Mail (or aggregator) tracking API for every active dispatch and writes status history to Supabase.
2. **Status notifications** — customers get emailed on dispatch (already works) **and** on meaningful status changes (out for delivery, delivered, delivery attempted, etc.).
3. **Non-delivery detection** — server tracks UK working days and flags Tracked 24 / Tracked 48 dispatches that pass the 10-working-day non-delivery threshold.
4. **Compensation claims** — for overdue dispatches the server auto-prepares a Royal Mail compensation claim packet and either files it or queues it for operator review (legal/T&C dependent).

### Distribution + monetisation
- **Target user: non-technical.** No terminals, no `.env` editing. Double-click installer; simple config UI; license key for activation.
- **Multi-tenant web service** on Render. Each operator has an account, a license key, and a credit balance. *(Phase 0 done.)*
- **Hard server gate.** The desktop client cannot dispatch without a successful authenticated call to the server (which decrements credits). No server → no emails sent, no claims filed. *(Phase 0 done.)*
- **Pricing model:** free trial (first N dispatches via `FREE_TRIAL_CREDITS`), then credit packs or monthly subscription. Stripe. *(Phase 0 done.)*
- **Local-only bits the user keeps:** PDF watcher, file-system access (Dropbox folders, SMTP creds for *their* email sending).
- **Server-only bits (gated):** dispatch authorisation *(done)*, tracking polling, status notifications, claim preparation/filing, dashboard.

### Parked work
- Build/distribution polish for non-technical users (signed installer, config UI in place of `.env`, friendly errors). Resume after the server-side capabilities (tracking, notifications, claims, billing) are in place — no point shipping pretty installers for a product whose business model isn't wired yet.
- Webapp-vs-desktop pivot question. Still open. The Phase 1 framework migration (FastAPI on a Render Pro host) makes a future pivot easier but does not resolve it.
