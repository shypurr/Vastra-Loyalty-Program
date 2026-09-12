"""Persistence layer for the loyalty QR API.

Dual backend: MySQL when DATABASE_URL is set (production / AWS RDS), SQLite
otherwise (zero-setup local development). The rest of the app uses SQLite-style
calls (``?`` placeholders, ``cur.lastrowid``, ``db.executescript``); a thin
adapter translates those to PyMySQL when running on MySQL, so application code
stays backend-agnostic.

Portability rules for anyone editing SQL here or in main.py:
  * Keep writing sqlite style: ``?`` / named ``:param``, ``datetime('now')``,
    ``cur.lastrowid``.
  * No ``%`` literals anywhere in SQL — PyMySQL uses pyformat placeholders, so
    a stray ``%`` collides with parameter binding (this is why the timestamp
    translation below uses ``CAST(... AS CHAR)`` and not ``DATE_FORMAT``).
  * No ``;`` inside SCHEMA comments — ``executescript`` splits statements on
    ``;`` (full-line ``--`` comments are stripped first, but stay safe).
  * Column types are written so ONE declaration serves both backends: SQLite
    picks affinity from the type name (``VARCHAR(n)``/``MEDIUMTEXT`` -> TEXT,
    ``DOUBLE`` -> REAL, length ignored), while MySQL needs the concrete widths
    because it cannot index a bare TEXT column.
  * Any text column that is a PRIMARY KEY, UNIQUE, or indexed MUST be
    ``VARCHAR(n)``, never ``TEXT``.
"""

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

DB_PATH = Path(__file__).resolve().parent.parent / "qr_api.db"
DATABASE_URL = os.environ.get("DATABASE_URL")
IS_MYSQL = bool(DATABASE_URL)

if IS_MYSQL:
    if DATABASE_URL.startswith(("postgres://", "postgresql://")):
        # Guards against a stale Neon connection string left in the host's
        # environment after the MySQL migration: fail loudly at boot instead of
        # surfacing as an unreadable driver error on the first request.
        raise RuntimeError(
            "DATABASE_URL is a PostgreSQL URL, but this app now runs on MySQL. "
            "Set a mysql://user:pass@host:3306/dbname URL (or unset "
            "DATABASE_URL to use local SQLite).")
    import pymysql
    from pymysql.cursors import DictCursor

# Case-sensitive columns get an explicit binary collation on MySQL. SQLite
# compares text case-sensitively by default, but MySQL's default collation
# (utf8mb4_0900_ai_ci) does not — without this, QR tokens, manual codes,
# session tokens and login usernames would all match case-insensitively,
# unlike today.
#
# Applied to every credential and identifier: login usernames, QR/session
# tokens, manual codes, gift-claim references and SSO external ids.
# (Passwords need nothing here — verify_password runs PBKDF2 in Python and
# compares with secrets.compare_digest; password_hash is never compared in
# SQL, so it has always been byte-exact regardless of collation.)
# Descriptive text — shop names, people's names, regions, notes — keeps the
# case-insensitive default, so searching and grouping behave naturally.
# NOTE: a foreign key's collation must match its referent, which is why
# points_ledger.token carries this too.
_CS = " COLLATE utf8mb4_bin" if IS_MYSQL else ""

_SCHEMA_TMPL = """
CREATE TABLE IF NOT EXISTS manufacturers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(100){CS} NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,   -- pbkdf2 salt$hash (hex)
    display_name VARCHAR(255) NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,  -- 1 = super admin (creates accounts)
    created_at VARCHAR(32) NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token VARCHAR(64){CS} PRIMARY KEY,
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id)
        ON DELETE CASCADE,
    created_at VARCHAR(32) NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id),
    name VARCHAR(255) NOT NULL,
    sku VARCHAR(191) NOT NULL UNIQUE,
    loyalty_points INTEGER NOT NULL DEFAULT 0,
    created_at VARCHAR(32) NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS retailers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id),
    name VARCHAR(255) NOT NULL,
    shop_name VARCHAR(255) NOT NULL,
    region VARCHAR(120) NOT NULL,
    phone VARCHAR(32),
    username VARCHAR(100){CS} UNIQUE,   -- retailer login (null = no login yet)
    password_hash VARCHAR(255),
    lat DOUBLE,   -- auto-filled from region city lookup, manual override allowed
    lng DOUBLE,
    location_source VARCHAR(16),  -- 'city' (lookup) | 'gps' (locked from first scan)
    created_at VARCHAR(32) NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS retailer_tokens (
    token VARCHAR(64){CS} PRIMARY KEY,
    retailer_id INTEGER NOT NULL REFERENCES retailers(id) ON DELETE CASCADE,
    created_at VARCHAR(32) NOT NULL DEFAULT (datetime('now'))
);

-- Distributors sit between a manufacturer and its retailers (manuf ->
-- distributor -> retailer), for tracking/attribution only (no login).
-- A retailer links to one via retailers.distributor_id (nullable).
CREATE TABLE IF NOT EXISTS distributors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id),
    name VARCHAR(255) NOT NULL,
    phone VARCHAR(32),
    region VARCHAR(120),
    created_at VARCHAR(32) NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schemes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id),
    name VARCHAR(255) NOT NULL,
    -- DEFAULT ('') not DEFAULT '': MySQL only accepts a literal default on a
    -- TEXT column when written as an expression. SQLite accepts both forms.
    description TEXT NOT NULL DEFAULT (''),
    start_date VARCHAR(10) NOT NULL,  -- YYYY-MM-DD inclusive
    end_date VARCHAR(10) NOT NULL,    -- YYYY-MM-DD inclusive
    bonus_points INTEGER NOT NULL,
    created_at VARCHAR(32) NOT NULL DEFAULT (datetime('now'))
);

-- Products covered by a scheme. No rows for a scheme = covers ALL products.
CREATE TABLE IF NOT EXISTS scheme_products (
    scheme_id INTEGER NOT NULL REFERENCES schemes(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    PRIMARY KEY (scheme_id, product_id)
);

CREATE TABLE IF NOT EXISTS qr_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- nullable: under the Product SoR model the batch carries a product snapshot
    -- (product_external_id / product_name / product_sku) instead of a local
    -- product. product_id is kept only for legacy/transitional rows.
    product_id INTEGER REFERENCES products(id),
    quantity INTEGER NOT NULL,
    -- points promised per code, frozen at generation time (printed boxes
    -- keep their value even if the product's points change later)
    points_per_code INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',  -- pending | saved
    created_at VARCHAR(32) NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS qr_codes (
    token VARCHAR(64){CS} PRIMARY KEY,
    manual_code VARCHAR(16){CS} NOT NULL UNIQUE,  -- 6-char fallback if QR damaged
    batch_id INTEGER NOT NULL REFERENCES qr_batches(id) ON DELETE CASCADE,
    is_parent INTEGER NOT NULL DEFAULT 0,  -- 1 = box code aggregating children
    parent_token VARCHAR(64){CS},          -- child -> its box code
    redeemed_at VARCHAR(32),
    redeemed_by INTEGER REFERENCES retailers(id),
    created_at VARCHAR(32) NOT NULL DEFAULT (datetime('now'))
);

-- Wallet ledger. entry_type:
--   scan        +points  earned by scanning a code (token/product set)
--   gift_redeem -points  spent claiming a gift
--   refund      +points  gift claim rejected
--   adjustment  +/-      manual correction by manufacturer (note set)
--   transfer    +/-      points moved between retailers (counterparty set)
--   scan_reversed +points  a scan undone by the manufacturer (was 'scan',
--                          excluded from scan analytics by the entry_type filter)
--   reversal    -points  offsetting deduction written when a scan is reversed
--                        (token/note/created_by set)
-- Balance for a retailer = SUM(points). Scan analytics filter entry_type='scan'.
CREATE TABLE IF NOT EXISTS points_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    entry_type VARCHAR(20) NOT NULL DEFAULT 'scan',
    token VARCHAR(64){CS} REFERENCES qr_codes(token),
    product_id INTEGER REFERENCES products(id),
    points INTEGER NOT NULL,          -- total = base + bonus for scans
    base_points INTEGER NOT NULL DEFAULT 0,
    bonus_points INTEGER NOT NULL DEFAULT 0,
    scheme_id INTEGER REFERENCES schemes(id),  -- scheme that paid the bonus
    counterparty_retailer_id INTEGER REFERENCES retailers(id),  -- transfers
    note TEXT,                          -- reason for adjustments/transfers/gifts
    created_by INTEGER REFERENCES manufacturers(id),  -- who made a manual entry
    region VARCHAR(120),
    scanned_at VARCHAR(32) NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id),
    name VARCHAR(255) NOT NULL,
    description TEXT NOT NULL DEFAULT (''),
    points_cost INTEGER NOT NULL,
    image_url VARCHAR(500),
    active INTEGER NOT NULL DEFAULT 1,
    created_at VARCHAR(32) NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gift_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    gift_id INTEGER NOT NULL REFERENCES gifts(id),
    reference VARCHAR(32){CS},               -- proof code shown to retailer
    points_spent INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    ledger_id INTEGER REFERENCES points_ledger(id),  -- the debit row
    created_at VARCHAR(32) NOT NULL DEFAULT (datetime('now')),
    decided_at VARCHAR(32)
);

-- The manufacturer's product catalog, keyed by product_external_id rather than
-- a local product row -- see docs/integration/PRODUCT_INTEGRATION.md.
-- Two kinds of row live here, told apart by `source`:
--   source = 'import' -- a product imported from the manufacturer's CSV. This
--     is the v1 catalog: name/sku come from the CSV and every other CSV column
--     is kept verbatim in the attrs JSON blob.
--   source IS NULL    -- a legacy points-override for a Vastra design, from
--     when the catalog was pulled from Vastra's get-design-ids. No name/sku.
-- Only 'import' rows count as a catalog, so legacy rows never surface as
-- nameless products.
CREATE TABLE IF NOT EXISTS product_points (
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id),
    product_external_id VARCHAR(191){CS} NOT NULL,
    points INTEGER NOT NULL DEFAULT 0,
    name VARCHAR(255),
    sku VARCHAR(191),
    -- JSON object of the free-form CSV columns, in CSV order. MEDIUMTEXT, not
    -- TEXT: a wide CSV can exceed TEXT's 64 KB cap, which MySQL in strict mode
    -- rejects outright rather than truncating.
    attrs MEDIUMTEXT,
    source VARCHAR(20),    -- 'import' or NULL (legacy)
    updated_at VARCHAR(32) NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (manufacturer_id, product_external_id)
);
"""

