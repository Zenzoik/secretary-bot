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
    log_level: str = "INFO"
    update_queue_size: int = 1000

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

        queue_size_raw = os.getenv("UPDATE_QUEUE_SIZE", "1000")
        try:
            queue_size = int(queue_size_raw)
        except ValueError as exc:
            raise ConfigurationError("UPDATE_QUEUE_SIZE must be an integer") from exc
        if queue_size < 1:
            raise ConfigurationError("UPDATE_QUEUE_SIZE must be positive")

        return cls(
            bot_token=bot_token,
            webhook_secret=webhook_secret,
            public_base_url=public_base_url,
            echo_enabled=_parse_bool("POC_ECHO_ENABLED", default=False),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            update_queue_size=queue_size,
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
