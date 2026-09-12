"""GET /qr/batches must report how many box (parent) codes each batch holds.

The panel's print-scope selector offers "Box codes only", and qr_batches stores
neither items_per_box nor a box count — so without this the saved-batches list
would offer that option on batches that have no box codes at all.
"""


def auth(client, username="acme", password="acmepass"):
    r = client.post("/auth/login", json={"username": username,
                                         "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _generate(client, headers, quantity, items_per_box=None):
    body = {"product_external_id": "P-1", "product_name": "Saree",
            "product_sku": "SKU-1", "points_per_code": 10,
            "quantity": quantity}
    if items_per_box:
        body["items_per_box"] = items_per_box
    r = client.post("/qr/generate", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def test_box_count_reported_per_batch(client, seed):
    h = auth(client)
    boxed = _generate(client, h, quantity=10, items_per_box=5)
    flat = _generate(client, h, quantity=3)
    assert boxed["boxes"] == 2  # generation already reports it

    rows = {b["id"]: b for b in client.get("/qr/batches", headers=h).json()}
    assert rows[boxed["batch_id"]]["boxes"] == 2
    # A batch generated without items_per_box has none — this is the case the
    # panel must not offer "Box codes only" for.
    assert rows[flat["batch_id"]]["boxes"] == 0
    # The seed fixture's hand-inserted batch is a single child, no boxes.
    assert rows[seed["bid"]]["boxes"] == 0


def test_box_count_survives_the_status_filter(client, seed):
    h = auth(client)
    boxed = _generate(client, h, quantity=6, items_per_box=3)
    client.post(f"/qr/batches/{boxed['batch_id']}/save", headers=h)

    saved = client.get("/qr/batches?status=saved", headers=h).json()
    assert [b["id"] for b in saved] == [boxed["batch_id"]]
    assert saved[0]["boxes"] == 2


def test_box_count_does_not_leak_across_manufacturers(client, seed):
    """The count is per batch, and batches stay manufacturer-scoped."""
    from app.auth import hash_password
    from app.database import get_db

    h = auth(client)
    _generate(client, h, quantity=4, items_per_box=2)
    with get_db() as db:
        db.execute(
            "INSERT INTO manufacturers (username, password_hash, display_name,"
            " is_admin) VALUES (?,?,?,0)",
            ("other", hash_password("otherpass"), "Other Co"))

    other = client.get("/qr/batches", headers=auth(client, "other", "otherpass"))
    assert other.json() == []
