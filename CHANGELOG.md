# Changelog

Notable changes to the Loyalty QR API. Dates are when the change went live on
production. Schema changes are additive (`_MIGRATIONS`), applied by `migrate()`
on startup — no reseed, existing data preserved.

## Unreleased (existing-QR import · print box codes only)

### Added
- **Import QR codes printed by a previous platform** (`POST /qr/codes/import`).
  The manufacturer is migrating off Genefied and their stickers are already in
  the market on stock sitting in godowns and on shelves, so reprinting is not an
  option — those codes have to keep working exactly as printed. Each CSV row
  becomes a `qr_codes` row under a synthetic batch per product
  (`qr_batches.source = 'imported'`), taking its frozen `points_per_code` from
  that product's catalog entry. Our own `token`/`manual_code` are still
  generated, so an imported code behaves like any other everywhere else. Panel:
  **Import existing QR** in the Products tab's overflow, which chunks the file
  500 rows at a time with a progress percentage — a legacy export can carry six
  figures of codes. New suite: `tests/test_legacy_qr_import.py`.
- **Legacy code matching** (`qr_codes.legacy_code`, `qr_codes.legacy_scratch`).
  The old sticker encodes a link to *that* platform's host
  (`http://gverify.me/?Brand-Qi7YhrsToYI0Dbew`), so a phone camera opens their
  site and never reaches us: these codes are scannable only through an in-app
  scanner passing the raw decoded string, or by typing the scratch code.
  `_find_code` resolves a canonical key — the token after the last hyphen —
  derived identically at import and at scan time, so the same sticker matches
  whether the scanner returns the URL with a raw space, `%20` or `+`, or just
  the token. Matching order is unchanged for our own codes: token, then
  `manual_code` (uppercased, dashes stripped — which would destroy a
  case-sensitive legacy token, hence the separate branches), then legacy.
  The `code` field on `POST /scan`, `POST /yourapp/scan` and
  `POST /yourapp/qr/lookup` now accepts 200 characters instead of 64: a payload
  URL's length is driven by the brand name embedded in it, and at 64 a brand of
  ordinary length failed validation before the code was ever looked up. This
  matters most on `/yourapp/scan`, which is the ONLY route by which a legacy
  code can be redeemed at all.
  Scan reversal (`GET /scans/lookup`, `POST /scans/reverse`) resolves legacy
  codes too — without it a wrongly-scanned legacy sticker could be credited but
  never undone, because the manufacturer types what the retailer reads out and
  never our generated `manual_code`.
- **Two guards worth knowing about.** The brand prefix embedded in the payload
  is constant across one export, so it is used as an import-time check: a row
  carrying a different one is another brand's file and is refused rather than
  imported, since it would credit the wrong manufacturer's retailers. And
  `legacy_scratch` is deliberately **not** unique — 6 digits is a million
  possibilities against the ~1.07 billion of our own `manual_code`, and
  `manual_code` is `UNIQUE` *globally*, so two tenants importing their own
  legacy history would collide; scratch codes are instead matched scoped to the
  scanning retailer's manufacturer.

### Fixed
- **A scanned payload URL now resolves for our OWN codes too.** Our printed QR
  encodes `{QR_BASE_URL}/{token}`, but `_find_code` only ever matched the bare
  token, so an in-app scanner forwarding the whole decoded string got a `404
  Invalid code`. That was pre-existing and latent; it becomes load-bearing with
  the legacy import, because forwarding the raw scan is exactly how a legacy
  code reaches us. Both formats now reduce to their final segment, so neither
  side has to parse the other's URL shape.
- **A typed code matching both a manual code and an imported scratch code is
  refused (409) instead of silently picking one.** Our manual-code alphabet
  excludes 0 and 1 but includes 2-9, so an all-digit manual code is possible and
  can equal a 6-digit legacy scratch code within one manufacturer. Whichever won
  would have redeemed a sticker still sitting on a shelf. Related: a typed code
  belonging to another tenant no longer masks this retailer's own scratch code.

### Added
- **Print-scope selector on the Generate QR modal** — *All codes* / *Box codes
  only* / *Item codes only*, defaulting to All, each showing its own count. A
  batch of 1,000+ items previously printed every child sticker alongside the box
  stickers, producing a PDF that was slow to build and unwieldy to handle when
  only the carton codes were wanted. The filter is applied at **print time**, not
  generation time, so child codes are still created, a box scan still credits
  every one of them, and a saved batch can be reprinted at a different scope
  later. The scope is applied inside `buildStickerPdf` at the point it already
  splits children from parents, which keeps the progress percentage correct for
  free, and it lands in the filename (`…-boxes.pdf`) so printing both scopes from
  one batch gives two distinct files. The saved-batches list carries its own
  scope select and hides the Print button on batches that have no box codes.
