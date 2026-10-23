"""Staff accounts: a team working one shop."""

import uuid
from unittest import mock

import pytest

from app.api.v1.endpoints import events, staff as staff_endpoints
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.user import User
from tests.conftest import PASSWORD, Seller
from tests.test_receipts import order_with_receipt


def add_staff(client, owner, name="Dara", email=None):
    """Add a staff member and sign them in through the emailed invite."""
    email = email or f"{uuid.uuid4().hex[:10]}@example.com"
    with mock.patch.object(staff_endpoints, "send_staff_invite") as invite:
        r = client.post("/api/v1/staff/", json={"email": email, "full_name": name},
                        headers=owner.headers)
    assert r.status_code == 201, r.text
    token = invite.call_args.args[1]
    assert invite.call_args.args[0] == email
    r = client.post("/api/v1/auth/reset-password",
                    json={"token": token, "new_password": PASSWORD})
    assert r.status_code == 200, r.text
    login = client.post("/api/v1/auth/login", data={"username": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return Seller(client, email, {"Authorization": f"Bearer {login.json()['access_token']}"})


@pytest.fixture
def staff(client, seller):
    return add_staff(client, seller)


def test_the_invite_link_sets_a_password_and_signs_them_in(client, seller, staff):
    me = client.get("/api/v1/auth/me", headers=staff.headers).json()
    assert me["role"] == "staff"
    assert me["member_name"] == "Dara"
    assert me["member_email"] == staff.email
    # The shop they see is their owner's.
    owner = client.get("/api/v1/auth/me", headers=seller.headers).json()
    assert me["id"] == owner["id"] and me["business_name"] == owner["business_name"]
    assert owner["role"] == "owner"


def test_staff_and_owner_work_the_same_catalogue_and_inbox(client, seller, staff):
    owners = seller.product(name="Silk Scarf")
    made_by_staff = staff.product(name="Krama")
    conversation = seller.conversation()

    names = {p["name"] for p in client.get("/api/v1/products/", headers=staff.headers).json()}
    assert names == {"Silk Scarf", "Krama"}
    assert client.get(f"/api/v1/products/{made_by_staff['id']}",
                      headers=seller.headers).status_code == 200
    assert [c["id"] for c in client.get("/api/v1/conversations/", headers=staff.headers).json()] \
        == [conversation["id"]]
    assert owners  # created by the owner, visible to staff above


def test_a_staff_reply_takes_the_thread_in_their_own_name(client, seller, staff, db):
    conversation = seller.conversation()
    r = client.post(f"/api/v1/conversations/{conversation['id']}/messages",
                    json={"content": "I'll help you", "message_type": "text"},
                    headers=staff.headers)
    assert r.status_code == 201, r.text

    staff_id = db.query(User).filter(User.email == staff.email).one().id
    row = db.query(Conversation).one()
    assert row.assigned_user_id == staff_id
    listed = client.get("/api/v1/conversations/?assignment=me", headers=staff.headers).json()
    assert [c["id"] for c in listed] == [conversation["id"]]
    assert listed[0]["assigned_user_name"] == "Dara"
    assert client.get("/api/v1/conversations/?assignment=me", headers=seller.headers).json() == []


def test_a_thread_can_be_assigned_to_a_teammate_but_not_a_stranger(
        client, seller, staff, other_seller, db):
    conversation = seller.conversation()
    staff_id = str(db.query(User).filter(User.email == staff.email).one().id)
    stranger = str(db.query(User).filter(User.email == other_seller.email).one().id)

    ok = client.patch(f"/api/v1/conversations/{conversation['id']}",
                      json={"assigned_user_id": staff_id}, headers=seller.headers)
    assert ok.status_code == 200 and ok.json()["assigned_user_id"] == staff_id
    refused = client.patch(f"/api/v1/conversations/{conversation['id']}",
                           json={"assigned_user_id": stranger}, headers=seller.headers)
    assert refused.status_code == 404


def test_confirming_a_payment_records_who_did_it(client, seller, staff, db):
    order, receipt, _customer, _product = order_with_receipt(seller, db)
    r = client.post(f"/api/v1/orders/{order['id']}/receipts/{receipt.id}/confirm",
                    headers=staff.headers)
    assert r.status_code == 200, r.text
    staff_id = db.query(User).filter(User.email == staff.email).one().id
    db.expire_all()
    assert db.query(Attachment).filter(Attachment.id == receipt.id).one().reviewed_by_user_id \
        == staff_id


@pytest.mark.parametrize("method, path, body", [
    ("post", "/api/v1/integrations/", {"platform": "telegram", "external_id": "x",
                                      "access_token": "t"}),
    ("get", "/api/v1/integrations/", None),
    ("post", "/api/v1/auth/telegram-link", None),
    ("patch", "/api/v1/auth/me", {"business_name": "Mine now"}),
    ("patch", "/api/v1/auth/me", {"khqr_enabled": True}),
    ("post", "/api/v1/staff/", {"email": "x@example.com", "full_name": "X"}),
])
def test_staff_cannot_touch_the_shops_keys_or_team(client, seller, staff, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    r = getattr(client, method)(path, headers=staff.headers, **kwargs)
    assert r.status_code == 403, r.text


def test_staff_cannot_change_payment_qrs(client, seller, staff):
    r = client.post("/api/v1/payment-qrs/", data={"name": "Mine"},
                    files={"file": ("qr.png", b"\x89PNG\r\n\x1a\nx", "image/png")},
                    headers=staff.headers)
    assert r.status_code == 403
    assert client.get("/api/v1/payment-qrs/", headers=staff.headers).status_code == 200


def test_staff_may_change_their_own_name_and_language(client, seller, staff, db):
    r = client.patch("/api/v1/auth/me", json={"full_name": "Dara Sok", "language": "km"},
                     headers=staff.headers)
    assert r.status_code == 200, r.text
    assert r.json()["member_name"] == "Dara Sok"
    owner = db.query(User).filter(User.email == seller.email).one()
    assert owner.full_name == "Sok Dara" and owner.language == "en"


def test_removing_someone_signs_them_out_and_frees_their_threads(client, seller, staff, db):
    conversation = seller.conversation()
    client.post(f"/api/v1/conversations/{conversation['id']}/messages",
                json={"content": "hi", "message_type": "text"}, headers=staff.headers)
    staff_id = db.query(User).filter(User.email == staff.email).one().id

    assert client.delete(f"/api/v1/staff/{staff_id}", headers=seller.headers).status_code == 204

    assert client.get("/api/v1/products/", headers=staff.headers).status_code == 401
    db.expire_all()
    assert db.query(Conversation).one().assigned_user_id is None
    members = client.get("/api/v1/staff/", headers=seller.headers).json()
    assert [(m["role"], m["is_active"]) for m in members] == [("owner", True), ("staff", False)]


def test_an_email_already_in_use_is_refused(client, seller, other_seller):
    r = client.post("/api/v1/staff/", json={"email": other_seller.email, "full_name": "X"},
                    headers=seller.headers)
    assert r.status_code == 409


def test_another_shop_cannot_manage_this_shops_staff(client, seller, staff, other_seller, db):
    staff_id = db.query(User).filter(User.email == staff.email).one().id
    r = client.patch(f"/api/v1/staff/{staff_id}", json={"is_active": False},
                     headers=other_seller.headers)
    assert r.status_code == 404
    assert len(client.get("/api/v1/staff/", headers=other_seller.headers).json()) == 1


def test_closing_the_shop_closes_its_staff_out(client, seller, staff, db):
    db.query(User).filter(User.email == seller.email).update({User.is_active: False})
    db.commit()
    assert client.get("/api/v1/products/", headers=staff.headers).status_code == 401


def test_staff_receive_their_shops_live_events(client, seller, staff, db):
    token = staff.headers["Authorization"].removeprefix("Bearer ")
    owner_id = str(db.query(User).filter(User.email == seller.email).one().id)
    assert events._shop_for(token) == owner_id
