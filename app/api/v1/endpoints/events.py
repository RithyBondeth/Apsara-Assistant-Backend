"""GET /api/v1/events — a Server-Sent Events stream of the shop's inbox changes."""

import asyncio

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app.api.deps import oauth2_scheme
from app.core.config import settings
from app.core.security import decode_access_token
from app.database import SessionLocal
from app.models.user import User
from app.services import realtime

router = APIRouter()


def _shop_for(token: str | None) -> str:
    """Authenticate once, with a session of its own that is closed before the
    stream starts — a dependency-held session would pin a pool connection
    for as long as the tab stays open."""
    user_id = decode_access_token(token) if token else None
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id, User.is_active == True).first()  # noqa: E712
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        return str(user.id)
    finally:
        db.close()


@router.get("")
async def inbox_events(
    request: Request,
    bearer_token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias=settings.AUTH_COOKIE_NAME),
):
    shop_id = await run_in_threadpool(_shop_for, bearer_token or cookie_token)
    queue = realtime.hub.subscribe(shop_id)

    async def stream():
        try:
            # Reconnect after 3s if dropped; "ready" lets the client refetch
            # anything that changed while it was not listening.
            yield "retry: 3000\n\n" + realtime.sse({"type": "ready"})
            while not await request.is_disconnected():
                try:
                    payload = await asyncio.wait_for(queue.get(), realtime.HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # Keeps proxies from closing an idle stream.
                    yield ": keepalive\n\n"
                    continue
                yield realtime.sse(payload)
        finally:
            realtime.hub.unsubscribe(shop_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
