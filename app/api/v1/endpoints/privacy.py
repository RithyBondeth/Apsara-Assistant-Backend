"""Data deletion on a person's request, as Meta requires of a Messenger app.

When someone removes the app from their Facebook settings and asks for their
data to go, Meta POSTs a signed request here. The answer is a confirmation
code and a page where they can check it (APP_BASE_URL/data-deletion).
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import get_db
from app.models.customer import Customer
from app.models.data_deletion_request import DataDeletionRequest
from app.services.data_deletion import erase_customer
from app.services.platforms import MESSENGER

logger = logging.getLogger(__name__)

router = APIRouter()


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def parse_signed_request(signed_request: str) -> dict | None:
    """Meta's signed_request, verified against the app secret, or None."""
    try:
        signature, payload = signed_request.split(".", 1)
        data = json.loads(_b64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if not settings.META_APP_SECRET or (data.get("algorithm") or "").upper() != "HMAC-SHA256":
        return None
    expected = hmac.new(settings.META_APP_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    try:
        given = _b64(signature)
    except ValueError:
        return None
    return data if hmac.compare_digest(expected, given) else None


class DeletionStatus(BaseModel):
    confirmation_code: str
    status: str
    customers_erased: int


@router.post("/meta-data-deletion")
def meta_data_deletion(signed_request: str = Form(...), db: Session = Depends(get_db)):
    """Erase what every shop here holds for the person Meta names.

    Meta identifies them by an app-scoped id. Customers are stored by their
    page-scoped Messenger id, which is usually a different number, so a match
    is not guaranteed; the request is recorded and answered either way, and
    the status page tells the person how to reach a shop directly.
    """
    data = parse_signed_request(signed_request)
    subject = str((data or {}).get("user_id") or "")
    if not subject:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signed request")

    customers = db.query(Customer).filter(
        Customer.platform == MESSENGER, Customer.platform_id == subject).all()
    for customer in customers:
        erase_customer(db, customer)
    request = DataDeletionRequest(
        confirmation_code=secrets.token_hex(8).upper(),
        source="meta",
        subject_hash=hashlib.sha256(subject.encode()).hexdigest(),
        customers_erased=len(customers),
        status="completed",
    )
    db.add(request)
    db.commit()
    logger.info("Meta data deletion %s erased %s customer(s)",
                request.confirmation_code, request.customers_erased)
    base = settings.APP_BASE_URL.rstrip("/")
    return {"url": f"{base}/data-deletion?code={request.confirmation_code}",
            "confirmation_code": request.confirmation_code}


@router.get("/deletion-requests/{code}", response_model=DeletionStatus)
def deletion_status(code: str, db: Session = Depends(get_db)):
    request = db.query(DataDeletionRequest).filter(
        DataDeletionRequest.confirmation_code == code.strip().upper()).first()
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No request with that code")
    return DeletionStatus(confirmation_code=request.confirmation_code,
                          status=request.status, customers_erased=request.customers_erased)