SCHEMA = _SCHEMA_TMPL.format(CS=_CS)

# Secondary indexes, kept out of SCHEMA because MySQL has no
# "CREATE INDEX IF NOT EXISTS" — init_db() creates them per backend.
# (table, index_name, column_list)
_INDEXES = [
    ("qr_codes", "idx_qr_codes_batch", "batch_id"),
    ("qr_codes", "idx_qr_codes_parent", "parent_token"),
    ("points_ledger", "idx_ledger_type", "entry_type"),
    ("points_ledger", "idx_ledger_token", "token"),
    ("gift_claims", "idx_gift_claims_manuf", "manufacturer_id"),
    ("points_ledger", "idx_ledger_retailer", "retailer_id"),
    ("points_ledger", "idx_ledger_product", "product_id"),
    ("points_ledger", "idx_ledger_manuf", "manufacturer_id"),
    ("products", "idx_products_manuf", "manufacturer_id"),
    ("retailers", "idx_retailers_manuf", "manufacturer_id"),
    ("distributors", "idx_distributors_manuf", "manufacturer_id"),
    ("product_points", "idx_product_points_manuf", "manufacturer_id"),
    # Scratch codes are looked up on every typed legacy scan; non-unique by
    # design (see _MIGRATIONS), so the manufacturer scoping happens in the query.
    ("qr_codes", "idx_qr_codes_legacy_scratch", "legacy_scratch"),
]

# All tables, dropped on reset (order irrelevant — FK checks are disabled).
_DROP_ORDER = ("gift_claims", "gifts", "points_ledger", "qr_codes",
               "qr_batches", "scheme_products", "schemes", "retailer_tokens",
               "retailers", "distributors", "product_points", "products",
               "auth_tokens", "manufacturers")


# ------------------------------------------------------------------- MySQL

def _mysql_schema() -> str:
    """SCHEMA with the handful of declarations MySQL spells differently.
    Column *types* are already portable (see the module docstring); only the
    autoincrement clause, the timestamp default and the table options differ."""
    s = SCHEMA
    s = s.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                  "INT AUTO_INCREMENT PRIMARY KEY")
    s = s.replace("(datetime('now'))", "(CAST(UTC_TIMESTAMP() AS CHAR))")
    # Every CREATE TABLE in SCHEMA closes with "\n);".
    s = s.replace("\n);", "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;")
    return s


