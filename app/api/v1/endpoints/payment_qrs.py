from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.currency import CURRENCIES
from app.database import get_db
from app.models.payment_qr import PaymentQr
from app.models.user import User
from app.schemas.payment_qr import PaymentQrOut, PaymentQrUpdate
from app.services.media import MAX_PAYMENT_QRS, read_image_upload

router = APIRouter()


def _lock_user(db: Session, user_id: UUID) -> None:
    db.query(User).filter(User.id == user_id).with_for_update().one()


def _clean_optional(value: str | None) -> str | None:
    cleaned = value.strip() if value else ""
    return cleaned or None


@router.get("/", response_model=list[PaymentQrOut])
def list_payment_qrs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(PaymentQr)
        .filter(PaymentQr.user_id == current_user.id)
        .order_by(PaymentQr.is_default.desc(), PaymentQr.created_at)
        .all()
    )


@router.post("/", response_model=PaymentQrOut, status_code=status.HTTP_201_CREATED)
def create_payment_qr(
    name: str = Form(min_length=1, max_length=100),
    bank_name: str | None = Form(default=None, max_length=100),
    account_name: str | None = Form(default=None, max_length=100),
    currency: str | None = Form(default=None),
    is_default: bool = Form(default=False),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _lock_user(db, current_user.id)
    existing = db.query(PaymentQr).filter(PaymentQr.user_id == current_user.id).all()
    if len(existing) >= MAX_PAYMENT_QRS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A shop can have at most {MAX_PAYMENT_QRS} payment QR codes",
        )
    if currency and currency not in CURRENCIES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Currency must be USD or KHR")
    cleaned_name = name.strip()
    if not cleaned_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Name cannot be empty")
    blob, content_type, file_name = read_image_upload(file)
    make_default = is_default or not existing
    if make_default:
        for qr in existing:
            qr.is_default = False
        db.flush()
    qr = PaymentQr(
        user_id=current_user.id,
        name=cleaned_name,
        bank_name=_clean_optional(bank_name),
        account_name=_clean_optional(account_name),
        currency=currency,
        blob=blob,
        content_type=content_type,
        file_name=file_name,
        file_size=len(blob),
        is_active=True,
        is_default=make_default,
    )
    db.add(qr)
    db.commit()
    db.refresh(qr)
    return qr


@router.patch("/{qr_id}", response_model=PaymentQrOut)
def update_payment_qr(
    qr_id: UUID,
    payload: PaymentQrUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _lock_user(db, current_user.id)
    qrs = (
        db.query(PaymentQr)
        .filter(PaymentQr.user_id == current_user.id)
        .order_by(PaymentQr.created_at, PaymentQr.id)
        .all()
    )
    qr = next((item for item in qrs if item.id == qr_id), None)
    if qr is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment QR not found")
    data = payload.model_dump(exclude_unset=True)
    if data.get("is_default") is True:
        for item in qrs:
            item.is_default = False
        db.flush()
        data["is_active"] = True
    if data.get("is_active") is False:
        data["is_default"] = False
    was_default = qr.is_default
    for field, value in data.items():
        if field in {"name", "bank_name", "account_name"} and value is not None:
            value = value.strip()
        setattr(qr, field, value)

    if was_default and not qr.is_default:
        db.flush()

    active = [item for item in qrs if item.is_active]
    if active and not any(item.is_default for item in active):
        active[0].is_default = True
    db.commit()
    db.refresh(qr)
    return qr


@router.delete("/{qr_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_payment_qr(
    qr_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _lock_user(db, current_user.id)
    qrs = (
        db.query(PaymentQr)
        .filter(PaymentQr.user_id == current_user.id)
        .order_by(PaymentQr.created_at, PaymentQr.id)
        .all()
    )
    qr = next((item for item in qrs if item.id == qr_id), None)
    if qr is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment QR not found")
    was_default = qr.is_default
    db.delete(qr)
    db.flush()
    if was_default:
        replacement = next((item for item in qrs if item.id != qr.id and item.is_active), None)
        if replacement:
            replacement.is_default = True
    db.commit()
