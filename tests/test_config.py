import re
from pathlib import Path

import pytest

from enji_guard_radar import config as config_module
from enji_guard_radar.cli import read_config
from enji_guard_radar.config import ConfigError, RadarConfig, parse_config
from enji_guard_radar.high_history import HISTORY_WINDOW

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "radar.example.yaml"
PROJECT = "00000000-0000-4000-8000-00000000a002"


def parse(text: str) -> RadarConfig:
    return parse_config(text, source="test.yaml")


@pytest.mark.parametrize("days", [1, 14, 365])
def test_strict_stale_days_accepts_bounds(days: int) -> None:
    assert parse(f"defaults:\n  stale_after_days: {days}\n").defaults.stale_after_days == days


@pytest.mark.parametrize("days", ["0", "366", "14.0", "14.5", "'14'", "true", "null"])
def test_strict_stale_days_rejects(days: str) -> None:
    with pytest.raises(ConfigError, match=r"test\.yaml: defaults\.stale_after_days"):
        parse(f"defaults:\n  stale_after_days: {days}\n")


@pytest.mark.parametrize("value", [1, 3, 50])
def test_strict_collapse_above_accepts(value: int) -> None:
    assert parse(f"alerts:\n  collapse_above: {value}\n").alerts.collapse_above == value


@pytest.mark.parametrize("value", ["0", "51", "3.0", "'3'", "false"])
def test_strict_collapse_above_rejects(value: str) -> None:
    with pytest.raises(ConfigError, match=r"test\.yaml: alerts\.collapse_above"):
        parse(f"alerts:\n  collapse_above: {value}\n")


def test_strict_project_override_bounds_and_mode() -> None:
    with pytest.raises(ConfigError, match=rf"projects\.{PROJECT}\.stale_after_days"):
        parse(f'projects:\n  "{PROJECT}": {{stale_after_days: 0}}\n')
    with pytest.raises(ConfigError, match=rf"projects\.{PROJECT}\.mode"):
        parse(f'projects:\n  "{PROJECT}": {{mode: sleeping}}\n')


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ("colour: red\n", "colour"),
        ("defaults:\n  stale_days: 3\n", "defaults.stale_days"),
        ("alerts:\n  collapse: 3\n", "alerts.collapse"),
        (f'projects:\n  "{PROJECT}": {{stale_days: 3}}\n', f"projects.{PROJECT}.stale_days"),
        ("defaults:\n  telegram_bot_token: abc\n", "defaults.telegram_bot_token"),
    ],
)
def test_unknown_key_names_file_and_dotted_key(text: str, where: str) -> None:
    with pytest.raises(ConfigError) as error:
        parse(text)
    assert f"test.yaml: {where}:" in str(error.value)
    assert "abc" not in str(error.value)


def test_duplicate_project_id_is_rejected_with_the_key() -> None:
    text = f'projects:\n  "{PROJECT}": {{mode: paused}}\n  "{PROJECT}": {{mode: paused}}\n'
    with pytest.raises(ConfigError, match=rf"test\.yaml: line 3: duplicated key '{PROJECT}'"):
        parse(text)


def test_duplicate_field_in_one_mapping_is_rejected_with_the_key() -> None:
    with pytest.raises(ConfigError, match="duplicated key 'stale_after_days'"):
        parse("defaults:\n  stale_after_days: 3\n  stale_after_days: 4\n")


@pytest.mark.parametrize("text", ["- a\n- b\n", "42\n", "just text\n"])
def test_top_level_must_be_a_mapping(text: str) -> None:
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        parse(text)


def test_integer_project_id_key_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"projects\.1"):
        parse("projects:\n  1: {mode: paused}\n")


def test_yaml_syntax_error_names_the_line() -> None:
    with pytest.raises(ConfigError, match=r"test\.yaml: line \d+"):
        parse("defaults: [unclosed\n")


def test_python_object_tags_are_refused() -> None:
    with pytest.raises(ConfigError):
        parse("defaults: !!python/object/apply:os.system ['true']\n")


@pytest.mark.parametrize("text", ["", "\n", "# only a comment\n"])
def test_empty_config_gives_defaults(text: str) -> None:
    assert parse(text) == RadarConfig()


