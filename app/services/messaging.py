from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InboundMessage:
    """A normalized inbound message extracted from a platform update.

    Shared across channel adapters (telegram, messenger, …) so the webhook
    layer can treat every platform the same way.
    """

    external_user_id: str  # platform-side sender id (Telegram chat id, Messenger PSID)
    sender_name: str
    text: str
    # Platform-unique id for this message/update, used to de-duplicate
    # redelivered webhooks (Telegram update_id, Messenger message.mid).
    event_id: str | None = None
    # An image the customer sent, as the owning channel refers to it: a
    # Telegram file_id, or a Meta CDN URL. It is deliberately NOT a usable
    # link — resolving it needs the seller's access token, so only that
    # channel's service can fetch it (see services/media.py).
    image_ref: str | None = None
    # A voice note the customer recorded, referenced the same indirect way as
    # image_ref — a Telegram file_id, resolvable only with the seller's token.
    voice_ref: str | None = None
    # Length in seconds as the platform reports it, before we fetch anything.
    # Lets the caller reject a twenty-minute clip without paying to download
    # and transcribe it first.
    voice_duration: int | None = None

    @property
    def has_image(self) -> bool:
        return self.image_ref is not None

    @property
    def has_voice(self) -> bool:
        return self.voice_ref is not None
