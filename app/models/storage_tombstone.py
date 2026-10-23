import uuid

from sqlalchemy import DDL, Column, DateTime, String, event
from sqlalchemy.dialects.postgresql import UUID

from app.core.clock import utcnow
from app.database import Base

# Tables whose rows may point at a stored object. Shared with the migration.
MEDIA_TABLES = ("attachments", "product_images", "payment_qrs")

TOMBSTONE_FUNCTION = """
CREATE OR REPLACE FUNCTION record_storage_tombstone() RETURNS trigger AS $$
BEGIN
    IF OLD.storage_key IS NOT NULL THEN
        INSERT INTO storage_tombstones (id, storage_key, created_at)
        VALUES (gen_random_uuid(), OLD.storage_key, (now() AT TIME ZONE 'utc'));
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql
"""


def tombstone_trigger(table: str) -> str:
    return (f"CREATE TRIGGER {table}_storage_tombstone AFTER DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION record_storage_tombstone()")


class StorageTombstone(Base):
    """A stored object whose row is gone, waiting to be deleted from the bucket.

    Written by a trigger, not the ORM: most media rows disappear through
    `ON DELETE CASCADE`, which no Python code observes.
    """

    __tablename__ = "storage_tombstones"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    storage_key = Column(String(512), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)


def track_deletions(table) -> None:
    """Attach the tombstone trigger to a media table whenever it is created.

    Tests and fresh databases are built with create_all, not migrations, so
    the trigger has to come along with the table itself.
    """
    event.listen(table, "after_create", DDL(TOMBSTONE_FUNCTION))
    event.listen(table, "after_create", DDL(tombstone_trigger(table.name)))
