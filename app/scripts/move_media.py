"""Move stored media between Postgres and the object store.

    python -m app.scripts.move_media            # database -> bucket
    python -m app.scripts.move_media --to database

Run after switching MEDIA_STORAGE to s3, so rows written before the switch
leave the database too. Safe to stop and rerun: each row is committed as it
moves, and a moved row is skipped next time. Old objects are deleted through
the tombstone sweep, never here.
"""

import argparse
import logging

from sqlalchemy.orm import undefer

from app.core.config import settings
from app.database import SessionLocal
from app.models.attachment import Attachment
from app.models.payment_qr import PaymentQr
from app.models.product_image import ProductImage
from app.models.storage_tombstone import StorageTombstone
from app.services import storage

logger = logging.getLogger("app.scripts.move_media")

MODELS = ((Attachment, "attachments", "file_type"),
          (ProductImage, "product-images", "content_type"),
          (PaymentQr, "payment-qrs", "content_type"))


def to_bucket(db, batch: int = 50) -> int:
    moved = 0
    for model, kind, type_column in MODELS:
        while True:
            rows = (db.query(model).options(undefer(model.blob))
                    .filter(model.blob.isnot(None)).limit(batch).all())
            if not rows:
                break
            for row in rows:
                row.storage_key = storage.put(kind, row.blob, getattr(row, type_column))
                row.blob = None
                db.commit()
                moved += 1
    return moved


def to_database(db, batch: int = 50) -> int:
    moved = 0
    for model, _kind, _type in MODELS:
        while True:
            rows = (db.query(model).filter(model.storage_key.isnot(None))
                    .limit(batch).all())
            if not rows:
                break
            for row in rows:
                key = row.storage_key
                row.blob = storage.get(key)
                row.storage_key = None
                # The object is no longer referenced; let the sweep remove it.
                db.add(StorageTombstone(storage_key=key))
                db.commit()
                moved += 1
    return moved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", choices=("s3", "database"), default="s3")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if not settings.S3_BUCKET:
        parser.error("S3_BUCKET is not set")

    db = SessionLocal()
    try:
        moved = to_bucket(db) if args.to == "s3" else to_database(db)
    finally:
        db.close()
    logger.info("Moved %s object(s) to %s", moved, args.to)


if __name__ == "__main__":
    main()
