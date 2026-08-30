from __future__ import annotations

import re
from pathlib import Path

from secretary_bot import texts
from secretary_bot.templates import DEFAULT_TEMPLATES, TemplateCode

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "secretary_bot"
UI_MODULES = (
    "control.py",
    "morning.py",
    "notifications.py",
    "pipeline.py",
    "runtime.py",
    "templates.py",
)


def test_visible_cyrillic_is_centralized_in_the_text_catalog() -> None:
    cyrillic = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")

    leaked = {
        name: sorted(set(cyrillic.findall((SOURCE_ROOT / name).read_text())))
        for name in UI_MODULES
        if cyrillic.search((SOURCE_ROOT / name).read_text())
    }

    assert leaked == {}


def test_shipped_templates_and_controls_are_ukrainian() -> None:
    assert DEFAULT_TEMPLATES[TemplateCode.OFF_HOURS_DEFAULT] == texts.OFF_HOURS_TEMPLATE
    assert DEFAULT_TEMPLATES[TemplateCode.MONEY_PRIORITY] == texts.MONEY_PRIORITY_TEMPLATE
    assert "Сьогодні" in texts.BUTTON_TODAY
    assert "вимкнено" in texts.SECRETARY_OFF


def test_text_catalog_contains_no_russian_only_letters() -> None:
    source = (SOURCE_ROOT / "texts.py").read_text()

    assert re.search(r"[ыэъёЫЭЪЁ]", source) is None
