import pytest

from secretary_bot.config import ConfigurationError, Settings


@pytest.fixture(autouse=True)
def access_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MASTER_TELEGRAM_USER_ID", "42")
    monkeypatch.setenv("BOT_USERNAME", "secretary_test_bot")


def test_defaults_keep_the_bot_offline_and_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "valid_secret")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ALLOWED_CHAT_IDS", raising=False)

    settings = Settings.from_env()

    assert settings.anthropic_api_key is None
    assert settings.allowed_chat_ids == frozenset()


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


def test_database_url_must_use_an_async_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "valid_secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secretary@localhost/secretary")

    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        Settings.from_env()


def test_allowed_chat_ids_are_parsed_and_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "valid_secret")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "100, 200,100")

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


def test_classifier_timeout_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "valid_secret")
    monkeypatch.setenv("CLASSIFIER_TIMEOUT_SECONDS", "0")

    with pytest.raises(ConfigurationError, match="CLASSIFIER_TIMEOUT_SECONDS"):
        Settings.from_env()


def test_master_user_id_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "valid_secret")
    monkeypatch.delenv("MASTER_TELEGRAM_USER_ID")

    with pytest.raises(ConfigurationError, match="MASTER_TELEGRAM_USER_ID"):
        Settings.from_env()


def test_bot_username_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("WEBHOOK_SECRET", "valid_secret")
    monkeypatch.setenv("BOT_USERNAME", "bad username")

    with pytest.raises(ConfigurationError, match="BOT_USERNAME"):
        Settings.from_env()