- **`boxes` on `GET /qr/batches`** — a correlated `COUNT(*)` of each batch's
  parent codes. `qr_batches` stores neither `items_per_box` nor a box count, so
  without it the saved-batches list could not tell which batches have box codes
  and would offer "Box codes only" on batches that have none. No schema change.
  New suite: `tests/test_qr_batch_boxes.py`.

## 2026-08-11 (`status` flag for YourApp · panel toolbar, exports & blank states)

### Added
- **`status` — the did-it-work boolean, `/yourapp/*` only.** Every response
  from `POST /yourapp/qr/lookup`, `POST /yourapp/scan` and
  `POST /yourapp/points` now carries `status`: `true` when the call worked,
  `false` when it did not, so YourApp can separate "no data / already used"
  from "the API failed" without reading the HTTP code. **HTTP codes are
  unchanged.** Successes are stamped by the endpoints; failures by four
  exception handlers (`_http_exc`, `_validation_exc`, `_ratelimit_exc`,
  `_unhandled_exc`), which delegate to FastAPI's own handlers and re-emit the
  body via `_stamp_false`, preserving the code and headers (`Retry-After` on a
  `429`). That covers errors raised before the endpoint body runs — bad
  `X-API-Key` `401`, integration off `503`, rate limit `429`, validation `422`
  — and unhandled crashes, which now answer
  `500 {"detail": "Internal server error", "status": false}` instead of a dead
  connection. Nothing outside `/yourapp/` is touched. New suite:
  `tests/test_yourapp_api_status.py`, which also asserts the flag never leaks
  into `/auth/login` or `POST /scan`.
- **Export CSV on every data tab** — Claims, Customers, Distributors, Gifts,
  Products and Redemptions, via the shared client-side `utils/csv.js`
  (RFC-4180 + BOM `Blob`; no dependency, no new endpoint). Claims walks all
  pages; Redemptions exports all three statuses.
- **Sample CSV downloads** (`utils/sampleCsv.js`) for the four importable
  lists, with headers copied from the server's own `_CSV_*_HEADERS` alias
  tuples, so a filled-in sample always resolves.
- **Shared `EmptyState`** for blank tabs (instead of a header-only table) and a
  shared `SearchBox` for every searchable tab.

### Changed
- **BREAKING for YourApp only — `status` → `qrStatus` on
  `POST /yourapp/qr/lookup`.** The code's own `available|redeemed` state moved
  to `qrStatus` when the boolean took the name. That rename is the *only* field
  change; `/yourapp/scan` still reports its outcome through `redeemed`. An
  already-redeemed code is a *successful* call: `status: true` +
  `qrStatus: "redeemed"`. `status` strings on panel endpoints (scheme
  `active|upcoming|previous`, claim `pending|approved|rejected`, batch
  `pending|saved`) are unrelated fields and are untouched.
- **Every tab's toolbar is now the same three parts** (`components/Toolbar.jsx`
  + `icons.jsx`): pill buttons for the permanent actions, an overflow `⋮` at
  the far right (Export CSV, Sample CSV, Delete all), and a circular FAB
  bottom-right for the add form — the old "+ Add …" / "+ New scheme" header
  buttons are gone. Overflow items hide themselves while their list is empty,
  and an empty `⋮` renders nothing.
- **The panel's login screen is Vastra mobile + OTP only.** The password form
  was removed from the UI; `POST /auth/login` still exists and remains the only
  way a super admin can get a token, but it must now be called directly.
- **`totals.products` on `GET /analytics/dashboard`** counts the imported
  catalog (`product_points` where `source = 'import'`) instead of the legacy
  `products` table, falling back to the samples when the catalog is empty and
  `USE_SAMPLE_PRODUCTS` is on — the card and the Products tab can no longer
  disagree.

## 2026-08-07 (sticker PDFs render in the browser)

### Changed
- **The panel builds the A4 sticker sheet client-side**
  (`panel/src/utils/stickerPdf.js`, jsPDF + `qrcode`) instead of calling
  `GET /qr/batches/{id}/print`. It is a direct port of `app/pdf_service.py` —
  same grid, sizes and fonts, so printed output is unchanged (the only
  difference is that jsPDF measures y from the page top, reportlab from the
  bottom). Reason: the PDF grows ~5.2 KB per code, so a 2,000-sticker batch is
  10.3 MB and exceeds Lambda's 6 MB buffered response cap, while the same batch
  as JSON is ~180 KB and costs the server no PNG rendering. Rendering now takes
  ~10–20s for 2,000 codes, so the modal shows progress.
