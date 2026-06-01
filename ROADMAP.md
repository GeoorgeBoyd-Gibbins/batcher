# Batcher Roadmap

Phased architectural plan for growing Batcher from a single-operator internal tool into a distributable product for small UK businesses shipping via Royal Mail Click & Drop.

Delivery order is driven by one principle: **money first, then features that justify the price.** Billing infrastructure lands early so every later capability is automatically gated. Tracking is the central feature, then notifications, then claims (the unique value-prop). Installer polish is last.

The current Render service (`id5data2`) hosts Phase 0. **Phase 1 migrates Batcher routes off it** onto an existing FastAPI app on a Render Pro plan (the host that already serves clive-ai.co.uk and, soon, diy-conveyancing.co.uk). From Phase 1 onward, all new server work lives in that FastAPI app under a `/batcher/*` path prefix. `id5data2` stays alive for Acuity + the other non-Batcher work — it just stops owning the Batcher product.

---

## Security posture

Phase 0 ships to a **small private beta only** (≤10 users). A security review surfaced gaps — CSRF, session hardening, plaintext token in cookie, account enumeration, no rate limiting, no request-size limits — and only the four critical findings were patched in Phase 0. The remaining gaps are deliberately deferred to Phase 1, where the FastAPI migration is the right point to add proper middleware-level protection (CSRF, rate limiting via slowapi, request-size limits, secure session cookies, constant-time account-existence responses). `SIGNUPS_OPEN=false` is the Phase 0 default so the URL isn't discoverable while billing settles.

---

## Phase 0 — Multi-tenant foundation + Stripe gate — **DELIVERED 2026-05-19**

**As built.** Render service `id5data2` is now multi-tenant: Supabase Auth + per-account long-lived bearer tokens, atomic `fn_record_dispatch` RPC (advisory-locked, idempotent on `(account_id, client_dispatch_id)`), `credit_ledger` as source of truth with a derived `account_credit_balance` view, Stripe with 6 SKUs (3 credit packs + 3 monthly tiers) and an idempotent webhook keyed on `stripe_event_id`. Legacy `/tracking` survives as a single-tenant shim mapped to the operator's account via `LEGACY_OPERATOR_ACCOUNT_ID` — same `client_dispatch_id` formula as the new client so cutover can never double-charge. The desktop client now stores tokens in the OS keyring (0600 file fallback), uses `requests` against `/v1/dispatches`, retries once on transport errors, surfaces 402 with a top-up modal, and POSTs *before* sending the customer email. 84 server tests + 40 client tests, both on GitHub Actions CI. Free trial via `FREE_TRIAL_CREDITS` env var (default 50). Shipping to private beta only — see "Security posture" above.

**Original plan (preserved for reference).**

**Goal.** Turn the single-tenant Render service into a multi-tenant authenticated service that charges per dispatch, so every later feature is implicitly billable.

**Server work.**
- Supabase Auth (email + password, magic link). Already in stack; ships an account UI for free; gives row-level security. Don't introduce Clerk/Auth0.
- New tables:
  - `accounts` (id, owner_user_id, plan_type, stripe_customer_id, credit_balance, subscription_status, subscription_renews_at)
  - `dispatches` (existing — add `account_id`, `client_dispatch_id` for idempotency)
  - `credit_ledger` (account_id, delta, reason, dispatch_id nullable, stripe_event_id nullable, created_at)
  - `api_tokens` (account_id, token_hash, last_used_at, revoked_at)
- New endpoints:
  - `POST /v1/dispatches` (replaces current `/tracking`; authenticated, idempotent on `client_dispatch_id`, decrements credit or asserts subscription, returns 402 if no credit)
  - `POST /v1/auth/token` (exchange login for desktop token)
  - `GET /v1/account`
  - Stripe webhook (`checkout.session.completed`, `invoice.paid`, `customer.subscription.updated`)
- Stripe: two products — `credit_pack_*` (one-off, metadata `credits=N`) and `monthly_subscription` (recurring, metadata `monthly_credits=N` or unlimited flag). Webhook applies credits via `credit_ledger`. Use `stripe_event_id` as a unique key — that's your idempotency for billing.
- Public marketing/account page (single Next.js or Flask-rendered page on the same Render service): sign up, buy credits, manage subscription via Stripe Customer Portal.

