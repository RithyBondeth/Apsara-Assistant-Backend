from app.models.user import User


def default_payment_qr_url(user: User) -> str | None:
    """The one QR the assistant should send, with legacy-link compatibility."""
    qrs = list(user.payment_qrs)
    if qrs:
        selected = next((qr for qr in qrs if qr.is_active and qr.is_default), None)
        selected = selected or next((qr for qr in qrs if qr.is_active), None)
        return selected.url if selected else None
    return user.payment_qr_url