- **`GET /qr/batches/{id}` now returns `is_parent`, `items` and `payload` per
  code** (children first, then box codes) — the same three `POST /qr/generate`
  already returned — so a *saved* batch can be re-printed client-side.
  `payload` is built server-side from `QR_BASE_URL` and must never be rebuilt
  in a browser: a drifted frontend copy would print stickers pointing at a dead
  host.
- `GET /qr/batches/{id}/print` and `app/pdf_service.py` are unchanged and still
  serve direct API callers; the panel simply no longer calls them.

## 2026-08-05 (AWS Lambda · opening balances · import fixes)

### Added
- **Lambda-runnable image + AWS runbook.** The `Dockerfile` gains the AWS
  Lambda Web Adapter (pinned `0.9.1`) with `AWS_LWA_INVOKE_MODE=response_stream`;
  `docs/integration/AWS_LAMBDA_DEPLOY.md` documents the five load-bearing
  requirements (adapter, `RESPONSE_STREAM` Function URL, ≥1,769 MB memory,
  internet egress for Vastra login, RDS Proxy).
- **`bootstrap_admin.py`** — creates the first super admin (and optionally a
  password-login manufacturer) non-destructively and idempotently: it never
  resets an existing password and drops nothing, so unlike `seed.py` it is safe
  against a real database. It also runs `init_db`/`migrate`/`create_constraints`,
  so it doubles as the out-of-band way to apply the schema before traffic.
- **Opening balances on retailer import.** A customer export's current-points
  column (`Point Balance` and friends) is carried over as one `points_ledger`
  row of the new `entry_type='import_opening'` per **newly created** retailer.
  The wallet and the Customers tab pick it up; every `entry_type='scan'` filter
  ignores it, so a carried-over number cannot inflate scan analytics. Skipped
  duplicate rows are left alone (no double-crediting on a re-import); negative
  cells are refused and reported in `errors`; `'14.00'`, `'1,005.00'` and
  `'₹ 500'` all parse.

### Fixed
- **Retailer import: the shop-or-name rule is per row, not per file.** A file
  carrying both `Firm Name` and `Name` fills the firm in only for shops that
  trade under one, so a blank shop cell now falls back to the person's name and
  the row imports. Only a row with *neither* is an error. Rejecting those cost a
  real 2,230-row import 420 rows.
- **Distributor linking on customer import** matches existing distributors
  case-insensitively before creating one, so a customer list imported after the
  distributor list attaches to those rows instead of creating near-duplicates.
- **`DELETE /retailers` / `DELETE /retailers/{id}`** no longer treat an
  `import_opening` row as protective history — a balance carried over by the
  very import being undone is part of that import, so the retailer is deleted
  with it. Real scan/claim history still protects a retailer.

## 2026-08-03 (MySQL migration)

Production moves from PostgreSQL/Neon to **MySQL 8.0.13+** (AWS RDS). SQLite
remains the zero-setup local dev and test backend. No API, panel or webview
behaviour changes — see `docs/integration/MYSQL_SETUP.md` for provisioning.

### Changed
- **`app/database.py`** — the `_PGConn`/psycopg adapter is replaced by
  `_MyConn`/PyMySQL. `lastrowid` is now native, so the `_ID_TABLES` registry
  and its `RETURNING id` rewrite are gone. `datetime('now')`/`date('now')`
  translate to `CAST(UTC_TIMESTAMP()/UTC_DATE() AS CHAR)` — never
  `DATE_FORMAT`, whose `%` would collide with pyformat placeholders.
- **`SCHEMA` column types** are now written to serve both backends from one
  declaration: `VARCHAR(n)` for every indexed/UNIQUE/PK text column (MySQL
  cannot index a bare `TEXT`), `DOUBLE` for coordinates, `MEDIUMTEXT` for the
  `product_points.attrs` CSV blob. SQLite takes affinity from the type name
  and ignores the widths, so its behaviour is unchanged.
- **`requirements.txt`** — `psycopg[binary]` → `pymysql[rsa]`.
- **`seed.py` / `seed_extra.py`** — the destructive-op opt-in is now
  `ALLOW_MYSQL=1` (was `ALLOW_NEON=1`).
- **Logins are now compared exactly.** Both login endpoints previously
  case-folded the submitted username (`body.username.strip().lower()`), so
  `SURYA` logged in as `surya` on every backend. They now match byte-for-byte,
  and `manufacturers.username`/`retailers.username` carry `utf8mb4_bin` so the
  database agrees. Stored usernames are still canonicalised to lowercase at
  creation, so a login is typed in lowercase. Passwords were never affected —
  PBKDF2 is verified in Python and `password_hash` is never compared in SQL.
  QR tokens, manual codes, session tokens, gift-claim references and SSO
  `external_id`s are likewise case-sensitive via `utf8mb4_bin`.