def test_empty_project_entry_behaves_like_no_entry() -> None:
    config = parse(f'projects:\n  "{PROJECT}": {{}}\n')
    assert config.thresholds_for(PROJECT) == RadarConfig().thresholds_for(PROJECT)
    assert config.mode_for(PROJECT) == "active"


@pytest.mark.parametrize(
    "text",
    [
        f'defaults:\n  stale_after_days: 7\nprojects:\n  "{PROJECT}": {{stale_after_days: 30}}\n',
        f'projects:\n  "{PROJECT}": {{stale_after_days: 30}}\ndefaults:\n  stale_after_days: 7\n',
    ],
)
def test_project_override_wins_whatever_the_section_order(text: str) -> None:
    config = parse(text)
    assert config.thresholds_for(PROJECT).stale_after_days == 30
    assert config.thresholds_for("other").stale_after_days == 7


def test_pause_and_exclude_modes() -> None:
    config = parse("projects:\n  p1: {mode: paused}\n  p2: {mode: excluded}\n")
    assert [config.mode_for(p) for p in ("p1", "p2", "p3")] == ["paused", "excluded", "active"]


def test_example_config_equals_defaults() -> None:
    assert parse_config(EXAMPLE.read_text(encoding="utf-8"), source=str(EXAMPLE)) == RadarConfig()


def test_read_config_without_a_path_gives_defaults() -> None:
    assert read_config(None) == RadarConfig()


