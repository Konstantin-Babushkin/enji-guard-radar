from pathlib import Path

import pytest

from enji_guard_radar.cli import read_config
from enji_guard_radar.config import ConfigError, RadarConfig, parse_config

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
