"""Per-project high-priority audit types and reminder interval, and the `sheet:` config section."""

from pathlib import Path

import pytest
from test_high_pipeline import CALM_CONFIG, PID, SECURITY, Sim

from enji_guard_radar.config import ConfigError, RadarConfig, parse_config

TECH_HEALTH = "audit.tech-health"
OTHER = "p-2"


def parse(text: str) -> RadarConfig:
    return parse_config(text, source="test")


def test_project_overrides_beat_the_global_values() -> None:
    config = parse(
        "high:\n  high_priority_audit_types: [audit.security]\n"
        "reminders:\n  every_days: 7\n"
        f'projects:\n  "{PID}":\n    high_priority_audit_types: [audit.tech-health]\n'
        "    reminder_every_days: 3\n"
    )
    assert config.high_types_for(PID) == (TECH_HEALTH,)
    assert config.high_types_for(OTHER) == (SECURITY,)
    assert config.reminder_days_for(PID) == 3
    assert config.reminder_days_for(OTHER) == 7


def test_unset_project_overrides_fall_back_to_global_and_to_every_type() -> None:
    config = parse(f'projects:\n  "{PID}":\n    stale_after_days: 5\n')
    assert config.high_types_for(PID) is None
    assert config.reminder_days_for(PID) == 7


@pytest.mark.parametrize(
    ("body", "path"),
    [
        ("reminder_every_days: 0", f"projects.{PID}.reminder_every_days"),
        ("reminder_every_days: 91", f"projects.{PID}.reminder_every_days"),
        ("reminder_every_days: weekly", f"projects.{PID}.reminder_every_days"),
        ("high_priority_audit_types: []", f"projects.{PID}.high_priority_audit_types"),
        ("high_priority_audit_types: [security]", f"projects.{PID}.high_priority_audit_types"),
        (
            "high_priority_audit_types: [audit.security, audit.security]",
            f"projects.{PID}.high_priority_audit_types",
        ),
    ],
)
def test_bad_project_overrides_are_rejected_with_the_field_path(body: str, path: str) -> None:
    with pytest.raises(ConfigError, match=path.replace(".", r"\.")):
        parse(f'projects:\n  "{PID}":\n    {body}\n')


def test_sheet_section_ships_off() -> None:
    config = parse("defaults: {}\n")
    assert config.sheet.enabled is False
    assert config.sheet.spreadsheet_id is None


def test_enabled_sheet_needs_a_spreadsheet_id() -> None:
    with pytest.raises(ConfigError, match=r"sheet"):
        parse("sheet:\n  enabled: true\n")
    config = parse("sheet:\n  enabled: true\n  spreadsheet_id: 1AbC-def_GHIjklmnop\n")
    assert config.sheet.spreadsheet_id == "1AbC-def_GHIjklmnop"


def test_sheet_section_rejects_unknown_keys_and_bad_ids() -> None:
    with pytest.raises(ConfigError, match=r"sheet\.tab"):
        parse("sheet:\n  tab: projects\n")
    with pytest.raises(ConfigError, match=r"sheet\.spreadsheet_id"):
        parse("sheet:\n  spreadsheet_id: 'has spaces and /slashes'\n")


def test_example_config_documents_the_sheet_section_switched_off() -> None:
    example = Path(__file__).resolve().parents[1] / "config" / "radar.example.yaml"
    config = parse(example.read_text(encoding="utf-8"))
    assert config.sheet.enabled is False
    assert "sheet:" in example.read_text(encoding="utf-8")


def test_project_list_makes_a_type_low_priority_only_for_that_project(tmp_path: Path) -> None:
    sim = Sim(
        tmp_path,
        CALM_CONFIG + f'projects:\n  "{PID}":\n    high_priority_audit_types: [{TECH_HEALTH}]\n',
    )
    sim.poll(0)
    sim.add_run(high=3, day=1)
    report = sim.poll(1)
    assert report.messages == ()
    assert sim.history().open is True
    assert [c for c in sim.state().conditions if c.key.level == "high"] == []


def test_project_list_beats_a_global_list(tmp_path: Path) -> None:
    sim = Sim(
        tmp_path,
        CALM_CONFIG
        + f"high:\n  high_priority_audit_types: [{SECURITY}]\n"
        + f'projects:\n  "{PID}":\n    high_priority_audit_types: [{TECH_HEALTH}]\n',
    )
    sim.poll(0)
    sim.add_run(high=3, day=1)
    assert sim.poll(1).messages == ()
    sim.add_run(TECH_HEALTH, high=2, day=2)
    assert len(sim.poll(2).messages) == 1


def test_project_reminder_interval_beats_the_global_one(tmp_path: Path) -> None:
    config = CALM_CONFIG + f'projects:\n  "{PID}":\n    reminder_every_days: 3\n'
    sim = Sim(tmp_path, config, reminders_on=True)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    assert len(sim.poll(1).messages) == 1
    assert sim.poll(3).reminders_delivered == 0
    assert sim.poll(4).reminders_delivered == 1
