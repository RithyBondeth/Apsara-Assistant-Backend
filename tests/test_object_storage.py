"""Media bytes in an S3-compatible bucket instead of Postgres.

Run against moto's in-process S3, so the boto3 calls are real ones.
"""

from unittest import mock

import boto3
import pytest
from moto import mock_aws

from app.core.config import settings
from app.models.attachment import Attachment
from app.models.payment_qr import PaymentQr
from app.models.product_image import ProductImage
from app.models.storage_tombstone import StorageTombstone
from app.scripts import move_media
from app.services import queue, storage
from tests import ai, webhooks as wh
from tests.test_product_images import PNG, upload
from tests.test_receipt_ocr import order_on_channel, reading, receipts_of, send_photo
from tests.test_seller_alerts import alerts, link

BUCKET = "apsara-test-media"


@pytest.fixture
def bucket(monkeypatch):
    """S3 mode, against a fresh in-process bucket."""
    with mock_aws():
        monkeypatch.setattr(settings, "MEDIA_STORAGE", "s3")
        monkeypatch.setattr(settings, "S3_BUCKET", BUCKET)
        monkeypatch.setattr(settings, "S3_REGION", "us-east-1")
        monkeypatch.setattr(settings, "S3_ACCESS_KEY_ID", "test")
        monkeypatch.setattr(settings, "S3_SECRET_ACCESS_KEY", "test")
        storage._client.cache_clear()
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        yield s3
    storage._client.cache_clear()


def objects(s3) -> dict[str, bytes]:
    listed = s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])
    return {o["Key"]: s3.get_object(Bucket=BUCKET, Key=o["Key"])["Body"].read() for o in listed}


def test_a_product_photo_goes_to_the_bucket_and_is_served_from_it(client, seller, db, bucket):
    product = seller.product()
    [image] = upload(client, seller, product["id"], [("front.png", PNG, "image/png")]).json()

    row = db.query(ProductImage).one()
    assert row.blob is None
    assert row.storage_key.startswith("media/product-images/")
    assert objects(bucket) == {row.storage_key: PNG}

    served = client.get(image["url"])
    assert served.status_code == 200
    assert served.content == PNG
    assert served.headers["content-type"] == "image/png"


def test_a_payment_qr_goes_to_the_bucket(client, seller, db, bucket):
    r = client.post("/api/v1/payment-qrs/", data={"name": "ABA USD"},
                    files={"file": ("qr.png", PNG, "image/png")}, headers=seller.headers)
    assert r.status_code == 201, r.text

    row = db.query(PaymentQr).one()
    assert row.blob is None and row.storage_key
    assert client.get(r.json()["url"]).content == PNG


def test_a_customer_receipt_is_kept_in_the_bucket_and_still_read(client, seller, db, bucket):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)

    with wh.sends(), alerts(), ai.replies(reading()) as captured:
        send_photo(client, integration)

    row = db.query(Attachment).one()
    assert row.blob is None
    assert objects(bucket)[row.storage_key] == b"receipt-bytes"
    # The vision model was handed the bytes from the bucket.
    assert captured["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg")
    [listed] = receipts_of(client, seller, order)
    assert listed["ocr_status"] == "read"

    content = client.get(f"/api/v1/attachments/{row.id}/content", headers=seller.headers)
    assert content.status_code == 200
    assert content.content == b"receipt-bytes"


def test_deleting_through_a_cascade_removes_the_object(client, seller, db, bucket):
    product = seller.product()
    upload(client, seller, product["id"], [("front.png", PNG, "image/png")])
    key = db.query(ProductImage).one().storage_key

    sweep = storage.sweep_tombstones
    # The image row goes by cascade, not by any code that knows about the
    # bucket. Housekeeping is held back so the tombstone can be seen.
    with mock.patch.object(storage, "sweep_tombstones"):
        assert client.delete(f"/api/v1/products/{product['id']}",
                             headers=seller.headers).status_code == 204
    db.expire_all()
    assert db.query(ProductImage).count() == 0
    assert [t.storage_key for t in db.query(StorageTombstone).all()] == [key]
    assert objects(bucket) == {key: PNG}

    assert sweep(db) == 1
    assert objects(bucket) == {}
    assert db.query(StorageTombstone).count() == 0


def test_deleting_one_image_removes_its_object(client, seller, db, bucket):
    product = seller.product()
    [image] = upload(client, seller, product["id"], [("front.png", PNG, "image/png")]).json()

    assert client.delete(f"/api/v1/products/{product['id']}/images/{image['id']}",
                         headers=seller.headers).status_code == 204
    queue.drain()  # housekeeping sweeps tombstones

    assert objects(bucket) == {}


def test_a_sweep_that_cannot_reach_the_bucket_keeps_the_tombstone(client, seller, db, bucket):
    db.add(StorageTombstone(storage_key="media/x/1"))
    db.commit()
    with mock.patch.object(storage, "remove", side_effect=storage.StorageError("down")):
        assert storage.sweep_tombstones(db) == 0
    assert db.query(StorageTombstone).count() == 1


def test_an_unreachable_bucket_is_a_retryable_503(client, seller, bucket):
    product = seller.product()
    with mock.patch.object(storage, "put", side_effect=storage.StorageError("down")):
        r = upload(client, seller, product["id"], [("front.png", PNG, "image/png")])
    assert r.status_code == 503


def test_rows_from_before_the_switch_still_serve_and_can_be_moved(
        client, seller, db, bucket, monkeypatch):
    product = seller.product()
    monkeypatch.setattr(settings, "MEDIA_STORAGE", "database")
    [image] = upload(client, seller, product["id"], [("front.png", PNG, "image/png")]).json()
    monkeypatch.setattr(settings, "MEDIA_STORAGE", "s3")
    assert db.query(ProductImage).one().storage_key is None

    # Served from Postgres while it is still there...
    assert client.get(image["url"]).content == PNG

    assert move_media.to_bucket(db) == 1
    db.expire_all()
    row = db.query(ProductImage).one()
    assert row.storage_key and objects(bucket) == {row.storage_key: PNG}
    # ...and from the bucket afterwards, at the same URL.
    assert client.get(image["url"]).content == PNG

    assert move_media.to_database(db) == 1
    db.expire_all()
    assert db.query(ProductImage).one().storage_key is None
    assert client.get(image["url"]).content == PNG


def test_s3_mode_without_a_bucket_refuses_to_start(monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_STORAGE", "s3")
    monkeypatch.setattr(settings, "S3_BUCKET", "")
    with pytest.raises(RuntimeError, match="S3_BUCKET"):
        settings.check_media_storage()