### Fixed
- **Gift-claim double-spend under concurrency on MySQL.** InnoDB defaults to
  REPEATABLE READ, under which the claim guard's `SELECT ... FOR UPDATE`
  serializes correctly but the follow-up balance read still returns the
  transaction's original snapshot. Measured on the 20-thread proof: 12 claims
  succeeded and the wallet reached −1090. `get_db()` now pins
  `READ COMMITTED` per connection (and `time_zone = '+00:00'`, since
  timestamps are stored and compared as UTC strings).
- **Foreign keys are created on MySQL.** MySQL parses inline `REFERENCES`
  written inside a column definition and silently creates nothing, so all 24
  relationships would have vanished — cascade deletes included.
  `_mysql_foreign_keys()` derives them from `SCHEMA` and re-emits them as
  table-level constraints.
- **Partial indexes rebuilt.** MySQL has none. The two `external_id` guards
  map directly (a MySQL UNIQUE index already allows unlimited NULLs);
  `uq_ledger_active_scan_token` becomes the stored generated column
  `points_ledger.active_scan_token`, which recomputes to NULL on scan reversal
  and so releases the token for a rescan, matching the old semantics exactly.
- **DDL is guarded by `information_schema`** rather than `IF [NOT] EXISTS`,
  which MySQL does not support for indexes or columns.

### Added
- **`docs/integration/MYSQL_SETUP.md`** — provisioning handoff: server version,
  charset/collation, required grants, connection-string format, and post-deploy
  verification queries.
- **`tests/test_mysql_schema.py`** — DDL idempotency across two startups, all
  24 foreign keys plus cascade/orphan behaviour, the generated-column index
  including release-on-reversal, the collation split, and UTC timestamp format.
- `tests/test_race_postgres.py` → **`tests/test_race_mysql.py`**. Setting
  `DATABASE_URL` now also runs the entire functional suite against MySQL.

## 2026-07-18 (YourApp server-to-server scan, phone-verified)

### Added
- **`POST /yourapp/scan`** — YourApp's backend scans on a retailer's behalf
  with no retailer session: body `{phone, code, lat?, lng?}`, auth via the
  shared `X-API-Key` header (new env `YOURAPP_API_KEY`; unset → `503`). The
  retailer is resolved by **phone number** (digits-only, last-10 match) among
  the retailers of the scanned code's manufacturer — unknown phone `403`,
  duplicate phones `409`, blocked `403`. Same response shape as `/scan`; when
  lat/lng are sent the shop pin/city/address refresh exactly like
  `POST /retailer/location` (best-effort, never fails the scan).
- **`POST /yourapp/qr/lookup`** — read-only code preview for YourApp's
  backend (same `X-API-Key` auth): product snapshot, base/bonus/total points
  (current best active scheme), `status: available|redeemed`, box item count,
  and the redeeming shop. Never redeems.
- **CSV import phone guard** — `POST /retailers/import` now rejects rows whose
  normalized phone duplicates another retailer of the same manufacturer
  (phone is an identity key for `/yourapp/scan`; missing phone stays allowed).

### Changed
- `/scan`'s redemption core was extracted into shared helpers (`_find_code`,
  `_best_scheme`, `_redeem_code`) used by both scan endpoints — behavior of
  `/scan` is unchanged. `POST /retailer/location`'s pin-refresh logic became
  the shared `_refresh_retailer_pin`. No schema changes.

## 2026-07-17 (Scan reversal + Vastra OTP login)

### Added
- **Scan reversal** (manufacturer). `GET /scans/lookup?code=` (who scanned a
  code, when, for how many points) and `POST /scans/reverse`: flips the
  original ledger rows to `entry_type='scan_reversed'` (auto-excluded from all
  scan analytics/claims), writes offsetting negative `reversal` rows deducting
  exactly the credited points (base + bonus at scan time; refused with `409`
  if the wallet can't cover it — no negative balances), and clears
  `redeemed_at`/`redeemed_by` so the rightful retailer can rescan. Box scans
  reverse whole (a child code redeemed via its box resolves to the box).
  Panel: Claims tab gained a **Find scan by code** lookup box and a per-row
  **↩ Reverse** action (confirm-dialog gated).
- **Manufacturer OTP login via Vastra** (panel Login is dual-mode: password or
  mobile + OTP). `POST /auth/vastra/send-otp` / `POST /auth/vastra/verify-otp`
  proxy Vastra's `loyalty-signup`/`loyalty-verifyotp` (`app/vastra_client.py`,
  contract verified against staging 2026-07-16). Verify matches by
  `external_id` (= Vastra `organization_Id`) or auto-provisions the account,
  and stores Vastra's `access_token` in `manufacturers.vastra_access_token`
  (new `_MIGRATIONS` column). Env: `VASTRA_API_BASE_URL`, `VASTRA_API_KEY`,
  `VASTRA_API_TIMEOUT` (default 10s), `VASTRA_UDID`, `VASTRA_DEVICE_TYPE`.

