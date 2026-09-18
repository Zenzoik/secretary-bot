import json
from copy import deepcopy

import pytest

from secretary_bot.web_api import ClassifierPayload, parse_classifier_expansion

CODE = "type_21e0ac3888cc4412b54b668b9868d2a4"


def payload():
    return ClassifierPayload(
        directions=[
            {"code": "general", "label": "Загальне", "description": "Решта"},
            {"code": "money", "label": "Гроші", "description": "Оплата"},
            {
                "code": CODE,
                "label": "Підтримка",
                "description": "Вхід до кабінету",
                "reply_template": "Перевірю",
                "priority": "high",
            },
        ],
        system_prompt="Класифікуй вхідні повідомлення.",
        model="gpt-5.6-luna",
        confidence_min="0.7",
    )


def output():
    return {
        "system_prompt": f"Категорії general, money, {CODE}: класифікуй повідомлення.",
        "directions": [
            {"code": "general", "keywords": []},
            {"code": "money", "keywords": ["рахунок"]},
            {"code": CODE, "keywords": [" УВІЙТИ ", "пароль", "увійти"]},
        ],
    }


def test_expansion_normalizes_code_escapes_and_keywords_without_mutating_input():
    data = output()
    data["system_prompt"] = data["system_prompt"].replace("_", "\\_")
    request = payload()
    before = request.model_dump()
    result = parse_classifier_expansion(json.dumps(data), request)
    assert CODE in result["system_prompt"]
    assert "\\_" not in result["system_prompt"]
    assert result["directions"][-1]["keywords"] == ["увійти", "пароль"]
    assert request.model_dump() == before


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "unknown",
        "duplicate",
        "empty",
        "long",
        "separator",
        "type",
        "prompt_code",
        "stale_code",
    ],
)
def test_expansion_rejects_partial_or_invalid_output(problem):
    data = deepcopy(output())
    if problem == "missing":
        data["directions"].pop()
    elif problem == "unknown":
        data["directions"][-1]["code"] = "another"
    elif problem == "duplicate":
        data["directions"][-1]["code"] = "money"
    elif problem == "empty":
        data["directions"][-1]["keywords"] = []
    elif problem == "long":
        data["directions"][-1]["keywords"] = ["a" * 41]
    elif problem == "separator":
        data["directions"][-1]["keywords"] = ["пароль, вхід"]
    elif problem == "type":
        data["directions"][-1]["keywords"] = "пароль"
    elif problem == "prompt_code":
        data["system_prompt"] = data["system_prompt"].replace(CODE, CODE + "wrong")
    elif problem == "stale_code":
        data["system_prompt"] += " Видалений тип type_deadbeef — старе правило."
    with pytest.raises(ValueError):
        parse_classifier_expansion(json.dumps(data), payload())


def test_expansion_preserves_disabled_keywords_and_clears_general():
    request = payload()
    request.directions[-1].is_active = False
    request.directions[-1].keywords = ["вхід"]
    data = output()
    data["system_prompt"] = data["system_prompt"].replace(f", {CODE}", "")
    data["directions"][0]["keywords"] = ["все"]
    result = parse_classifier_expansion(json.dumps(data), request)
    assert result["directions"][0]["keywords"] == []
    assert result["directions"][-1]["keywords"] == ["вхід"]
