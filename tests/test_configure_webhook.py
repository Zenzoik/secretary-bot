from __future__ import annotations

from secretary_bot.configure_webhook import _ALLOWED_UPDATES


def test_updates_the_pipeline_consumes_are_subscribed() -> None:
    assert {"business_connection", "business_message", "callback_query"} <= set(_ALLOWED_UPDATES)