### Changed
- **`GET /vastra/products` now authenticates to Vastra with the stored per-org
  `access_token`** (from OTP login) instead of a global credential; sessions
  without one (password login, or after logout) get
  `409 No Vastra session`. `/auth/logout` wipes the stored Vastra token.
- **Ledger scan-token unique index rescoped.** `uq_ledger_scan_token`
  (one ledger row per token) dropped and replaced by
  `uq_ledger_active_scan_token` (`WHERE token IS NOT NULL AND
  entry_type = 'scan'`) so a reversed scan, its reversal row, and a rescan can
  share the token — still one *active* scan credit per code. Idempotent
  `_CONSTRAINTS` swap, applied automatically on boot.
- Panel masthead (logo + name + burger menu) is now sticky on scroll.

## 2026-07-09 (Session hardening: single active session + emergency lockout)

Two auth changes, both additive and backward-safe (no reseed).

### Changed
- **Single active session.** `issue_token`/`issue_retailer_token` (`app/auth.py`)
  now delete the principal's existing tokens before inserting the new one, so at
  most one token row survives per user. A new login on any device or path
  (password *or* SSO) invalidates the previous token. Fixes the unbounded growth
  of `auth_tokens`/`retailer_tokens` from sessions that never call logout.

### Added
- **Emergency lockout flag.** New `blocked` column (`INTEGER NOT NULL DEFAULT 0`)
  on `manufacturers` and `retailers` (`_MIGRATIONS`). Flipped by hand in the DB;
  when `1`, login is refused and `current_user`/`current_retailer` reject the
  account's existing token — both return `403 Account is blocked`. Set back to
  `0` to restore. Enforced across `/auth/login`, `/auth/retailer/login`, and both
  SSO exchange endpoints.

## 2026-07-08 (Vastra product catalog: pull-based, panel-driven QR generation)

QR generation moved entirely into the loyalty admin panel — the manufacturer
logs in directly (no Vastra-app/server-to-server call), and the panel's
product picker now pulls Vastra's live catalog server-side instead of a
local `products` table. Local product CRUD/import is removed; the
manufacturer's points-per-scan value is loyalty's own data now.

### Added
- `app/vastra_client.py` — server-side client for Vastra's product-list API
  (`VASTRA_API_BASE_URL`/`VASTRA_API_KEY`/`VASTRA_API_TIMEOUT` env vars).
  Real contract still TBD with the Vastra team; this is a best-effort scaffold.
- `product_points` table (`manufacturer_id`, `product_external_id`, `points`)
  — the manufacturer's own loyalty-points value per Vastra product.
- `GET /vastra/products` (merges Vastra's live list with stored points
  overrides) and `PUT /vastra/products/{external_id}/points` (upsert).
- Panel: Products tab now lists the Vastra-sourced catalog with inline
  points editing only; `GenerateQrModal` sends the primary `/qr/generate`
  contract (`product_external_id` + snapshot + `points_per_code`).

### Removed
- `POST /products`, `PATCH /products/{id}`, `DELETE /products/{id}`,
  `POST /products/import` and their panel UI (add/edit/delete/CSV import).
  `GET /products` is kept, read-only, for the Schemes/Claims tabs and
  `/web/generate` (legacy local rows only — no new rows are written).

### Known follow-ups
- `scheme_products.product_id` still FKs the legacy local `products` table —
  new Vastra-sourced products can't get scheme-specific bonuses until it
  migrates to `product_external_id`.
- Manufacturer SSO's (`POST /auth/sso/manufacturer`) continued purpose is
  unclear now that QR generation no longer needs it.

## 2026-06-25 (Product System-of-Record: reference + snapshot)

Vastra is now the System of Record for products; the loyalty backend stores a
product **reference** (`product_external_id`) + immutable **snapshot**
(`product_name`/`product_sku`) and no longer joins the local `products` table on
the QR/scan/claims/analytics paths. Phase 1 — dual-contract; the Products page
and product CRUD remain for transition.

### Added
- **New `POST /qr/generate` contract (primary):**
  `{ product_external_id, product_name, product_sku, points_per_code, quantity, items_per_box? }`.
  Loyalty trusts the Vastra-supplied snapshot and does **no** products lookup.
  The legacy `{ product_id }` body is still accepted (panel / `/web/generate`).
- `product_external_id` on the scan response `product` object and as a `/claims`
  filter.
- Idempotent startup backfill (`_backfill_product_snapshots`) populating the new
  columns for pre-migration batches/ledger rows from the transitional products table.