def _mysql_foreign_keys():
    """Foreign keys parsed out of SCHEMA's inline REFERENCES clauses.

    MySQL *silently ignores* inline references written in a column definition
    ("REFERENCES t(c)" as part of the column) — it parses them and creates
    nothing. Only table-level FOREIGN KEY constraints are honoured. Without
    this, every relationship in SCHEMA would vanish on MySQL while still being
    enforced on SQLite: deleting a scheme would orphan its scheme_products
    rows, deleting a retailer would leave its tokens behind, and a batch could
    be discarded out from under scanned ledger rows.

    Derived from SCHEMA rather than kept as a second hand-maintained list, so a
    new REFERENCES clause can never drift out of sync with its constraint.

    Yields (table, constraint_name, ddl).
    """
    for block in re.finditer(
            r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", SCHEMA, re.S):
        table, body = block.group(1), block.group(2)
        # Strip comments, then split the body into column definitions. A
        # definition may wrap across lines (the ON DELETE CASCADE clauses do).
        body = re.sub(r"--[^\n]*", "", body)
        for col_def in body.split(","):
            col_def = " ".join(col_def.split())
            m = re.match(
                r"(\w+) .*?REFERENCES (\w+)\((\w+)\)(?: (ON DELETE CASCADE))?$",
                col_def)
            if not m:
                continue
            col, ref_table, ref_col, cascade = m.groups()
            name = f"fk_{table}_{col}"
            ddl = (f"ALTER TABLE {table} ADD CONSTRAINT {name}"
                   f" FOREIGN KEY ({col}) REFERENCES {ref_table}({ref_col})")
            if cascade:
                ddl += " ON DELETE CASCADE"
            yield table, name, ddl


def _mysql_connect_kwargs() -> dict:
    """Parse DATABASE_URL into PyMySQL connect kwargs.

    Accepts mysql:// and mysql+pymysql:// URLs. TLS (required by RDS in most
    setups) is enabled with ``?ssl=true``, or ``?ssl_ca=/path/to/bundle.pem``
    to also verify the server certificate.
    """
    u = urlparse(DATABASE_URL)
    kw = {
        "host": u.hostname or "127.0.0.1",
        "port": u.port or 3306,
        "user": unquote(u.username) if u.username else "",
        "password": unquote(u.password) if u.password else "",
        "database": (u.path or "").lstrip("/"),
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": False,
    }
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if q.get("ssl_ca"):
        kw["ssl"] = {"ca": q["ssl_ca"]}
    elif q.get("ssl", "").lower() in ("1", "true", "required", "yes"):
        kw["ssl"] = {}
    return kw


def _translate(sql: str, named: bool) -> str:
    """Rewrite sqlite SQL fragments to their MySQL equivalents."""
    # CAST(... AS CHAR) rather than DATE_FORMAT: the format string's '%' would
    # collide with PyMySQL's pyformat placeholders. The output is
    # 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD', byte-identical to sqlite's.
    # UTC (not NOW()) because sqlite's datetime('now') is UTC and timestamps
    # here are compared and bucketed as strings.
    sql = sql.replace("datetime('now')", "CAST(UTC_TIMESTAMP() AS CHAR)")
    sql = sql.replace("date('now')", "CAST(UTC_DATE() AS CHAR)")
    if named:
        # :name -> %(name)s  (only when params is a dict; such queries carry
        # no time-format literals, so colons here are always placeholders)
        sql = re.sub(r":([a-zA-Z]\w*)", r"%(\1)s", sql)
    else:
        sql = sql.replace("?", "%s")
    return sql


class _Cursor:
    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        # Exposed so application code can branch on how many rows an
        # UPDATE/DELETE actually affected — used by the conditional
        # "redeem only if still unredeemed" scan guard. PyMySQL's cursor
        # reports rowcount the same way sqlite3 does.
        return self._cur.rowcount

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)


