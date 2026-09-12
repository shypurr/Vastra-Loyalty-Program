"""Loyalty QR API — multi-manufacturer backend for Vastra (generate) and
YourApp (scan) plus the manufacturer admin panel.

Roles:
- Super admin (is_admin=1): creates manufacturer accounts. Owns no data.
- Manufacturer: products, retailers, schemes, batches, claims — all scoped
  to the logged-in account.
- Retailer side (/scan, /public/*): open for the webview demo; in production
  YourApp's session must supply the retailer identity.

Webview pages for the demo are served at /web/generate and /web/scan; the
built React panel (panel/dist) is served at /panel when present.
"""

import json
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (current_admin, current_manufacturer, current_retailer,
                   current_user, hash_password, issue_retailer_token,
                   issue_token, new_temp_password, verify_password,
                   verify_sso_assertion)
from .database import IS_MYSQL, create_constraints, get_db, init_db, migrate
from .geo import coords_for, known_places, nearest_city, reverse_address
from .pdf_service import build_pdf
from .qr_service import (new_manual_code, new_reference, new_token,
                         payload_for, render_png)
# fetch_vastra_products is deliberately not imported: the catalog is CSV-imported
# now. It stays in vastra_client.py, dormant, for a future reconnect.
from .vastra_client import (VastraApiError, VastraRejection, send_login_otp,
                            verify_login_otp)

WEB_DIR = Path(__file__).resolve().parent / "web"
PANEL_DIST = Path(__file__).resolve().parent.parent / "panel" / "dist"


def _backfill_coords() -> None:
    """Resolve coordinates for retailers whose region wasn't in the lookup
    when they were added (e.g. state names supported later)."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, region FROM retailers WHERE lat IS NULL"
        ).fetchall()
        for r in rows:
            coords = coords_for(r["region"])
            if coords:
                db.execute(
                    """UPDATE retailers SET lat = ?, lng = ?,
                       location_source = 'city' WHERE id = ?""",
                    (coords[0], coords[1], r["id"]),
                )


def _backfill_product_snapshots() -> None:
    """One-time, idempotent backfill of the product reference/snapshot columns
    introduced by the Product System-of-Record migration. Fills only NULLs from
    the (transitional) products table so batches/scans created before the
    migration keep displaying correctly without a live products join. Safe to run
    on every startup; non-fatal if it cannot complete."""
    try:
        with get_db() as db:
            db.execute(
                """UPDATE qr_batches SET manufacturer_id = (
                       SELECT p.manufacturer_id FROM products p
                       WHERE p.id = qr_batches.product_id)
                   WHERE manufacturer_id IS NULL AND product_id IS NOT NULL""")
            db.execute(
                """UPDATE qr_batches SET product_name = (
                       SELECT p.name FROM products p
                       WHERE p.id = qr_batches.product_id)
                   WHERE product_name IS NULL AND product_id IS NOT NULL""")
            db.execute(
                """UPDATE qr_batches SET product_sku = (
                       SELECT p.sku FROM products p
                       WHERE p.id = qr_batches.product_id)
                   WHERE product_sku IS NULL AND product_id IS NOT NULL""")
            db.execute(
                """UPDATE points_ledger SET product_name = (
                       SELECT p.name FROM products p
                       WHERE p.id = points_ledger.product_id)
                   WHERE product_name IS NULL AND product_id IS NOT NULL""")
            db.execute(
                """UPDATE points_ledger SET product_sku = (
                       SELECT p.sku FROM products p
                       WHERE p.id = points_ledger.product_id)
                   WHERE product_sku IS NULL AND product_id IS NOT NULL""")
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    migrate()
    create_constraints()
    _backfill_coords()
    _backfill_product_snapshots()
    yield


app = FastAPI(title="Loyalty QR API", version="4.0.0", lifespan=lifespan)

# Panel dev server. Same-origin in production, so this is dev-only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- rate limiting (Fix 4) ----------
# Pragmatic per-endpoint limits via slowapi. Authenticated endpoints are keyed
# by the bearer token (so one retailer can't exhaust another's budget when they
# share a NAT IP); login endpoints are keyed by client IP. Every limit is
# overridable by env var, and the whole feature can be switched off with
# RL_ENABLED=0. In-memory storage by default; set RL_STORAGE_URI (e.g. a redis://
# URL) when running more than one process.
import os as _os  # noqa: E402

from slowapi import Limiter, _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from slowapi.util import get_remote_address  # noqa: E402

RL_ENABLED = _os.environ.get("RL_ENABLED", "1") != "0"
RL_LOGIN = _os.environ.get("RL_LOGIN", "10/minute")        # manuf + retailer login
RL_SCAN = _os.environ.get("RL_SCAN", "60/minute")          # generous for bulk scanning
RL_CLAIM = _os.environ.get("RL_CLAIM", "20/minute")
RL_QRGEN = _os.environ.get("RL_QRGEN", "30/minute")
RL_IMPORT = _os.environ.get("RL_IMPORT", "600/hour")


def _client_key(request: Request) -> str:
    """Rate-limit bucket key: the caller's bearer token (or ?token=) when
    present, else their IP. Keeps per-retailer limits independent on shared IPs."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
        if tok:
            return tok
    return request.query_params.get("token") or get_remote_address(request)


_limiter_kwargs = {"key_func": _client_key, "enabled": RL_ENABLED}
if _os.environ.get("RL_STORAGE_URI"):
    _limiter_kwargs["storage_uri"] = _os.environ["RL_STORAGE_URI"]
limiter = Limiter(**_limiter_kwargs)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ---------- YourApp server-to-server integration ----------
# YourApp's backend calls /yourapp/* directly (no retailer session): a shared
# secret in the X-API-Key header authenticates the caller, and the retailer is
# identified by the phone number registered in `retailers` (imported from
# YourApp's own user data via CSV). Env-only config mirroring SSO_SECRET:
# unset -> endpoints fail closed with 503.
import hmac as _hmac  # noqa: E402

YOURAPP_API_KEY = _os.environ.get("YOURAPP_API_KEY", "")

# Every /yourapp/* response carries `status`: True when the call worked, False
# when it did not — so YourApp can tell "the API is fine, there is just no
# data" apart from "the API failed", without reading the HTTP code. The HTTP
# codes themselves are unchanged (404 stays 404).
#
# `status` is the boolean and nothing else. The code's own redeemed/available
# state, which used to hold this name, is `qrStatus` on /yourapp/qr/lookup —
# that endpoint only. Never put a string back in `status`.
#
# Success is stamped by the three endpoints themselves. Failure is stamped
# here, so it also covers errors raised before the endpoint body runs (a bad
# X-API-Key, a rate limit, request validation) and unhandled crashes — the
# case where knowing the API is down matters most.
YOURAPP_PREFIX = "/yourapp/"


def _is_yourapp(request: Request) -> bool:
    return request.url.path.startswith(YOURAPP_PREFIX)


def require_yourapp_key(request: Request) -> None:
    if not YOURAPP_API_KEY:
        raise HTTPException(503, "YourApp integration is not configured")
    supplied = request.headers.get("x-api-key", "")
    if not _hmac.compare_digest(supplied, YOURAPP_API_KEY):
        raise HTTPException(401, "Invalid API key")


from fastapi.exception_handlers import (  # noqa: E402
    http_exception_handler as _default_http_handler,
    request_validation_exception_handler as _default_validation_handler,
)
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from starlette.exceptions import HTTPException as _StarletteHTTPException  # noqa: E402


def _stamp_false(resp: Response) -> JSONResponse:
    """Re-emit an error response with `status: False` added, keeping its HTTP
    status code, its headers (Retry-After on a 429) and its original body."""
    try:
        data = json.loads(resp.body)
    except Exception:
        data = {"detail": resp.body.decode("utf-8", "replace")}
    if not isinstance(data, dict):
        data = {"detail": data}
    data["status"] = False
    out = JSONResponse(data, status_code=resp.status_code)
    for key, value in resp.headers.items():
        if key.lower() not in ("content-length", "content-type"):
            out.headers[key] = value
    return out


# Each handler delegates to the framework's own so non-/yourapp routes keep
# exactly the responses they return today; only /yourapp/* is restamped.
async def _http_exc(request: Request, exc):
    resp = await _default_http_handler(request, exc)
    return _stamp_false(resp) if _is_yourapp(request) else resp


async def _validation_exc(request: Request, exc):
    resp = await _default_validation_handler(request, exc)
    return _stamp_false(resp) if _is_yourapp(request) else resp


async def _ratelimit_exc(request: Request, exc):
    resp = _rate_limit_exceeded_handler(request, exc)
    return _stamp_false(resp) if _is_yourapp(request) else resp


async def _unhandled_exc(request: Request, exc):
    """A crash is the case where YourApp most needs to hear `status: False`,
    so answer 500 with the flag instead of letting the connection die with no
    body. Anything outside /yourapp/* re-raises and keeps the default
    behaviour (and the traceback in the logs)."""
    if not _is_yourapp(request):
        raise exc
    return JSONResponse(
        {"detail": "Internal server error", "status": False},
        status_code=500,
    )


app.add_exception_handler(_StarletteHTTPException, _http_exc)
app.add_exception_handler(RequestValidationError, _validation_exc)
app.add_exception_handler(RateLimitExceeded, _ratelimit_exc)
app.add_exception_handler(Exception, _unhandled_exc)


def _norm_phone(s: str | None) -> str:
    """Digits only, last 10 kept — matches Indian numbers regardless of a
    +91/0 prefix, spaces, or dashes."""
    digits = "".join(ch for ch in (s or "") if ch.isdigit())
    return digits[-10:]


def _retailer_by_phone(db, manufacturer_id: int, phone: str) -> dict:
    """Resolve a retailer of one manufacturer by registered phone number.
    Comparison is on normalized digits, in Python, so stored formatting never
    matters (retailer counts per manufacturer are small). Tenancy comes from
    the scanned code's batch, so a phone can never credit across tenants."""
    want = _norm_phone(phone)
    if len(want) < 10:
        raise HTTPException(422, "Invalid phone number")
    matches = [dict(r) for r in db.execute(
        """SELECT * FROM retailers
           WHERE manufacturer_id = ? AND phone IS NOT NULL""",
        (manufacturer_id,),
    ) if _norm_phone(r["phone"]) == want]
    if not matches:
        raise HTTPException(403, "Phone number not registered")
    if len(matches) > 1:
        raise HTTPException(409, "Multiple retailers share this phone number")
    if matches[0]["blocked"]:
        raise HTTPException(403, "Account is blocked")
    return matches[0]


# ---------- schemas ----------

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class SsoIn(BaseModel):
    # A parent-app (Vastra / YourApp) signed HS256 JWT. Verified by
    # auth.verify_sso_assertion; the loyalty token is minted only if it checks out.
    assertion: str = Field(min_length=1, max_length=4096)


class ManufacturerIn(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=6, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)


class ProductPointsIn(BaseModel):
    points: int = Field(ge=0)


class RetailerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    shop_name: str = Field(min_length=1, max_length=200)
    # Optional: when left blank, the city is inferred from the retailer's
    # first scan location (reverse-geocoded to the nearest known city).
    region: str = Field(default="", max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    # Distributor this retailer belongs to (manuf -> distributor -> retailer).
    distributor_id: int | None = Field(default=None)
    # Manual override; when omitted, resolved from region city name
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    # Parent-system (YourApp) stable id, set at provisioning time so the SSO
    # exchange can resolve this retailer. Unique per manufacturer.
    external_id: str | None = Field(default=None, max_length=200)


class SchemeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    start_date: date
    end_date: date
    bonus_points: int = Field(ge=1)
    product_ids: list[int] = Field(
        default_factory=list,
        description="Products covered; empty list = all products",
    )


class GenerateIn(BaseModel):
    # New (primary) contract: Vastra is the product System of Record and sends a
    # reference (product_external_id) + immutable snapshot (name/sku) + the frozen
    # points value. Loyalty trusts it and never touches the products table.
    product_external_id: str | None = Field(default=None, max_length=200)
    product_name: str | None = Field(default=None, max_length=200)
    product_sku: str | None = Field(default=None, max_length=100)
    # Transitional/legacy: the loyalty product id used by the admin panel and the
    # /web/generate page. Used only when product_external_id is absent; resolved
    # once against the products table to build the same snapshot. Remove once those
    # clients call the Vastra backend instead.
    product_id: int | None = Field(default=None)
    quantity: int = Field(ge=1, le=10_000)
    points_per_code: int | None = Field(
        default=None, ge=0,
        description="Points frozen per code. Required with product_external_id; "
                    "with legacy product_id, defaults to the product's points.",
    )
    items_per_box: int | None = Field(
        default=None, ge=2, le=1000,
        description="If set, group children into box (parent) QR codes; "
                    "scanning one box registers all its items at once",
    )


class ScanIn(BaseModel):
    code: str = Field(
        min_length=1, max_length=64,
        description="QR token or 6-char manual code (dashes/spaces ok)",
    )
    # Where this scan happened. Captured once per webview session on the
    # client and sent with every scan; null when location was denied.
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


class GiftClaimIn(BaseModel):
    gift_id: int


# ---------- auth ----------

@app.post("/auth/login")
@limiter.limit(RL_LOGIN, key_func=get_remote_address)
def login(request: Request, body: LoginIn):
    with get_db() as db:
        # Exact match, no case folding: a login is a credential and is compared
        # byte-for-byte, like the password and the session token. Stored
        # usernames are canonicalised to lowercase at creation time (see
        # create_manufacturer / _assign_retailer_login / _vastra_username), so
        # in practice the username must be typed lowercase. The MySQL column
        # carries COLLATE utf8mb4_bin so the database agrees.
        row = db.execute(
            "SELECT * FROM manufacturers WHERE username = ?",
            (body.username.strip(),),
        ).fetchone()
        if not row or not verify_password(body.password, row["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        if row["blocked"]:
            raise HTTPException(403, "Account is blocked")
        token = issue_token(db, row["id"])
    return {
        "token": token,
        "display_name": row["display_name"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
    }


@app.post("/auth/logout")
def logout(request: Request, user: dict = Depends(current_user)):
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    with get_db() as db:
        if token:
            db.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))
        # Security: after logout the DB holds no live credentials for this
        # account — the stored Vastra access_token goes too (a fresh one is
        # minted on the next OTP login; no-op for password/admin logins).
        db.execute(
            "UPDATE manufacturers SET vastra_access_token = NULL WHERE id = ?",
            (user["id"],))
    return {"ok": True}


@app.post("/auth/sso/manufacturer")
@limiter.limit(RL_LOGIN, key_func=get_remote_address)
def sso_manufacturer(request: Request, body: SsoIn):
    """SSO exchange for the Vastra App: verify a parent-signed assertion and mint
    a normal loyalty manufacturer token. The manufacturer must already exist
    (imported by Vastra), matched by external_id; unknown principals are rejected,
    never created. Returns the same body as /auth/login."""
    claims = verify_sso_assertion(body.assertion, "manufacturer")
    external_id = str(claims["sub"]).strip()
    with get_db() as db:
        row = db.execute(
            """SELECT id, username, display_name, is_admin, blocked
               FROM manufacturers WHERE external_id = ?""",
            (external_id,),
        ).fetchone()
        if not row:
            raise HTTPException(403, "Manufacturer not provisioned")
        if row["blocked"]:
            raise HTTPException(403, "Account is blocked")
        token = issue_token(db, row["id"])
    return {
        "token": token,
        "display_name": row["display_name"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
    }


# ---------- manufacturer auth via Vastra OTP ----------
# The manufacturer logs in with their existing Vastra credentials: Vastra
# texts an OTP to the organization's registered mobile, we verify it with
# Vastra server-side, then find-or-create the manufacturer by
# external_id = organization_Id (first login auto-provisions — no second
# account) and mint the same opaque token /auth/login issues.

class VastraOtpSendIn(BaseModel):
    mobile: str = Field(min_length=8, max_length=15, pattern=r"^[0-9]+$")
    country_code: str = Field(default="+91", min_length=2, max_length=5,
                              pattern=r"^\+[0-9]+$")
    is_resend: int = Field(default=0, ge=0, le=1)


class VastraOtpVerifyIn(BaseModel):
    mobile: str = Field(min_length=8, max_length=15, pattern=r"^[0-9]+$")
    country_code: str = Field(default="+91", min_length=2, max_length=5,
                              pattern=r"^\+[0-9]+$")
    otp: str = Field(min_length=3, max_length=10, pattern=r"^[0-9]+$")


def _vastra_username(db, org: dict, external_id: str) -> str:
    """Derive a unique panel username for an auto-provisioned manufacturer
    (mirrors _assign_retailer_login: readable base, id-suffix on clash)."""
    raw = org.get("org_url") or org.get("organization_name") or ""
    base = "".join(c for c in raw.lower() if c.isalnum()) or f"vastra{external_id}"
    username = base
    if db.execute("SELECT 1 FROM manufacturers WHERE username = ?",
                  (username,)).fetchone():
        username = f"{base}{external_id}"
    return username


@app.post("/auth/vastra/send-otp")
@limiter.limit(RL_LOGIN, key_func=get_remote_address)
def vastra_send_otp(request: Request, body: VastraOtpSendIn):
    """Step 1: have Vastra text an OTP to the org's registered mobile."""
    try:
        message = send_login_otp(body.country_code, body.mobile, body.is_resend)
    except VastraRejection as exc:
        raise HTTPException(403, exc.message)
    except VastraApiError as exc:
        raise HTTPException(502, f"Vastra login service unavailable: {exc}")
    return {"ok": True, "message": message}


@app.post("/auth/vastra/verify-otp")
@limiter.limit(RL_LOGIN, key_func=get_remote_address)
def vastra_verify_otp(request: Request, body: VastraOtpVerifyIn):
    """Step 2: verify the OTP with Vastra, log the manufacturer in.
    Stores Vastra's access_token server-side (used to pull the org's design
    list; wiped again on logout). Returns the same body as /auth/login."""
    try:
        org = verify_login_otp(body.country_code, body.mobile, body.otp)
    except VastraRejection as exc:
        raise HTTPException(401, exc.message)
    except VastraApiError as exc:
        raise HTTPException(502, f"Vastra login service unavailable: {exc}")
    external_id = str(org["organization_Id"]).strip()
    display_name = ((org.get("organization_name") or "").strip()
                    or f"Vastra org {external_id}")
    with get_db() as db:
        row = db.execute(
            """SELECT id, username, is_admin, blocked
               FROM manufacturers WHERE external_id = ?""",
            (external_id,),
        ).fetchone()
        if row and row["blocked"]:
            raise HTTPException(403, "Account is blocked")
        if row:
            mid, username = row["id"], row["username"]
            is_admin = bool(row["is_admin"])
            db.execute(
                """UPDATE manufacturers
                   SET display_name = ?, vastra_access_token = ?
                   WHERE id = ?""",
                (display_name, org["access_token"], mid))
        else:
            username = _vastra_username(db, org, external_id)
            # Random throwaway password: OTP-provisioned accounts log in via
            # Vastra only; nobody ever knows this password.
            cur = db.execute(
                """INSERT INTO manufacturers
                   (username, password_hash, display_name, external_id,
                    vastra_access_token)
                   VALUES (?, ?, ?, ?, ?)""",
                (username, hash_password(new_temp_password()), display_name,
                 external_id, org["access_token"]))
            mid, is_admin = cur.lastrowid, False
        token = issue_token(db, mid)
    return {
        "token": token,
        "display_name": display_name,
        "username": username,
        "is_admin": is_admin,
    }


# ---------- retailer auth (YourApp side) ----------

@app.post("/auth/retailer/login")
@limiter.limit(RL_LOGIN, key_func=get_remote_address)
def retailer_login(request: Request, body: LoginIn):
    with get_db() as db:
        # Exact match — see the note in login() above.
        row = db.execute(
            "SELECT * FROM retailers WHERE username = ?",
            (body.username.strip(),),
        ).fetchone()
        if (not row or not row["password_hash"]
                or not verify_password(body.password, row["password_hash"])):
            raise HTTPException(401, "Invalid username or password")
        if row["blocked"]:
            raise HTTPException(403, "Account is blocked")
        token = issue_retailer_token(db, row["id"])
        manufacturer = db.execute(
            "SELECT display_name FROM manufacturers WHERE id = ?",
            (row["manufacturer_id"],),
        ).fetchone()
    return {
        "token": token,
        "retailer_id": row["id"],
        "shop_name": row["shop_name"],
        "name": row["name"],
        "region": row["region"],
        "manufacturer": manufacturer["display_name"] if manufacturer else None,
        # Set while the retailer is still on the auto-generated password. It is
        # advisory: the webview shows a reminder pointing at the burger menu's
        # Change password — it never forces a reset or blocks login.
        "must_change": bool(row["must_change"]),
    }


@app.post("/auth/sso/retailer")
@limiter.limit(RL_LOGIN, key_func=get_remote_address)
def sso_retailer(request: Request, body: SsoIn):
    """SSO exchange for YourApp: verify a parent-signed assertion and mint a normal
    loyalty retailer token. The retailer must already be provisioned by its
    manufacturer (with external_id); unknown retailers are rejected, never created.
    manufacturer_external_id scopes the lookup to the correct tenant, so one
    parent's id space can't resolve into another. Same body as /auth/retailer/login
    (minus must_change — SSO does not use a loyalty password)."""
    claims = verify_sso_assertion(body.assertion, "retailer")
    external_id = str(claims["sub"]).strip()
    manuf_external = str(claims.get("manufacturer_external_id") or "").strip()
    if not manuf_external:
        raise HTTPException(401, "Assertion missing manufacturer_external_id")
    with get_db() as db:
        manuf = db.execute(
            "SELECT id, display_name FROM manufacturers WHERE external_id = ?",
            (manuf_external,),
        ).fetchone()
        if not manuf:
            raise HTTPException(403, "Manufacturer not provisioned")
        row = db.execute(
            """SELECT * FROM retailers
               WHERE manufacturer_id = ? AND external_id = ?""",
            (manuf["id"], external_id),
        ).fetchone()
        if not row:
            raise HTTPException(403, "Retailer not provisioned")
        if row["blocked"]:
            raise HTTPException(403, "Account is blocked")
        token = issue_retailer_token(db, row["id"])
    return {
        "token": token,
        "retailer_id": row["id"],
        "shop_name": row["shop_name"],
        "name": row["name"],
        "region": row["region"],
        "manufacturer": manuf["display_name"],
    }


class RetailerPasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


@app.post("/retailer/password")
def change_retailer_password(body: RetailerPasswordIn,
                             retailer: dict = Depends(current_retailer)):
    """Retailer sets a new password from the webview burger menu. Verifies the
    current one, stores only the PBKDF2 hash, and clears the must_change flag
    (which drops the default-password reminder). Always voluntary."""
    if body.new_password == body.current_password:
        raise HTTPException(422, "New password must differ from the current one")
    with get_db() as db:
        row = db.execute(
            "SELECT password_hash FROM retailers WHERE id = ?",
            (retailer["id"],),
        ).fetchone()
        if (not row or not row["password_hash"]
                or not verify_password(body.current_password,
                                       row["password_hash"])):
            raise HTTPException(401, "Current password is incorrect")
        db.execute(
            "UPDATE retailers SET password_hash = ?, must_change = 0 "
            "WHERE id = ?",
            (hash_password(body.new_password), retailer["id"]),
        )
    return {"ok": True}


@app.post("/auth/retailer/logout")
def retailer_logout(request: Request,
                    retailer: dict = Depends(current_retailer)):
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    with get_db() as db:
        if token:
            db.execute(
                "DELETE FROM retailer_tokens WHERE token = ?", (token,))
    return {"ok": True}


@app.get("/retailer/me")
def retailer_me(retailer: dict = Depends(current_retailer)):
    with get_db() as db:
        balance = _balance(db, retailer["id"])
        manufacturer = db.execute(
            "SELECT display_name FROM manufacturers WHERE id = ?",
            (retailer["manufacturer_id"],),
        ).fetchone()
    return {
        "retailer_id": retailer["id"],
        "shop_name": retailer["shop_name"],
        "name": retailer["name"],
        "region": retailer["region"],
        "manufacturer": manufacturer["display_name"] if manufacturer else None,
        "location_source": retailer["location_source"],
        "balance": balance,
        "must_change": bool(retailer["must_change"]),
    }


@app.get("/auth/me")
def me(user: dict = Depends(current_user)):
    return user


# ---------- super admin: manufacturer accounts ----------

@app.post("/admin/manufacturers", status_code=201)
def create_manufacturer(body: ManufacturerIn,
                        admin: dict = Depends(current_admin)):
    with get_db() as db:
        try:
            cur = db.execute(
                """INSERT INTO manufacturers
                   (username, password_hash, display_name)
                   VALUES (?, ?, ?)""",
                (body.username.strip().lower(), hash_password(body.password),
                 body.display_name),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(409, "Username already taken")
            raise
        row = db.execute(
            """SELECT id, username, display_name, created_at
               FROM manufacturers WHERE id = ?""",
            (cur.lastrowid,),
        ).fetchone()
    return dict(row)


@app.get("/admin/manufacturers")
def list_manufacturers(admin: dict = Depends(current_admin)):
    with get_db() as db:
        rows = db.execute(
            """SELECT m.id, m.username, m.display_name, m.created_at,
                      (SELECT COUNT(*) FROM products p
                       WHERE p.manufacturer_id = m.id) AS products,
                      (SELECT COUNT(*) FROM retailers r
                       WHERE r.manufacturer_id = m.id) AS retailers,
                      (SELECT COUNT(*) FROM points_ledger l
                       WHERE l.manufacturer_id = m.id) AS scans
               FROM manufacturers m WHERE m.is_admin = 0 ORDER BY m.id"""
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- products (manufacturer-scoped) ----------
# CRUD is gone: the catalog lives in product_points now (see /catalog/products
# below). GET /products stays for legacy readers (Schemes, Claims, and the
# /web/generate demo webview) that still key off the local table's rows —
# no new rows are written to it going forward.

@app.get("/products")
def list_products(user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        rows = db.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM points_ledger l
                       WHERE l.product_id = p.id) AS scans
               FROM products p WHERE p.manufacturer_id = ? ORDER BY p.id""",
            (user["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- product catalog (manufacturer-scoped) ----------
# v1: the catalog is the manufacturer's OWN CSV import, stored in product_points
# with source = 'import'. Vastra's product API is deliberately NOT a source here
# — get-design-ids returns no product names, so it could only ever supply design
# numbers. Vastra OTP *login* is a separate path and is unaffected by any of
# this (it still stores vastra_access_token for a future reconnect).
#
# Resolution order in GET /catalog/products:
#   1. imported rows        -> the catalog
#   2. USE_SAMPLE_PRODUCTS  -> the hardcoded samples (testing only)
#   3. []                   -> the panel prompts for an import
# Never merged: one import and the samples are gone for that manufacturer.

# Testing fallback so the QR-generate → scan → redeem flow works without
# importing a CSV first. Off by default so a new deployment is go-live safe:
# a manufacturer with an empty catalog is prompted to import rather than shown
# three demo products they never created. Set USE_SAMPLE_PRODUCTS=1 for demos.
USE_SAMPLE_PRODUCTS = _os.environ.get("USE_SAMPLE_PRODUCTS", "0") == "1"

_SAMPLE_PRODUCTS = [
    {"external_id": "SAMPLE-001", "name": "Banarasi Silk Saree",
     "sku": "BNS-SILK-01", "points": 100},
    {"external_id": "SAMPLE-002", "name": "Cotton Handloom Kurta",
     "sku": "CTN-KURTA-02", "points": 50},
    {"external_id": "SAMPLE-003", "name": "Chanderi Dupatta",
     "sku": "CHN-DUP-03", "points": 75},
]

# Accepted spellings for the two required columns, after _norm_header(). The
# manufacturer's export is whatever their other system produces, so match
# generously rather than dictating a format.
# p_name/p_code cover the "P-Name"/"P-Code" abbreviations the client's product
# list uses on its filter row, in case its export writes those as headers.
_CSV_NAME_HEADERS = ("name", "product_name", "p_name", "item_name",
                     "design_name", "product")
_CSV_CODE_HEADERS = ("code", "product_code", "p_code", "sku", "item_code",
                     "design_number", "style_code", "article_code")
# retailer_points is what the client's product export calls the loyalty value
# (their file also carries distributor_points, which is not ours to read).
_CSV_POINTS_HEADERS = ("points", "loyalty_points", "points_per_scan",
                       "retailer_points")
# Row-number columns are a render index that re-numbers on every export; they
# carry no meaning once imported. "" catches headers like "#".
_CSV_IGNORED_HEADERS = ("", "sno", "s_no", "sr_no", "serial", "no")
# Source systems export the literal text "null" for empty cells — keep that
# word out of the manufacturer's table.
_CSV_NULLISH = {"null", "none", "n/a", "na", "-", "--"}

# Accepted spellings for the retailer / distributor / gift columns, matched the
# same generous way as the product catalog: every one of these lists is exported
# from whatever system the manufacturer already runs, so "Mobile" has to land in
# `phone` and "city" in `region` without anyone hand-editing a header row first.
#
# Within a list, earlier aliases win (see _pick_col) — that is how a retailer
# file carrying both "Shop Name" and "Name" resolves each to the right field.
_CSV_PHONE_HEADERS = ("phone", "mobile", "mobile_no", "mobile_number",
                      "phone_no", "phone_number", "contact",
                      "contact_no", "contact_number", "whatsapp",
                      "whatsapp_number")
# City before state: the client's retailer export carries both, and the city is
# the more precise pin for geo.coords_for.
_CSV_REGION_HEADERS = ("region", "city", "state", "area", "district",
                       "zone", "territory", "location")

_CSV_DIST_NAME_HEADERS = ("name", "distributor_name", "distributor",
                          "party_name", "firm_name", "agency_name",
                          "company_name", "shop_name")

# A retailer row needs a shop *or* a person — per row, not just per file. Files
# carrying only "Name" use it for both; files carrying both columns (the client's
# export does) leave the firm blank for retailers trading under their own name,
# and those rows fall back to the person rather than being rejected.
_CSV_RET_SHOP_HEADERS = ("shop_name", "shop", "store_name", "firm_name",
                         "business_name", "party_name", "name")
_CSV_RET_NAME_HEADERS = ("name", "owner_name", "owner", "contact_person",
                         "person_name", "retailer_name", "proprietor")
_CSV_RET_ADDRESS_HEADERS = ("address", "address1", "address_1", "street",
                            "street_address", "shop_address", "addr")
_CSV_RET_DISTRIBUTOR_HEADERS = ("distributor", "distributor_name", "dealer",
                                "dealer_name", "agency", "agency_name",
                                "supplier", "supplier_name", "wholesaler",
                                "stockist", "parent", "parent_name",
                                "mapped_distributor", "assigned_distributor",
                                "under", "belongs_to")
# The retailer's *current wallet balance* as the manufacturer's system reports
# it — "Point Balance" is what the client's export calls it. Imported as one
# opening-balance ledger entry, not as scan history (see import_retailers_csv).
# Balance spellings come first so a file carrying both a balance and some other
# "points" column credits the balance.
_CSV_RET_POINTS_HEADERS = ("point_balance", "points_balance", "balance",
                           "current_points", "total_points", "wallet_balance",
                           "wallet", "opening_balance", "points", "point")
# Deliberately narrow: external_id is the SSO identity key, so it is only ever
# read from a column that says so outright — never guessed from an "id" column.
_CSV_RET_EXTERNAL_ID_HEADERS = ("external_id", "externalid")

_CSV_GIFT_NAME_HEADERS = ("name", "gift_name", "reward_name", "product_name",
                          "item_name", "product", "title")
_CSV_GIFT_POINTS_HEADERS = ("points_cost", "product_points", "points",
                            "points_required", "redeem_points",
                            "loyalty_points")
_CSV_GIFT_DESC_HEADERS = ("description", "desc", "details", "subtitle",
                          "sub_title")
_CSV_GIFT_IMAGE_HEADERS = ("image_url", "image", "images", "photo", "picture",
                           "img", "image_link")


def _norm_header(h: str) -> str:
    """'Product Name' / 'PRODUCT_NAME' / 'Product  Name' -> 'product_name'."""
    squashed = "".join(c if c.isalnum() else "_" for c in (h or "").strip().lower())
    return "_".join(p for p in squashed.split("_") if p)


def _pick_col(fields: list[str], norm: dict, aliases) -> str | None:
    """The CSV field matching the earliest alias in `aliases`.

    Alias order beats column order on purpose: a file carrying both "Name" and
    "Shop Name" must resolve to "Name" whichever way round its columns sit."""
    for alias in aliases:
        for f in fields:
            if norm[f] == alias:
                return f
    return None


def _clean_cell(v) -> str:
    s = (v or "").strip()
    return "" if s.lower() in _CSV_NULLISH else s


def _parse_points(v) -> int | None:
    """Whole points from a CSV cell, or None if it isn't a number.

    Balances export as '14.00', '1,005.00' or '₹ 500' depending on the source
    system; the ledger stores whole points, so the decimal is rounded away."""
    s = _clean_cell(v).replace(",", "").replace("₹", "").replace("+", "").strip()
    if not s:
        return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def _first_image(cell: str) -> str:
    """One image URL out of an image cell.

    The client's reward export writes the column as a JSON array of URLs
    (`["https://…"]`) because their system allows several photos per reward;
    loyalty stores a single `gifts.image_url`, so the first usable entry wins.
    A plain URL is passed through untouched."""
    s = _clean_cell(cell)
    if not s.startswith("["):
        return s
    import json
    try:
        items = json.loads(s)
    except ValueError:
        return ""
    for item in items if isinstance(items, list) else []:
        url = _clean_cell(item if isinstance(item, str) else "")
        # Their exporter writes the literal string "undefined" for a missing
        # image, which would render as a broken thumbnail in the shop.
        if url and url.lower() != "undefined":
            return url
    return ""


def _parse_catalog_csv(text: str) -> dict:
    """Parse a product CSV. Returns rows ready for product_points plus the
    free-form column list (original header text, in CSV order).

    A file missing a required column is rejected whole (422) rather than
    half-imported — a catalog of nameless products can't be printed on a
    sticker."""
    import csv as csvmod
    import io

    reader = csvmod.DictReader(io.StringIO(text))
    fields = [f for f in (reader.fieldnames or []) if (f or "").strip()]
    norm = {f: _norm_header(f) for f in fields}
    name_col = _pick_col(fields, norm, _CSV_NAME_HEADERS)
    code_col = _pick_col(fields, norm, _CSV_CODE_HEADERS)
    missing = []
    if not name_col:
        missing.append("a product name column (one of: "
                       + ", ".join(_CSV_NAME_HEADERS) + ")")
    if not code_col:
        missing.append("a product code column (one of: "
                       + ", ".join(_CSV_CODE_HEADERS) + ")")
    if missing:
        raise HTTPException(422, "CSV is missing " + " and ".join(missing))
    points_col = _pick_col(fields, norm, _CSV_POINTS_HEADERS)
    attr_cols = [f for f in fields
                 if f not in (name_col, code_col, points_col)
                 and norm[f] not in _CSV_IGNORED_HEADERS]

    by_code: dict[str, dict] = {}
    errors: list[str] = []
    skipped = 0
    for i, raw in enumerate(reader, start=2):  # row 1 is the header
        name = _clean_cell(raw.get(name_col))
        code = _clean_cell(raw.get(code_col))
        if not name or not code:
            skipped += 1
            errors.append(f"row {i}: missing product "
                          f"{'name' if not name else 'code'}")
            continue
        points = 0
        if points_col:
            cell = _clean_cell(raw.get(points_col))
            if cell:
                try:
                    points = int(float(cell))
                except ValueError:
                    skipped += 1
                    errors.append(f"row {i}: points '{cell}' is not a number")
                    continue
        if code in by_code:
            errors.append(f"row {i}: duplicate product code '{code}' "
                          f"— the last row wins")
        by_code[code] = {
            "external_id": code, "name": name, "sku": code, "points": points,
            "attrs": {c: _clean_cell(raw.get(c)) for c in attr_cols},
        }
    return {"rows": list(by_code.values()), "columns": attr_cols,
            "errors": errors, "skipped": skipped,
            "has_points": bool(points_col)}


def _imported_catalog(db, mid: int) -> tuple[list[dict], list[str]]:
    """The manufacturer's imported products + the union of their attr columns,
    in first-seen order."""
    rows = db.execute(
        """SELECT product_external_id, name, sku, points, attrs
           FROM product_points
           WHERE manufacturer_id = ? AND source = 'import'
           ORDER BY name, product_external_id""", (mid,)).fetchall()
    products: list[dict] = []
    columns: list[str] = []
    for r in rows:
        try:
            attrs = json.loads(r["attrs"]) if r["attrs"] else {}
        except ValueError:
            attrs = {}
        for key in attrs:
            if key not in columns:
                columns.append(key)
        products.append({
            "external_id": r["product_external_id"], "name": r["name"],
            "sku": r["sku"], "points": r["points"], "attrs": attrs,
        })
    return products, columns


@app.get("/catalog/products")
def list_catalog_products(user: dict = Depends(current_manufacturer)):
    """The manufacturer's catalog: imported rows, else samples, else empty.
    `columns` is the free-form CSV column list the panel renders between the
    fixed name/code columns and Points."""
    with get_db() as db:
        products, columns = _imported_catalog(db, user["id"])
        if products:
            return {"products": products, "columns": columns,
                    "source": "import"}
        if not USE_SAMPLE_PRODUCTS:
            return {"products": [], "columns": [], "source": "empty"}
        # Points edited on a sample are stored as a legacy (source IS NULL)
        # override, so editing one never turns it into a catalog row.
        overrides = {
            r["product_external_id"]: r["points"]
            for r in db.execute(
                "SELECT product_external_id, points FROM product_points "
                "WHERE manufacturer_id = ?", (user["id"],))
        }
    return {
        "products": [{**p, "attrs": {},
                      "points": overrides.get(p["external_id"], p["points"])}
                     for p in _SAMPLE_PRODUCTS],
        "columns": [], "source": "sample",
    }


class CatalogImportIn(BaseModel):
    csv: str = Field(min_length=1, description="Raw CSV text")
    mode: str = Field("upsert", pattern="^(upsert|replace)$")


@app.post("/catalog/products/import")
@limiter.limit(RL_IMPORT)
def import_catalog_csv(request: Request, body: CatalogImportIn,
                       user: dict = Depends(current_manufacturer)):
    """Import the product catalog from CSV text (sent as JSON, no multipart).

    Required columns: a product name and a product code, in any of the
    spellings above and any case. `points` is optional. Every other column is
    kept verbatim and shown in the panel. The product code is the identity.

    mode=upsert  — refresh matching codes, add new ones, delete nothing.
                   Points set in the panel survive unless the CSV carries a
                   points column.
    mode=replace — drop this manufacturer's imported rows first.

    Neither mode affects already-issued QR codes: qr_batches and points_ledger
    carry immutable product name/sku snapshots."""
    parsed = _parse_catalog_csv(body.csv)
    mid = user["id"]
    created = updated = 0
    with get_db() as db:
        if body.mode == "replace":
            db.execute(
                "DELETE FROM product_points WHERE manufacturer_id = ? "
                "AND source = 'import'", (mid,))
        existing = {
            r["product_external_id"]: r["points"] for r in db.execute(
                "SELECT product_external_id, points FROM product_points "
                "WHERE manufacturer_id = ? AND source = 'import'", (mid,))
        }
        for row in parsed["rows"]:
            eid = row["external_id"]
            points = (row["points"] if parsed["has_points"]
                      else existing.get(eid, row["points"]))
            attrs = json.dumps(row["attrs"], ensure_ascii=False)
            if eid in existing:
                db.execute(
                    """UPDATE product_points
                       SET name = ?, sku = ?, attrs = ?, points = ?,
                           source = 'import', updated_at = datetime('now')
                       WHERE manufacturer_id = ? AND product_external_id = ?""",
                    (row["name"], row["sku"], attrs, points, mid, eid))
                updated += 1
            else:
                # A legacy override for the same external id would collide on
                # the primary key; promote it into a real catalog row instead
                # of failing the whole import.
                db.execute(
                    "DELETE FROM product_points WHERE manufacturer_id = ? "
                    "AND product_external_id = ?", (mid, eid))
                db.execute(
                    """INSERT INTO product_points
                       (manufacturer_id, product_external_id, points, name,
                        sku, attrs, source)
                       VALUES (?, ?, ?, ?, ?, ?, 'import')""",
                    (mid, eid, points, row["name"], row["sku"], attrs))
                created += 1
    return {"created": created, "updated": updated,
            "skipped": parsed["skipped"], "errors": parsed["errors"],
            "columns": parsed["columns"]}


@app.delete("/catalog/products")
def delete_catalog(user: dict = Depends(current_manufacturer)):
    """Clear the whole imported catalog (the panel's "Delete all", behind a
    confirmation). Legacy source IS NULL rows are left alone, and already-issued
    QR codes keep working from their own snapshots."""
    mid = user["id"]
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM product_points "
            "WHERE manufacturer_id = ? AND source = 'import'", (mid,)).fetchone()
        db.execute(
            "DELETE FROM product_points WHERE manufacturer_id = ? "
            "AND source = 'import'", (mid,))
    return {"deleted": row["n"]}


@app.delete("/catalog/products/{external_id}", status_code=204)
def delete_catalog_product(external_id: str,
                           user: dict = Depends(current_manufacturer)):
    """Remove one imported product. Already-printed QR batches keep working —
    they carry their own product name/sku snapshot."""
    with get_db() as db:
        row = db.execute(
            "SELECT 1 FROM product_points WHERE manufacturer_id = ? "
            "AND product_external_id = ? AND source = 'import'",
            (user["id"], external_id)).fetchone()
        if not row:
            raise HTTPException(404, "Product not found")
        db.execute(
            "DELETE FROM product_points WHERE manufacturer_id = ? "
            "AND product_external_id = ? AND source = 'import'",
            (user["id"], external_id))
    return None


@app.put("/catalog/products/{external_id}/points")
def set_product_points(external_id: str, body: ProductPointsIn,
                       user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        row = db.execute(
            "SELECT 1 FROM product_points WHERE manufacturer_id = ? "
            "AND product_external_id = ?", (user["id"], external_id),
        ).fetchone()
        if row:
            db.execute(
                "UPDATE product_points SET points = ?, "
                "updated_at = datetime('now') WHERE manufacturer_id = ? "
                "AND product_external_id = ?",
                (body.points, user["id"], external_id),
            )
        else:
            db.execute(
                "INSERT INTO product_points "
                "(manufacturer_id, product_external_id, points) "
                "VALUES (?, ?, ?)",
                (user["id"], external_id, body.points),
            )
    return {"external_id": external_id, "points": body.points}


# ---------- retailers (manufacturer-scoped) ----------

@app.post("/retailers", status_code=201)
def create_retailer(body: RetailerIn, user: dict = Depends(current_manufacturer)):
    region = (body.region or "").strip()
    lat, lng = body.lat, body.lng
    source = "gps" if (lat is not None and lng is not None) else None
    if (lat is None or lng is None) and region:
        coords = coords_for(region)
        if coords:
            lat, lng = coords
            source = "city"
    external_id = (body.external_id or "").strip() or None
    with get_db() as db:
        distributor_id = _distributor_id_for(db, user["id"], body.distributor_id)
        try:
            cur = db.execute(
                """INSERT INTO retailers
                   (manufacturer_id, name, shop_name, region, phone, lat, lng,
                    location_source, distributor_id, external_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user["id"], body.name, body.shop_name, region, body.phone,
                 lat, lng, source, distributor_id, external_id),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(409, "external_id already in use")
            raise
        rid = cur.lastrowid
        username, password = _assign_retailer_login(db, body.shop_name, rid)
        row = db.execute(
            "SELECT * FROM retailers WHERE id = ?", (rid,)
        ).fetchone()
    out = _clean_retailer(row)
    # Plaintext password is returned only here, at creation, so the panel can
    # show the manufacturer the credentials to hand to the retailer.
    out["login_username"] = username
    out["login_password"] = password
    return out


def _assign_retailer_login(db, shop_name: str, rid: int) -> tuple[str, str]:
    """Derive and store a retailer login: username = first alphanumeric word of
    the shop name (lowercased); password = ``<username>123``. A username
    clash gets the id appended so the UNIQUE constraint always holds. The login
    is flagged must_change=1 — a *reminder* flag only: the webview shows a
    "you're still on the default password" notice linking to the burger menu's
    Change password, and never blocks the retailer from using the app.
    Returns (username, plaintext temporary pw) — surfaced once at creation so
    the panel can show the manufacturer the credentials to hand over."""
    first = (shop_name.split() or ["shop"])[0].lower()
    base = "".join(ch for ch in first if ch.isalnum()) or "shop"
    username = base
    if db.execute("SELECT 1 FROM retailers WHERE username = ?",
                  (username,)).fetchone():
        username = f"{base}{rid}"
    password = f"{username}123"
    db.execute(
        "UPDATE retailers SET username = ?, password_hash = ?, must_change = 1 "
        "WHERE id = ?",
        (username, hash_password(password), rid),
    )
    return username, password


def _find_or_create_distributor(db, mid: int, name: str) -> int | None:
    """Resolve a distributor by name within a manufacturer, creating it if new.
    Returns its id, or None when the name is blank."""
    name = (name or "").strip()
    if not name:
        return None
    row = db.execute(
        """SELECT id FROM distributors
           WHERE manufacturer_id = ? AND LOWER(name) = LOWER(?)""",
        (mid, name),
    ).fetchone()
    if row:
        return row["id"]
    cur = db.execute(
        "INSERT INTO distributors (manufacturer_id, name) VALUES (?, ?)",
        (mid, name),
    )
    return cur.lastrowid


def _distributor_id_for(db, mid: int, distributor_id):
    """Validate that a distributor_id (if given) belongs to this manufacturer."""
    if distributor_id is None:
        return None
    ok = db.execute(
        "SELECT 1 FROM distributors WHERE id = ? AND manufacturer_id = ?",
        (distributor_id, mid),
    ).fetchone()
    if not ok:
        raise HTTPException(400, "Distributor not found")
    return distributor_id


def _clean_retailer(row) -> dict:
    """Drop the password hash before returning a retailer to the panel."""
    d = dict(row)
    d.pop("password_hash", None)
    d["has_login"] = bool(d.get("username"))
    return d


@app.get("/retailers")
def list_retailers(user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        rows = db.execute(
            """SELECT r.*,
                      d.name AS distributor_name,
                      (SELECT COUNT(*) FROM points_ledger l
                       WHERE l.retailer_id = r.id AND l.entry_type = 'scan')
                          AS scans,
                      (SELECT COALESCE(SUM(points), 0) FROM points_ledger l
                       WHERE l.retailer_id = r.id) AS points
               FROM retailers r
               LEFT JOIN distributors d ON d.id = r.distributor_id
               WHERE r.manufacturer_id = ? ORDER BY r.id""",
            (user["id"],),
        ).fetchall()
    return [_clean_retailer(r) for r in rows]


class RetailerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    shop_name: str | None = Field(default=None, min_length=1, max_length=200)
    region: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    # Nullable on purpose: sending an explicit null unassigns the distributor.
    distributor_id: int | None = Field(default=None)


@app.patch("/retailers/{retailer_id}")
def update_retailer(retailer_id: int, body: RetailerUpdate,
                    user: dict = Depends(current_manufacturer)):
    # distributor_id is handled separately so an explicit null can unassign it
    # (the None-filter below would otherwise drop it).
    fields = {k: v for k, v in body.model_dump().items()
              if v is not None and k != "distributor_id"}
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM retailers WHERE id = ? AND manufacturer_id = ?",
            (retailer_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Retailer not found")
        if "distributor_id" in body.model_fields_set:
            fields["distributor_id"] = _distributor_id_for(
                db, user["id"], body.distributor_id)
        if not fields:
            raise HTTPException(422, "Nothing to update")
        # Region change re-resolves the map position unless an exact GPS
        # location is already locked.
        if ("region" in fields and fields["region"] != row["region"]
                and row["location_source"] != "gps"):
            coords = coords_for(fields["region"])
            fields["lat"], fields["lng"] = coords if coords else (None, None)
            fields["location_source"] = "city" if coords else None
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(
            f"UPDATE retailers SET {sets} WHERE id = ?",
            [*fields.values(), retailer_id],
        )
        out = db.execute(
            "SELECT * FROM retailers WHERE id = ?", (retailer_id,)
        ).fetchone()
    return _clean_retailer(out)


@app.delete("/retailers/{retailer_id}", status_code=204)
def delete_retailer(retailer_id: int,
                    user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM retailers WHERE id = ? AND manufacturer_id = ?",
            (retailer_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Retailer not found")
        # An untouched imported opening balance is not history — it is part of
        # the import being undone, so it does not protect the row (and goes
        # with it). Anything else — a scan, a claim, a manual adjustment —
        # does: those rows and claims need to keep an owner.
        scans = db.execute(
            """SELECT COUNT(*) AS n FROM points_ledger
               WHERE retailer_id = ? AND entry_type <> 'import_opening'""",
            (retailer_id,),
        ).fetchone()["n"]
        if scans:
            raise HTTPException(
                409, "Retailer has scan history; cannot delete "
                     "(claims would lose their owner)")
        db.execute("DELETE FROM points_ledger WHERE retailer_id = ?",
                   (retailer_id,))
        db.execute("DELETE FROM retailers WHERE id = ?", (retailer_id,))


@app.delete("/retailers")
def delete_all_retailers(user: dict = Depends(current_manufacturer)):
    """Clear the customer list (the panel's "Delete all", behind a
    confirmation).

    Retailers with scan history are kept, not deleted — their ledger rows and
    claims have to keep pointing at an owner, which is the same rule the
    single-retailer delete enforces with a 409. An `import_opening` row is the
    one exception: a balance carried over by the very import being undone is
    not history, so it is deleted along with its retailer — otherwise a bad
    customer-list import would be un-undoable. Logins of the deleted retailers
    go with them (retailer_tokens cascades). Returns how many of each, so the
    panel can say why a few rows survived."""
    mid = user["id"]
    with get_db() as db:
        total = db.execute(
            "SELECT COUNT(*) AS n FROM retailers WHERE manufacturer_id = ?",
            (mid,)).fetchone()["n"]
        # Resolved to ids in Python: MySQL cannot reference the table a DELETE
        # targets from a subquery inside the same statement.
        doomed = [r["id"] for r in db.execute(
            """SELECT r.id FROM retailers r
               WHERE r.manufacturer_id = ?
                 AND NOT EXISTS (SELECT 1 FROM points_ledger l
                                 WHERE l.retailer_id = r.id
                                   AND l.entry_type <> 'import_opening')""",
            (mid,))]
        for i in range(0, len(doomed), 500):
            chunk = doomed[i:i + 500]
            qs = ",".join("?" for _ in chunk)
            db.execute(
                f"DELETE FROM points_ledger WHERE retailer_id IN ({qs})", chunk)
            db.execute(
                f"DELETE FROM retailers WHERE id IN ({qs})", chunk)
    return {"deleted": len(doomed), "skipped": total - len(doomed)}


def _balance(db, retailer_id: int) -> int:
    return db.execute(
        "SELECT COALESCE(SUM(points), 0) AS total FROM points_ledger"
        " WHERE retailer_id = ?",
        (retailer_id,),
    ).fetchone()["total"]


class AdjustIn(BaseModel):
    points: int = Field(description="Positive to add, negative to remove")
    note: str = Field(min_length=1, max_length=300)


@app.post("/retailers/{retailer_id}/adjust")
def adjust_points(retailer_id: int, body: AdjustIn,
                  user: dict = Depends(current_manufacturer)):
    """Manual correction by the manufacturer. Cannot push a wallet below 0."""
    if body.points == 0:
        raise HTTPException(422, "points must be non-zero")
    with get_db() as db:
        r = db.execute(
            "SELECT id FROM retailers WHERE id = ? AND manufacturer_id = ?",
            (retailer_id, user["id"]),
        ).fetchone()
        if not r:
            raise HTTPException(404, "Retailer not found")
        if _balance(db, retailer_id) + body.points < 0:
            raise HTTPException(409, "Adjustment would make the wallet negative")
        db.execute(
            """INSERT INTO points_ledger
               (manufacturer_id, retailer_id, entry_type, points, note,
                created_by)
               VALUES (?, ?, 'adjustment', ?, ?, ?)""",
            (user["id"], retailer_id, body.points, body.note, user["id"]),
        )
        return {"retailer_id": retailer_id, "balance": _balance(db, retailer_id)}


class TransferIn(BaseModel):
    from_retailer_id: int
    to_retailer_id: int
    points: int = Field(gt=0)
    note: str = Field(min_length=1, max_length=300)


@app.post("/retailers/transfer")
def transfer_points(body: TransferIn,
                    user: dict = Depends(current_manufacturer)):
    """Move points between two of the manufacturer's retailers (fixes a scan
    credited to the wrong shop)."""
    if body.from_retailer_id == body.to_retailer_id:
        raise HTTPException(422, "Cannot transfer to the same retailer")
    with get_db() as db:
        rows = db.execute(
            "SELECT id FROM retailers WHERE id IN (?, ?) AND manufacturer_id = ?",
            (body.from_retailer_id, body.to_retailer_id, user["id"]),
        ).fetchall()
        if len(rows) != 2:
            raise HTTPException(404, "Both retailers must belong to you")
        if _balance(db, body.from_retailer_id) < body.points:
            raise HTTPException(409, "Sender has insufficient points")
        db.execute(
            """INSERT INTO points_ledger
               (manufacturer_id, retailer_id, entry_type, points,
                counterparty_retailer_id, note, created_by)
               VALUES (?, ?, 'transfer', ?, ?, ?, ?)""",
            (user["id"], body.from_retailer_id, -body.points,
             body.to_retailer_id, body.note, user["id"]),
        )
        db.execute(
            """INSERT INTO points_ledger
               (manufacturer_id, retailer_id, entry_type, points,
                counterparty_retailer_id, note, created_by)
               VALUES (?, ?, 'transfer', ?, ?, ?, ?)""",
            (user["id"], body.to_retailer_id, body.points,
             body.from_retailer_id, body.note, user["id"]),
        )
        return {
            "from": {"id": body.from_retailer_id,
                     "balance": _balance(db, body.from_retailer_id)},
            "to": {"id": body.to_retailer_id,
                   "balance": _balance(db, body.to_retailer_id)},
        }


@app.get("/retailer/points")
def retailer_points(retailer: dict = Depends(current_retailer)):
    """Just the logged-in retailer's total points — nothing else."""
    with get_db() as db:
        return {"total_points": _balance(db, retailer["id"])}


@app.get("/retailer/wallet")
def retailer_wallet(retailer: dict = Depends(current_retailer)):
    """The logged-in retailer's balance and transaction history."""
    rid = retailer["id"]
    with get_db() as db:
        balance = _balance(db, rid)
        history = db.execute(
            """SELECT l.points, l.base_points, l.bonus_points, l.scanned_at,
                      l.entry_type, l.note,
                      l.product_name AS product_name,
                      l.product_sku AS sku, s.name AS scheme_name
               FROM points_ledger l
               LEFT JOIN schemes s ON s.id = l.scheme_id
               WHERE l.retailer_id = ? ORDER BY l.scanned_at DESC LIMIT 100""",
            (rid,),
        ).fetchall()
    return {
        "retailer_id": rid,
        "shop_name": retailer["shop_name"],
        "region": retailer["region"],
        "balance": balance,
        "history": [dict(h) for h in history],
    }


@app.get("/public/cities")
def public_cities():
    """Known place names for the add-customer autocomplete."""
    return known_places()


class LocationIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


def _refresh_retailer_pin(rid: int, lat: float, lng: float) -> dict:
    """Refresh the shop's exact pin, city, and street address from where the
    retailer actually scanned — **latest wins**, every scanning session. A wrong
    city entered at registration self-corrects to the real one, and the
    manufacturer gets a precise, visitable address. The address is reverse-
    geocoded best-effort; if that lookup fails we keep the previous address (the
    'View on map' link still works from the fresh coordinates). Shared by
    POST /retailer/location (webview) and /yourapp/scan (server-to-server)."""
    city = nearest_city(lat, lng)
    address = reverse_address(lat, lng)
    with get_db() as db:
        db.execute(
            """UPDATE retailers SET lat = ?, lng = ?, location_source = 'gps',
                   region = COALESCE(?, region), address = COALESCE(?, address)
               WHERE id = ?""",
            (lat, lng, city, address, rid),
        )
    return {"region": city, "address": address}


@app.post("/retailer/location")
def set_retailer_location(body: LocationIn,
                          retailer: dict = Depends(current_retailer)):
    out = _refresh_retailer_pin(retailer["id"], body.lat, body.lng)
    return {"updated": True, **out}


# ---------- distributors (manufacturer-scoped) ----------

class DistributorIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=20)
    region: str | None = Field(default=None, max_length=100)


class DistributorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=20)
    region: str | None = Field(default=None, max_length=100)


@app.post("/distributors", status_code=201)
def create_distributor(body: DistributorIn,
                       user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO distributors (manufacturer_id, name, phone, region)
               VALUES (?, ?, ?, ?)""",
            (user["id"], body.name.strip(), body.phone, body.region),
        )
        row = db.execute(
            "SELECT * FROM distributors WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


@app.get("/distributors")
def list_distributors(user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        rows = db.execute(
            """SELECT d.*,
                      (SELECT COUNT(*) FROM retailers r
                       WHERE r.distributor_id = d.id) AS retailers,
                      (SELECT COUNT(*) FROM points_ledger l
                       WHERE l.distributor_id = d.id
                         AND l.entry_type = 'scan') AS scans,
                      (SELECT COALESCE(SUM(points), 0) FROM points_ledger l
                       WHERE l.distributor_id = d.id
                         AND l.entry_type = 'scan') AS points
               FROM distributors d
               WHERE d.manufacturer_id = ? ORDER BY d.name""",
            (user["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.patch("/distributors/{distributor_id}")
def update_distributor(distributor_id: int, body: DistributorUpdate,
                       user: dict = Depends(current_manufacturer)):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(422, "Nothing to update")
    with get_db() as db:
        row = db.execute(
            "SELECT 1 FROM distributors WHERE id = ? AND manufacturer_id = ?",
            (distributor_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Distributor not found")
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(
            f"UPDATE distributors SET {sets} WHERE id = ?",
            [*fields.values(), distributor_id],
        )
        out = db.execute(
            "SELECT * FROM distributors WHERE id = ?", (distributor_id,)
        ).fetchone()
    return dict(out)


@app.delete("/distributors/{distributor_id}", status_code=204)
def delete_distributor(distributor_id: int,
                       user: dict = Depends(current_manufacturer)):
    """Deleting a distributor unlinks its retailers (they are NOT deleted).
    Past scans keep their recorded distributor_id (point-in-time history)."""
    with get_db() as db:
        row = db.execute(
            "SELECT 1 FROM distributors WHERE id = ? AND manufacturer_id = ?",
            (distributor_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Distributor not found")
        db.execute(
            """UPDATE retailers SET distributor_id = NULL
               WHERE distributor_id = ? AND manufacturer_id = ?""",
            (distributor_id, user["id"]),
        )
        db.execute("DELETE FROM distributors WHERE id = ?", (distributor_id,))


@app.delete("/distributors")
def delete_all_distributors(user: dict = Depends(current_manufacturer)):
    """Clear the distributor list (the panel's "Delete all", behind a
    confirmation). Like the single delete, retailers are only unlinked (never
    deleted) and past scans keep the distributor_id they recorded at scan
    time — so the dashboard's history stays intact."""
    mid = user["id"]
    with get_db() as db:
        n = db.execute("SELECT COUNT(*) AS n FROM distributors "
                       "WHERE manufacturer_id = ?", (mid,)).fetchone()["n"]
        db.execute("UPDATE retailers SET distributor_id = NULL "
                   "WHERE manufacturer_id = ?", (mid,))
        db.execute("DELETE FROM distributors WHERE manufacturer_id = ?", (mid,))
    return {"deleted": n}


class ImportIn(BaseModel):
    csv: str = Field(min_length=1, description="Raw CSV text")


@app.post("/retailers/import")
@limiter.limit(RL_IMPORT)
def import_retailers_csv(request: Request, body: ImportIn,
                         user: dict = Depends(current_manufacturer)):
    """Bulk-create retailers from CSV text.

    Headers are matched, not dictated (`_norm_header` + the `_CSV_RET_*` alias
    lists), so the manufacturer's own export lands in the right fields: "Mobile"
    fills `phone`, "city" fills `region` (in preference to "state"), "address1"
    fills `address`. Only a shop or name column is required — a file carrying
    just "Name" uses it for both the shop and the owner.

    **Phone is the dedupe key when present**, because phone is what identifies
    the retailer on /yourapp/scan; two different shops legitimately share an
    owner's first name, so falling back to the name would drop real rows. Rows
    with no phone at all dedupe on shop name instead. `external_id` (read only
    from a column that says so) is the parent-system id used by the SSO
    exchange and is unique per manufacturer. Each retailer gets an auto-login
    and the distributor is found-or-created by name and linked. Returns
    generated credentials so the panel can show them once.

    **A points/balance column carries the wallet over.** "Point Balance" (and
    the other `_CSV_RET_POINTS_HEADERS` spellings) is the retailer's current
    balance in the manufacturer's existing system, so it is written as a single
    `adjustment` ledger row — the wallet and the Customers tab's Points column
    pick it up, while `entry_type='scan'` analytics stay untouched (an imported
    balance is not scan history). Only *newly created* retailers get it: a row
    skipped as a duplicate is left alone, so re-importing a stale list can never
    double-credit or claw back points a retailer has since earned."""
    import csv as csvmod
    import io
    mid = user["id"]
    reader = csvmod.DictReader(io.StringIO(body.csv))
    fields = [f for f in (reader.fieldnames or []) if (f or "").strip()]
    norm = {f: _norm_header(f) for f in fields}
    shop_col = _pick_col(fields, norm, _CSV_RET_SHOP_HEADERS)
    if not shop_col:
        raise HTTPException(422, "CSV is missing a shop or name column (one "
                                 "of: " + ", ".join(_CSV_RET_SHOP_HEADERS) + ")")
    name_col = _pick_col(fields, norm, _CSV_RET_NAME_HEADERS)
    phone_col = _pick_col(fields, norm, _CSV_PHONE_HEADERS)
    region_col = _pick_col(fields, norm, _CSV_REGION_HEADERS)
    address_col = _pick_col(fields, norm, _CSV_RET_ADDRESS_HEADERS)
    dist_col = _pick_col(fields, norm, _CSV_RET_DISTRIBUTOR_HEADERS)
    ext_col = _pick_col(fields, norm, _CSV_RET_EXTERNAL_ID_HEADERS)
    points_col = _pick_col(fields, norm, _CSV_RET_POINTS_HEADERS)
    created = skipped = 0
    points_credited = 0
    errors: list[str] = []
    credentials: list[dict] = []
    with get_db() as db:
        # Phone identifies the retailer on /yourapp/scan, so it must stay
        # unique (normalized) within the manufacturer. Missing phone is still
        # allowed (webview-only retailers).
        phones_in_use = {
            _norm_phone(r["phone"]) for r in db.execute(
                """SELECT phone FROM retailers
                   WHERE manufacturer_id = ? AND phone IS NOT NULL""",
                (mid,),
            )
        }
        shops_in_use = {
            r["shop_name"].lower() for r in db.execute(
                """SELECT shop_name FROM retailers
                   WHERE manufacturer_id = ?""",
                (mid,),
            )
        }
        ext_ids_in_use = {
            r["external_id"] for r in db.execute(
                """SELECT external_id FROM retailers
                   WHERE manufacturer_id = ? AND external_id IS NOT NULL""",
                (mid,),
            )
        }
        dist_map = {
            r["lname"]: r["id"] for r in db.execute(
                """SELECT id, LOWER(name) AS lname FROM distributors
                   WHERE manufacturer_id = ?""",
                (mid,),
            )
        }
        usernames_in_use = {
            r["username"] for r in db.execute(
                "SELECT username FROM retailers WHERE username IS NOT NULL"
            )
        }
        max_id_row = db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM retailers").fetchone()
        next_id = (max_id_row["m"] if max_id_row else 0) + 1

        to_create: list[dict] = []

        for i, raw in enumerate(reader, start=2):  # row 1 is the header
            # A file carrying both "Firm Name" and "Name" fills the firm in only
            # for shops that trade under one; an individual retailer leaves it
            # blank. Fall back to the person's name rather than dropping the
            # row — the same substitution line 'name_val' makes in reverse.
            person = _clean_cell(raw.get(name_col)) if name_col else ""
            shop = _clean_cell(raw.get(shop_col)) or person
            if not shop:
                errors.append(f"row {i}: missing shop name")
                continue
            phone = (_clean_cell(raw.get(phone_col)) if phone_col else "") or None
            norm_phone = _norm_phone(phone) if phone else ""
            if norm_phone:
                if norm_phone in phones_in_use:
                    skipped += 1
                    continue
            elif shop.lower() in shops_in_use:
                skipped += 1
                continue
            external_id = (_clean_cell(raw.get(ext_col)) if ext_col else "") or None
            if external_id and external_id in ext_ids_in_use:
                skipped += 1
                errors.append(f"row {i}: external_id '{external_id}' already in use")
                continue
            if norm_phone:
                phones_in_use.add(norm_phone)
            shops_in_use.add(shop.lower())
            if external_id:
                ext_ids_in_use.add(external_id)

            region = (_clean_cell(raw.get(region_col)) if region_col else "")
            coords = coords_for(region) if region else None
            lat, lng = coords if coords else (None, None)
            address = (_clean_cell(raw.get(address_col))
                       if address_col else "") or None

            dist_name = _clean_cell(raw.get(dist_col)) if dist_col else ""
            distributor_id = None
            if dist_name:
                dl = dist_name.strip().lower()
                if dl in dist_map:
                    distributor_id = dist_map[dl]
                else:
                    cur = db.execute(
                        "INSERT INTO distributors (manufacturer_id, name) VALUES (?, ?)",
                        (mid, dist_name.strip()),
                    )
                    distributor_id = cur.lastrowid
                    dist_map[dl] = distributor_id

            first = (shop.split() or ["shop"])[0].lower()
            base = "".join(ch for ch in first if ch.isalnum()) or "shop"
            username = base
            if username in usernames_in_use:
                username = f"{base}{next_id}"
                while username in usernames_in_use:
                    next_id += 1
                    username = f"{base}{next_id}"
            usernames_in_use.add(username)
            next_id += 1

            password = f"{username}123"
            name_val = person or shop

            # Carried-over wallet balance. It becomes one 'adjustment' ledger
            # row after the insert — never a 'scan', so it lands in the wallet
            # and the Customers "Points" column without inventing scan history
            # in the dashboard, claims or region/product analytics.
            opening = _parse_points(raw.get(points_col)) if points_col else None
            if opening is not None and opening < 0:
                errors.append(f"row {i}: negative points '{raw.get(points_col)}' ignored")
                opening = None

            to_create.append({
                "name": name_val, "shop_name": shop, "region": region,
                "phone": phone, "lat": lat, "lng": lng,
                "source": "city" if coords else None,
                "distributor_id": distributor_id, "external_id": external_id,
                "address": address, "username": username, "password": password,
                "points": opening or 0,
            })

        import concurrent.futures
        import os
        passwords = [item["password"] for item in to_create]
        if passwords:
            max_workers = min(32, (os.cpu_count() or 4) + 4)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                p_hashes = list(pool.map(lambda p: hash_password(p, iterations=20_000), passwords))
        else:
            p_hashes = []

        for item, phash in zip(to_create, p_hashes):
            cur = db.execute(
                """INSERT INTO retailers
                   (manufacturer_id, name, shop_name, region, phone, lat, lng,
                    location_source, distributor_id, external_id, address,
                    username, password_hash, must_change)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (mid, item["name"], item["shop_name"], item["region"], item["phone"],
                 item["lat"], item["lng"], item["source"], item["distributor_id"],
                 item["external_id"], item["address"], item["username"], phash),
            )
            if item["points"]:
                db.execute(
                    """INSERT INTO points_ledger
                       (manufacturer_id, retailer_id, entry_type, points, note,
                        created_by, distributor_id)
                       VALUES (?, ?, 'import_opening', ?, ?, ?, ?)""",
                    (mid, cur.lastrowid, item["points"],
                     "Opening balance imported from customer list", mid,
                     item["distributor_id"]),
                )
                points_credited += item["points"]
            created += 1
            credentials.append(
                {"shop_name": item["shop_name"], "username": item["username"],
                 "password": item["password"]}
            )
    # Echo the resolved mapping so the panel can show which of the file's own
    # columns were read (a wrong guess is otherwise invisible).
    return {"created": created, "skipped": skipped,
            "points_credited": points_credited,
            "errors": errors, "credentials": credentials,
            "columns": {"shop_name": shop_col, "name": name_col,
                        "phone": phone_col, "region": region_col,
                        "address": address_col, "distributor": dist_col,
                        "points": points_col, "external_id": ext_col}}


@app.post("/distributors/import")
@limiter.limit(RL_IMPORT)
def import_distributors_csv(request: Request, body: ImportIn,
                            user: dict = Depends(current_manufacturer)):
    """Bulk-create distributors from CSV text.

    Headers are matched the same generous way the product catalog import matches
    them (`_norm_header` + alias lists), so a distributor list exported from the
    manufacturer's own system imports as-is: "Mobile"/"Contact No" fill `phone`,
    "City"/"State" fill `region`, and row-number columns are ignored. Only a
    name column is required. Rows whose name already exists for this
    manufacturer are skipped."""
    import csv as csvmod
    import io
    mid = user["id"]
    reader = csvmod.DictReader(io.StringIO(body.csv))
    fields = [f for f in (reader.fieldnames or []) if (f or "").strip()]
    norm = {f: _norm_header(f) for f in fields}
    name_col = _pick_col(fields, norm, _CSV_DIST_NAME_HEADERS)
    if not name_col:
        raise HTTPException(422, "CSV is missing a distributor name column "
                                 "(one of: "
                                 + ", ".join(_CSV_DIST_NAME_HEADERS) + ")")
    phone_col = _pick_col(fields, norm, _CSV_PHONE_HEADERS)
    region_col = _pick_col(fields, norm, _CSV_REGION_HEADERS)
    created = skipped = 0
    errors: list[str] = []
    with get_db() as db:
        for i, raw in enumerate(reader, start=2):  # row 1 is the header
            name = _clean_cell(raw.get(name_col))
            if not name:
                errors.append(f"row {i}: missing name")
                continue
            if db.execute(
                """SELECT 1 FROM distributors
                   WHERE manufacturer_id = ? AND LOWER(name) = LOWER(?)""",
                (mid, name),
            ).fetchone():
                skipped += 1
                continue
            phone = _clean_cell(raw.get(phone_col)) if phone_col else ""
            region = _clean_cell(raw.get(region_col)) if region_col else ""
            db.execute(
                """INSERT INTO distributors (manufacturer_id, name, phone, region)
                   VALUES (?, ?, ?, ?)""",
                (mid, name, phone or None, region or None),
            )
            created += 1
    # Echo the resolved mapping so the panel can show which of the file's
    # columns were actually read (a wrong guess is otherwise invisible).
    return {"created": created, "skipped": skipped, "errors": errors,
            "columns": {"name": name_col, "phone": phone_col,
                        "region": region_col}}


# ---------- QR generation (manufacturer-scoped) ----------

@app.post("/qr/generate", status_code=201)
@limiter.limit(RL_QRGEN)
def generate_qr_batch(request: Request, body: GenerateIn,
                      user: dict = Depends(current_manufacturer)):
    mid = user["id"]
    # Resolve the product snapshot for this batch.
    #  Primary path: Vastra (the product System of Record) supplies a reference
    #  (product_external_id) + snapshot (name/sku) + the frozen points value;
    #  loyalty trusts it and never reads the products table.
    #  Transitional path: the panel / /web/generate still send a loyalty
    #  product_id, looked up once to build the same snapshot.
    product_id = None
    if body.product_external_id:
        if not (body.product_name and body.product_sku
                and body.points_per_code is not None):
            raise HTTPException(
                422, "product_name, product_sku and points_per_code are "
                     "required with product_external_id")
        product_external_id = body.product_external_id.strip()
        product_name, product_sku = body.product_name, body.product_sku
        points = body.points_per_code
    elif body.product_id is not None:
        with get_db() as db:
            product = db.execute(
                "SELECT * FROM products WHERE id = ? AND manufacturer_id = ?",
                (body.product_id, mid),
            ).fetchone()
        if not product:
            raise HTTPException(404, "Product not found")
        product_id = product["id"]
        product_external_id = None
        product_name, product_sku = product["name"], product["sku"]
        points = (body.points_per_code if body.points_per_code is not None
                  else product["loyalty_points"])
    else:
        raise HTTPException(
            422, "product_external_id (or legacy product_id) is required")

    with get_db() as db:
        # manufacturer_id is stored directly on the batch (no products join);
        # points_per_code stays frozen exactly as before.
        cur = db.execute(
            """INSERT INTO qr_batches
               (manufacturer_id, product_id, product_external_id,
                product_name, product_sku, quantity, points_per_code)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (mid, product_id, product_external_id, product_name, product_sku,
             body.quantity, points),
        )
        batch_id = cur.lastrowid

        existing = {
            r["manual_code"]
            for r in db.execute("SELECT manual_code FROM qr_codes")
        }

        def fresh_manual() -> str:
            m = new_manual_code()
            while m in existing:
                m = new_manual_code()
            existing.add(m)
            return m

        # Child codes (one per item).
        children = [(new_token(), fresh_manual()) for _ in range(body.quantity)]

        # Optional box (parent) codes: each parent links a run of children.
        parents = []  # (token, manual, [child_tokens])
        if body.items_per_box:
            for i in range(0, body.quantity, body.items_per_box):
                group = children[i:i + body.items_per_box]
                parents.append(
                    (new_token(), fresh_manual(), [t for t, _ in group]))

        child_to_parent = {}
        for p_token, _, child_tokens in parents:
            for ct in child_tokens:
                child_to_parent[ct] = p_token

        db.executemany(
            """INSERT INTO qr_codes
               (token, manual_code, batch_id, is_parent, parent_token)
               VALUES (?, ?, ?, 0, ?)""",
            [(t, m, batch_id, child_to_parent.get(t)) for t, m in children],
        )
        if parents:
            db.executemany(
                """INSERT INTO qr_codes
                   (token, manual_code, batch_id, is_parent, parent_token)
                   VALUES (?, ?, ?, 1, NULL)""",
                [(t, m, batch_id) for t, m, _ in parents],
            )

    return {
        "batch_id": batch_id,
        "product_id": product_id,
        "product_external_id": product_external_id,
        "product_name": product_name,
        "product_sku": product_sku,
        "quantity": body.quantity,
        "points_per_code": points,
        "items_per_box": body.items_per_box,
        "boxes": len(parents),
        "status": "pending",
        "codes": [
            {"token": t, "manual_code": m, "payload": payload_for(t)}
            for t, m in children
        ],
        "boxes_codes": [
            {"token": t, "manual_code": m, "payload": payload_for(t),
             "items": len(ch)}
            for t, m, ch in parents
        ],
        "actions": {
            "save": f"/qr/batches/{batch_id}/save",
            "print": f"/qr/batches/{batch_id}/print",
            "discard": f"/qr/batches/{batch_id}",
        },
    }


# ---------- batches (manufacturer-scoped) ----------

def _get_batch(db, batch_id: int, manufacturer_id: int):
    batch = db.execute(
        """SELECT b.*, b.product_sku AS sku
           FROM qr_batches b
           WHERE b.id = ? AND b.manufacturer_id = ?""",
        (batch_id, manufacturer_id),
    ).fetchone()
    if not batch:
        raise HTTPException(404, "Batch not found")
    return batch


@app.post("/qr/batches/{batch_id}/save")
def save_batch(batch_id: int, user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        _get_batch(db, batch_id, user["id"])
        db.execute(
            "UPDATE qr_batches SET status = 'saved' WHERE id = ?", (batch_id,)
        )
    return {"batch_id": batch_id, "status": "saved"}


@app.get("/qr/batches")
def list_batches(status: str | None = Query(None, pattern="^(pending|saved)$"),
                 user: dict = Depends(current_manufacturer)):
    # `boxes` lets the panel's print-scope selector know whether a saved batch
    # has box codes at all: qr_batches stores neither items_per_box nor a count,
    # so without it "Box codes only" would be offered on batches that have none.
    sql = """SELECT b.*, b.product_sku AS sku,
                    (SELECT COUNT(*) FROM qr_codes c
                      WHERE c.batch_id = b.id AND c.is_parent = 1) AS boxes
             FROM qr_batches b
             WHERE b.manufacturer_id = ?"""
    args: list = [user["id"]]
    if status:
        sql += " AND b.status = ?"
        args.append(status)
    sql += " ORDER BY b.id DESC"
    with get_db() as db:
        rows = db.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


@app.get("/qr/batches/{batch_id}")
def get_batch(batch_id: int, user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        batch = _get_batch(db, batch_id, user["id"])
        # is_parent + items + payload let the panel rebuild the sticker sheet
        # client-side for a saved batch (POST /qr/generate already returns the
        # same three for a freshly generated one). Children first, then box
        # codes, matching the order the sheet is laid out in.
        codes = db.execute(
            """SELECT c.token, c.manual_code, c.redeemed_at, c.redeemed_by,
                      c.is_parent,
                      (SELECT COUNT(*) FROM qr_codes ch
                       WHERE ch.parent_token = c.token) AS items
               FROM qr_codes c
               WHERE c.batch_id = ? ORDER BY c.is_parent, c.token""",
            (batch_id,),
        ).fetchall()
    out = dict(batch)
    # payload is built here, never in the browser: QR_BASE_URL is server-side
    # config and a frontend copy that drifted would point printed stickers at
    # the wrong host.
    out["codes"] = [{**dict(c), "payload": payload_for(c["token"])}
                    for c in codes]
    return out


@app.get("/qr/batches/{batch_id}/print")
def print_batch(batch_id: int, user: dict = Depends(current_manufacturer)):
    """Printable A4 PDF. Accepts ?token= auth so it can open in a new tab."""
    with get_db() as db:
        batch = _get_batch(db, batch_id, user["id"])
        # Children first, then box (parent) codes; each parent carries its
        # item count so the sticker can be labelled.
        child_rows = db.execute(
            """SELECT token, manual_code FROM qr_codes
               WHERE batch_id = ? AND is_parent = 0 ORDER BY token""",
            (batch_id,),
        ).fetchall()
        codes = [(r["token"], r["manual_code"], 0) for r in child_rows]
        parent_rows = db.execute(
            """SELECT token, manual_code,
                      (SELECT COUNT(*) FROM qr_codes ch
                       WHERE ch.parent_token = c.token) AS items
               FROM qr_codes c
               WHERE c.batch_id = ? AND c.is_parent = 1 ORDER BY c.token""",
            (batch_id,),
        ).fetchall()
        codes += [(r["token"], r["manual_code"], r["items"])
                  for r in parent_rows]
    pdf = build_pdf(batch["product_name"], batch["sku"], codes)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'inline; filename="loyalty-qr-batch-{batch_id}.pdf"'
        },
    )


@app.delete("/qr/batches/{batch_id}", status_code=204)
def discard_batch(batch_id: int, user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        _get_batch(db, batch_id, user["id"])
        db.execute("DELETE FROM qr_codes WHERE batch_id = ?", (batch_id,))
        db.execute("DELETE FROM qr_batches WHERE id = ?", (batch_id,))


# ---------- single code image (open) ----------

@app.get("/qr/codes/{token}/image")
def code_image(token: str):
    with get_db() as db:
        row = db.execute(
            "SELECT token FROM qr_codes WHERE token = ?", (token,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Code not found")
    return Response(content=render_png(token), media_type="image/png")


# ---------- scan & redeem (retailer side, authenticated) ----------

def _find_code(db, raw_code: str):
    """Look up a code (QR token or 6-char manual code, dashes/spaces ok) with
    its batch fields. Returns the row or None."""
    code = raw_code.strip().replace("-", "").replace(" ", "").upper()
    return db.execute(
        """SELECT c.token, c.redeemed_at, c.redeemed_by, c.is_parent,
                  b.points_per_code,
                  b.product_id AS product_id,
                  b.product_external_id AS product_external_id,
                  b.product_name AS product_name, b.product_sku AS sku,
                  b.manufacturer_id
           FROM qr_codes c
           JOIN qr_batches b ON b.id = c.batch_id
           WHERE c.token = ? OR c.manual_code = ?""",
        (raw_code.strip(), code),
    ).fetchone()


def _best_scheme(db, manufacturer_id: int, product_id):
    """The most generous active scheme of this manufacturer covering the
    product (no stacking). None when no scheme applies."""
    return db.execute(
        """SELECT s.id, s.name, s.bonus_points FROM schemes s
           WHERE s.manufacturer_id = ?
             AND date('now') BETWEEN s.start_date AND s.end_date
             AND (NOT EXISTS (SELECT 1 FROM scheme_products sp
                              WHERE sp.scheme_id = s.id)
                  OR EXISTS (SELECT 1 FROM scheme_products sp
                             WHERE sp.scheme_id = s.id
                               AND sp.product_id = ?))
           ORDER BY s.bonus_points DESC LIMIT 1""",
        (manufacturer_id, product_id),
    ).fetchone()


def _redeem_code(db, retailer: dict, row, lat, lng) -> dict:
    """Race-safe redemption core, shared by /scan (webview, token-derived
    retailer) and /yourapp/scan (server-to-server, phone-derived retailer).
    ``row`` is a _find_code result (or None). Points always go to the passed
    retailer. Single authority for rewards: base points + best scheme bonus."""
    rid = retailer["id"]
    # Fix 5 (enumeration oracle): a non-existent code and a code that
    # belongs to another manufacturer return the *identical* 404, so an
    # attacker cannot use the response to tell a real cross-tenant code
    # from a fake one. "Already redeemed" stays distinct below because the
    # retailer needs that feedback and it only ever leaks the state of a
    # code they already legitimately hold.
    if not row or retailer["manufacturer_id"] != row["manufacturer_id"]:
        raise HTTPException(404, "Invalid code")

    # A box (parent) code registers all of its still-unredeemed children;
    # a plain code registers just itself. The actual race-safe redemption
    # happens via the conditional UPDATE below — these reads only drive
    # the not-yet-redeemed error message for the non-concurrent case.
    if row["is_parent"]:
        tokens = [
            c["token"] for c in db.execute(
                """SELECT token FROM qr_codes
                   WHERE parent_token = ? AND redeemed_at IS NULL""",
                (row["token"],),
            )
        ]
        if not tokens:
            raise HTTPException(409, "Box already redeemed")
    else:
        if row["redeemed_at"]:
            raise HTTPException(409, "Code already redeemed")
        tokens = [row["token"]]

    # All children of a box share one product, so the per-item value is equal.
    scheme = _best_scheme(db, row["manufacturer_id"], row["product_id"])
    base = row["points_per_code"]
    bonus = scheme["bonus_points"] if scheme else 0
    per = base + bonus

    # Fix 1 (double-spend race): claim each child token with a *conditional*
    # UPDATE that only matches a still-unredeemed row, then credit points
    # ONLY for the rows this transaction actually won. Under concurrency the
    # database serializes these row updates; the loser sees rowcount == 0
    # (the row was redeemed by the winner) and credits nothing. This makes
    # the check-and-mark a single atomic database operation instead of the
    # previous read-then-write TOCTOU. The partial UNIQUE index
    # uq_ledger_active_scan_token (one entry_type='scan' row per token)
    # is the belt-and-suspenders backstop.
    credited = []
    for t in tokens:
        cur = db.execute(
            """UPDATE qr_codes SET redeemed_at = datetime('now'),
                                   redeemed_by = ?
               WHERE token = ? AND redeemed_at IS NULL""",
            (rid, t),
        )
        if cur.rowcount != 1:
            continue  # another concurrent scan already claimed this token
        db.execute(
            """INSERT INTO points_ledger
               (manufacturer_id, retailer_id, token, product_id,
                product_external_id, product_name, product_sku, points,
                base_points, bonus_points, scheme_id, region, lat, lng,
                distributor_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["manufacturer_id"], rid, t,
             row["product_id"], row["product_external_id"],
             row["product_name"], row["sku"], per, base, bonus,
             scheme["id"] if scheme else None, retailer["region"],
             lat, lng, retailer["distributor_id"]),
        )
        credited.append(t)

    # If a concurrent request beat us to every token, nothing was credited.
    if not credited:
        raise HTTPException(
            409, "Box already redeemed" if row["is_parent"]
            else "Code already redeemed")
    if row["is_parent"]:
        # Mark the box itself redeemed (conditional, so the loser is a no-op).
        db.execute(
            """UPDATE qr_codes SET redeemed_at = datetime('now'),
                                   redeemed_by = ?
               WHERE token = ? AND redeemed_at IS NULL""",
            (rid, row["token"]),
        )

    count = len(credited)
    balance = db.execute(
        "SELECT COALESCE(SUM(points), 0) AS total FROM points_ledger"
        " WHERE retailer_id = ?",
        (rid,),
    ).fetchone()["total"]

    return {
        "redeemed": True,
        "is_box": bool(row["is_parent"]),
        "items_registered": count,
        "product": {"id": row["product_id"],
                    "external_id": row["product_external_id"],
                    "name": row["product_name"], "sku": row["sku"]},
        "points_awarded": per * count,
        "base_points": base * count,
        "bonus_points": bonus * count,
        "scheme": ({"id": scheme["id"], "name": scheme["name"]}
                   if scheme else None),
        "retailer": {"id": rid,
                     "shop_name": retailer["shop_name"],
                     "region": retailer["region"]},
        "new_balance": balance,
    }


@app.post("/scan")
@limiter.limit(RL_SCAN)
def scan(request: Request, body: ScanIn,
         retailer: dict = Depends(current_retailer)):
    """Redeem a code by QR token or 6-char manual code. Points always go to
    the logged-in retailer, so a code can't be credited to another account."""
    with get_db() as db:
        return _redeem_code(db, retailer, _find_code(db, body.code),
                            body.lat, body.lng)


# ---------- YourApp server-to-server scan (phone-verified) ----------

class YourAppScanIn(BaseModel):
    phone: str = Field(min_length=10, max_length=20,
                       description="Retailer's registered phone (as in YourApp)")
    code: str = Field(min_length=1, max_length=64)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


class YourAppLookupIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class YourAppPointsIn(BaseModel):
    phone: str = Field(min_length=10, max_length=20,
                       description="Retailer's registered phone (as in YourApp)")


@app.post("/yourapp/qr/lookup")
@limiter.limit(RL_SCAN)
def yourapp_qr_lookup(request: Request, body: YourAppLookupIn,
                      _key: None = Depends(require_yourapp_key)):
    """Read-only code preview for YourApp's backend: what the code is worth
    and whether it was already scanned. Never redeems, never changes state.
    Points are per item; a box (is_box) credits base/bonus/total per child."""
    with get_db() as db:
        row = _find_code(db, body.code)
        if not row:
            raise HTTPException(404, "Invalid code")
        scheme = _best_scheme(db, row["manufacturer_id"], row["product_id"])
        items = 1
        if row["is_parent"]:
            items = db.execute(
                "SELECT COUNT(*) AS n FROM qr_codes WHERE parent_token = ?",
                (row["token"],),
            ).fetchone()["n"]
        redeemed_by_shop = None
        if row["redeemed_by"]:
            shop = db.execute(
                "SELECT shop_name FROM retailers WHERE id = ?",
                (row["redeemed_by"],),
            ).fetchone()
            redeemed_by_shop = shop["shop_name"] if shop else None
    base = row["points_per_code"]
    bonus = scheme["bonus_points"] if scheme else 0
    return {
        # The code's own state. It lives in `qrStatus` because `status` is
        # reserved for the boolean did-the-call-work flag — never merge them.
        "qrStatus": "redeemed" if row["redeemed_at"] else "available",
        "is_box": bool(row["is_parent"]),
        "items": items,
        "product": {"external_id": row["product_external_id"],
                    "name": row["product_name"], "sku": row["sku"]},
        "base_points": base,
        "bonus_points": bonus,
        "total_points": base + bonus,
        "scheme": ({"id": scheme["id"], "name": scheme["name"]}
                   if scheme else None),
        "redeemed_at": row["redeemed_at"],
        "redeemed_by_shop": redeemed_by_shop,
        "status": True,
    }


@app.post("/yourapp/points")
@limiter.limit(RL_SCAN)
def yourapp_points(request: Request, body: YourAppPointsIn,
                   _key: None = Depends(require_yourapp_key)):
    """Total points balance for a retailer, called by YourApp's backend.
    Identified by phone (as in YourApp) — no code, so tenancy is resolved by
    the phone alone: unique match required across all manufacturers."""
    want = _norm_phone(body.phone)
    if len(want) < 10:
        raise HTTPException(422, "Invalid phone number")
    with get_db() as db:
        # `phone` must be selected: the match is done in Python (normalised),
        # not in SQL, so the column has to come back with the row.
        matches = [dict(r) for r in db.execute(
            "SELECT id, blocked, phone FROM retailers WHERE phone IS NOT NULL")
            if _norm_phone(r["phone"]) == want]
        if not matches:
            raise HTTPException(403, "Phone number not registered")
        if len(matches) > 1:
            raise HTTPException(409, "Multiple retailers share this phone number")
        if matches[0]["blocked"]:
            raise HTTPException(403, "Account is blocked")
        return {"total_points": _balance(db, matches[0]["id"]),
                "status": True}


@app.post("/yourapp/scan")
@limiter.limit(RL_SCAN)
def yourapp_scan(request: Request, body: YourAppScanIn,
                 _key: None = Depends(require_yourapp_key)):
    """Scan on behalf of a retailer, called by YourApp's backend. The retailer
    is identified by phone number, verified against the phone registered in
    this system (imported from YourApp data) — scoped to the scanned code's
    manufacturer, so a phone can never credit across tenants. Same response
    shape as /scan."""
    with get_db() as db:
        row = _find_code(db, body.code)
        if not row:
            raise HTTPException(404, "Invalid code")
        retailer = _retailer_by_phone(db, row["manufacturer_id"], body.phone)
        result = _redeem_code(db, retailer, row, body.lat, body.lng)
    if body.lat is not None and body.lng is not None:
        # Same latest-wins shop-pin refresh the webview does via
        # POST /retailer/location; best-effort, never fails the scan.
        try:
            _refresh_retailer_pin(retailer["id"], body.lat, body.lng)
        except Exception:
            pass
    # Stamped here rather than inside _redeem_code, so the webview's own
    # POST /scan keeps the exact response shape it has today.
    return {**result, "status": True}


# ---------- schemes / campaigns (manufacturer-scoped) ----------

def _scheme_status(start_date: str, end_date: str) -> str:
    today = date.today().isoformat()
    if today < start_date:
        return "upcoming"
    if today > end_date:
        return "previous"
    return "active"


def _scheme_out(db, row) -> dict:
    products = db.execute(
        """SELECT p.id, p.name, p.sku FROM scheme_products sp
           JOIN products p ON p.id = sp.product_id WHERE sp.scheme_id = ?""",
        (row["id"],),
    ).fetchall()
    out = dict(row)
    out["status"] = _scheme_status(row["start_date"], row["end_date"])
    out["products"] = [dict(p) for p in products]  # empty = all products
    out["all_products"] = not products
    return out


@app.post("/schemes", status_code=201)
def create_scheme(body: SchemeIn, user: dict = Depends(current_manufacturer)):
    if body.end_date < body.start_date:
        raise HTTPException(422, "end_date must be on or after start_date")
    with get_db() as db:
        if body.product_ids:
            found = db.execute(
                f"""SELECT COUNT(*) AS n FROM products
                    WHERE manufacturer_id = ?
                      AND id IN ({','.join('?' * len(body.product_ids))})""",
                [user["id"], *body.product_ids],
            ).fetchone()["n"]
            if found != len(set(body.product_ids)):
                raise HTTPException(404, "One or more product_ids not found")
        cur = db.execute(
            """INSERT INTO schemes
               (manufacturer_id, name, description, start_date, end_date,
                bonus_points)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user["id"], body.name, body.description,
             body.start_date.isoformat(), body.end_date.isoformat(),
             body.bonus_points),
        )
        scheme_id = cur.lastrowid
        db.executemany(
            "INSERT INTO scheme_products (scheme_id, product_id) VALUES (?, ?)",
            [(scheme_id, pid) for pid in set(body.product_ids)],
        )
        row = db.execute(
            "SELECT * FROM schemes WHERE id = ?", (scheme_id,)
        ).fetchone()
        return _scheme_out(db, row)


@app.get("/schemes")
def list_schemes(status: str | None = Query(
        None, pattern="^(active|upcoming|previous)$"),
        user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        rows = db.execute(
            """SELECT * FROM schemes WHERE manufacturer_id = ?
               ORDER BY start_date DESC""",
            (user["id"],),
        ).fetchall()
        out = [_scheme_out(db, r) for r in rows]
    if status:
        out = [s for s in out if s["status"] == status]
    return out


@app.delete("/schemes/{scheme_id}", status_code=204)
def delete_scheme(scheme_id: int, user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM schemes WHERE id = ? AND manufacturer_id = ?",
            (scheme_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Scheme not found")
        used = db.execute(
            "SELECT COUNT(*) AS n FROM points_ledger WHERE scheme_id = ?",
            (scheme_id,),
        ).fetchone()["n"]
        if used:
            raise HTTPException(
                409, "Scheme has redemptions against it; cannot delete")
        db.execute("DELETE FROM schemes WHERE id = ?", (scheme_id,))


# ---------- claims (manufacturer-scoped) ----------

@app.get("/claims")
def list_claims(
    product_id: int | None = None,
    product_external_id: str | None = None,
    retailer_id: int | None = None,
    region: str | None = None,
    scheme_id: int | None = None,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(current_manufacturer),
):
    where = " WHERE l.manufacturer_id = ? AND l.entry_type = 'scan'"
    args: list = [user["id"]]
    if product_id is not None:  # legacy/transitional filter
        where += " AND l.product_id = ?"
        args.append(product_id)
    if product_external_id is not None:
        where += " AND l.product_external_id = ?"
        args.append(product_external_id)
    if retailer_id is not None:
        where += " AND l.retailer_id = ?"
        args.append(retailer_id)
    if region:
        where += " AND l.region = ?"
        args.append(region)
    if scheme_id is not None:
        where += " AND l.scheme_id = ?"
        args.append(scheme_id)
    if date_from:
        where += " AND l.scanned_at >= ?"
        args.append(date_from)
    if date_to:
        where += " AND l.scanned_at <= ?"
        args.append(date_to)

    # A box (parent) scan writes one ledger row per child code; collapse those
    # into a single claim so the list isn't flooded with duplicate retailer
    # rows. Children of one box share the same qr_codes.parent_token (a box is
    # scanned once), so COALESCE(parent_token, token) is a stable group key —
    # NULL parent_token (plain scans) groups on the unique token, i.e. one row
    # each. Done at query time, so it also tidies existing data (no migration).
    group_key = "COALESCE(q.parent_token, l.token)"
    with get_db() as db:
        total = db.execute(
            f"""SELECT COUNT(*) AS n FROM (
                  SELECT 1 FROM points_ledger l
                  JOIN qr_codes q ON q.token = l.token
                  {where} GROUP BY {group_key}
                ) sub""",
            args,
        ).fetchone()["n"]
        rows = db.execute(
            f"""SELECT MIN(l.id) AS id, l.scanned_at,
                       COUNT(*) AS item_count,
                       SUM(l.points) AS points,
                       SUM(l.base_points) AS base_points,
                       SUM(l.bonus_points) AS bonus_points,
                       l.region, MAX(q.parent_token) AS parent_token,
                       {group_key} AS token,
                       l.product_id AS product_id,
                       MAX(l.product_external_id) AS product_external_id,
                       MAX(l.product_name) AS product_name,
                       MAX(l.product_sku) AS sku,
                       r.id AS retailer_id, r.name AS retailer_name,
                       r.shop_name, r.lat, r.lng,
                       s.id AS scheme_id, s.name AS scheme_name
                FROM points_ledger l
                JOIN qr_codes q ON q.token = l.token
                JOIN retailers r ON r.id = l.retailer_id
                LEFT JOIN schemes s ON s.id = l.scheme_id
                {where}
                GROUP BY {group_key}, l.scanned_at, l.region,
                         l.product_id,
                         r.id, r.name, r.shop_name, r.lat, r.lng,
                         s.id, s.name
                ORDER BY l.scanned_at DESC LIMIT ? OFFSET ?""",
            args + [limit, offset],
        ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "claims": [dict(r) for r in rows],
    }


# ---------- scan reversal (manufacturer-scoped) ----------
# A retailer sometimes scans a code that another retailer bought. The
# manufacturer can look the code up, see who was credited, deduct exactly
# those points and re-open the code so the rightful retailer can scan it.
# Mechanics: the original ledger rows flip entry_type 'scan'->'scan_reversed'
# (so every entry_type='scan' filter — claims, dashboard, retailer stats —
# excludes them with no query changes) and an offsetting negative 'reversal'
# row per item keeps balance = SUM(points) and the audit trail intact.

def _resolve_scan_group(db, manufacturer_id: int, code: str) -> dict:
    """Look up a code (QR token or 6-char manual code) owned by this
    manufacturer and expand it to the token group a reversal must cover:
    a box parent covers the whole box, a child redeemed *via its box* also
    covers the whole box (a box scan is the only path that marks the parent
    redeemed), anything else covers just itself. For redeemed box groups the
    children are limited to those claimed by the box scanner, so items a
    different retailer scanned individually beforehand are untouched."""
    norm = code.strip().replace("-", "").replace(" ", "").upper()
    row = db.execute(
        """SELECT c.token, c.manual_code, c.is_parent, c.parent_token,
                  c.redeemed_at, c.redeemed_by, b.points_per_code,
                  b.product_name, b.product_sku AS sku, b.manufacturer_id
           FROM qr_codes c
           JOIN qr_batches b ON b.id = c.batch_id
           WHERE c.token = ? OR c.manual_code = ?""",
        (code.strip(), norm),
    ).fetchone()
    # Same enumeration-proof 404 as /scan: an unknown code and another
    # manufacturer's code are indistinguishable from the response.
    if not row or row["manufacturer_id"] != manufacturer_id:
        raise HTTPException(404, "Invalid code")

    box = None
    if row["is_parent"]:
        box = row
    elif row["parent_token"]:
        p = db.execute(
            """SELECT token, redeemed_at, redeemed_by FROM qr_codes
               WHERE token = ?""",
            (row["parent_token"],),
        ).fetchone()
        if p and p["redeemed_at"]:
            box = p

    if box:
        tokens = [
            c["token"] for c in db.execute(
                """SELECT token FROM qr_codes
                   WHERE parent_token = ? AND redeemed_by = ?""",
                (box["token"], box["redeemed_by"]),
            )
        ] if box["redeemed_at"] else []
        return {
            "group_token": box["token"], "manual_code": row["manual_code"],
            "is_box": True, "tokens": tokens,
            "redeemed_at": box["redeemed_at"],
            "redeemed_by": box["redeemed_by"],
            "points_per_code": row["points_per_code"],
            "product_name": row["product_name"], "sku": row["sku"],
        }
    return {
        "group_token": row["token"], "manual_code": row["manual_code"],
        "is_box": False, "tokens": [row["token"]],
        "redeemed_at": row["redeemed_at"], "redeemed_by": row["redeemed_by"],
        "points_per_code": row["points_per_code"],
        "product_name": row["product_name"], "sku": row["sku"],
    }


def _scan_rows(db, group: dict):
    """The still-active scan ledger rows a reversal of this group would undo."""
    if not group["tokens"]:
        return []
    qs = ",".join("?" * len(group["tokens"]))
    return db.execute(
        f"""SELECT id, retailer_id, token, points, base_points, bonus_points,
                   scheme_id, product_id, product_external_id, product_name,
                   product_sku, region, distributor_id, scanned_at
            FROM points_ledger
            WHERE token IN ({qs}) AND entry_type = 'scan'
              AND retailer_id = ?""",
        (*group["tokens"], group["redeemed_by"]),
    ).fetchall()


@app.get("/scans/lookup")
def lookup_scan(code: str, user: dict = Depends(current_manufacturer)):
    """Who redeemed this code, when, and for how many points — the read-only
    companion of POST /scans/reverse (drives the panel's lookup box)."""
    with get_db() as db:
        g = _resolve_scan_group(db, user["id"], code)
        out = {
            "token": g["group_token"], "manual_code": g["manual_code"],
            "is_box": g["is_box"], "product_name": g["product_name"],
            "sku": g["sku"], "points_per_code": g["points_per_code"],
        }
        if not g["redeemed_at"]:
            return {**out, "redeemed": False, "reversible": False,
                    "reason": "Code has not been scanned yet"}
        rows = _scan_rows(db, g)
        if not rows:
            return {**out, "redeemed": True, "reversible": False,
                    "reason": "No active scan credit found for this code"}
        total = sum(r["points"] for r in rows)
        rid = rows[0]["retailer_id"]
        retailer = db.execute(
            "SELECT id, name, shop_name FROM retailers WHERE id = ?", (rid,)
        ).fetchone()
        scheme = db.execute(
            "SELECT name FROM schemes WHERE id = ?", (rows[0]["scheme_id"],)
        ).fetchone() if rows[0]["scheme_id"] else None
        balance = _balance(db, rid)
        return {
            **out, "redeemed": True,
            "reversible": balance >= total,
            "reason": None if balance >= total
            else "Retailer's balance is below the scanned points",
            "item_count": len(rows),
            "scanned_at": g["redeemed_at"],
            "points": total,
            "base_points": sum(r["base_points"] for r in rows),
            "bonus_points": sum(r["bonus_points"] for r in rows),
            "scheme_name": scheme["name"] if scheme else None,
            "retailer": dict(retailer) if retailer else {"id": rid},
            "retailer_balance": balance,
        }


class ReverseIn(BaseModel):
    code: str
    note: str | None = Field(None, max_length=300)


@app.post("/scans/reverse")
def reverse_scan(body: ReverseIn, user: dict = Depends(current_manufacturer)):
    """Undo a scan credited to the wrong retailer: deduct exactly the points
    the ledger recorded (base + bonus at scan time) and re-open the code(s)
    so the rightful retailer can scan them again. Box scans reverse whole."""
    with get_db() as db:
        g = _resolve_scan_group(db, user["id"], body.code)
        rows = _scan_rows(db, g)
        if not rows:
            raise HTTPException(409, "Code is not redeemed")
        rid = rows[0]["retailer_id"]
        total = sum(r["points"] for r in rows)
        # ponytail: no negative balances for now — the expected flow is to
        # reject the retailer's pending gift claim first (refund) and then
        # reverse. Relax to allow a negative wallet if that ever changes.
        if _balance(db, rid) < total:
            raise HTTPException(
                409, "Retailer's balance is below the scanned points; "
                     "reject their pending gift claims first")
        # Atomic claim of the reversal (mirrors /scan's conditional UPDATE):
        # a concurrent reversal of the same group loses on rowcount and rolls
        # back, so points are never deducted twice.
        ids = [r["id"] for r in rows]
        qs = ",".join("?" * len(ids))
        cur = db.execute(
            f"""UPDATE points_ledger SET entry_type = 'scan_reversed'
                WHERE id IN ({qs}) AND entry_type = 'scan'""",
            ids,
        )
        if cur.rowcount != len(ids):
            raise HTTPException(409, "Scan already reversed")
        note = (body.note or "").strip() or "Reversed — scanned by wrong retailer"
        for r in rows:
            db.execute(
                """INSERT INTO points_ledger
                   (manufacturer_id, retailer_id, entry_type, token,
                    product_id, product_external_id, product_name,
                    product_sku, points, base_points, bonus_points,
                    scheme_id, region, distributor_id, note, created_by)
                   VALUES (?, ?, 'reversal', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?)""",
                (user["id"], rid, r["token"], r["product_id"],
                 r["product_external_id"], r["product_name"],
                 r["product_sku"], -r["points"], -r["base_points"],
                 -r["bonus_points"], r["scheme_id"], r["region"],
                 r["distributor_id"], note, user["id"]),
            )
        # Re-open the codes so the rightful retailer can scan them.
        qs = ",".join("?" * len(g["tokens"]))
        db.execute(
            f"""UPDATE qr_codes SET redeemed_at = NULL, redeemed_by = NULL
                WHERE token IN ({qs})""",
            g["tokens"],
        )
        if g["is_box"]:
            db.execute(
                """UPDATE qr_codes SET redeemed_at = NULL, redeemed_by = NULL
                   WHERE token = ?""",
                (g["group_token"],),
            )
        return {
            "reversed": True,
            "is_box": g["is_box"],
            "items": len(rows),
            "points_deducted": total,
            "retailer_id": rid,
            "new_balance": _balance(db, rid),
            "token": g["group_token"],
        }


# ---------- dashboard (manufacturer-scoped) ----------

@app.get("/analytics/dashboard")
def dashboard(user: dict = Depends(current_manufacturer)):
    mid = user["id"]
    with get_db() as db:
        totals = db.execute(
            """SELECT
                 (SELECT COUNT(*) FROM retailers WHERE manufacturer_id = :m)
                     AS retailers,
                 (SELECT COUNT(*) FROM product_points
                  WHERE manufacturer_id = :m AND source = 'import')
                     AS products,
                 (SELECT COUNT(*) FROM points_ledger
                  WHERE manufacturer_id = :m AND entry_type = 'scan')
                     AS scans,
                 (SELECT COALESCE(SUM(points), 0) FROM points_ledger
                  WHERE manufacturer_id = :m AND entry_type = 'scan')
                     AS points_awarded,
                 (SELECT COUNT(*) FROM qr_codes c
                  JOIN qr_batches b ON b.id = c.batch_id
                  WHERE b.manufacturer_id = :m) AS codes_issued,
                 (SELECT COUNT(*) FROM gift_claims
                  WHERE manufacturer_id = :m) AS redeem_total,
                 (SELECT COUNT(*) FROM gift_claims
                  WHERE manufacturer_id = :m AND status = 'pending')
                     AS redeem_pending,
                 (SELECT COUNT(*) FROM gift_claims
                  WHERE manufacturer_id = :m AND status = 'approved')
                     AS redeem_approved""",
            {"m": mid},
        ).fetchone()
        by_region = db.execute(
            """SELECT COALESCE(NULLIF(region, ''), 'Unspecified') AS region,
                      COUNT(*) AS scans, SUM(points) AS points
               FROM points_ledger WHERE manufacturer_id = ?
                 AND entry_type = 'scan'
               GROUP BY COALESCE(NULLIF(region, ''), 'Unspecified')
               ORDER BY scans DESC""",
            (mid,),
        ).fetchall()
        # Grouped by the product reference from scan ledger snapshots (no products
        # join). Key falls back external_id -> sku -> legacy product_id so both
        # new (Vastra) and pre-migration rows group correctly. Note: only products
        # with scan activity appear (loyalty is no longer the product catalog).
        by_product = db.execute(
            """SELECT COALESCE(l.product_external_id, l.product_sku,
                               CAST(l.product_id AS CHAR)) AS id,
                      MAX(l.product_name) AS name,
                      MAX(l.product_sku) AS sku,
                      MAX(l.product_external_id) AS product_external_id,
                      COUNT(l.id) AS scans,
                      COALESCE(SUM(l.points), 0) AS points
               FROM points_ledger l
               WHERE l.manufacturer_id = ? AND l.entry_type = 'scan'
               GROUP BY COALESCE(l.product_external_id, l.product_sku,
                                 CAST(l.product_id AS CHAR))
               ORDER BY scans DESC""",
            (mid,),
        ).fetchall()
        by_distributor = db.execute(
            """SELECT d.id, d.name,
                      (SELECT COUNT(*) FROM retailers r
                       WHERE r.distributor_id = d.id) AS retailers,
                      COUNT(l.id) AS scans,
                      COALESCE(SUM(l.points), 0) AS points
               FROM distributors d
               LEFT JOIN points_ledger l
                 ON l.distributor_id = d.id AND l.entry_type = 'scan'
               WHERE d.manufacturer_id = ?
               GROUP BY d.id, d.name ORDER BY scans DESC""",
            (mid,),
        ).fetchall()
        top_retailers = db.execute(
            """SELECT r.id, r.name, r.shop_name, r.region, COUNT(*) AS scans,
                      SUM(l.points) AS points
               FROM points_ledger l JOIN retailers r ON r.id = l.retailer_id
               WHERE l.manufacturer_id = ? AND l.entry_type = 'scan'
               GROUP BY r.id ORDER BY points DESC LIMIT 10""",
            (mid,),
        ).fetchall()
        # Monthly QR generation vs scans. Month bucket is substr(...,1,7) ->
        # 'YYYY-MM', portable across both SQLite and MySQL (no strftime).
        gen_rows = db.execute(
            """SELECT substr(c.created_at, 1, 7) AS month, COUNT(*) AS n
               FROM qr_codes c
               JOIN qr_batches b ON b.id = c.batch_id
               WHERE b.manufacturer_id = ?
               GROUP BY substr(c.created_at, 1, 7)""",
            (mid,),
        ).fetchall()
        scan_month_rows = db.execute(
            """SELECT substr(scanned_at, 1, 7) AS month, COUNT(*) AS n
               FROM points_ledger
               WHERE manufacturer_id = ? AND entry_type = 'scan'
               GROUP BY substr(scanned_at, 1, 7)""",
            (mid,),
        ).fetchall()
        # One dot per place a retailer actually scanned: use the per-scan GPS
        # captured at scan time, falling back to the retailer's pinned shop
        # coords for scans recorded without a location. Bucketed in Python by
        # ~11m (4 decimals) so repeated scans at one spot collapse to a single
        # weighted dot while distinct spots stay separate (the panel map clusters
        # nearby dots for display); keeps the SQL backend-portable.
        scan_rows = db.execute(
            """SELECT r.id, r.name, r.shop_name, r.region,
                      COALESCE(l.lat, r.lat) AS lat,
                      COALESCE(l.lng, r.lng) AS lng,
                      l.points AS points, l.scanned_at AS scanned_at
               FROM points_ledger l JOIN retailers r ON r.id = l.retailer_id
               WHERE l.manufacturer_id = ? AND l.entry_type = 'scan'
                 AND COALESCE(l.lat, r.lat) IS NOT NULL
                 AND COALESCE(l.lng, r.lng) IS NOT NULL""",
            (mid,),
        ).fetchall()
    buckets: dict[tuple, dict] = {}
    for s in scan_rows:
        lat, lng = round(s["lat"], 4), round(s["lng"], 4)
        key = (s["id"], lat, lng)
        b = buckets.get(key)
        if b is None:
            b = {"id": s["id"], "name": s["name"], "shop_name": s["shop_name"],
                 "region": s["region"], "lat": lat, "lng": lng,
                 "scans": 0, "points": 0, "last_scan": s["scanned_at"]}
            buckets[key] = b
        b["scans"] += 1
        b["points"] += s["points"] or 0
        if s["scanned_at"] and s["scanned_at"] > (b["last_scan"] or ""):
            b["last_scan"] = s["scanned_at"]
    map_points = list(buckets.values())
    # Merge generation + scan counts into one {month, generated, scanned} series.
    months: dict[str, dict] = {}
    for row in gen_rows:
        m = row["month"]
        if m:
            months.setdefault(m, {"month": m, "generated": 0, "scanned": 0})
            months[m]["generated"] = row["n"]
    for row in scan_month_rows:
        m = row["month"]
        if m:
            months.setdefault(m, {"month": m, "generated": 0, "scanned": 0})
            months[m]["scanned"] = row["n"]
    by_month = [months[m] for m in sorted(months)]
    # Mirror /catalog/products' precedence so the card agrees with the Products
    # tab: imported rows, else the samples when they are switched on.
    totals = dict(totals)
    if not totals["products"] and USE_SAMPLE_PRODUCTS:
        totals["products"] = len(_SAMPLE_PRODUCTS)
    return {
        "totals": totals,
        "by_region": [dict(r) for r in by_region],
        "by_product": [dict(r) for r in by_product],
        "by_distributor": [dict(r) for r in by_distributor],
        "top_retailers": [dict(r) for r in top_retailers],
        "map_points": [dict(r) for r in map_points],
        "by_month": by_month,
    }


# ---------- gift catalog (manufacturer-scoped) ----------

class GiftIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    points_cost: int = Field(ge=1)
    image_url: str | None = None  # URL or data: URI (uploaded photo)


class GiftUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    points_cost: int | None = Field(default=None, ge=1)
    image_url: str | None = None  # URL or data: URI (uploaded photo)
    active: bool | None = None


@app.post("/gifts", status_code=201)
def create_gift(body: GiftIn, user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO gifts
               (manufacturer_id, name, description, points_cost, image_url)
               VALUES (?, ?, ?, ?, ?)""",
            (user["id"], body.name, body.description, body.points_cost,
             body.image_url),
        )
        row = db.execute(
            "SELECT * FROM gifts WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


@app.get("/gifts")
def list_gifts(user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        rows = db.execute(
            """SELECT g.*,
                      (SELECT COUNT(*) FROM gift_claims gc
                       WHERE gc.gift_id = g.id) AS claims
               FROM gifts g WHERE g.manufacturer_id = ? ORDER BY g.id""",
            (user["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.patch("/gifts/{gift_id}")
def update_gift(gift_id: int, body: GiftUpdate,
                user: dict = Depends(current_manufacturer)):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "active" in fields:
        fields["active"] = 1 if fields["active"] else 0
    if not fields:
        raise HTTPException(422, "Nothing to update")
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM gifts WHERE id = ? AND manufacturer_id = ?",
            (gift_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Gift not found")
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE gifts SET {sets} WHERE id = ?",
                   [*fields.values(), gift_id])
        out = db.execute(
            "SELECT * FROM gifts WHERE id = ?", (gift_id,)).fetchone()
    return dict(out)


@app.delete("/gifts/{gift_id}", status_code=204)
def delete_gift(gift_id: int, user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM gifts WHERE id = ? AND manufacturer_id = ?",
            (gift_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Gift not found")
        used = db.execute(
            "SELECT COUNT(*) AS n FROM gift_claims WHERE gift_id = ?",
            (gift_id,),
        ).fetchone()["n"]
        if used:
            raise HTTPException(
                409, "Gift has claims; deactivate it instead of deleting")
        db.execute("DELETE FROM gifts WHERE id = ?", (gift_id,))


@app.delete("/gifts")
def delete_all_gifts(user: dict = Depends(current_manufacturer)):
    """Clear the reward shop (the panel's "Delete all", behind a confirmation).

    Rewards that already have claims are kept, not deleted — a claim has to
    keep pointing at the reward it was made for, which is the same rule the
    single-reward delete enforces with a 409. Returns how many of each."""
    mid = user["id"]
    with get_db() as db:
        keep = db.execute(
            """SELECT COUNT(*) AS n FROM gifts g
               WHERE g.manufacturer_id = ?
                 AND EXISTS (SELECT 1 FROM gift_claims gc
                             WHERE gc.gift_id = g.id)""", (mid,)).fetchone()["n"]
        gone = db.execute(
            """SELECT COUNT(*) AS n FROM gifts g
               WHERE g.manufacturer_id = ?
                 AND NOT EXISTS (SELECT 1 FROM gift_claims gc
                                 WHERE gc.gift_id = g.id)""",
            (mid,)).fetchone()["n"]
        db.execute(
            """DELETE FROM gifts
               WHERE manufacturer_id = ?
                 AND NOT EXISTS (SELECT 1 FROM gift_claims gc
                                 WHERE gc.gift_id = gifts.id)""", (mid,))
    return {"deleted": gone, "skipped": keep}


@app.post("/gifts/import")
@limiter.limit(RL_IMPORT)
def import_gifts_csv(request: Request, body: ImportIn,
                     user: dict = Depends(current_manufacturer)):
    """Bulk-create rewards from CSV text.

    Same generous header matching as the other imports (`_CSV_GIFT_*`). A
    reward name and a points column are both required — a reward with no price
    in points cannot go in the shop — while description and image are optional.
    The image cell may be a plain URL or the JSON array the client's export
    writes (`_first_image`). Rows whose name already exists are skipped."""
    import csv as csvmod
    import io
    mid = user["id"]
    reader = csvmod.DictReader(io.StringIO(body.csv))
    fields = [f for f in (reader.fieldnames or []) if (f or "").strip()]
    norm = {f: _norm_header(f) for f in fields}
    name_col = _pick_col(fields, norm, _CSV_GIFT_NAME_HEADERS)
    points_col = _pick_col(fields, norm, _CSV_GIFT_POINTS_HEADERS)
    missing = []
    if not name_col:
        missing.append("a reward name column (one of: "
                       + ", ".join(_CSV_GIFT_NAME_HEADERS) + ")")
    if not points_col:
        missing.append("a points column (one of: "
                       + ", ".join(_CSV_GIFT_POINTS_HEADERS) + ")")
    if missing:
        raise HTTPException(422, "CSV is missing " + " and ".join(missing))
    desc_col = _pick_col(fields, norm, _CSV_GIFT_DESC_HEADERS)
    image_col = _pick_col(fields, norm, _CSV_GIFT_IMAGE_HEADERS)
    created = skipped = 0
    errors: list[str] = []
    with get_db() as db:
        for i, raw in enumerate(reader, start=2):  # row 1 is the header
            name = _clean_cell(raw.get(name_col))
            if not name:
                errors.append(f"row {i}: missing reward name")
                continue
            cell = _clean_cell(raw.get(points_col))
            try:
                points = int(float(cell))
            except ValueError:
                skipped += 1
                errors.append(f"row {i}: points '{cell}' is not a number")
                continue
            if points < 1:
                skipped += 1
                errors.append(f"row {i}: points must be 1 or more")
                continue
            if db.execute(
                """SELECT 1 FROM gifts
                   WHERE manufacturer_id = ? AND LOWER(name) = LOWER(?)""",
                (mid, name),
            ).fetchone():
                skipped += 1
                continue
            image = _first_image(raw.get(image_col)) if image_col else ""
            if len(image) > 500:  # gifts.image_url is VARCHAR(500)
                errors.append(f"row {i}: image URL too long — reward imported "
                              "without it")
                image = ""
            db.execute(
                """INSERT INTO gifts
                   (manufacturer_id, name, description, points_cost, image_url)
                   VALUES (?, ?, ?, ?, ?)""",
                (mid, name,
                 (_clean_cell(raw.get(desc_col)) if desc_col else ""),
                 points, image or None),
            )
            created += 1
    return {"created": created, "skipped": skipped, "errors": errors,
            "columns": {"name": name_col, "points_cost": points_col,
                        "description": desc_col, "image_url": image_col}}


# ---------- gift shop + claims (retailer side, authenticated) ----------

@app.get("/retailer/shop")
def retailer_shop(retailer: dict = Depends(current_retailer)):
    """Shop view for the logged-in retailer: wallet balance + the active
    gifts of their manufacturer, flagged affordable or not."""
    rid = retailer["id"]
    with get_db() as db:
        balance = _balance(db, rid)
        gifts = db.execute(
            """SELECT id, name, description, points_cost, image_url
               FROM gifts WHERE manufacturer_id = ? AND active = 1
               ORDER BY points_cost""",
            (retailer["manufacturer_id"],),
        ).fetchall()
    return {
        "retailer_id": rid,
        "shop_name": retailer["shop_name"],
        "balance": balance,
        "gifts": [
            {**dict(g), "affordable": balance >= g["points_cost"]}
            for g in gifts
        ],
    }


@app.post("/retailer/claim", status_code=201)
@limiter.limit(RL_CLAIM)
def claim_gift(request: Request, body: GiftClaimIn,
               retailer: dict = Depends(current_retailer)):
    """Logged-in retailer claims a gift. Points are deducted immediately; a
    rejected claim refunds them. The manufacturer fulfils the gift offline."""
    rid = retailer["id"]
    with get_db() as db:
        gift = db.execute(
            "SELECT * FROM gifts WHERE id = ? AND active = 1", (body.gift_id,)
        ).fetchone()
        if not gift:
            raise HTTPException(404, "Gift not available")
        if gift["manufacturer_id"] != retailer["manufacturer_id"]:
            raise HTTPException(403, "Gift belongs to another manufacturer")
        # Fix 2 (claim double-spend race): serialize concurrent claims for this
        # retailer by taking a row lock on the retailer BEFORE reading the
        # balance. On MySQL, SELECT ... FOR UPDATE makes a second simultaneous
        # claim block until the first commits, so it then re-reads the
        # already-reduced balance and is correctly rejected — a wallet can never
        # go negative. This depends on the READ COMMITTED isolation level set per
        # connection in database.get_db(): under InnoDB's REPEATABLE READ default
        # the _balance() re-read below would still see the pre-claim snapshot.
        # SQLite serializes writers at the database level, so no explicit lock
        # clause is needed (and it has no FOR UPDATE syntax).
        if IS_MYSQL:
            db.execute("SELECT id FROM retailers WHERE id = ? FOR UPDATE", (rid,))
        if _balance(db, rid) < gift["points_cost"]:
            raise HTTPException(409, "Not enough points")

        debit = db.execute(
            """INSERT INTO points_ledger
               (manufacturer_id, retailer_id, entry_type, points, note)
               VALUES (?, ?, 'gift_redeem', ?, ?)""",
            (gift["manufacturer_id"], rid, -gift["points_cost"],
             f"Claim: {gift['name']}"),
        )
        existing = {
            r["reference"] for r in db.execute(
                "SELECT reference FROM gift_claims WHERE reference IS NOT NULL")
        }
        reference = new_reference()
        while reference in existing:
            reference = new_reference()
        cur = db.execute(
            """INSERT INTO gift_claims
               (manufacturer_id, retailer_id, gift_id, reference, points_spent,
                ledger_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (gift["manufacturer_id"], rid, body.gift_id, reference,
             gift["points_cost"], debit.lastrowid),
        )
        return {
            "claim_id": cur.lastrowid,
            "reference": reference,
            "gift": gift["name"],
            "points_spent": gift["points_cost"],
            "status": "pending",
            "new_balance": _balance(db, rid),
        }


@app.get("/retailer/claims")
def retailer_claims(retailer: dict = Depends(current_retailer)):
    """The logged-in retailer's gift claim history (order-history style)."""
    with get_db() as db:
        rows = db.execute(
            """SELECT gc.id, gc.reference, gc.points_spent, gc.status,
                      gc.created_at, gc.decided_at,
                      g.name AS gift_name, g.image_url, g.description
               FROM gift_claims gc JOIN gifts g ON g.id = gc.gift_id
               WHERE gc.retailer_id = ? ORDER BY gc.id DESC""",
            (retailer["id"],),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["reference"] = d["reference"] or f"CLM-{d['id']}"
        out.append(d)
    return out


@app.get("/gift-claims")
def list_gift_claims(status: str | None = Query(
        None, pattern="^(pending|approved|rejected)$"),
        user: dict = Depends(current_manufacturer)):
    sql = """SELECT gc.id, gc.reference, gc.points_spent, gc.status,
                    gc.created_at, gc.decided_at, g.name AS gift_name,
                    r.id AS retailer_id, r.shop_name, r.name AS retailer_name,
                    r.region
             FROM gift_claims gc
             JOIN gifts g ON g.id = gc.gift_id
             JOIN retailers r ON r.id = gc.retailer_id
             WHERE gc.manufacturer_id = ?"""
    args: list = [user["id"]]
    if status:
        sql += " AND gc.status = ?"
        args.append(status)
    sql += " ORDER BY gc.id DESC"
    with get_db() as db:
        rows = db.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def _decide_claim(db, claim_id, manufacturer_id):
    claim = db.execute(
        "SELECT * FROM gift_claims WHERE id = ? AND manufacturer_id = ?",
        (claim_id, manufacturer_id),
    ).fetchone()
    if not claim:
        raise HTTPException(404, "Claim not found")
    if claim["status"] != "pending":
        raise HTTPException(409, f"Claim already {claim['status']}")
    return claim


@app.post("/gift-claims/{claim_id}/approve")
def approve_claim(claim_id: int, user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        _decide_claim(db, claim_id, user["id"])
        db.execute(
            """UPDATE gift_claims SET status = 'approved',
               decided_at = datetime('now') WHERE id = ?""",
            (claim_id,),
        )
    return {"claim_id": claim_id, "status": "approved"}


@app.post("/gift-claims/{claim_id}/reject")
def reject_claim(claim_id: int, user: dict = Depends(current_manufacturer)):
    with get_db() as db:
        claim = _decide_claim(db, claim_id, user["id"])
        # Refund the points that were deducted at claim time.
        db.execute(
            """INSERT INTO points_ledger
               (manufacturer_id, retailer_id, entry_type, points, note)
               VALUES (?, ?, 'refund', ?, ?)""",
            (user["id"], claim["retailer_id"], claim["points_spent"],
             "Refund: gift claim rejected"),
        )
        db.execute(
            """UPDATE gift_claims SET status = 'rejected',
               decided_at = datetime('now') WHERE id = ?""",
            (claim_id,),
        )
    return {"claim_id": claim_id, "status": "rejected",
            "refunded": claim["points_spent"]}


# ---------- webview pages + built panel ----------

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/panel/")

@app.get("/web", include_in_schema=False)
def web_home():
    return FileResponse(WEB_DIR / "home.html")


@app.get("/web/generate", include_in_schema=False)
def web_generate():
    return FileResponse(WEB_DIR / "generate.html")


@app.get("/web/scan", include_in_schema=False)
@app.get("/web/scan/{token}", include_in_schema=False)
def web_scan(token: str | None = None):
    return FileResponse(WEB_DIR / "scan.html")


@app.get("/web/shop", include_in_schema=False)
def web_shop():
    return FileResponse(WEB_DIR / "shop.html")


@app.get("/web/claims", include_in_schema=False)
def web_claims():
    return FileResponse(WEB_DIR / "claims.html")


@app.get("/web/vastra-logo.png", include_in_schema=False)
def web_logo():
    return FileResponse(WEB_DIR / "vastra-logo.png", media_type="image/png")


if PANEL_DIST.exists():
    app.mount("/panel", StaticFiles(directory=PANEL_DIST, html=True),
              name="panel")
