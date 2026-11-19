import re
from typing import get_args

import pytest

from enji_guard_radar.texts import (
    CATALOGS,
    Entry,
    Language,
    counted,
    noun,
    parity_problems,
    placeholders,
    plural_index,
    severity_word,
    text,
)

COUNTS = (1, 2, 5, 11, 12, 21, 22, 25)
NOUN_KEYS = sorted(key for key, entry in CATALOGS["en"].items() if isinstance(entry, tuple))
LANGUAGES = get_args(Language)


@pytest.mark.parametrize(
    ("count", "want"),
    [(0, 1), (1, 0), (2, 1), (3, 1), (4, 1), (5, 1), (11, 1), (12, 1), (21, 1), (100, 1)],
)
def test_english_plural_index(count: int, want: int) -> None:
    assert plural_index("en", count) == want


@pytest.mark.parametrize(
    ("count", "want"),
    [
        (0, 2), (1, 0), (2, 1), (3, 1), (4, 1), (5, 2), (10, 2), (11, 2), (12, 2), (13, 2),
        (14, 2), (15, 2), (20, 2), (21, 0), (22, 1), (24, 1), (25, 2), (100, 2), (101, 0),
        (111, 2), (112, 2), (121, 0),
    ],
)  # fmt: skip
def test_russian_plural_index(count: int, want: int) -> None:
    assert plural_index("ru", count) == want


@pytest.mark.parametrize("key", NOUN_KEYS)
@pytest.mark.parametrize("count", COUNTS)
def test_every_counted_noun_in_english(key: str, count: int) -> None:
    forms = CATALOGS["en"][key]
    assert isinstance(forms, tuple)
    assert counted("en", key, count) == f"{count} {forms[0 if count == 1 else 1]}"


@pytest.mark.parametrize("key", NOUN_KEYS)
@pytest.mark.parametrize("count", COUNTS)
def test_every_counted_noun_in_russian_follows_the_rule(key: str, count: int) -> None:
    forms = CATALOGS["ru"][key]
    assert isinstance(forms, tuple)
    index = 0 if count % 10 == 1 and count % 100 != 11 else 2
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        index = 1
    assert noun("ru", key, count) == forms[index]
    assert counted("ru", key, count) == f"{count} {forms[index]}"


@pytest.mark.parametrize(
    ("count", "day", "finding"),
    [
        (1, "день", "находка"),
        (2, "дня", "находки"),
        (5, "дней", "находок"),
        (11, "дней", "находок"),
        (12, "дней", "находок"),
        (21, "день", "находка"),
        (22, "дня", "находки"),
        (25, "дней", "находок"),
    ],
)
def test_russian_days_and_findings(count: int, day: str, finding: str) -> None:
    assert counted("ru", "noun.day", count) == f"{count} {day}"
    assert re.match(rf"{count} {finding}(\s|$)", counted("ru", "noun.finding", count))


def test_severity_words() -> None:
    assert [severity_word("en", v) for v in ("high", "medium", "low", "unknown")] == [
        "high",
        "medium",
        "low",
        "unknown",
    ]
    assert severity_word("ru", "high") == "высокая"


def test_text_and_noun_reject_the_wrong_kind() -> None:
    with pytest.raises(TypeError):
        text("en", "noun.day")
    with pytest.raises(TypeError):
        noun("en", "severity.high", 1)


def test_parity_of_the_real_catalogs() -> None:
    assert parity_problems(CATALOGS["en"], CATALOGS["ru"]) == []


def test_parity_reports_a_key_missing_in_either_language() -> None:
    en = {"a": "x {n}", "b": "y"}
    assert parity_problems(en, {"a": "ключ {n}"}) == ["b: missing in ru"]
    assert parity_problems({"a": "x"}, {"a": "ключ", "c": "цвет"}) == ["c: missing in en"]


def test_parity_reports_a_missing_placeholder_by_key_and_name() -> None:
    problems = parity_problems({"k": "{a} and {b}"}, {"k": "только {a}"})
    assert problems == ["k: ru misses placeholder(s) ['b']"]


def test_parity_reports_an_unknown_placeholder() -> None:
    assert parity_problems({"k": "{a}"}, {"k": "{a} {z}"}) == [
        "k: ru has unknown placeholder(s) ['z']"
    ]


def test_parity_reports_wrong_plural_forms_and_kind_mismatch() -> None:
    en: dict[str, Entry] = {"n": ("day", "days"), "t": "x"}
    ru: dict[str, Entry] = {"n": ("день", "дня"), "t": ("раз", "два", "три")}
    problems = parity_problems(en, ru)
    assert "n: ru needs 3 noun forms, has 2" in problems
    assert "t: en and ru differ in kind (template vs counted noun)" in problems


def test_parity_reports_placeholders_inside_noun_forms() -> None:
    problems = parity_problems({"n": ("a {x}", "b")}, {"n": ("раз", "два", "три")})
    assert problems == ["n: en noun forms must not hold placeholders"]


def test_placeholders_lists_names_once() -> None:
    assert placeholders("{a} {b} {a} <b>x</b>") == {"a", "b"}


@pytest.mark.parametrize("language", LANGUAGES)
def test_catalog_hygiene(language: Language) -> None:
    for key, entry in CATALOGS[language].items():
        for template in (entry,) if isinstance(entry, str) else entry:
            assert "&" not in template, key
            assert "<" not in re.sub(r"</?b>", "", template), key
            assert ">" not in re.sub(r"</?b>", "", template), key
            if isinstance(entry, str):
                assert template.format(**dict.fromkeys(placeholders(template), "x"))
