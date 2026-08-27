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