class _MyConn:
    """Adapter giving a PyMySQL connection the sqlite-style surface the app
    expects: ``execute`` returning a cursor with ``lastrowid``,
    ``executemany`` and ``executescript``."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        # Pass None (not an empty tuple) when there are no parameters so
        # PyMySQL skips interpolation entirely.
        try:
            cur.execute(_translate(sql, isinstance(params, dict)),
                        params if params else None)
        except pymysql.err.IntegrityError as exc:
            if exc.args and exc.args[0] == 1062:  # ER_DUP_ENTRY
                # Surface as the sqlite-style message the app already checks for.
                raise Exception(f"UNIQUE constraint failed: {exc}") from exc
            raise
        return _Cursor(cur, lastrowid=cur.lastrowid)

    def executemany(self, sql, seq):
        cur = self._conn.cursor()
        cur.executemany(_translate(sql, False), list(seq))
        # PyMySQL's bulk-insert path does not always set lastrowid, and it would
        # be the id of the FIRST row anyway. No caller uses it after an
        # executemany, so report None rather than guessing.
        return _Cursor(cur, lastrowid=getattr(cur, "lastrowid", None))

    def executescript(self, script):
        cur = self._conn.cursor()
        # Drop full-line "--" comments before splitting so a ';' inside a comment
        # can't break a statement in two (MySQL ignores them anyway).
        cleaned = "\n".join(line for line in script.splitlines()
                            if not line.lstrip().startswith("--"))
        for stmt in cleaned.split(";"):
            if stmt.strip():
                cur.execute(stmt)
        return _Cursor(cur)


def _mysql_columns(db, table: str) -> set:
    """Column names of `table` — MySQL's stand-in for PRAGMA table_info."""
    return {r["name"] for r in db.execute(
        "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?", (table,))}