### Changed (read paths now use snapshots, no products join)
- `/scan`, `/qr/batches`, `/qr/batches/{id}`, `/qr/batches/{id}/print`,
  `/retailer/wallet`, `/claims`, `/analytics/dashboard`.
- Batch tenancy now comes from `qr_batches.manufacturer_id` directly.
- Dashboard `by_product` groups by the ledger product snapshot, so it lists only
  products **with scan activity** (loyalty is no longer the catalog).

### Schema (additive)
- New columns: `qr_batches.manufacturer_id` / `product_external_id` /
  `product_name` / `product_sku`; `points_ledger.product_external_id` /
  `product_name` / `product_sku`. Indexes `idx_qr_batches_manuf`,
  `idx_ledger_product_ext`. `qr_batches.product_id` relaxed to nullable
  (Postgres: idempotent `DROP NOT NULL`; SQLite: nullable in `SCHEMA`).

### Unchanged
- QR engine (tokens/manual codes/box logic), wallet, points calculation,
  schemes, claims, analytics math, permissions, auth, SSO. `scheme_products`
  still references `product_id` (later phase). Product CRUD + Products page kept.

## 2026-06-25 (native-app SSO)

### Added
- **Single sign-on for native Vastra/YourApp apps.** Two exchange endpoints —
  `POST /auth/sso/manufacturer` and `POST /auth/sso/retailer` — verify a
  parent-app **HS256 JWT** and mint the *same* opaque bearer tokens as the
  password logins, so every other endpoint is unchanged. Verification
  (`verify_sso_assertion`, `app/auth.py`) pins `algorithms=["HS256"]`, checks
  `aud`/`iss`/`role`/`exp` and an `iat`-freshness window, and trusts only the
  signed `sub`.
- **`external_id` provisioning.** `POST /retailers` and the `/retailers/import`
  CSV now accept an optional `external_id` (the parent-system id, unique per
  manufacturer); manufacturers carry one too. SSO matches principals by
  `external_id` and **never auto-creates** — unknown/cross-tenant → `403`,
  bad/expired assertion → `401`.

### Schema (additive)
- New columns `manufacturers.external_id`, `retailers.external_id` (`_MIGRATIONS`)
  and two partial unique indexes (`_CONSTRAINTS`): `uq_manuf_external` (global)
  and `uq_retailer_external` on `(manufacturer_id, external_id)`. Applied
  idempotently on boot.

### Config
- New env: `SSO_SECRET` (required to enable SSO; unset → endpoints return `503`),
  `SSO_ISSUERS` (default `vastra,yourapp`), `SSO_AUDIENCE` (default `loyalty`),
  `SSO_MAX_AGE` (default `120`s). New dependency: `pyjwt`.

### Notes
- Manufacturer password login is unchanged (the web panel has no parent session
  to exchange); retailer access is SSO-only in production. No existing endpoint
  behaviour changed.

## 2026-06-24 (Vastra brand re-theme)

### Changed
- **Re-skinned both surfaces (panel + retailer/manufacturer webviews) to the
  Vastra brand.** New palette on a light base: brand blue `#0191D0`, dark blue
  `#1D466F`, coral `#FB624B`, near-black text `#112134`; modal/scan backdrops use
  the brand overlay `#1A00557A`. Functional status colors kept (approved = green,
  pending = amber, rejected/error = coral). Typeface switched from Fraunces + IBM
  Plex to a **Helvetica** system stack (`'Helvetica Neue', Helvetica, Arial,
  sans-serif`; headings bold), with a monospace stack for codes/tokens; the Google
  Fonts links were removed. `theme-color` meta set to `#0191D0`.
- The palette lives as CSS variables in `panel/src/styles.css` `:root` and is
  mirrored in each webview's inline `:root` (`app/web/*.html`); chart colors
  (`Dashboard.jsx`, `BarChart.jsx`), the scan confetti array, map markers, and
  status badges were re-pointed to the new palette.

### Notes
- Presentation-only — no backend, schema, or endpoint changes. **QR codes stay
  black/white** for scanner reliability; the Vastra logo is unchanged.

## 2026-06-24 (exports, in-panel QR, retailer nav, box-grouped claims)

### Added
- **Export CSV on every data tab.** Customers, Distributors, Products, Reward
  shop, Claims, and Redemptions each get an **↓ Export CSV** button. Fully
  client-side (new `panel/src/utils/csv.js`, RFC-4180 + UTF-8 BOM, no new
  endpoint or dependency). Claims walks every server page with the current
  filters; Redemptions exports **all three statuses** (pending/approved/rejected)
  in one file with a status column.
- **In-panel QR generation.** The Products **Generate QR** button now opens a
  modal (`panel/src/components/GenerateQrModal.jsx`) — generate, print PDF, save
  for later, and browse saved batches — instead of opening the standalone
  `/web/generate` webview in a new tab. Backend `/qr/generate` contract unchanged.
