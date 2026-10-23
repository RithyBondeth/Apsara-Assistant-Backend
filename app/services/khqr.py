"""Dynamic KHQR codes, and checking with Bakong whether one was paid.

A static bank QR leaves the customer to type the amount and the seller to
squint at a screenshot. A dynamic KHQR carries the order's exact amount and
currency, and Bakong — the national payment rail every Cambodian bank app
settles through — can be asked whether a transaction for that exact code
happened, by the MD5 of the code's text. So an order paid by KHQR is marked
paid without anyone reading a receipt.

The code is EMVCo merchant-presented QR as profiled by the National Bank of
Cambodia (KHQR): tag-length-value fields, ending in a CRC-16/CCITT-FALSE.

Checking needs a Bakong Open API token, issued by NBC to a registered email
and valid for 90 days: per seller (`users.bakong_api_token`, encrypted) or one
for the platform (`BAKONG_API_TOKEN`). NBC serves the API to Cambodian
addresses; a server hosted abroad sets BAKONG_API_URL to a relay inside the
country.
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import httpx

from app.core.clock import utcnow
from app.core.config import settings
from app.core.crypto import decrypt

logger = logging.getLogger(__name__)

CURRENCY_CODES = {"KHR": "116", "USD": "840"}
MERCHANT_CATEGORY = "5999"  # miscellaneous retail
NAME_LIMIT = 25
CITY_LIMIT = 15
BILL_LIMIT = 25
CHECK_TIMEOUT = 10.0


class KhqrError(ValueError):
    """The shop's KHQR details cannot make a valid code."""


def _field(tag: str, value: str) -> str:
    if len(value) > 99:
        raise KhqrError(f"KHQR field {tag} is too long")
    return f"{tag}{len(value):02d}{value}"


def crc16(data: str) -> str:
    """CRC-16/CCITT-FALSE, as EMVCo QR requires, in four upper-case hex digits."""
    crc = 0xFFFF
    for byte in data.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def _amount(amount: Decimal, currency: str) -> str:
    if currency == "KHR":
        return str(int(Decimal(amount).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    text = f"{Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _ascii(text: str, limit: int) -> str:
    """Bank apps render these two fields from ASCII; Khmer names belong in
    the optional alternate-language template, which is left out."""
    cleaned = "".join(ch for ch in (text or "") if 32 <= ord(ch) < 127).strip()
    return cleaned[:limit]


def build(*, account_id: str, merchant_name: str, merchant_city: str,
          amount: Decimal, currency: str, bill_number: str,
          merchant_id: str | None = None, acquiring_bank: str | None = None,
          expires_at_ms: int | None = None, created_at_ms: int | None = None) -> str:
    """The text of a dynamic KHQR for one payment."""
    if currency not in CURRENCY_CODES:
        raise KhqrError("KHQR supports USD and KHR only")
    if not account_id or "@" not in account_id:
        raise KhqrError("The Bakong account ID looks like name@bank")
    if Decimal(amount) <= 0:
        raise KhqrError("A KHQR payment needs an amount")
    name = _ascii(merchant_name, NAME_LIMIT)
    if not name:
        raise KhqrError("The merchant name must use Latin letters")
    city = _ascii(merchant_city, CITY_LIMIT) or "Phnom Penh"

    # Individual accounts use template 29; a bank-registered merchant, 30.
    if merchant_id:
        account = _field("30", _field("00", account_id) + _field("01", merchant_id)
                         + _field("02", acquiring_bank or ""))
    else:
        account = _field("29", _field("00", account_id))

    created = created_at_ms or int(utcnow().timestamp() * 1000)
    timestamps = _field("00", str(created))
    if expires_at_ms:
        timestamps += _field("01", str(expires_at_ms))

    body = "".join([
        _field("00", "01"),
        _field("01", "12"),  # dynamic: one amount, one payment
        account,
        _field("52", MERCHANT_CATEGORY),
        _field("53", CURRENCY_CODES[currency]),
        _field("54", _amount(amount, currency)),
        _field("58", "KH"),
        _field("59", name),
        _field("60", city),
        _field("62", _field("01", bill_number[:BILL_LIMIT])),
        _field("99", timestamps),
    ]) + "6304"
    return body + crc16(body)


def md5(payload: str) -> str:
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def parse(payload: str) -> dict[str, str]:
    """Top-level tags of a KHQR string. For tests and for showing a code back."""
    fields, i = {}, 0
    while i < len(payload):
        tag, length = payload[i:i + 2], int(payload[i + 2:i + 4])
        fields[tag] = payload[i + 4:i + 4 + length]
        i += 4 + length
    return fields


def png(payload: str) -> bytes:
    import segno

    out = io.BytesIO()
    segno.make(payload, error="m").save(out, kind="png", scale=8, border=3)
    return out.getvalue()


def expiry():
    return utcnow() + timedelta(minutes=settings.KHQR_EXPIRE_MINUTES)


# ── Asking Bakong ────────────────────────────────────────────────────────────

FOUND = "found"
NOT_FOUND = "not_found"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Check:
    outcome: str
    amount: Decimal | None = None
    currency: str | None = None
    reference: str | None = None
    detail: str = ""


def api_token(user) -> str | None:
    if user.bakong_api_token:
        try:
            return decrypt(user.bakong_api_token)
        except Exception:
            logger.warning("Stored Bakong token for %s cannot be decrypted", user.id)
    return settings.BAKONG_API_TOKEN or None


def check(payload_md5: str, token: str) -> Check:
    """Whether Bakong has a transaction for this code. Never raises."""
    url = settings.BAKONG_API_URL.rstrip("/") + "/v1/check_transaction_by_md5"
    try:
        response = httpx.post(url, json={"md5": payload_md5},
                              headers={"Authorization": f"Bearer {token}"},
                              timeout=CHECK_TIMEOUT)
    except httpx.HTTPError as exc:
        return Check(UNAVAILABLE, detail=f"Bakong unreachable: {exc}")
    if response.status_code >= 400:
        return Check(UNAVAILABLE,
                     detail=f"Bakong answered {response.status_code}: {response.text[:200]}")
    try:
        body = response.json()
    except ValueError:
        return Check(UNAVAILABLE, detail="Bakong answered with something other than JSON")

    if body.get("responseCode") == 0 and body.get("data"):
        data = body["data"]
        try:
            amount = Decimal(str(data.get("amount")))
        except Exception:
            amount = None
        return Check(FOUND, amount=amount, currency=(data.get("currency") or "").upper() or None,
                     reference=data.get("hash") or data.get("externalRef"))
    # errorCode 1: no transaction for this md5 yet. Anything else — an expired
    # or unknown token, most often — is a problem on our side to surface.
    if body.get("errorCode") in (1, None):
        return Check(NOT_FOUND)
    return Check(UNAVAILABLE, detail=f"Bakong error {body.get('errorCode')}: "
                                     f"{body.get('responseMessage')}")