def test_read_config_empty_file_gives_defaults(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    assert read_config(path) == RadarConfig()


def test_read_config_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        read_config(tmp_path / "missing.yaml")


# --- high section and high_aged_after_days ---


def test_high_defaults() -> None:
    config = RadarConfig()
    assert config.high.clear_after_clean_runs == 2
    assert config.high.audit_types is None
    assert config.thresholds_for("any").high_aged_after_days == 14


@pytest.mark.parametrize("value", [1, 2, 10])
def test_high_clear_after_clean_runs_accepts_range(value: int) -> None:
    assert parse(f"high: {{clear_after_clean_runs: {value}}}").high.clear_after_clean_runs == value


@pytest.mark.parametrize("value", ["0", "11", "true", "'2'", "2.5"])
def test_high_clear_after_clean_runs_rejects(value: str) -> None:
    with pytest.raises(ConfigError, match=r"c\.yaml: high\.clear_after_clean_runs"):
        parse_config(f"high: {{clear_after_clean_runs: {value}}}", source="c.yaml")


@pytest.mark.parametrize("value", [1, 14, 365])
def test_high_aged_after_days_accepts_range_default_and_project(value: int) -> None:
    config = parse(
        f"defaults: {{high_aged_after_days: {value}}}\n"
        f"projects: {{p1: {{high_aged_after_days: {value}}}}}\n"
    )
    assert config.defaults.high_aged_after_days == value
    assert config.thresholds_for("p1").high_aged_after_days == value


@pytest.mark.parametrize("value", ["0", "366", "true", "'14'"])
def test_high_aged_after_days_rejects(value: str) -> None:
    with pytest.raises(ConfigError, match=r"defaults\.high_aged_after_days"):
        parse_config(f"defaults: {{high_aged_after_days: {value}}}", source="c.yaml")
    with pytest.raises(ConfigError, match=r"projects\.p1\.high_aged_after_days"):
        parse_config(f"projects: {{p1: {{high_aged_after_days: {value}}}}}", source="c.yaml")


@pytest.mark.parametrize(
    "value",
    ["null", "[audit.security]", "[audit.security, audit.public-api-access-safety]"],
)
def test_high_audit_types_accepts(value: str) -> None:
    config = parse(f"high: {{audit_types: {value}}}")
    expected = None if value == "null" else tuple(value.strip("[]").split(", "))
    assert config.high.audit_types == expected


@pytest.mark.parametrize(
    "value",
    ["[]", "[security]", "[audit.Security]", "[audit.security, audit.security]", "audit.security"],
)
def test_high_audit_types_rejects(value: str) -> None:
    with pytest.raises(ConfigError, match=r"c\.yaml: high\.audit_types"):
        parse_config(f"high: {{audit_types: {value}}}", source="c.yaml")


def test_high_unknown_key_names_file_and_key() -> None:
    with pytest.raises(ConfigError, match=r"c\.yaml: high\.rearm"):
        parse_config("high: {rearm: 1}", source="c.yaml")


def test_high_aged_override_is_independent_of_stale_override() -> None:
    config = parse(
        "defaults: {high_aged_after_days: 30, stale_after_days: 7}\n"
        "projects: {p1: {high_aged_after_days: 3}, p2: {stale_after_days: 5}}\n"
    )
    got = [
        (
            p,
            config.thresholds_for(p).stale_after_days,
            config.thresholds_for(p).high_aged_after_days,
        )
        for p in ("p1", "p2", "p3")
    ]
    assert got == [("p1", 7, 3), ("p2", 5, 30), ("p3", 7, 30)]


def test_ops_defaults() -> None:
    assert RadarConfig().ops.failures_before_alert == 3


@pytest.mark.parametrize("value", [1, 3, 20])
def test_ops_failures_before_alert_accepts_range(value: int) -> None:
    assert parse(f"ops:\n  failures_before_alert: {value}\n").ops.failures_before_alert == value


@pytest.mark.parametrize("value", ["0", "21", "3.0", "'3'", "true", "null"])
def test_ops_failures_before_alert_rejects(value: str) -> None:
    with pytest.raises(ConfigError, match=r"test\.yaml: ops\.failures_before_alert"):
        parse(f"ops:\n  failures_before_alert: {value}\n")


def test_ops_unknown_key_names_file_and_key() -> None:
    with pytest.raises(ConfigError) as error:
        parse("ops:\n  failures: 3\n")
    assert "test.yaml: ops.failures:" in str(error.value)


# --- Phase 3 sections (digest, reminders, scores, recurring, tooling gaps) ---

PHASE3_TEXT = f"""
digest:
  weekday: friday
  hour_utc: 7
  grace_hours: 48
  max_items: 12
reminders:
  every_days: 14
scores:
  ai_readiness_below: 40
  tests_below: 50
recurring:
  after_runs: 5
tooling_gaps:
  window_days: 90
  audit_types: [audit.security, audit.tests]
projects:
  "{PROJECT}":
    ai_readiness_below: 70
    tests_below: 0
    tooling_window_days: 30
  other: {{}}
"""


def test_phase3_sections_reach_their_accessors(tmp_path: Path) -> None:
    path = tmp_path / "radar.yaml"
    path.write_text(PHASE3_TEXT, encoding="utf-8")
    config = read_config(path)
    assert config.digest.weekday_number == 4
    assert (config.digest.hour_utc, config.digest.grace_hours, config.digest.max_items) == (
        7,
        48,
        12,
    )
    assert config.reminders.every_days == 14
    assert config.recurring.after_runs == 5
    assert config.score_threshold_for(PROJECT, "audit.ai-readiness") == 70
    assert config.score_threshold_for(PROJECT, "audit.tests") == 0
    assert config.score_threshold_for("other", "audit.ai-readiness") == 40
    assert config.score_threshold_for("other", "audit.tests") == 50
    assert config.score_threshold_for(PROJECT, "audit.security") is None
    assert config.tooling_window_for(PROJECT) == 30
    assert config.tooling_window_for("other") == 90
    assert config.tooling_gaps.audit_types == ("audit.security", "audit.tests")


def test_phase3_defaults_are_the_decided_ones() -> None:
    config = RadarConfig()
    assert (config.digest.weekday, config.digest.weekday_number) == ("wednesday", 2)
    assert (config.digest.hour_utc, config.digest.grace_hours, config.digest.max_items) == (
        5,
        24,
        10,
    )
    assert config.reminders.every_days == 7
    assert (config.scores.ai_readiness_below, config.scores.tests_below) == (None, None)
    assert config.recurring.after_runs == 3
    assert config.tooling_gaps.window_days == 60
    assert config.tooling_gaps.audit_types == (
        "audit.dead-code",
        "audit.implementation-unification",
        "audit.security",
        "audit.dependency-hygiene",
    )


def test_phase3_score_rules_ship_off() -> None:
    config = RadarConfig()
    assert config.score_threshold_for("any", "audit.ai-readiness") is None
    assert config.score_threshold_for("any", "audit.tests") is None
    assert config.tooling_window_for("any") == 60


def test_phase3_recurring_max_fits_the_stored_history() -> None:
    assert config_module.RECURRING_AFTER_MAX <= HISTORY_WINDOW


# (section, key, accepted values, rejected values); rejected are YAML scalars.
INT_KEYS = [
    ("digest", "hour_utc", [0, 5, 23], ["24", "-1"]),
    ("digest", "grace_hours", [1, 24, 168], ["0", "169"]),
    ("digest", "max_items", [3, 10, 50], ["2", "51"]),
    ("reminders", "every_days", [1, 7, 90], ["0", "91"]),
    ("scores", "ai_readiness_below", [0, 50, 100], ["101", "-1"]),
    ("scores", "tests_below", [0, 50, 100], ["101", "-1"]),
    ("recurring", "after_runs", [2, 3, 20], ["1", "21"]),
    ("tooling_gaps", "window_days", [7, 60, 365], ["6", "366"]),
]
WRONG_TYPES = ["'5'", "5.0", "true", "false"]


def _int_rows(accepted: bool) -> list[tuple[str, str, object]]:
    return [
        (section, key, value)
        for section, key, good, bad in INT_KEYS
        for value in (good if accepted else bad + WRONG_TYPES)
    ]


@pytest.mark.parametrize(("section", "key", "value"), _int_rows(accepted=True))
def test_phase3_int_keys_accept_the_range(section: str, key: str, value: int) -> None:
    config = parse(f"{section}: {{{key}: {value}}}")
    assert getattr(getattr(config, section), key) == value


@pytest.mark.parametrize(("section", "key", "value"), _int_rows(accepted=False))
def test_phase3_int_keys_reject_without_echoing_the_value(
    section: str, key: str, value: str
) -> None:
    with pytest.raises(ConfigError) as error:
        parse_config(f"{section}: {{{key}: {value}}}", source="c.yaml")
    message = str(error.value)
    assert f"c.yaml: {section}.{key}:" in message
    assert "input_value" not in message
    assert re.search(rf"(?<![\w.-]){re.escape(value.strip(chr(39)))}(?![\w.])", message) is None


@pytest.mark.parametrize("key", ["ai_readiness_below", "tests_below"])
def test_scores_null_means_off(key: str) -> None:
    assert getattr(parse(f"scores: {{{key}: null}}").scores, key) is None


@pytest.mark.parametrize("name", config_module.WEEKDAY_NAMES)
def test_digest_weekday_accepts_every_name(name: str) -> None:
    config = parse(f"digest: {{weekday: {name}}}")
    assert config.digest.weekday == name
    assert config.digest.weekday_number == config_module.WEEKDAY_NAMES.index(name)


def test_digest_weekday_monday_is_zero_and_sunday_is_six() -> None:
    assert parse("digest: {weekday: monday}").digest.weekday_number == 0
    assert parse("digest: {weekday: sunday}").digest.weekday_number == 6


@pytest.mark.parametrize("value", ["Wednesday", "wed", "3", "null", "''"])
def test_digest_weekday_rejects(value: str) -> None:
    with pytest.raises(ConfigError) as error:
        parse_config(f"digest: {{weekday: {value}}}", source="c.yaml")
    assert "c.yaml: digest.weekday:" in str(error.value)
    assert "input_value" not in str(error.value)


def test_tooling_gaps_audit_types_accepts_a_list() -> None:
    config = parse("tooling_gaps: {audit_types: [audit.tests, audit.dead-code]}")
    assert config.tooling_gaps.audit_types == ("audit.tests", "audit.dead-code")


@pytest.mark.parametrize(
    "value",
    ["null", "[]", "[security]", "[audit.Security]", "[audit.]", "[audit.tests, audit.tests]"],
)
def test_tooling_gaps_audit_types_rejects(value: str) -> None:
    with pytest.raises(ConfigError, match=r"c\.yaml: tooling_gaps\.audit_types"):
        parse_config(f"tooling_gaps: {{audit_types: {value}}}", source="c.yaml")


def test_tooling_gaps_and_high_share_the_audit_type_messages() -> None:
    messages = []
    for section in ("high", "tooling_gaps"):
        with pytest.raises(ConfigError) as error:
            parse_config(f"{section}: {{audit_types: [audit.a, audit.a]}}", source="c.yaml")
        messages.append(str(error.value).split(": ", 2)[-1])
    assert messages[0] == messages[1]


def test_phase3_high_audit_types_still_accepts_null_and_a_list() -> None:
    assert parse("high: {audit_types: null}").high.audit_types is None
    assert parse("high: {audit_types: [audit.security]}").high.audit_types == ("audit.security",)


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ("digest: {colour: red}", "digest.colour"),
        ("reminders: {every: 3}", "reminders.every"),
        ("scores: {security_below: 3}", "scores.security_below"),
        ("recurring: {after: 3}", "recurring.after"),
        ("tooling_gaps: {window: 3}", "tooling_gaps.window"),
        (f"projects: {{'{PROJECT}': {{tooling_days: 9}}}}", f"projects.{PROJECT}.tooling_days"),
    ],
)
def test_phase3_unknown_key_names_file_and_dotted_key(text: str, where: str) -> None:
    with pytest.raises(ConfigError) as error:
        parse(text)
    assert f"test.yaml: {where}:" in str(error.value)