- **Retailer webview burger menu.** A top-right menu (Home · Scan · Reward shop ·
  Claims history · Log out) added to `/web`, `/web/scan`, `/web/shop`,
  `/web/claims`, matching the panel's burger pattern.
- **Scan celebration animation.** The scan result screen now plays a confetti
  burst and counts the points up from zero, replacing the plain text reveal.

### Changed
- **Claims list groups box (parent) scans.** A box scan writes one ledger row per
  child code, which flooded the Claims list with duplicate retailer rows. The
  `/claims` query now collapses a box into **one row** (`📦 Box · N items`, points
  summed) via `COALESCE(q.parent_token, l.token)` joined to `qr_codes` — done at
  query time, so it also tidies existing data with **no migration**. Plain scans
  are unchanged (one row each). The row carries `item_count` + `parent_token`.
- **"Scan another" reopens the camera automatically** — the extra "Start camera"
  tap after a scan is gone.
- **Login URL no longer shows the QR token.** A scanned deep link sends
  unauthenticated users to `/web?next=/web/scan/<token>`; `home.html` now captures
  `next` in memory and rewrites the address bar to a clean `/web` (the token is
  still used to redeem after login).

### Fixed
- **Box (parent) sticker PDF label.** The product name + manual code rendered on
  and below the box's bottom border. The box QR was shrunk slightly and both
  labels repositioned inside the border (`app/pdf_service.py`); child stickers
  unchanged.

### Notes
- No schema change. The box-grouped Claims view is purely a query/display change
  and applies retroactively to data already on Neon.

## 2026-06-23 (dashboard expansion)

### Added
- **Second stat row on the Dashboard — redemption requests.** Three new cards:
  total redeem requests, pending requests, approved requests. Backed by new
  `redeem_total` / `redeem_pending` / `redeem_approved` counts (from `gift_claims`)
  in the `/analytics/dashboard` `totals`.
- **QR analytics charts.** A new Dashboard section with a **Year selector** and two
  themed, hand-rolled SVG bar charts (no new dependency, `panel/src/components/BarChart.jsx`):
  **Month-wise QR generation** (codes generated per month) and **Generated vs scanned**
  (grouped bars per month). Powered by a new `by_month` array
  (`{month, generated, scanned}`) on `/analytics/dashboard`, bucketed with
  `substr(created_at, 1, 7)` so it is portable across SQLite and Postgres. Each chart
  shows a per-year subtotal so the bars reconcile to a number on the card.

### Changed
- **First stat row reordered into a funnel:** Retailers · Products · Codes issued ·
  Codes scanned · Points awarded (the old "Scans" card is relabelled "Codes scanned").
- **Region-wise card no longer shows a blank row.** Scans from retailers with no
  region (region is optional at registration) are now grouped under the label
  **"Unspecified"** instead of an empty cell (`COALESCE(NULLIF(region, ''), 'Unspecified')`).

### Notes
- Cards are **all-time** totals; the QR charts are **per-month within the selected
  year**, which is why a single bar never equals an all-time card. The charts' bars
  always sum to the card totals across all years.

## 2026-06-23

### Added
- **Precise shop location + address.** When a retailer shares location it is now
  reverse-geocoded to a full street address (free OpenStreetMap Nominatim, in
  `geo.reverse_address`) and stored in the new nullable `retailers.address`
  column. The Customers "Location" column shows the address plus a Google Maps
  **"View on map"** link to the exact pin. Scan capture switched to high-accuracy
  GPS for a tighter pin.
- **CSV import for Products and Distributors.** "Import CSV" buttons on both tabs
  (matching Customers) with new `POST /products/import` and
  `POST /distributors/import`, reporting created/updated/skipped/errors via a
  shared `ImportResult` component.

### Changed
- **Shop location now updates every scan (latest wins).** Previously the pin
  locked on the first GPS scan and a registered city never corrected. Now each
  scanning session refreshes the pin, city, and address — so a wrong city (e.g.
  Surat) self-corrects to where the retailer actually scans (Ahmedabad).
- **Location asked up front, before scanning.** A trust-framed verification popup
  ("Confirm it's really you") replaces the after-the-fact capture and the "Share
  shop location" button. It is **not** a hard block: a retailer who can't grant it
  taps ✕ and the scan falls back to their registered city (shown in Claims).
- **Map dots clustered** (`leaflet.markercluster`) with street-level zoom; per-dot
  precision tightened to ~11m.

### Fixed
- **Product CSV import ignored the points column** unless it was named exactly
  `loyalty_points` — a CSV with `PointsPerScan` imported everything at 0 points.
  Points are now matched under several header aliases, and import **upserts by
  SKU** (updates an existing product instead of skipping), so re-importing a
  corrected CSV fixes existing rows with no manual editing.

