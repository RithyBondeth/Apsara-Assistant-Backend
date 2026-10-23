"""The assistant sending a product's photo."""

import uuid

from app.models.message import Message
from tests import ai, webhooks as wh
from tests.test_product_images import JPEG, PNG, upload
from tests.test_seller_alerts import alerts, customer_says, link, the_conversation


def with_photo(client, seller, name="Silk Krama", files=None):
    product = seller.product(name=name)
    upload(client, seller, product["id"], files or [("front.png", PNG, "image/png")])
    detail = client.get(f"/api/v1/products/{product['id']}", headers=seller.headers).json()
    return detail


def rehearse(client, seller, conversation, reply, text="show me"):
    with ai.replies(reply) as captured:
        r = client.post(f"/api/v1/chat/{conversation['id']}",
                        json={"message": text, "message_type": "text"}, headers=seller.headers)
    assert r.status_code == 200, r.text
    return r.json(), captured


def test_only_products_with_a_photo_are_offered_one(client, seller):
    pictured = with_photo(client, seller)
    plain = seller.product(name="Plain Mug")

    _, captured = rehearse(client, seller, seller.conversation(), "ok", text="hi")

    prompt = captured.system_prompt
    assert f"Photo available: [SEND_PHOTO:{pictured['id']}]" in prompt
    assert f"[SEND_PHOTO:{plain['id']}]" not in prompt
    assert "PRODUCT PHOTOS" in prompt


def test_a_shop_without_photos_gets_no_photo_rules(client, seller):
    seller.product(name="Plain Mug")
    _, captured = rehearse(client, seller, seller.conversation(), "ok", text="hi")
    assert "PRODUCT PHOTOS" not in captured.system_prompt


def test_a_customer_on_telegram_receives_the_photo_after_the_text(client, seller, db):
    product = with_photo(client, seller)
    primary = next(image for image in product["images"] if image["is_primary"])
    integration = link(client, seller, db)

    with wh.sends() as texts, wh.sends_images() as images, alerts(), \
            ai.replies(f"Here it is!\n[SEND_PHOTO:{product['id']}]"):
        customer_says(client, integration, "can I see the krama?")

    assert [t["text"] for t in texts] == ["Here it is!"]
    assert [i["image_url"] for i in images] == [primary["url"]]
    conversation = the_conversation(client, seller)
    thread = client.get(f"/api/v1/conversations/{conversation['id']}/messages",
                        headers=seller.headers).json()
    photo = thread[-1]
    assert photo["message_type"] == "image"
    assert photo["content"] == "(Photo sent: Silk Krama)"
    assert photo["attachments"][0]["file_url"] == primary["url"]


def test_the_primary_image_is_the_one_sent(client, seller, db):
    product = with_photo(client, seller, files=[("a.png", PNG, "image/png"),
                                                ("b.jpg", JPEG, "image/jpeg")])
    second = product["images"][1]
    r = client.put(f"/api/v1/products/{product['id']}/images/order",
                   json={"image_ids": [i["id"] for i in product["images"]],
                         "primary_image_id": second["id"]}, headers=seller.headers)
    assert r.status_code == 200, r.text

    body, _ = rehearse(client, seller, seller.conversation(), f"[SEND_PHOTO:{product['id']}]")

    assert body["photo_messages"][0]["attachments"][0]["file_url"] == second["url"]


def test_an_unknown_or_foreign_id_sends_nothing_and_is_never_shown(
        client, seller, other_seller):
    theirs = with_photo(client, other_seller, name="Rival Bag")
    with_photo(client, seller)

    body, _ = rehearse(client, seller, seller.conversation(),
                       f"Sure!\n[SEND_PHOTO:{theirs['id']}]\n[SEND_PHOTO:{uuid.uuid4()}]")

    assert body["photo_messages"] == []
    assert body["ai_message"]["content"] == "Sure!"


def test_photos_are_deduplicated_and_capped(client, seller):
    ids = [with_photo(client, seller, name=f"Krama {i}")["id"] for i in range(5)]
    reply = "\n".join(f"[SEND_PHOTO:{i}]" for i in [ids[0], ids[0], *ids[1:]])

    body, _ = rehearse(client, seller, seller.conversation(), "Here:\n" + reply)

    assert [m["content"] for m in body["photo_messages"]] == [
        "(Photo sent: Krama 0)", "(Photo sent: Krama 1)", "(Photo sent: Krama 2)"]


def test_a_refused_photo_is_not_recorded_but_the_text_is(client, seller, db):
    product = with_photo(client, seller)
    integration = link(client, seller, db)

    with wh.sends(), wh.sends_images(ok=False), alerts(), \
            ai.replies(f"Here it is!\n[SEND_PHOTO:{product['id']}]"):
        customer_says(client, integration, "can I see it?")

    kinds = [m.message_type for m in db.query(Message).filter(
        Message.sender_type == "assistant").all()]
    assert kinds == ["text"]
