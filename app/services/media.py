from __future__ import annotations

from fastapi import HTTPException, UploadFile, status

from app.core.config import settings

MAX_IMAGE_BYTES = 5_000_000
MAX_PRODUCT_IMAGES = 8
MAX_PAYMENT_QRS = 5

IMAGE_SIGNATURES = {
    "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    "image/webp": lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
}


def read_image_upload(upload: UploadFile) -> tuple[bytes, str, str]:
    """Read one bounded raster image and verify its bytes, not its filename."""
    content_type = (upload.content_type or "").lower()
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    verifier = IMAGE_SIGNATURES.get(content_type)
    if verifier is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Images must be PNG, JPEG, or WebP",
        )

    data = upload.file.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Each image must be 5 MB or smaller",
        )
    if not verifier(data):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File contents do not match the declared image type",
        )
    return data, content_type, (upload.filename or "image")[:255]


def store_or_503(row, kind: str, data: bytes, content_type: str) -> None:
    """Store an upload's bytes, turning an unreachable bucket into a 503 the
    seller can retry rather than a 500."""
    from app.services import storage

    try:
        storage.store(row, kind, data, content_type)
    except storage.StorageError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Could not save the image right now. Please try again.")


def read_or_503(row) -> bytes | None:
    from app.services import storage

    try:
        return storage.read(row)
    except storage.StorageError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Media storage is unavailable")


def public_media_url(kind: str, media_id) -> str:
    base = settings.API_BASE_URL.rstrip("/")
    return f"{base}/api/v1/media/{kind}/{media_id}"
