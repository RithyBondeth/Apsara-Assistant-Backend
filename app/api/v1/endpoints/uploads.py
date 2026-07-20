from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, File, Query, UploadFile, status

from app.api.deps import get_current_user
from app.core import errors
from app.models.user import User
from app.schemas.upload import UploadResult
from app.services import cloudinary_service

router = APIRouter()

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_BYTES = 5 * 1024 * 1024  # 5 MB


@router.post("/image", response_model=UploadResult)
async def upload_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Upload a product image to Cloudinary and return its hosted URL."""
    if not cloudinary_service.is_configured():
        raise errors.uploads_not_configured()

    if file.content_type not in ALLOWED_TYPES:
        raise errors.unsupported_image_type(', '.join(sorted(ALLOWED_TYPES)))

    contents = await file.read()
    if len(contents) > MAX_BYTES:
        raise errors.image_too_large("5 MB")
    if not contents:
        raise errors.empty_file()

    try:
        result = await cloudinary_service.upload_image(contents, file.filename or "upload")
    except httpx.HTTPError as exc:
        raise errors.upload_failed(str(exc))

    return UploadResult(**result)


@router.delete("/image", status_code=status.HTTP_204_NO_CONTENT)
async def delete_image(
    public_id: str = Query(..., description="Cloudinary public_id returned at upload time"),
    current_user: User = Depends(get_current_user),
):
    """Delete a previously uploaded image from Cloudinary to avoid orphans."""
    if not cloudinary_service.is_configured():
        raise errors.uploads_not_configured()

    try:
        await cloudinary_service.delete_image(public_id)
    except httpx.HTTPError as exc:
        raise errors.delete_failed(str(exc))
