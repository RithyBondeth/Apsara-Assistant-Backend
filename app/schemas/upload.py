from __future__ import annotations

from pydantic import BaseModel


class UploadResult(BaseModel):
    url: str
    public_id: str | None = None
