from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.platforms import SUPPORTED_PLATFORMS, platform_list
from app.database import get_db
from app.models.customer import Customer
from app.models.user import User
from app.schemas.customer import CustomerCreate, CustomerOut, CustomerUpdate

router = APIRouter()


def _validate_platform(platform: str | None) -> None:
    """A customer's platform is optional, but if set it must be a real channel.

    Customers are looked up by (platform, platform_id) when a webhook arrives,
    so an unsupported value here would never match the inbound record and would
    silently duplicate the customer instead of linking to them.
    """
    if platform and platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform. Supported: {platform_list()}",
        )


@router.get("/", response_model=list[CustomerOut])
def list_customers(
    skip: int = 0,
    limit: int = 50,
    platform: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Customer).filter(Customer.user_id == current_user.id)
    if platform:
        query = query.filter(Customer.platform == platform)
    return query.order_by(Customer.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _validate_platform(payload.platform)

    if payload.platform and payload.platform_id:
        existing = db.query(Customer).filter(
            Customer.user_id == current_user.id,
            Customer.platform == payload.platform,
            Customer.platform_id == payload.platform_id,
        ).first()
        if existing:
            return existing

    customer = Customer(**payload.model_dump(), user_id=current_user.id)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(
    customer_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    customer = db.query(Customer).filter(
        Customer.id == customer_id, Customer.user_id == current_user.id
    ).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return customer


@router.patch("/{customer_id}", response_model=CustomerOut)
def update_customer(
    customer_id: UUID,
    payload: CustomerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _validate_platform(payload.platform)

    customer = db.query(Customer).filter(
        Customer.id == customer_id, Customer.user_id == current_user.id
    ).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(customer, field, value)
    db.commit()
    db.refresh(customer)
    return customer


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer(
    customer_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    customer = db.query(Customer).filter(
        Customer.id == customer_id, Customer.user_id == current_user.id
    ).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    db.delete(customer)
    db.commit()