**Client work.**
- Replace `TRACKING_API_KEY` env var with a per-account token obtained via a one-time login dialog. Persist in OS keychain (macOS Keychain; Windows Credential Manager later).
- Send `client_dispatch_id = sha256(account_id + order_number + tracking_number)` so retries don't double-charge.
- Surface `402 Payment Required` cleanly: GUI banner "Out of credits — top up at [url]". CLI exits non-zero with same message.

**External dependencies.** Stripe account + products + webhook secret. Domain + TLS for the account UI (Render handles this).

**Key decisions + trade-offs.**
- **Hard gate while offline.** The client *must* call the server before sending. If offline → operator can't dispatch. This is acceptable: dispatching requires email anyway. Don't try to design an offline grace mode — it leaks credits and complicates support.
- **Idempotency.** Charging credit must be inside the same transaction as inserting the dispatch row, keyed on `client_dispatch_id`. If the client retries, it gets the cached response, not a second charge.
- **Auth model.** Supabase Auth for humans on the web; long-lived bearer tokens for the desktop. Don't try to make the desktop client use OAuth flows — too painful for a Tkinter app.
- **Subscription vs credits.** Treat subscriptions as "credits topped up to N monthly + unlimited dispatch if `unlimited=true`". Single code path through `credit_ledger`.

**Deferrable.** Free trial logic (just hand-grant credits to early users). Per-seat licensing. Refunds (Stripe Portal handles it).

---

## Phase 1 — Framework + host migration, then tracking polling

Phase 1 has two halves. The migration lands first because the tracking poller wants async, the security gaps want middleware, and the current Render Free dyno's cold starts will hurt a background poller. The poller itself is the original Phase 1 goal.

### Phase 1a — Migrate Batcher off `id5data2` onto the FastAPI host

**Goal.** Move Batcher's server-side code out of the Flask `id5data2` app into the existing FastAPI app on Render Pro that already serves clive-ai.co.uk (and will serve diy-conveyancing.co.uk). Batcher routes live under a `/batcher/*` path prefix.

**Why now.**
- **Render Pro = no cold starts.** Phase 1b's poller can't tolerate a sleeping dyno.
- **Native async** for the poller and concurrent provider fetches.
- **Pydantic + OpenAPI** for free request validation and a usable API contract for any future web/mobile client.
- **Middleware story** for the deferred security work (CSRF, rate limiting, request-size limits) — fits naturally onto FastAPI/Starlette middleware.
- The `id5data2` Flask app stays alive for Acuity + non-Batcher work; it just stops owning Batcher.

**Work.**
- Port the four blueprints (`web/auth_routes`, `web/account_routes`, `web/billing_routes`, `api_v1`) + `stripe_webhook` to FastAPI routers under `/batcher/*`. Templates → Jinja2 via `fastapi.templating`. Sessions → `starlette.middleware.sessions` with hardened cookie flags.
- Rewrite `fn_record_dispatch` invocation via `asyncpg` or `supabase-py` async client.
- Add middleware for the deferred Phase 0 security gaps (CSRF for browser-session routes, slowapi rate limiting, request-size limits, secure session cookies, constant-time account-existence).
- **New Supabase project** for Batcher (clean schema isolation from Clive's data). Re-apply `0001_phase0_multitenant.sql` and any subsequent Phase 1 migrations.
- DNS: point the Batcher account UI at a subdomain of the FastAPI host (e.g. `app.batcher.<tld>`).
- Update desktop client default `BATCHER_API_URL` to the new host; ship a client update that prompts users to re-sign-in if the old URL 404s.
- Retire Batcher code from `id5data2` once cutover is verified (keep the legacy `/tracking` shim there if any beta user is still on the un-updated client; otherwise delete).

**Beta data migration — decision deferred to Phase 1 kickoff.** Phase 0 beta is expected to be ≤10 users. Options:
- (a) Hand-migrate `accounts`, `api_tokens`, `credit_ledger`, `dispatch_tracking` rows via SQL export/import. Stripe `customer_id` references survive unchanged.
- (b) Ask beta users to re-sign-up and grant them compensation credits.

Pick (a) if the beta has paying customers by then; (b) if it's still operator + 1–2 testers.

**Cost.** ~3 days including the Supabase project move.

**Verification.** All 84 server tests pass after porting (rewrite the ones bound to Flask test-client). End-to-end: signup → mint token → dispatch on the new host; Stripe webhook idempotency holds; legacy `/tracking` either still works on `id5data2` or has been retired with no client breakage.

### Phase 1b — Tracking polling with swappable provider

**Goal.** Server periodically fetches Royal Mail tracking status for every active dispatch and writes a status history per dispatch.

**Server work.**
- New tables:
  - `tracking_events` (dispatch_id, raw_status, normalised_status enum, event_time, location, source_provider, fetched_at, raw_payload jsonb)
  - `tracking_poll_state` (dispatch_id, next_poll_at, poll_attempts, terminal bool)
- **Provider abstraction.** Single interface `TrackingProvider.fetch(tracking_number, service_type) -> NormalisedStatus[]`. Two implementations: `RoyalMailApiProvider` and `RoyalMailScraperProvider`. Selected by config (`TRACKING_PROVIDER=api|scrape`).
- **Status normalisation.** Enum: `pre_transit`, `in_transit`, `out_for_delivery`, `delivered`, `attempted`, `held`, `returning`, `exception`. Provider returns normalised + raw. All downstream logic uses normalised.
- **Polling scheduler.** Background worker on Render. Polls based on `next_poll_at`. Backoff: every 2h while `in_transit`, every 30min while `out_for_delivery`, terminal on `delivered` (poll once 24h later to confirm), every 6h while `exception` for 5 days then stop.
- `GET /v1/dispatches/:id/tracking` (auth-gated, account-scoped) for the future dashboard.

**Client work.** None. (Optionally: surface "view tracking" link in GUI pointing at dashboard URL.)

**External dependencies.**
- Royal Mail Shipping API access (pending).
- If scraping: Playwright + headless Chromium. On Render, this needs the **Docker runtime**. Memory: a single Chromium tab is ~250–400 MB; default Render plan is 512 MB — too tight. Plan on **Render Standard (2 GB)** or move the scraper to **Browserless.io / Browserbase / Scrapingbee** as a managed Playwright endpoint. Strongly recommend the managed option.

**Key decisions + trade-offs.**
- **Worker topology.** (a) Render Cron Jobs hitting an internal endpoint every N minutes (simplest, fine up to ~1000 dispatches); (b) dedicated background worker dyno. **Start with (a).**
- **Scraper isolation.** If self-hosting Playwright, run scraper as a *separate* worker dyno from the API.
- **Rate limiting.** Cap concurrent fetches (e.g. 2). Randomise jitter.
- **Storage.** Store raw payload as `jsonb` for replay. When the provider changes (API granted later), you can replay historical raw data to backfill normalised events.

**Deferrable.** Webhook-from-RM (they don't offer this). Per-account custom poll cadences. Full dashboard UI — endpoint returning JSON is enough.

---

## Phase 2 — Customer status notifications

**Goal.** Customers receive emails on meaningful tracking transitions, not just dispatch.

**Server work.**
- New table: `notifications` (dispatch_id, kind enum, sent_at, channel, to_address, status). Unique constraint `(dispatch_id, kind)` prevents duplicate sends.
- Notification engine triggered when a `tracking_event` is inserted: if normalised status crosses a threshold (`out_for_delivery`, `delivered`, `attempted`, `exception`) and no row in `notifications` for that `(dispatch_id, kind)`, queue a send.
- **Email sending: server-side via operator's SMTP.** Two options:
  - (a) **Server-relayed (recommended):** operator stores SMTP creds in the account (encrypted at rest via Supabase Vault or a `pgcrypto` column with a server-held key) and the server sends as them. Server-side notifications can't depend on the client being running.
  - (b) Client-pulled: client polls server for "things to send" when it's open. Fragile — defeats the purpose.
- `POST /v1/account/smtp` (account-scoped, stores encrypted creds).
- Template system: per-account customisable HTML templates with merge fields (`{{tracking_number}}`, `{{customer_name}}`, `{{service_type}}`).

**Client work.**
- Config UI gains an SMTP section that uploads creds to the server.
- **Recommended simplification:** have *all* customer email come from the server post-Phase 2, including the initial dispatch. The desktop client only handles its own files + PDF, never SMTP.

**External dependencies.** None new. Optional: transactional provider like Postmark as a fallback when operator SMTP fails.

**Key decisions + trade-offs.**
- **Encryption of SMTP creds.** Per-account symmetric key encrypted with a master key in Render env. Acceptable for this product's threat model.
- **Send timing.** Send `out_for_delivery` immediately. Hold `delivered` for 10 min before sending to absorb scanner flutter (delivered → not_delivered → delivered seen at RM scanners).
- **De-duplication.** Unique constraint on `(dispatch_id, kind)` is the safety net.

**Deferrable.** SMS. Per-recipient opt-out. Template editor UI.

---

## Phase 3 — Non-delivery detection

**Goal.** Server flags Tracked 24/48 dispatches that have passed 10 UK working days without delivery, so claims can be raised.

**Server work.**
- `uk_bank_holidays` table — seed from `gov.uk/bank-holidays.json` at startup, refresh weekly.
- Pure function `working_days_between(start, end, holidays)`.
- Daily job (Render Cron, 09:00 UK time): for each dispatch where `service_type IN ('Tracked 24', 'Tracked 48')` AND not delivered AND `working_days_since(despatch_date) >= 10` AND not already flagged → insert into `claim_candidates` (dispatch_id, eligible_at, reason, status='pending_review').
- Daily digest email to operator of newly-eligible dispatches.

**Client work.** None.

**External dependencies.** `gov.uk/bank-holidays.json` (free, no auth).

**Key decisions + trade-offs.**
- **Timezone.** `Europe/London` for working-day arithmetic; store everything as UTC.
- **Threshold.** Hard-code 10 working days but put it in a `claim_rules` config table so it can be tweaked without a deploy.

**Deferrable.** Special Delivery rules (different timing). Loss vs delay distinction.

---

## Phase 4 — Automated compensation claims

**Goal.** For each eligible claim candidate, server prepares a complete claim packet and either submits it or queues it for one-click operator approval.

**Server work.**
- New tables:
  - `claims` (candidate_id, status enum [`drafted`, `awaiting_approval`, `submitted`, `accepted`, `rejected`, `paid`], packet_url, submitted_at, rm_reference, payout_amount)
  - `claim_documents` (claim_id, kind, storage_url)
- **Packet builder.** Cover letter PDF, proof of postage (the stamp PDF — needs to be uploaded to the server during dispatch), tracking history printout, customer's order value (operator-provided).
- **Submission path — pick ONE first.** Ranked:
  1. **Email to RM claims address with PDF attachments.** Simplest. Trackable. Recommend for v1.
  2. **Web form submission via Playwright.** Brittle — RM redesigns forms.
  3. **RM Business Account claims API.** Only if available.
- **Approval workflow.** Default `awaiting_approval` (sits in dashboard, operator clicks "submit"). Per-account `auto_submit_claims=true` flips to auto-fire. **Default off** — let operators trust the system before automating.
- `GET /v1/claims`, `POST /v1/claims/:id/approve`, `POST /v1/claims/:id/reject`.

**Client work.**
- Dispatch payload gains optional `order_value_gbp` field.
- Stamp PDF gets uploaded to the server at dispatch time (currently only in Dropbox). `POST /v1/dispatches/:id/stamp` multipart endpoint. Storage: Supabase Storage bucket.

**External dependencies.**
- Royal Mail claims process documentation (operator needs to confirm current submission channel + required documents).
- PDF generation library (reportlab or weasyprint) on the server.
- Supabase Storage bucket for stamps + generated packets.

**Key decisions + trade-offs.**
- **Legal risk.** Auto-filing claims in the operator's name is real exposure. Default to operator approval. Make `auto_submit` a deliberate opt-in with terms acceptance.
- **Stamp PDF availability.** Phase 4 *requires* upload-at-dispatch. Worth front-loading into Phase 0.
- **Outcome tracking.** RM responses come by email. v1 can be manual: operator clicks "this claim was paid £X" in the dashboard. v2: parse RM reply emails (IMAP) for outcomes.

**Deferrable.** Auto-parsing RM responses. Multi-currency. Chasing letters after N days of no RM response.

---

## Phase 5 — Non-technical distribution

**Goal.** A small UK business owner can install Batcher, sign up, buy credits, and run a batch without ever opening a terminal.

**Server work.** None new.

**Client work.**
- Installers: macOS `.dmg`; Windows `.exe` via `pyinstaller` or `briefcase`. **Code-signed** on both platforms.
- First-run setup wizard (replaces `.env`): login → SMTP setup → folder location → test send.
- Auto-update: simplest viable is "check server for latest version, prompt user, link to download".
- License activation = login. No license keys needed if Supabase Auth is the gate.

**External dependencies.** Apple Developer account (£79/year). Windows code-signing cert (~£200/year). Hosting for download artifacts (Render static or a GitHub Release).

**Key decisions + trade-offs.**
- **No license keys.** Account login is sufficient identity.
- **Distribution channel.** Direct download from marketing site. No Mac App Store / MS Store — sandbox restrictions kill the file watcher.
- **Windows port effort.** Tkinter is cross-platform; Dropbox path and Keychain wrapper are the only macOS-specific pieces. Budget two weeks.

**Deferrable.** Linux build. In-app credit purchase (link out to Stripe Portal).

> **Note — partially absorbed by Phase 1a.** Some of Phase 5's auth-UX polish (browser-side login flows, account-management UI) gets a free leg-up from the FastAPI migration in Phase 1a, which is web-shaped by nature. The desktop installer + code-signing work is unaffected and still belongs here. Whether Phase 5 happens at all is still tied to the open webapp-vs-desktop question below.

---

## Dependency Graph

```
Phase 0 (Auth + Stripe + multi-tenant)
   │
   ├──> Phase 1 (Tracking polling)
   │       │
   │       ├──> Phase 2 (Status notifications)
   │       │
   │       └──> Phase 3 (Non-delivery detection)
   │                │
   │                └──> Phase 4 (Claims) — requires stamp-PDF upload from Phase 0/1
   │
   └──> Phase 5 (Distribution polish) — should not start until Phase 4 is live
```

Phases 2 and 3 are independent and can run in parallel after Phase 1.

---

## Top 3 Risks

1. **Royal Mail tracking access is denied AND scraping is unreliable.** Mitigation: prove the Playwright scraper end-to-end as part of Phase 1 *before* building notifications on top of it. Run it for a week against real dispatches and measure failure rate. Use a managed Playwright host (Browserless) so the variable is RM's site, not infra. Build a third fallback: a tracking aggregator (ParcelTrack, AfterShip) — quote them now.

2. **Auto-filed claims cause legal/operator trust blow-up.** Mitigation: ship Phase 4 with `auto_submit=false` default and a clear T&C acceptance. First ten claims for every new account go through manual approval regardless of setting. Treat the dashboard approval flow as the product, not the automation.

3. **Credit/billing race conditions or refund mishandling.** Mitigation: dispatch→credit decrement as a single Postgres transaction keyed on `client_dispatch_id`, with `credit_ledger` as the source of truth (never trust `accounts.credit_balance` for authorisation — compute from ledger). Build `/v1/account/ledger` from day one so refund disputes are auditable.

---

## Open Questions (architecture-changing)

1. **Royal Mail API outcome.** Currently on hold with Royal Mail. If denied, Phase 1's worker plan changes (Docker on Render or external Playwright host). Write the scraper provider in parallel either way.
2. **Server-side SMTP relay vs client SMTP.** Willing to store operators' SMTP creds on the server (encrypted)? If no, Phase 2 needs a different design (shared transactional provider like Postmark with DNS-verified sender addresses). Major fork.
3. **Claim submission channel.** Does RM accept compensation claims via email with PDF attachments, or only via the web form? Latter forces Playwright into Phase 4 even if Phase 1 is API.
4. **Free-trial mechanics.** "First N free dispatches per account" or "N days unlimited"? Affects credit-ledger seeding on signup.
5. **Stamp PDF storage at dispatch time.** Can the client start uploading the stamp PDF to the server from Phase 0? If yes, Phase 4 unblocks much faster.

---

## Critical Files

- `/Users/georgeboyd-gibbins/Projects/batcher/process_batches.py` — single dispatch entry point; needs `client_dispatch_id`, account token, server-uploaded stamp PDF, 402 handling
- `/Users/georgeboyd-gibbins/Projects/batcher/app.py` — GUI; needs login dialog, credit balance display, error surfacing, and (Phase 5) the config wizard
- `id5data2` repo: the dispatch endpoint handler (currently `/tracking`) — becomes the gate where Stripe + idempotency + multi-tenancy converge
- `id5data2` repo: a new `providers/` module for `TrackingProvider` (API + scraper implementations) — the linchpin of Phases 1–4
- `id5data2` repo: Supabase schema/migrations file — `accounts`, `credit_ledger`, `tracking_events`, `claim_candidates`, `claims` all need to land coherently; build it as a single migrations directory from Phase 0 onward

---

## Open architectural question: webapp vs desktop client

*Raised 2026-05-19. Still not resolved as of the Phase 0 ship. Phase 1a's move onto a FastAPI host makes a future pivot easier (server is already web-framework-shaped, async-capable, with OpenAPI for any future frontend) but does not itself answer the question. Re-decide before Phase 5.*

The roadmap above assumes the client stays a Tkinter desktop app. An alternative is to make the client itself a **webapp** — operators log in to a browser dashboard and upload PDFs there. Trade-offs to evaluate before locking in:

**What currently keeps the client local**
- Watches `batches/` for incoming `orders-*.pdf` files
- Files split PDFs into Dropbox customer folders (filesystem path)
- Holds operator's SMTP creds (Phase 2 moves these server-side anyway)

**What a webapp gains**
- **Phase 5 mostly evaporates.** No installer, no code-signing certs, no Windows port, no auto-update, no Keychain wrapper. The biggest deferred work item disappears.
- Works on any device with a browser — iPad, phone, Linux. No platform-specific bugs.
- Single deploy = no client/server version skew, no "user is on an old build" support load.
- The hard server gate becomes trivial — no client to circumvent or jailbreak.
- One auth path (Supabase Auth in the browser) instead of two (browser + desktop token exchange).
- Disaster recovery: all operator state is centralised.

**What a webapp loses**
- The folder-watcher workflow. Operator must drag-drop PDFs into the browser per batch instead of "dump in folder and forget." Mitigable: support a small CLI uploader or an email-in address for power users.
- Direct filesystem access to Dropbox. Replaced by Dropbox OAuth + API push from the server — arguably *cleaner* (no more "what's your local Dropbox path?" config).
- Some operators may prefer "my files stay on my machine." Mostly perception — dispatch records + customer emails already leave their machine today.

**New work a webapp requires**
- Server-side PDF parsing (lift from `process_batches.py` — already pure Python, easy port).
- File upload endpoint with size/MIME validation.
- Dropbox OAuth flow per account (replaces local filesystem write).
- A real frontend (React/Next or htmx + Flask templates).

**Initial recommendation.** Webapp looks like the better answer. The local-only pieces are weak (a folder watcher and a filesystem path), Phase 5 effort is large and largely eliminated, and Phase 4 already requires the stamp PDFs to live on the server. The main thing to check is whether operators currently rely on the folder-watcher workflow in a way that drag-drop would regress.

If we pivot to webapp, the roadmap collapses to:
- **Phase 0 (Auth + Stripe + multi-tenant)** — same, plus a basic web upload UI replacing the desktop dispatch flow
- **Phase 1–4** — unchanged
- **Phase 5** — replaced by web UI polish + Dropbox OAuth integration (much smaller than installer work)
