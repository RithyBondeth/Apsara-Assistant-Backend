"""Private receipt evidence and seller-controlled payment confirmation."""

from app.models.attachment import Attachment
from app.models.message import Message
from app.models.order import Order


def order_with_receipt(seller, db):
    product = seller.product(stock=5)
    customer = seller.customer()
    conversation = seller.conversation(customer["id"])
    response = seller.order(
        customer["id"], [{"product_id": product["id"], "quantity": 1}],
        conversation_id=conversation["id"],
    )
    assert response.status_code == 201, response.text
    message = Message(
        conversation_id=conversation["id"], sender_type="customer",
        message_type="image", content="Paid",
    )
    message.attachments.append(Attachment(
        blob=b"private-receipt", file_type="image/png", file_name="receipt.png",
        file_size=15, review_status="pending",
    ))
    db.add(message)
    db.commit()
    return response.json(), message.attachments[0], customer, product


def test_receipt_content_is_private_and_tenant_scoped(client, seller, other_seller, db):
    _, receipt, _, _ = order_with_receipt(seller, db)

    own = client.get(f"/api/v1/attachments/{receipt.id}/content", headers=seller.headers)
    foreign = client.get(
        f"/api/v1/attachments/{receipt.id}/content", headers=other_seller.headers
    )

    assert own.status_code == 200
    assert own.content == b"private-receipt"
    assert own.headers["cache-control"] == "private, no-store"
    assert foreign.status_code == 404


def test_seller_can_confirm_a_receipt_for_its_order(client, seller, db):
    order, receipt, _, _ = order_with_receipt(seller, db)

    listed = client.get(f"/api/v1/orders/{order['id']}/receipts",
                        headers=seller.headers)
    confirmed = client.post(
        f"/api/v1/orders/{order['id']}/receipts/{receipt.id}/confirm",
        headers=seller.headers,
    )

    assert listed.status_code == 200
    assert listed.json()[0]["id"] == str(receipt.id)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["payment_status"] == "paid"
    assert confirmed.json()["payment_method"] == "qr"
    row = db.query(Order).filter(Order.id == order["id"]).first()
    db.refresh(receipt)
    assert row.payment_receipt_attachment_id == receipt.id
    assert row.payment_confirmed_by_user_id is not None
    assert row.paid_at is not None
    assert receipt.review_status == "accepted"
    assert receipt.reviewed_by_user_id == row.payment_confirmed_by_user_id


def test_rejected_receipt_does_not_mark_the_order_paid(client, seller, db):
    order, receipt, _, _ = order_with_receipt(seller, db)

    response = client.post(
        f"/api/v1/orders/{order['id']}/receipts/{receipt.id}/reject",
        headers=seller.headers,
    )

    assert response.status_code == 200
    assert response.json()["review_status"] == "rejected"
    row = db.query(Order).filter(Order.id == order["id"]).first()
    assert row.payment_status == "unpaid"
    assert row.paid_at is None


def test_receipt_must_come_from_the_orders_conversation(client, seller, db):
    order, _, customer, _ = order_with_receipt(seller, db)
    other_conversation = seller.conversation(customer["id"], platform="telegram")
    other_message = Message(
        conversation_id=other_conversation["id"], sender_type="customer",
        message_type="image",
    )
    other_message.attachments.append(Attachment(
        blob=b"other", file_type="image/png", file_size=5, review_status="pending"
    ))
    db.add(other_message)
    db.commit()

    response = client.post(
        f"/api/v1/orders/{order['id']}/receipts/{other_message.attachments[0].id}/confirm",
        headers=seller.headers,
    )

    assert response.status_code == 404


def test_one_receipt_cannot_pay_two_orders(client, seller, db):
    order, receipt, customer, product = order_with_receipt(seller, db)
    second = seller.order(
        customer["id"], [{"product_id": product["id"], "quantity": 1}],
        conversation_id=order["conversation_id"],
    ).json()
    first = client.post(
        f"/api/v1/orders/{order['id']}/receipts/{receipt.id}/confirm",
        headers=seller.headers,
    )
    second_attempt = client.post(
        f"/api/v1/orders/{second['id']}/receipts/{receipt.id}/confirm",
        headers=seller.headers,
    )

    assert first.status_code == 200
    assert second_attempt.status_code == 409
