from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest

from secretary_bot.web_auth import WebAuthError, validate_init_data

TOKEN = "123456:TEST_TOKEN"
NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def signed_init_data(user_id: int, *, auth_date: datetime = NOW) -> str:
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {"id": user_id, "first_name": "Owner", "username": f"owner{user_id}"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_valid_telegram_init_data_returns_the_signed_identity() -> None:
    identity = validate_init_data(signed_init_data(42), bot_token=TOKEN, now=NOW)

    assert identity.user_id == 42
    assert identity.username == "owner42"
    assert identity.auth_date == NOW


def test_tampered_telegram_init_data_is_rejected() -> None:
    raw = signed_init_data(42).replace("owner42", "attacker")

    with pytest.raises(WebAuthError, match="invalid"):
        validate_init_data(raw, bot_token=TOKEN, now=NOW)


def test_expired_telegram_init_data_is_rejected() -> None:
    raw = signed_init_data(42, auth_date=NOW - timedelta(days=2))

    with pytest.raises(WebAuthError, match="expired"):
        validate_init_data(raw, bot_token=TOKEN, now=NOW)
