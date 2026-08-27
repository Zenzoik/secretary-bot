import pytest

from secretary_bot.config import ConfigurationError, Settings


def test_echo_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "valid_secret")
    monkeypatch.delenv("POC_ECHO_ENABLED", raising=False)

    settings = Settings.from_env()

    assert settings.echo_enabled is False


def test_webhook_secret_rejects_unsupported_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "contains spaces")

    with pytest.raises(ConfigurationError, match="WEBHOOK_SECRET"):
        Settings.from_env()


def test_public_url_requires_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "valid_secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://example.com")

    with pytest.raises(ConfigurationError, match="HTTPS"):
        Settings.from_env()


def test_echo_requires_at_least_one_allowlisted_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "valid_secret")
    monkeypatch.setenv("POC_ECHO_ENABLED", "true")
    monkeypatch.delenv("POC_ALLOWED_CHAT_IDS", raising=False)

    with pytest.raises(ConfigurationError, match="POC_ALLOWED_CHAT_IDS"):
        Settings.from_env()


def test_allowed_chat_ids_are_parsed_and_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "valid_secret")
    monkeypatch.setenv("POC_ALLOWED_CHAT_IDS", "100, 200,100")

    settings = Settings.from_env()

    assert settings.allowed_chat_ids == frozenset({100, 200})


def test_redis_url_requires_redis_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "valid_secret")
    monkeypatch.setenv("REDIS_URL", "http://localhost:6379")

    with pytest.raises(ConfigurationError, match="REDIS_URL"):
        Settings.from_env()


def test_dedup_ttl_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "valid_secret")
    monkeypatch.setenv("DEDUP_TTL_SECONDS", "0")

    with pytest.raises(ConfigurationError, match="DEDUP_TTL_SECONDS"):
        Settings.from_env()