def _mysql_indexes(db, table: str) -> set:
    """Index names on `table` — MySQL has no CREATE/DROP INDEX IF [NOT] EXISTS,
    so every index statement is guarded by this lookup instead."""
    return {r["name"] for r in db.execute(
        "SELECT DISTINCT INDEX_NAME AS name FROM information_schema.STATISTICS"
        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?", (table,))}


# ---------------------------------------------------------------- public API

def init_db() -> None:
    with get_db() as db:
        db.executescript(_mysql_schema() if IS_MYSQL else SCHEMA)
    for table, name, cols in _INDEXES:
        try:
            with get_db() as db:
                if IS_MYSQL:
                    if name not in _mysql_indexes(db, table):
                        db.execute(f"CREATE INDEX {name} ON {table}({cols})")
                else:
                    db.execute(
                        f"CREATE INDEX IF NOT EXISTS {name} ON {table}({cols})")
        except Exception:
            pass


# Columns added after the first schema version. Applied idempotently on every
# startup so deploying new code never requires wiping the database.
#
# Declarations follow the same portability rule as SCHEMA: VARCHAR(n) for any
# column that is (or will be) indexed, plain TEXT only for free-form values.
# {CS} expands to the binary collation on MySQL and to nothing on SQLite.
_MIGRATIONS = [
    ("retailers", "location_source", "VARCHAR(16)"),
    ("qr_codes", "is_parent", "INTEGER NOT NULL DEFAULT 0"),
    ("qr_codes", "parent_token", "VARCHAR(64){CS}"),
    ("points_ledger", "entry_type", "VARCHAR(20) NOT NULL DEFAULT 'scan'"),
    ("points_ledger", "base_points", "INTEGER NOT NULL DEFAULT 0"),
    ("points_ledger", "bonus_points", "INTEGER NOT NULL DEFAULT 0"),
    ("points_ledger", "scheme_id", "INTEGER"),
    ("points_ledger", "counterparty_retailer_id", "INTEGER"),
    ("points_ledger", "note", "TEXT"),
    ("points_ledger", "created_by", "INTEGER"),
    ("retailers", "username", "VARCHAR(100){CS}"),
    ("retailers", "password_hash", "VARCHAR(255)"),
    ("gift_claims", "reference", "VARCHAR(32){CS}"),
    # Per-scan capture location (where the QR was actually scanned), distinct
    # from the retailer's pinned shop coords. Null when location was denied.
    ("points_ledger", "lat", "DOUBLE"),
    ("points_ledger", "lng", "DOUBLE"),
    # Distributor layer (manuf -> distributor -> retailer). The retailer links to
    # one; the ledger records it per scan (locked at scan time, like region).
    ("retailers", "distributor_id", "INTEGER"),
    ("points_ledger", "distributor_id", "INTEGER"),
    # Reverse-geocoded street address of the shop, refreshed from the latest
    # scan location so the manufacturer can find the shop without asking.
    ("retailers", "address", "TEXT"),
    # Set to 1 when a retailer is created with a system-generated temporary
    # password, so the client can prompt for a change on first login. Existing
    # retailers default to 0 (their current login keeps working unchanged).
    ("retailers", "must_change", "INTEGER NOT NULL DEFAULT 0"),
    # SSO mapping keys: the parent system's stable id (Vastra manufacturer id /
    # YourApp retailer id). Set at provisioning time; the SSO exchange resolves a
    # signed assertion to a loyalty principal by external_id (see auth.verify_sso).
    # VARCHAR + binary collation: both are covered by unique indexes below, and an
    # SSO identity must match exactly.
    ("manufacturers", "external_id", "VARCHAR(100){CS}"),
    ("retailers", "external_id", "VARCHAR(100){CS}"),
    # Product System of Record migration: Vastra owns products; loyalty stores a
    # reference (product_external_id) + immutable snapshot (name/sku) and the
    # batch's own manufacturer_id, so QR generation/reads no longer depend on the
    # local products table. points_per_code stays frozen on the batch as before.
    ("qr_batches", "manufacturer_id", "INTEGER"),
    ("qr_batches", "product_external_id", "VARCHAR(191)"),
    ("qr_batches", "product_name", "VARCHAR(255)"),
    ("qr_batches", "product_sku", "VARCHAR(191)"),
    # Per-scan product snapshot on the ledger (point-in-time, like region /
    # distributor_id), so analytics/claims read history without joining products.
    ("points_ledger", "product_external_id", "VARCHAR(191)"),
    ("points_ledger", "product_name", "VARCHAR(255)"),
    ("points_ledger", "product_sku", "VARCHAR(191)"),
    # Emergency lockout flag, flipped by hand in the DB. 1 = blocked: login is
    # refused and existing tokens are rejected on their next request (= auto
    # logout). Existing rows default to 0 (active).
    ("manufacturers", "blocked", "INTEGER NOT NULL DEFAULT 0"),
    ("retailers", "blocked", "INTEGER NOT NULL DEFAULT 0"),
    # Vastra OTP login: the access_token Vastra mints at loyalty-verifyotp,
    # stored server-side only (never sent to the browser) and used to pull the
    # org's design list. Wiped on logout; refreshed on every OTP login.
    ("manufacturers", "vastra_access_token", "TEXT"),
    # Manual product catalog (CSV import). name/sku come from the CSV's product
    # name/code columns; attrs holds every other CSV column as a JSON object
    # keyed by the original header text, so the panel renders whatever the
    # manufacturer's file happened to contain. source = 'import' marks a real
    # catalog row -- pre-existing rows stay NULL and keep meaning "points
    # override for a Vastra design".
    ("product_points", "name", "VARCHAR(255)"),
    ("product_points", "sku", "VARCHAR(191)"),
    ("product_points", "attrs", "MEDIUMTEXT"),
    ("product_points", "source", "VARCHAR(20)"),
    # Legacy QR import (codes printed by the manufacturer's previous platform,
    # already in the market and unscannable otherwise). legacy_code is the
    # identifier parsed out of the old sticker's payload URL -- unique, because
    # it is the match key. legacy_scratch is that platform's typed fallback: a
    # 6-digit number, deliberately NOT unique, because 10^6 codes collide across
    # tenants far too easily (our own manual_code draws 6 of 32 symbols, ~1.07
    # billion) -- it is matched scoped to the scanning retailer's manufacturer.
    # Both binary-collated: the legacy token is case-sensitive base62, so
    # ai_ci matching would conflate distinct codes.
    ("qr_codes", "legacy_code", "VARCHAR(191){CS}"),
    ("qr_codes", "legacy_scratch", "VARCHAR(32){CS}"),
    # 'imported' marks a batch synthesised by the legacy import (no stickers of
    # ours were ever printed for it); NULL means we generated it.
    ("qr_batches", "source", "VARCHAR(16)"),
]


def migrate() -> None:
    """Add any missing columns to existing tables (new tables are handled by
    CREATE TABLE IF NOT EXISTS in init_db). Safe to run repeatedly. Each
    statement runs in its own transaction so one no-op never blocks the rest."""
    for table, col, decl in _MIGRATIONS:
        decl = decl.format(CS=_CS)
        try:
            with get_db() as db:
                if IS_MYSQL:
                    # MySQL has no ADD COLUMN IF NOT EXISTS.
                    if col not in _mysql_columns(db, table):
                        db.execute(
                            f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                else:
                    have = {
                        r["name"] for r in db.execute(
                            f"PRAGMA table_info({table})")
                    }
                    if col not in have:
                        db.execute(
                            f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        except Exception:
            pass


# Database-level guards added after the first schema version. Unlike _MIGRATIONS
# (plain ADD COLUMN), these can fail if legacy data already violates them, so each
# runs in its own try/except and a failure is non-fatal: the application-level
# atomic guards (conditional UPDATE in /scan) keep correctness even if the index
# could not be created. A failure here means pre-existing duplicate scan rows
# exist and should be de-duplicated manually before the index will apply.
_CONSTRAINTS = [
    # One *active* scan credit per QR token, enforced by the DB. Scoped to
    # entry_type = 'scan' so a reversed scan ('scan_reversed'), its negative
    # 'reversal' row, and a later legitimate rescan can all share the token.
    # The old token-only index (uq_ledger_scan_token) predates scan reversal
    # and would block those rows, so it is dropped first (idempotent no-op
    # once gone). Partial indexes are a SQLite (>= 3.8) feature; the MySQL
    # equivalent is built from a generated column in _mysql_constraints().
    # Defence-in-depth behind the conditional-UPDATE guards.
    "DROP INDEX IF EXISTS uq_ledger_scan_token",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_active_scan_token "
    "ON points_ledger(token) WHERE token IS NOT NULL AND entry_type = 'scan'",
    # SSO identity mapping must be unique. Manufacturer external_id is globally
    # unique; retailer external_id is unique *per manufacturer* (the same parent
    # id space can't leak across tenants). Partial indexes ignore NULLs so rows
    # provisioned without an external_id (e.g. dev/test logins) are unaffected.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_manuf_external "
    "ON manufacturers(external_id) WHERE external_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_retailer_external "
    "ON retailers(manufacturer_id, external_id) WHERE external_id IS NOT NULL",
    # Product SoR migration: batch tenancy now lives directly on qr_batches, and
    # analytics group scans by the product reference. Plain (non-unique) indexes.
    "CREATE INDEX IF NOT EXISTS idx_qr_batches_manuf "
    "ON qr_batches(manufacturer_id)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_product_ext "
    "ON points_ledger(product_external_id)",
    # The legacy match key must be unique, which is also what makes a re-sent
    # import chunk idempotent. Partial on SQLite so the vastly more common
    # generated codes (legacy_code IS NULL) are unaffected; MySQL needs no
    # predicate because a UNIQUE index there already treats NULLs as distinct.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_qr_legacy_code "
    "ON qr_codes(legacy_code) WHERE legacy_code IS NOT NULL",
]

# MySQL equivalents of the guards above. Two differences drive this list:
#   1. MySQL has no partial indexes. The two external_id guards need none — a
#      MySQL UNIQUE index already permits unlimited NULL rows, so the plain
#      unique index is an exact equivalent of "... WHERE external_id IS NOT NULL".
#   2. uq_ledger_active_scan_token filters on entry_type, not just NULL, so it is
#      rebuilt as a STORED generated column + unique index. The column recomputes
#      to NULL when a scan is reversed (entry_type -> 'scan_reversed'), which frees
#      the token for a legitimate rescan — exactly the partial index's semantics.
# (table, index_name, ddl)
_MYSQL_CONSTRAINTS = [
    ("points_ledger", "uq_ledger_active_scan_token",
     "ALTER TABLE points_ledger ADD UNIQUE INDEX uq_ledger_active_scan_token"
     " (active_scan_token)"),
    ("manufacturers", "uq_manuf_external",
     "ALTER TABLE manufacturers ADD UNIQUE INDEX uq_manuf_external"
     " (external_id)"),
    ("retailers", "uq_retailer_external",
     "ALTER TABLE retailers ADD UNIQUE INDEX uq_retailer_external"
     " (manufacturer_id, external_id)"),
    ("qr_batches", "idx_qr_batches_manuf",
     "CREATE INDEX idx_qr_batches_manuf ON qr_batches(manufacturer_id)"),
    ("points_ledger", "idx_ledger_product_ext",
     "CREATE INDEX idx_ledger_product_ext ON points_ledger(product_external_id)"),
    ("qr_codes", "uq_qr_legacy_code",
     "ALTER TABLE qr_codes ADD UNIQUE INDEX uq_qr_legacy_code (legacy_code)"),
]


def _mysql_foreign_key_names(db, table: str) -> set:
    return {r["name"] for r in db.execute(
        "SELECT CONSTRAINT_NAME AS name FROM information_schema.TABLE_CONSTRAINTS"
        " WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = ?"
        " AND CONSTRAINT_TYPE = 'FOREIGN KEY'", (table,))}


def _mysql_constraints() -> None:
    # Relationships SCHEMA declares inline, which MySQL would otherwise drop.
    for table, name, ddl in _mysql_foreign_keys():
        try:
            with get_db() as db:
                if name not in _mysql_foreign_key_names(db, table):
                    db.execute(ddl)
        except Exception:
            pass
    # The generated column backing uq_ledger_active_scan_token. Its collation
    # must match points_ledger.token's, or MySQL rejects the expression.
    try:
        with get_db() as db:
            if "active_scan_token" not in _mysql_columns(db, "points_ledger"):
                db.execute(
                    f"ALTER TABLE points_ledger ADD COLUMN active_scan_token"
                    f" VARCHAR(64){_CS} GENERATED ALWAYS AS"
                    f" (CASE WHEN entry_type = 'scan' THEN token END) STORED")
    except Exception:
        pass
    # Legacy token-only index, dropped for the same reason as on SQLite.
    try:
        with get_db() as db:
            if "uq_ledger_scan_token" in _mysql_indexes(db, "points_ledger"):
                db.execute(
                    "ALTER TABLE points_ledger DROP INDEX uq_ledger_scan_token")
    except Exception:
        pass
    for table, name, ddl in _MYSQL_CONSTRAINTS:
        try:
            with get_db() as db:
                if name not in _mysql_indexes(db, table):
                    db.execute(ddl)
        except Exception:
            pass


def create_constraints() -> None:
    """Idempotently add database-level guards. Safe to run on every startup;
    each statement is isolated so a no-op (or a legacy-data conflict) never
    blocks the rest or aborts boot."""
    if IS_MYSQL:
        _mysql_constraints()
        return
    for stmt in _CONSTRAINTS:
        try:
            with get_db() as db:
                db.execute(stmt)
        except Exception:
            pass


def reset_db() -> None:
    """Drop everything and recreate the schema. Used by the seed script;
    never called at app startup, so production data persists across deploys."""
    if IS_MYSQL:
        with get_db() as db:
            # MySQL has no DROP ... CASCADE; disable FK checks for the drop
            # instead (session-scoped, so it cannot leak to other connections).
            db.execute("SET FOREIGN_KEY_CHECKS = 0")
            db.execute("DROP TABLE IF EXISTS " + ", ".join(_DROP_ORDER))
            db.execute("SET FOREIGN_KEY_CHECKS = 1")
    elif DB_PATH.exists():
        DB_PATH.unlink()
    init_db()


@contextmanager
def get_db():
    if IS_MYSQL:
        conn = pymysql.connect(**_mysql_connect_kwargs())
        try:
            with conn.cursor() as cur:
                # READ COMMITTED, not InnoDB's REPEATABLE READ default: the gift
                # claim guard takes SELECT ... FOR UPDATE on the retailer row and
                # then re-reads the wallet balance with a plain SELECT. Under
                # REPEATABLE READ that second read would return the transaction's
                # original snapshot rather than the balance the winning claim just
                # committed, letting a concurrent claim overdraw the wallet.
                cur.execute(
                    "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
                # Timestamps are stored and compared as UTC strings (sqlite's
                # datetime('now') is UTC); MySQL's NOW()/UTC_TIMESTAMP() family
                # must not drift with the server's local zone.
                cur.execute("SET SESSION time_zone = '+00:00'")
            yield _MyConn(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
