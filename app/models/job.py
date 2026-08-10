import uuid

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.clock import utcnow
from app.database import Base

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


class Job(Base):
    """A unit of deferred work, durable across restarts.

    Backed by Postgres rather than a broker on purpose: the database is already
    deployed, and adding Redis would mean adding a service to operate. Claiming
    uses SELECT ... FOR UPDATE SKIP LOCKED, so several workers can share the
    table without handing the same job to two of them.
    """

    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind = Column(String, nullable=False)
    payload = Column(JSONB, nullable=False)

    status = Column(String, nullable=False, server_default=PENDING, index=True)
    attempts = Column(Integer, nullable=False, server_default="0")
    max_attempts = Column(Integer, nullable=False, server_default="5")

    # When this becomes eligible to run. Moved forward on each retry so a
    # failing dependency is not hammered.
    run_after = Column(DateTime, nullable=False, default=utcnow, index=True)
    # Set while a worker holds the job. A process killed mid-job leaves this
    # behind, which is how the reaper recognises work to release.
    locked_at = Column(DateTime)
    last_error = Column(Text)

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