@pytest.mark.parametrize("section", ["digest", "reminders", "scores", "recurring", "tooling_gaps"])
def test_phase3_duplicate_section_key_is_rejected(section: str) -> None:
    with pytest.raises(ConfigError, match="duplicated key"):
        parse(f"{section}: {{}}\n{section}: {{}}\n")


def test_recurring_duplicate_field_is_rejected() -> None:
    with pytest.raises(ConfigError, match="duplicated key 'after_runs'"):
        parse("recurring:\n  after_runs: 3\n  after_runs: 4\n")


@pytest.mark.parametrize(
    ("key", "good", "bad"),
    [
        ("ai_readiness_below", [0, 60, 100], ["101", "-1", "'5'", "5.0", "true"]),
        ("tests_below", [0, 60, 100], ["101", "-1", "'5'", "5.0", "true"]),
        ("tooling_window_days", [7, 90, 365], ["6", "366", "'9'", "9.0", "true"]),
    ],
)
def test_phase3_project_override_bounds(key: str, good: list[int], bad: list[str]) -> None:
    for value in good:
        entry = parse(f"projects: {{p1: {{{key}: {value}}}}}").projects["p1"]
        assert getattr(entry, key) == value
    assert getattr(parse("projects: {p1: {}}").projects["p1"], key) is None
    for value in bad:
        with pytest.raises(ConfigError, match=rf"test\.yaml: projects\.p1\.{key}:"):
            parse(f"projects: {{p1: {{{key}: {value}}}}}")