### Notes
- New DB column: `retailers.address` (nullable, additive).
- Added `docs/PROJECT_CONTEXT.md` — a full project orientation/summary.

## 2026-06-19

### Added
- **Distributor tracking (manufacturer → distributor → retailer).** A new
  `distributors` table (manufacturer-scoped) plus `retailers.distributor_id` lets
  manufacturers record which distributor each retailer sits under. Distributors
  are **tracking/attribution only** — no login, no wallet, no points of their own.
  - Distributor CRUD (`/distributors`); retailer create/update/list carry and
    validate `distributor_id`.
  - Every scan records `points_ledger.distributor_id`, **locked at scan time**
    (point-in-time, like `region`) so reassigning a retailer never rewrites history.
  - **`POST /retailers/import`** — self-serve CSV upload (posted as JSON text, no
    `python-multipart` dep). Columns: `shop_name` (required), `name`, `region`,
    `phone`, `distributor`. Each retailer gets an auto-login; the distributor is
    found-or-created by name and linked. Duplicate `shop_name` rows are skipped.
  - Dashboard `by_distributor` rollup (retailers + their scans/points — the
    retailers' figures, grouped, not distributor points).
  - Panel: **Distributors** tab; Customers tab gets a per-row assign-distributor
    dropdown, a distributor column/form field, and an **Import CSV** button.
- **Confirmation dialogs on points changes.** Every action that changes a points
  balance — redeem/claim, transfer, manual adjust (+/-), and approve/reject a
  redemption — now shows a themed confirmation dialog summarizing the effect
  before it commits. Panel uses a reusable promise-based `useConfirm()`
  (`panel/src/confirm.jsx`); the retailer webview gets a matching themed dialog in
  `shop.html`. Scanning to earn is exempt.

### Changed
- **Dashboard map clustering + street zoom.** Added `leaflet.markercluster`:
  dense cities collapse into a numbered cluster bubble that splits as you zoom
  (max zoom 12 → 18). Dots became `L.marker` + `divIcon` (markercluster can't
  cluster `circleMarker`). Scan-dot bucket precision sharpened 3 → 4 decimals
  (~110m → ~11m).
- **Panel navigation → burger menu.** The crowded horizontal tab bar is replaced
  by a single top-right burger button (shows the current tab) opening a dropdown
  with all tabs + Log out; closes on outside-click/Escape, themed to match.

### Fixed
- **Postgres deploy crash.** A `;` inside the new `distributors` schema comment
  split the statement in the PG adapter's `executescript` (SQLite handled it, so
  it passed locally but crashed `init_db` on Neon). Reworded the comment and
  hardened `executescript` to strip full-line `--` comments before splitting.
  **Rule: no `;` in `SCHEMA` comments.**

### Notes
- New DB objects: `distributors` table, `retailers.distributor_id`,
  `points_ledger.distributor_id` (all nullable/additive).

## 2026-06-18

### Added
- **Per-scan GPS location.** Each scan records where it happened: `points_ledger`
  gains nullable `lat`/`lng`, captured once per webview session on the client and
  sent with `POST /scan`. The dashboard India map plots scan *events* (grouped by
  retailer + rounded coords ~110m, weighted by count), falling back to the
  retailer's shop pin for scans without a location.
- **Optional city + auto-detect.** `region` is now optional when adding a
  retailer. If left blank, the first scan that shares GPS reverse-geocodes to the
  nearest known city (`geo.nearest_city`, offline haversine over `CITY_COORDS` —
  no external geocoder) and backfills the retailer's `region`.
- **Auto-created retailer logins.** `POST /retailers` now creates a login
  automatically: username = first word of the shop name (lowercased
  alphanumerics), password = `<username>123`, with the retailer id appended on a
  clash. The creation response returns the credentials once for the panel to show.

### Changed
- **Reliable mobile location capture.** Switched to city-level accuracy
  (`enableHighAccuracy: false`, 20s timeout, cached fix) — high-accuracy GPS
  routinely timed out on phones. Transient failures now retry (only a hard
  permission denial is remembered for the session). The scan result screen shows
  the capture status and a tap-to-share button (a user gesture is the most
  reliable way to get the location prompt on mobile).
- **Clean scan URL.** After a deep-linked QR token is read, the address bar is
  cosmetically rewritten to `/web/scan` (the token is still used for the scan).

### Notes
- New DB columns: `points_ledger.lat`, `points_ledger.lng` (both nullable).
- `seed.py` rebuilds from `SCHEMA` only and does not run `_MIGRATIONS`; run
  `migrate()` (or start the app) after seeding. See DEPLOY.md.
