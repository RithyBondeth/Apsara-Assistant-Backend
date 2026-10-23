"""The shop's team: the owner adds staff, who then work the same shop."""

import secrets
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_owner
from app.core.config import settings
from app.core.security import hash_password
from app.database import get_db
from app.models.conversation import Conversation
from app.models.user import User
from app.schemas.staff import MemberOut, StaffCreate, StaffUpdate
from app.services import team, verification
from app.services.email import send_staff_invite

router = APIRouter()


@router.get("/", response_model=list[MemberOut])
def list_members(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Everyone on the team — for anyone on it, so threads can be assigned."""
    return team.members(db, current_user.id)


def _staff(db: Session, shop: User, staff_id: UUID) -> User:
    member = db.query(User).filter(User.id == staff_id, User.shop_id == shop.id).first()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found")
    return member


@router.post("/", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
def add_staff(
    payload: StaffCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
):
    """Create a staff account and email them a link to set their password.

    Nobody chooses a password for anyone else: the account starts with a
    random one that is never shown, and the invite is a single-use reset link.
    """
    email = payload.email.lower()
    if db.query(User.id).filter(User.email == email).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="That email already has an Apsara account")
    if len(team.members(db, owner.id)) > team.MAX_STAFF:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"A shop can have at most {team.MAX_STAFF} staff")
    member = User(
        email=email,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        full_name=payload.full_name.strip(),
        shop_id=owner.id,
        role=team.STAFF,
        currency=owner.currency,
        language=owner.language,
    )
    db.add(member)
    db.flush()
    hours = settings.STAFF_INVITE_EXPIRE_HOURS
    token = verification.issue(db, member, verification.PASSWORD_RESET,
                               lifetime_minutes=hours * 60)
    if token:
        background.add_task(send_staff_invite, member.email, token,
                            owner.business_name or owner.full_name, owner.full_name, hours)
    db.refresh(member)
    return member


@router.post("/{staff_id}/invite", response_model=MemberOut)
def resend_invite(
    staff_id: UUID,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
):
    member = _staff(db, owner, staff_id)
    hours = settings.STAFF_INVITE_EXPIRE_HOURS
    token = verification.issue(db, member, verification.PASSWORD_RESET,
                               lifetime_minutes=hours * 60)
    if not token:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="An invite was sent moments ago")
    background.add_task(send_staff_invite, member.email, token,
                        owner.business_name or owner.full_name, owner.full_name, hours)
    return member


@router.patch("/{staff_id}", response_model=MemberOut)
def update_staff(
    staff_id: UUID,
    payload: StaffUpdate,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
):
    member = _staff(db, owner, staff_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("full_name"):
        member.full_name = changes["full_name"].strip()
    if "is_active" in changes:
        member.is_active = changes["is_active"]
        if not member.is_active:
            _unassign(db, owner, member)
    db.commit()
    db.refresh(member)
    return member


@router.delete("/{staff_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_staff(
    staff_id: UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
):
    """Take someone off the team. Their account is deactivated, not deleted:
    the notes they wrote and the payments they confirmed still name them."""
    member = _staff(db, owner, staff_id)
    member.is_active = False
    _unassign(db, owner, member)
    db.commit()


def _unassign(db: Session, owner: User, member: User) -> None:
    db.query(Conversation).filter(
        Conversation.user_id == owner.id, Conversation.assigned_user_id == member.id,
    ).update({Conversation.assigned_user_id: None}, synchronize_session=False)
