"""Where uploaded and downloaded media bytes live.

Product photos, payment QRs and customer attachments used to sit in Postgres
as `bytea`. That is simple and transactional, but every receipt and voice note
grows the database a seller pays for, its backups and its restore time. With
MEDIA_STORAGE=s3 new bytes go to any S3-compatible bucket (AWS S3, Cloudflare
R2, Railway buckets, MinIO) and the row keeps only the object key.

Rows written before the switch keep their `blob` and are still served from
it; `python -m app.scripts.move_media` moves them across. Reads therefore
always go through `read()`, never `row.blob`.

Objects are not served from the bucket directly. The API still answers
/api/v1/media/... and /attachments/.../content, so authorisation and the
response headers stay where they were and the bucket can stay private.
"""

from __future__ import annotations

import logging
import uuid
from functools import lru_cache

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)

DATABASE = "database"
S3 = "s3"


class StorageError(RuntimeError):
    """Raised when the object store could not be reached."""


def backend() -> str:
    return S3 if settings.MEDIA_STORAGE.lower() == S3 else DATABASE


@lru_cache(maxsize=1)
def _client():
    # Imported lazily: a database-only deployment never needs boto3 loaded.
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL or None,
        region_name=settings.S3_REGION or None,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY or None,
        config=Config(connect_timeout=5, read_timeout=20,
                      retries={"max_attempts": 3, "mode": "standard"}),
    )


def _key(kind: str) -> str:
    prefix = settings.S3_KEY_PREFIX.strip("/")
    name = f"{kind}/{uuid.uuid4().hex}"
    return f"{prefix}/{name}" if prefix else name


def put(kind: str, data: bytes, content_type: str | None) -> str:
    """Upload bytes and return their key."""
    key = _key(kind)
    try:
        _client().put_object(Bucket=settings.S3_BUCKET, Key=key, Body=data,
                             ContentType=content_type or "application/octet-stream")
    except Exception as exc:
        raise StorageError(f"Could not store {kind} object: {exc}") from exc
    return key


def get(key: str) -> bytes:
    try:
        return _client().get_object(Bucket=settings.S3_BUCKET, Key=key)["Body"].read()
    except Exception as exc:
        raise StorageError(f"Could not read object {key}: {exc}") from exc


def remove(key: str) -> None:
    try:
        _client().delete_object(Bucket=settings.S3_BUCKET, Key=key)
    except Exception as exc:
        raise StorageError(f"Could not delete object {key}: {exc}") from exc


# ── Row helpers ──────────────────────────────────────────────────────────────
# Each media model has a nullable `blob` and a nullable `storage_key`; exactly
# one is set once the bytes are stored.

def store(row, kind: str, data: bytes, content_type: str | None) -> None:
    """Put `data` wherever this deployment keeps media, on `row`."""
    if backend() == S3:
        row.storage_key = put(kind, data, content_type)
        row.blob = None
    else:
        row.blob = data
        row.storage_key = None


def read(row) -> bytes | None:
    """The row's bytes, from whichever place holds them."""
    if row.blob is not None:
        return row.blob
    if row.storage_key:
        return get(row.storage_key)
    return None


def has_content(model):
    """SQL condition: the row has stored bytes, wherever they are."""
    return or_(model.blob.isnot(None), model.storage_key.isnot(None))


# ── Deleted objects ──────────────────────────────────────────────────────────
# Rows are removed by cascades the ORM never sees — deleting a product, a
# conversation or a whole seller — so a database trigger records each removed
# key in `storage_tombstones` and housekeeping deletes the objects later.

def sweep_tombstones(db: Session, limit: int = 200) -> int:
    from app.models.storage_tombstone import StorageTombstone

    if backend() != S3:
        return 0
    rows = (db.query(StorageTombstone).order_by(StorageTombstone.created_at)
            .limit(limit).with_for_update(skip_locked=True).all())
    removed = 0
    for row in rows:
        try:
            remove(row.storage_key)
        except StorageError:
            logger.warning("Could not delete stored object %s; will retry", row.storage_key)
            continue
        db.delete(row)
        removed += 1
    db.commit()
    if removed:
        logger.info("Deleted %s stored object(s) no longer referenced", removed)
    return removed
