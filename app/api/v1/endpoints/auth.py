from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core import errors
from app.core.config import settings
from app.core.rate_limit import SlidingWindowRateLimiter, client_ip
from app.core.security import create_access_token, hash_password, verify_password
from app.database import get_db
from app.models.user import User
from app.schemas.user import (
    ForgotPasswordRequest,
    MessageResponse,
    OTPRequest,
    OTPVerify,
    PasswordChange,
    ResetPasswordRequest,
    Token,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.services import auth_tokens, email

router = APIRouter()

# Throttle the unauthenticated "send me a link/code" endpoints per client IP so
# they can't be used to spam a victim's inbox or enumerate accounts by timing.
_send_limiter = SlidingWindowRateLimiter(
    settings.AUTH_RATE_LIMIT, settings.AUTH_RATE_WINDOW_SECONDS
)

# Sign-in throttles. See the LOGIN_* settings for why there are two buckets.
_login_account_limiter = SlidingWindowRateLimiter(
    settings.LOGIN_RATE_LIMIT, settings.LOGIN_RATE_WINDOW_SECONDS
)
_login_ip_limiter = SlidingWindowRateLimiter(
    settings.LOGIN_IP_RATE_LIMIT, settings.LOGIN_RATE_WINDOW_SECONDS
)
_register_limiter = SlidingWindowRateLimiter(
    settings.REGISTER_RATE_LIMIT, settings.REGISTER_RATE_WINDOW_SECONDS
)

# Generic response for the request endpoints — deliberately identical whether or
# not the email matches an account, so the response never reveals who is registered.
_GENERIC_SENT = MessageResponse(
    message="If an account exists for that email, we've sent instructions to it."
)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, request: Request, db: Session = Depends(get_db)):
    if not _register_limiter.allow(client_ip(request)):
        raise errors.too_many_requests()

    if db.query(User).filter(User.email == payload.email).first():
        raise errors.email_already_registered()

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
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    # Both checks run BEFORE verify_password: bcrypt is intentionally slow, so an
    # unthrottled login endpoint is a CPU-exhaustion vector as much as a
    # credential-guessing one. Key the account bucket on the normalised email so
    # varying the case can't mint a fresh bucket.
    account_key = f"account:{form.username.strip().lower()}"
    if not _login_ip_limiter.allow(client_ip(request)):
        raise errors.too_many_requests()
    if not _login_account_limiter.allow(account_key):
        raise errors.too_many_requests()

    user = db.query(User).filter(User.email == form.username).first()
    if not user or not verify_password(form.password, user.password_hash):
        raise errors.invalid_credentials()

    if not user.is_active:
        raise errors.account_inactive()

    # Correct credentials clear the account's failure history. The IP bucket is
    # deliberately NOT reset — it exists to cap total unauthenticated bcrypt work
    # from one host, which a valid login doesn't excuse.
    _login_account_limiter.reset(account_key)
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


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: PasswordChange,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise errors.current_password_incorrect()
    current_user.password_hash = hash_password(payload.new_password)
    db.commit()


def _throttle(request: Request) -> None:
    if not _send_limiter.allow(client_ip(request)):
        raise errors.too_many_requests()


@router.post("/forgot-password", response_model=MessageResponse)
def forgot_password(
    payload: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)
):
    _throttle(request)
    user = db.query(User).filter(User.email == payload.email).first()
    # Only act for real, active accounts — but always return the same response so
    # the caller can't tell whether the address is registered.
    if user and user.is_active:
        raw = auth_tokens.issue_password_reset(db, user)
        reset_url = f"{settings.FRONTEND_BASE_URL}/reset-password?token={raw}"
        email.send_password_reset_email(user.email, reset_url)
    return _GENERIC_SENT


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    user = auth_tokens.redeem_password_reset(db, payload.token)
    if not user:
        raise errors.invalid_or_expired_reset_link()
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return MessageResponse(message="Your password has been reset. You can now sign in.")


@router.post("/otp/request", response_model=MessageResponse)
def request_otp(payload: OTPRequest, request: Request, db: Session = Depends(get_db)):
    _throttle(request)
    user = db.query(User).filter(User.email == payload.email).first()
    if user and user.is_active:
        code = auth_tokens.issue_otp(db, user)
        email.send_otp_email(user.email, code)
    return _GENERIC_SENT


@router.post("/otp/verify", response_model=Token)
def verify_otp(payload: OTPVerify, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not user.is_active or not auth_tokens.verify_otp(db, user, payload.code):
        raise errors.invalid_or_expired_code()
    return Token(access_token=create_access_token(str(user.id)))
