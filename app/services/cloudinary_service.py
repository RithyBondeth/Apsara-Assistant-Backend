from __future__ import annotations

import hashlib
import time

import httpx

from app.core.config import settings

UPLOAD_URL = "https://api.cloudinary.com/v1_1/{cloud_name}/{resource_type}/upload"
DESTROY_URL = "https://api.cloudinary.com/v1_1/{cloud_name}/{resource_type}/destroy"
DEFAULT_FOLDER = "apsara/products"

# Cloudinary has no "audio" resource type — audio is served by the video
# pipeline, so voice notes upload under `video/upload` despite having no
# picture. Passing `image` instead fails with an unhelpful format error.
AUDIO_RESOURCE_TYPE = "video"


def is_configured() -> bool:
    return bool(
        settings.CLOUDINARY_CLOUD_NAME
        and settings.CLOUDINARY_API_KEY
        and settings.CLOUDINARY_API_SECRET
    )


def _sign(params: dict[str, str]) -> str:
    """Build a Cloudinary signature: sha1 of sorted params + api_secret."""
    to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha1((to_sign + settings.CLOUDINARY_API_SECRET).encode()).hexdigest()


async def upload_image(
    file_bytes: bytes,
    filename: str,
    folder: str = DEFAULT_FOLDER,
    resource_type: str = "image",
) -> dict:
    """Signed-upload a file to Cloudinary and return its hosted URL.

    Returns ``{"url": <secure_url>, "public_id": <id>}``. Raises on HTTP error.
    Pass ``resource_type=AUDIO_RESOURCE_TYPE`` for voice notes.
    """
    timestamp = str(int(time.time()))
    signed = {"folder": folder, "timestamp": timestamp}
    data = {
        **signed,
        "api_key": settings.CLOUDINARY_API_KEY,
        "signature": _sign(signed),
    }
    url = UPLOAD_URL.format(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME, resource_type=resource_type
    )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, data=data, files={"file": (filename, file_bytes)})
        resp.raise_for_status()
        body = resp.json()

    return {"url": body.get("secure_url"), "public_id": body.get("public_id")}


async def delete_image(public_id: str, resource_type: str = "image") -> str:
    """Delete an image from Cloudinary by public_id. Returns the result string.

    Cloudinary returns ``{"result": "ok"}`` on success or ``"not found"`` if the
    asset is already gone — both are non-error outcomes for us.
    """
    timestamp = str(int(time.time()))
    signed = {"public_id": public_id, "timestamp": timestamp}
    data = {
        **signed,
        "api_key": settings.CLOUDINARY_API_KEY,
        "signature": _sign(signed),
    }
    url = DESTROY_URL.format(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME, resource_type=resource_type
    )

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, data=data)
        resp.raise_for_status()
        return resp.json().get("result", "")
