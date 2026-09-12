"""Importing QR codes printed by the manufacturer's previous platform.

The stickers are already in the market, so they cannot be reprinted: the codes
have to keep working exactly as printed. The payload is a link to the old
platform's host, e.g.

    http://gverify.me/?Hirshita Leggings-Qi7YhrsToYI0Dbew
                       |---- BRAND ----||- 16-char token -|

so matching is done on a canonical key (the token after the last hyphen),
derived identically at import time and at scan time. That matters because the
space in the URL comes back raw, %20 or + depending on which scanner decoded
it -- all three must resolve to the same row.

Every CSV below uses the previous platform's verbatim header row.
"""

import pytest

BRAND = "Hirshita Leggings"
TOK = ["Qi7YhrsToYI0Dbew", "Zk2MnpQrStUv3Wxy", "Ab9CdEfGhIjKlMn0"]

HEADER = ("ID,batch running code,unit code,scratch code,product code,MRP,"
          "created at,QR status\n")


def unit(tok):
    return "http://gverify.me/?" + BRAND + "-" + tok


def auth(client, username="acme", password="acmepass"):
    r = client.post("/auth/login", json={"username": username,
                                         "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def rauth(token):
    return {"Authorization": f"Bearer {token}"}


def row(i, tok, product="SKU-1", scratch=None, status="1"):
    return (f"{100000 + i},BR-{i},{unit(tok)},{scratch or ('12345' + str(i))},"
            f"{product},0.00,2025-04-11 10:22:31,{status}\n")


def csv_of(*rows):
    return HEADER + "".join(rows)


def catalog(mid, external_id="SKU-1", points=25, name="Saree"):
    """An imported catalog product -- the legacy import reads its points."""
    from app.database import get_db
    with get_db() as db:
        db.execute(
            "INSERT INTO product_points (manufacturer_id, product_external_id,"
            " points, name, sku, source) VALUES (?,?,?,?,?,'import')",
            (mid, external_id, points, name, external_id))


def clear_imported():
    """Wipe imported codes so the next form can be tested on a fresh sticker."""
    from app.database import get_db
    with get_db() as db:
        db.execute("DELETE FROM points_ledger")
        db.execute("DELETE FROM qr_codes WHERE legacy_code IS NOT NULL")
        db.execute("DELETE FROM qr_batches WHERE source = 'imported'")


def test_import_creates_codes_and_is_idempotent(client, seed):
    h = auth(client)
    catalog(seed["mid"])
    body = {"csv": csv_of(row(1, TOK[0]), row(2, TOK[1]))}

    r = client.post("/qr/codes/import", json=body, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] == 2
    assert out["skipped"] == 0
    assert out["errors"] == []
    # The resolved mapping is echoed so a mis-guessed column is visible.
    assert out["columns"]["unit_code"] == "unit code"
    assert out["columns"]["scratch"] == "scratch code"
    assert out["columns"]["product_code"] == "product code"

    # Re-sending the same chunk must change nothing -- this is what makes a
    # half-failed 100k upload safe to simply retry.
    again = client.post("/qr/codes/import", json=body, headers=h).json()
    assert again["created"] == 0
    assert again["skipped"] == 2


def test_points_come_from_the_catalog(client, seed):
    h = auth(client)
    catalog(seed["mid"], points=25)
    client.post("/qr/codes/import", json={"csv": csv_of(row(1, TOK[0]))},
                headers=h)
    r = client.post("/scan", json={"code": unit(TOK[0])},
                    headers=rauth(seed["rtoken"]))
    assert r.status_code == 200, r.text
    assert r.json()["points_awarded"] == 25


def test_all_five_code_forms_resolve_to_one_row(client, seed):
    """A scanner may hand back the URL with a raw space, %20 or +, or just the
    token. Plus the typed scratch code. All five are the same sticker."""
    h = auth(client)
    catalog(seed["mid"], points=25)
    forms = [
        unit(TOK[0]),                      # raw space, as printed
        unit(TOK[0]).replace(" ", "%20"),  # percent-encoded by the scanner
        unit(TOK[0]).replace(" ", "+"),    # plus-encoded by the scanner
        TOK[0],                            # bare token
        "123451",                          # typed scratch code
    ]
    for form in forms:
        client.post("/qr/codes/import", json={"csv": csv_of(row(1, TOK[0]))},
                    headers=h)
        r = client.post("/scan", json={"code": form},
                        headers=rauth(seed["rtoken"]))
        assert r.status_code == 200, f"{form!r} -> {r.text}"
        assert r.json()["points_awarded"] == 25
        clear_imported()


def test_imported_code_credits_only_once(client, seed):
    h = auth(client)
    catalog(seed["mid"], points=25)
    client.post("/qr/codes/import", json={"csv": csv_of(row(1, TOK[0]))},
                headers=h)
    first = client.post("/scan", json={"code": unit(TOK[0])},
                        headers=rauth(seed["rtoken"]))
    assert first.status_code == 200
    # Same sticker, different encoding -- still refused as already used.
    second = client.post("/scan", json={"code": TOK[0]},
                         headers=rauth(seed["rtoken"]))
    assert second.status_code == 409
    w = client.get("/retailer/wallet", headers=rauth(seed["rtoken"]))
    assert w.json()["balance"] == 25


def test_already_scanned_rows_import_as_redeemed(client, seed):
    """A code the old platform already burned must not be claimable again."""
    h = auth(client)
    catalog(seed["mid"])
    out = client.post("/qr/codes/import", json={
        "csv": csv_of(row(1, TOK[0], status="2"))}, headers=h).json()
    assert out["created"] == 1
    r = client.post("/scan", json={"code": unit(TOK[0])},
                    headers=rauth(seed["rtoken"]))
    assert r.status_code == 409


def test_unknown_product_code_is_an_error_not_a_silent_skip(client, seed):
    """A legacy code with no catalog product has no point value, so importing
    it would create a sticker worth an unknowable amount."""
    h = auth(client)
    catalog(seed["mid"], external_id="SKU-1")
    out = client.post("/qr/codes/import", json={
        "csv": csv_of(row(1, TOK[0], product="SKU-NOPE"))}, headers=h).json()
    assert out["created"] == 0
    assert len(out["errors"]) == 1
    assert "SKU-NOPE" in out["errors"][0]


def test_foreign_brand_prefix_is_rejected(client, seed):
    """Every row of one export shares a brand prefix; one that does not is
    another brand's file pasted in, and importing it would credit the wrong
    manufacturer's retailers."""
    h = auth(client)
    catalog(seed["mid"])
    bad = ("100002,BR-2,http://gverify.me/?Someone Else-" + TOK[1]
           + ",123452,SKU-1,0.00,2025-04-11 10:22:31,1\n")
    out = client.post("/qr/codes/import", json={
        "csv": csv_of(row(1, TOK[0]), bad)}, headers=h).json()
    assert out["created"] == 1
    assert len(out["errors"]) == 1
    assert "brand" in out["errors"][0].lower()


def test_scratch_codes_do_not_collide_across_manufacturers(client, seed):
    """6 digits is only a million codes, so two tenants importing their own
    legacy history will share some. Each must reach only its own."""
    from app.auth import hash_password, issue_retailer_token
    from app.database import get_db

    h = auth(client)
    catalog(seed["mid"], points=25)
    client.post("/qr/codes/import", json={
        "csv": csv_of(row(1, TOK[0], scratch="555555"))}, headers=h)

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO manufacturers (username, password_hash, display_name,"
            " is_admin) VALUES (?,?,?,0)",
            ("other", hash_password("otherpass"), "Other Co"))
        omid = cur.lastrowid
        cur = db.execute(
            "INSERT INTO retailers (manufacturer_id, name, shop_name, region,"
            " username, password_hash) VALUES (?,?,?,?,?,?)",
            (omid, "Om", "Om Shop", "Surat", "om", hash_password("x")))
        orid = cur.lastrowid
        ortoken = issue_retailer_token(db, orid)
        db.execute(
            "INSERT INTO product_points (manufacturer_id, product_external_id,"
            " points, name, sku, source) VALUES (?,?,?,?,?,'import')",
            (omid, "SKU-1", 70, "Other Saree", "SKU-1"))

    oh = auth(client, "other", "otherpass")
    client.post("/qr/codes/import", json={
        "csv": csv_of(row(9, TOK[2], scratch="555555"))}, headers=oh)

    # Each retailer typing the same scratch code gets their OWN tenant's code.
    a = client.post("/scan", json={"code": "555555"},
                    headers=rauth(seed["rtoken"]))
    b = client.post("/scan", json={"code": "555555"}, headers=rauth(ortoken))
    assert a.status_code == 200 and a.json()["points_awarded"] == 25
    assert b.status_code == 200 and b.json()["points_awarded"] == 70


def test_cross_tenant_legacy_code_is_a_uniform_404(client, seed):
    """The enumeration-oracle rule holds for imported codes too."""
    from app.auth import hash_password
    from app.database import get_db
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO manufacturers (username, password_hash, display_name,"
            " is_admin) VALUES (?,?,?,0)",
            ("other", hash_password("otherpass"), "Other Co"))
        omid = cur.lastrowid
        db.execute(
            "INSERT INTO product_points (manufacturer_id, product_external_id,"
            " points, name, sku, source) VALUES (?,?,?,?,?,'import')",
            (omid, "SKU-1", 70, "Other Saree", "SKU-1"))
    client.post("/qr/codes/import", json={"csv": csv_of(row(1, TOK[0]))},
                headers=auth(client, "other", "otherpass"))

    nonexistent = client.post("/scan", json={"code": "NoSuchTokenAtAll"},
                              headers=rauth(seed["rtoken"]))
    cross = client.post("/scan", json={"code": unit(TOK[0])},
                        headers=rauth(seed["rtoken"]))
    assert nonexistent.status_code == cross.status_code == 404
    assert (nonexistent.json()["detail"] == cross.json()["detail"]
            == "Invalid code")


def test_generated_codes_still_scan_unchanged(client, seed):
    """The legacy branch must not disturb the existing token/manual_code path:
    manual codes are still matched case-insensitively with dashes stripped."""
    r = client.post("/scan", json={"code": "aaa-aaa"},
                    headers=rauth(seed["rtoken"]))
    assert r.status_code == 200
    assert r.json()["points_awarded"] == 10


def test_wrong_scan_on_an_imported_code_can_be_reversed(client, seed):
    """Parity with generated codes: when the wrong retailer scans a legacy
    sticker, the manufacturer must be able to undo it. They will paste what the
    retailer read out -- the payload URL or the scratch code -- never our own
    manual_code, which is generated at import and printed nowhere."""
    h = auth(client)
    catalog(seed["mid"], points=25)
    client.post("/qr/codes/import", json={
        "csv": csv_of(row(1, TOK[0], scratch="777777"))}, headers=h)
    client.post("/scan", json={"code": unit(TOK[0])},
                headers=rauth(seed["rtoken"]))

    for code in (unit(TOK[0]), TOK[0], "777777"):
        found = client.get("/scans/lookup", params={"code": code}, headers=h)
        assert found.status_code == 200, f"{code!r} -> {found.text}"

    rev = client.post("/scans/reverse",
                      json={"code": unit(TOK[0]), "reason": "wrong shop"},
                      headers=h)
    assert rev.status_code == 200, rev.text

    # Wallet is back to zero and the code is scannable again by its real owner.
    w = client.get("/retailer/wallet", headers=rauth(seed["rtoken"]))
    assert w.json()["balance"] == 0
    again = client.post("/scan", json={"code": unit(TOK[0])},
                        headers=rauth(seed["rtoken"]))
    assert again.status_code == 200
    assert again.json()["points_awarded"] == 25


def test_chunks_accumulate_into_one_batch_per_product(client, seed):
    """The panel uploads a large file as separate requests. Each chunk must
    find the batch the previous one made, or a 100k import would leave 200
    fragmented batches and a wrong per-batch quantity."""
    from app.database import get_db
    h = auth(client)
    catalog(seed["mid"], points=25)

    for i, tok in enumerate(TOK):
        out = client.post("/qr/codes/import",
                          json={"csv": csv_of(row(i + 1, tok))},
                          headers=h).json()
        assert out["created"] == 1, out

    with get_db() as db:
        batches = db.execute(
            """SELECT id, quantity FROM qr_batches
               WHERE manufacturer_id = ? AND source = 'imported'""",
            (seed["mid"],)).fetchall()
        assert len(batches) == 1, "one batch per product, not one per chunk"
        assert batches[0]["quantity"] == 3

    # Every code from every chunk is scannable.
    for tok in TOK:
        r = client.post("/scan", json={"code": unit(tok)},
                        headers=rauth(seed["rtoken"]))
        assert r.status_code == 200, r.text
    w = client.get("/retailer/wallet", headers=rauth(seed["rtoken"]))
    assert w.json()["balance"] == 75


def test_two_products_get_two_batches(client, seed):
    """Points are frozen per batch, so codes for different products cannot
    share one."""
    from app.database import get_db
    h = auth(client)
    catalog(seed["mid"], external_id="SKU-1", points=25)
    catalog(seed["mid"], external_id="SKU-2", points=40, name="Kurti")

    out = client.post("/qr/codes/import", json={"csv": csv_of(
        row(1, TOK[0], product="SKU-1"),
        row(2, TOK[1], product="SKU-2"),
    )}, headers=h).json()
    assert out["created"] == 2, out

    with get_db() as db:
        rows = db.execute(
            """SELECT product_external_id, points_per_code, quantity
               FROM qr_batches WHERE manufacturer_id = ? AND source = 'imported'
               ORDER BY product_external_id""", (seed["mid"],)).fetchall()
    assert [(r["product_external_id"], r["points_per_code"], r["quantity"])
            for r in rows] == [("SKU-1", 25, 1), ("SKU-2", 40, 1)]

    a = client.post("/scan", json={"code": unit(TOK[0])},
                    headers=rauth(seed["rtoken"]))
    b = client.post("/scan", json={"code": unit(TOK[1])},
                    headers=rauth(seed["rtoken"]))
    assert a.json()["points_awarded"] == 25
    assert b.json()["points_awarded"] == 40


# ---------- the path this feature actually depends on ----------
#
# A printed legacy sticker points at the OLD platform's host, so a phone camera
# opens their site and never reaches us. The only way one of these codes can be
# redeemed here is an in-app scanner posting the raw decoded string to
# /yourapp/scan -- which makes these the load-bearing tests for the feature.

YOURAPP_KEY = "test-yourapp-key"
YH = {"X-API-Key": YOURAPP_KEY}
LONG_BRAND = "Some Longer Brand Name Pvt Ltd"


@pytest.fixture()
def yourapp(appmod, monkeypatch, seed):
    monkeypatch.setattr(appmod, "YOURAPP_API_KEY", YOURAPP_KEY)
    from app.database import get_db
    with get_db() as db:
        db.execute("UPDATE retailers SET phone = ? WHERE id = ?",
                   ("9876500001", seed["rid"]))
    return seed


def test_yourapp_scan_redeems_a_legacy_code(client, seed, yourapp):
    h = auth(client)
    catalog(seed["mid"], points=25)
    client.post("/qr/codes/import", json={"csv": csv_of(row(1, TOK[0]))},
                headers=h)

    r = client.post("/yourapp/scan",
                    json={"phone": "9876500001", "code": unit(TOK[0])},
                    headers=YH)
    assert r.status_code == 200, r.text
    assert r.json()["status"] is True
    assert r.json()["points_awarded"] == 25


def test_yourapp_lookup_previews_a_legacy_code(client, seed, yourapp):
    h = auth(client)
    catalog(seed["mid"], points=25)
    client.post("/qr/codes/import", json={"csv": csv_of(row(1, TOK[0]))},
                headers=h)

    r = client.post("/yourapp/qr/lookup", json={"code": unit(TOK[0])},
                    headers=YH)
    assert r.status_code == 200, r.text
    assert r.json()["qrStatus"] == "available"
    assert r.json()["total_points"] == 25


def test_a_long_brand_name_does_not_overflow_the_code_field(client, seed,
                                                            yourapp):
    """The payload length is driven by the brand name embedded in it, which is
    the manufacturer's, not ours. At the old 64-char cap a brand of ordinary
    length made every scan fail validation before it was ever looked up."""
    h = auth(client)
    catalog(seed["mid"], points=25)
    long_unit = "http://gverify.me/?" + LONG_BRAND + "-" + TOK[0]
    assert len(long_unit) > 64, "this test is pointless if it fits"
    csv = HEADER + ("100001,BR-1," + long_unit + ",123451,SKU-1,0.00,"
                    "2025-04-11 10:22:31,1\n")
    assert client.post("/qr/codes/import", json={"csv": csv},
                       headers=h).json()["created"] == 1

    for path, payload in (
        ("/yourapp/qr/lookup", {"code": long_unit}),
        ("/yourapp/scan", {"phone": "9876500001", "code": long_unit}),
    ):
        r = client.post(path, json=payload, headers=YH)
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.text}"
    # And the webview path too, which takes the same string.
    clear_imported()
    client.post("/qr/codes/import", json={"csv": csv}, headers=h)
    r = client.post("/scan", json={"code": long_unit},
                    headers=rauth(seed["rtoken"]))
    assert r.status_code == 200, r.text


# ---------- both kinds of sticker in circulation at once ----------
#
# After a migration the market holds BOTH the old platform's stickers and ours.
# Every code below is reachable by whatever string the scanner produced, and no
# code may ever resolve to a different sticker than the one that was scanned.


def test_our_own_qr_scans_when_the_full_payload_url_is_forwarded(client, seed):
    """Our printed QR encodes {QR_BASE_URL}/{token}. An in-app scanner forwards
    the whole decoded string, so the API has to accept the payload URL and not
    just the bare token -- otherwise our own stickers 404 through the very
    integration the legacy ones depend on."""
    from app.qr_service import payload_for
    r = client.post("/scan", json={"code": payload_for(seed["token"])},
                    headers=rauth(seed["rtoken"]))
    assert r.status_code == 200, r.text
    assert r.json()["points_awarded"] == 10


def test_our_own_qr_full_url_via_yourapp(client, seed, yourapp):
    from app.qr_service import payload_for
    r = client.post("/yourapp/scan",
                    json={"phone": "9876500001",
                          "code": payload_for(seed["token"])},
                    headers=YH)
    assert r.status_code == 200, r.text
    assert r.json()["points_awarded"] == 10


def test_typed_code_matching_both_a_manual_and_a_scratch_is_refused(client,
                                                                    seed):
    """Our manual_code alphabet excludes 0 and 1 but includes 2-9, so an
    all-digit manual code is possible and can equal a legacy scratch code. If
    one silently won, the retailer would redeem a sticker still sitting on a
    shelf. Refuse instead and tell them to scan."""
    from app.database import get_db
    h = auth(client)
    catalog(seed["mid"], points=25)
    clash = "234567"
    with get_db() as db:
        db.execute("UPDATE qr_codes SET manual_code = ? WHERE token = ?",
                   (clash, seed["token"]))
    client.post("/qr/codes/import", json={
        "csv": csv_of(row(1, TOK[0], scratch=clash))}, headers=h)

    r = client.post("/scan", json={"code": clash},
                    headers=rauth(seed["rtoken"]))
    assert r.status_code == 409, r.text
    assert "ambiguous" in r.json()["detail"].lower()
    # Neither sticker was touched.
    w = client.get("/retailer/wallet", headers=rauth(seed["rtoken"]))
    assert w.json()["balance"] == 0
    # Both remain individually reachable by their unambiguous forms.
    a = client.post("/scan", json={"code": seed["token"]},
                    headers=rauth(seed["rtoken"]))
    b = client.post("/scan", json={"code": unit(TOK[0])},
                    headers=rauth(seed["rtoken"]))
    assert a.status_code == 200 and a.json()["points_awarded"] == 10
    assert b.status_code == 200 and b.json()["points_awarded"] == 25


def test_unambiguous_all_digit_manual_code_still_works(client, seed):
    """The ambiguity guard must not break a plain numeric manual code when no
    legacy scratch collides with it."""
    from app.database import get_db
    with get_db() as db:
        db.execute("UPDATE qr_codes SET manual_code = ? WHERE token = ?",
                   ("234567", seed["token"]))
    r = client.post("/scan", json={"code": "234567"},
                    headers=rauth(seed["rtoken"]))
    assert r.status_code == 200, r.text
    assert r.json()["points_awarded"] == 10