def test_scores_project_value_beats_section_value() -> None:
    config = parse("scores: {tests_below: 50}\nprojects: {p1: {tests_below: 20}, p2: {}}")
    assert config.score_threshold_for("p1", "audit.tests") == 20
    assert config.score_threshold_for("p2", "audit.tests") == 50
    assert config.score_threshold_for("unlisted", "audit.tests") == 50


def test_scores_project_zero_wins_over_a_section_value() -> None:
    config = parse("scores: {tests_below: 50}\nprojects: {p1: {tests_below: 0}}")
    assert config.score_threshold_for("p1", "audit.tests") == 0


def test_scores_project_value_applies_with_the_section_off() -> None:
    config = parse("projects: {p1: {ai_readiness_below: 70}}")
    assert config.score_threshold_for("p1", "audit.ai-readiness") == 70
    assert config.score_threshold_for("p2", "audit.ai-readiness") is None


@pytest.mark.parametrize("action_key", ["audit.security", "audit.recon", "", "ai-readiness"])
def test_scores_unknown_audit_type_has_no_threshold(action_key: str) -> None:
    config = parse("scores: {ai_readiness_below: 50, tests_below: 50}")
    assert config.score_threshold_for("p1", action_key) is None


def test_tooling_gaps_window_project_override_beats_section() -> None:
    config = parse("tooling_gaps: {window_days: 90}\nprojects: {p1: {tooling_window_days: 14}}")
    assert config.tooling_window_for("p1") == 14
    assert config.tooling_window_for("p2") == 90


def test_phase3_example_documents_every_new_key() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    for key in (
        "weekday:",
        "hour_utc:",
        "grace_hours:",
        "max_items:",
        "every_days:",
        "ai_readiness_below:",
        "tests_below:",
        "after_runs:",
        "window_days:",
        "audit_types:",
        "tooling_window_days:",
    ):
        assert key in text, key
