from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from secretary_bot.classifier import Category


class TemplateCode(StrEnum):
    OFF_HOURS_DEFAULT = "off_hours_default"
    MONEY_PRIORITY = "money_priority"


# FR-5. The contact sees an ordinary message from the owner, so the wording has
# to be true: one message, no promises the owner has not made.
DEFAULT_TEMPLATES: Mapping[TemplateCode, str] = {
    TemplateCode.OFF_HOURS_DEFAULT: "Сейчас нерабочее время, отвечу позже",
    TemplateCode.MONEY_PRIORITY: (
        "Сейчас нерабочее время. Вопрос по оплате увидел, отвечу первым делом утром."
    ),
}


def template_for(category: Category) -> TemplateCode:
    if category is Category.MONEY:
        return TemplateCode.MONEY_PRIORITY
    return TemplateCode.OFF_HOURS_DEFAULT


def render(code: TemplateCode, *, overrides: Mapping[str, str] | None = None) -> str:
    """The owner's own wording wins; the shipped text is the fallback."""
    if overrides:
        text = overrides.get(code.value, "").strip()
        if text:
            return text
    return DEFAULT_TEMPLATES[code]
