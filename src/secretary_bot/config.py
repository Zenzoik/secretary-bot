from __future__ import annotations

import os
import re
from dataclasses import dataclass

_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigurationError(ValueError):
    """Raised when required application configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    webhook_secret: str
    public_base_url: str | None = None
    echo_enabled: bool = False
    allowed_chat_ids: frozenset[int] = frozenset()
    log_level: str = "INFO"
    update_queue_size: int = 1000
    redis_url: str = "redis://127.0.0.1:6379/0"
    dedup_ttl_seconds: int = 86400

    @property
    def webhook_url(self) -> str:
        if self.public_base_url is None:
            raise ConfigurationError("PUBLIC_BASE_URL is required to configure the webhook")
        return f"{self.public_base_url.rstrip('/')}/telegram/webhook"

    @classmethod
    def from_env(cls, *, require_public_url: bool = False) -> Settings:
        bot_token = _required("BOT_TOKEN")
        webhook_secret = _required("WEBHOOK_SECRET")
        if not _SECRET_PATTERN.fullmatch(webhook_secret):
            raise ConfigurationError(
                "WEBHOOK_SECRET must contain 1-256 characters: A-Z, a-z, 0-9, _ or -"
            )

        public_base_url = os.getenv("PUBLIC_BASE_URL") or None
        if require_public_url and public_base_url is None:
            raise ConfigurationError("PUBLIC_BASE_URL is required")
        if public_base_url is not None and not public_base_url.startswith("https://"):
            raise ConfigurationError("PUBLIC_BASE_URL must use HTTPS")

        queue_size = _parse_positive_int("UPDATE_QUEUE_SIZE", default=1000)
        dedup_ttl_seconds = _parse_positive_int("DEDUP_TTL_SECONDS", default=86400)
        redis_url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        if not redis_url.startswith(("redis://", "rediss://")):
            raise ConfigurationError("REDIS_URL must use redis:// or rediss://")

        echo_enabled = _parse_bool("POC_ECHO_ENABLED", default=False)
        allowed_chat_ids = _parse_chat_ids(os.getenv("POC_ALLOWED_CHAT_IDS", ""))
        if echo_enabled and not allowed_chat_ids:
            raise ConfigurationError(
                "POC_ALLOWED_CHAT_IDS must contain at least one chat ID when echo is enabled"
            )

        return cls(
            bot_token=bot_token,
            webhook_secret=webhook_secret,
            public_base_url=public_base_url,
            echo_enabled=echo_enabled,
            allowed_chat_ids=allowed_chat_ids,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            update_queue_size=queue_size,
            redis_url=redis_url,
            dedup_ttl_seconds=dedup_ttl_seconds,
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _parse_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ConfigurationError(f"{name} must be a boolean value")


def _parse_positive_int(name: str, *, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < 1:
        raise ConfigurationError(f"{name} must be positive")
    return value


def _parse_chat_ids(raw: str) -> frozenset[int]:
    if not raw.strip():
        return frozenset()

    chat_ids: set[int] = set()
    for item in raw.split(","):
        try:
            chat_id = int(item.strip())
        except ValueError as exc:
            raise ConfigurationError(
                "POC_ALLOWED_CHAT_IDS must be comma-separated integers"
            ) from exc
        if chat_id <= 0:
            raise ConfigurationError("POC_ALLOWED_CHAT_IDS must contain positive private chat IDs")
        chat_ids.add(chat_id)
    return frozenset(chat_ids)
