import uuid

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.core.clock import utcnow
from app.database import Base


class DataDeletionRequest(Base):
    """A person's request, through Meta, to have their data deleted.

    Meta shows the requester `confirmation_code` and a link to look it up, so
    the record is kept as proof of what was done. The subject's id is stored
    hashed: keeping it in the clear would keep the very thing they asked to
    have removed.
    """

    __tablename__ = "data_deletion_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    confirmation_code = Column(String(32), nullable=False, unique=True)
    source = Column(String(16), nullable=False)
    subject_hash = Column(String(64), nullable=False)
    customers_erased = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="completed")
    created_at = Column(DateTime, nullable=False, default=utcnow)
