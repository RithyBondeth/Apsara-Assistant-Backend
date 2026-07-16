from __future__ import annotations

# The full channel set — telegram, messenger, instagram, website. TikTok is not
# supported and is not planned.
SUPPORTED_PLATFORMS = frozenset({"telegram", "messenger", "instagram", "website"})

# Meta platforms verify inbound webhooks with the app secret.
META_PLATFORMS = frozenset({"messenger", "instagram"})


def platform_list() -> str:
    """Comma-separated set for error messages."""
    return ", ".join(sorted(SUPPORTED_PLATFORMS))
