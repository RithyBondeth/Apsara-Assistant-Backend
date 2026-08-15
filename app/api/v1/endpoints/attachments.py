"""Authenticated access to customer-supplied attachment bytes."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.services.platforms import SAFE_RECEIPT_TYPES

router = APIRouter()


@router.get("/{attachment_id}/content")
def attachment_content(
    attachment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return private bytes only when the attachment belongs to this seller."""
    attachment = (
        db.query(Attachment)
        .join(Message, Message.id == Attachment.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(Attachment.id == attachment_id, Conversation.user_id == current_user.id)
        .first()
    )
    # The same 404 covers missing, foreign, and URL-only attachments so this
    # endpoint cannot be used to enumerate another seller's evidence.
    if (not attachment or attachment.blob is None
            or attachment.file_type not in SAFE_RECEIPT_TYPES):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Attachment not found")

    return Response(
        content=attachment.blob,
        media_type=attachment.file_type or "application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )
