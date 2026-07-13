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
