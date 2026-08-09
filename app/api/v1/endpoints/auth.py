from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.database import get_db
from app.models.user import User
from app.schemas.auth import (
    ForgotPasswordRequest,
    MessageResponse,
    OtpRequest,
    OtpVerifyRequest,
    ResetPasswordRequest,
)
from app.schemas.user import Token, UserCreate, UserOut, UserUpdate
from app.services import verification
from app.services.email import send_login_otp, send_password_reset

router = APIRouter()

# Returned by both code-request endpoints whether or not the address belongs to
# an account. Confirming which emails are registered would turn either endpoint
# into a membership oracle.
CODE_SENT = "If that email is registered, a message is on its way."


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        business_name=payload.business_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form.username).first()
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is inactive")

    return Token(access_token=create_access_token(str(user.id)))


@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserOut)
def update_me(
    payload: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(current_user, field, value)
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout():
    """Acknowledge a sign-out.

    Access tokens are stateless and self-expiring, so there is no server-side
    session to end — the client discards the token. This exists so that a
    client can call it unconditionally, and as the place a token denylist
    would go if one is ever added.
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Password reset ───────────────────────────────────────────────────────────

@router.post("/forgot-password", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
def forgot_password(
    payload: ForgotPasswordRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == payload.email, User.is_active == True).first()
    if user:
        token = verification.issue(db, user, verification.PASSWORD_RESET)
        # None means one was sent moments ago; stay quiet rather than resend.
        if token:
            background.add_task(send_password_reset, user.email, token)

    return MessageResponse(detail=CODE_SENT)


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    user = verification.redeem_reset_token(db, payload.token)
    if not user or not user.is_active:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link is invalid or has expired. Request a new one.",
        )

    user.password_hash = hash_password(payload.new_password)
    # Anything else outstanding for this account dies with the old password —
    # including OTPs, which would otherwise still let an attacker back in.
    verification.invalidate_outstanding(db, user, verification.PASSWORD_RESET)
    verification.invalidate_outstanding(db, user, verification.LOGIN_OTP)
    db.commit()

    return MessageResponse(detail="Your password has been reset. You can now sign in.")


# ── One-time-code login ──────────────────────────────────────────────────────

@router.post("/otp/request", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
def request_otp(
    payload: OtpRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == payload.email, User.is_active == True).first()
    if user:
        code = verification.issue(db, user, verification.LOGIN_OTP)
        if code:
            background.add_task(send_login_otp, user.email, code)

    return MessageResponse(detail=CODE_SENT)


@router.post("/otp/verify", response_model=Token)
def verify_otp(payload: OtpVerifyRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email, User.is_active == True).first()
    # One message for a wrong code, an unknown address and a disabled account,
    # so a failed attempt says nothing about which of the three it was.
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="That code is invalid or has expired. Request a new one.",
    )
    if not user or not verification.redeem_otp(db, user, payload.code):
        raise invalid

    return Token(access_token=create_access_token(str(user.id)))
