"""The pure merge and validation of the settings sheet over the YAML config."""

import pytest
from sheet_fakes import PROJECT_A, PROJECT_B, globals_grid, projects_grid, sheet

from enji_guard_radar.config import RadarConfig, parse_config
from enji_guard_radar.sheet_settings import (
    LIST_GLOBAL_KEYS,
    SheetData,
    SheetInvalid,
    allowed_global_keys,
    apply_sheet,
)


def yaml_config(text: str = "") -> RadarConfig:
    return parse_config(text, source="test")


def invalid(base: RadarConfig, data: SheetData) -> SheetInvalid:
    with pytest.raises(SheetInvalid) as caught:
        apply_sheet(base, data)
    return caught.value


# --- the merge ------------------------------------------------------------------------------


def test_a_filled_cell_overrides_the_yaml_value_and_an_empty_cell_keeps_it() -> None:
    base = yaml_config(
        f'projects:\n  "{PROJECT_A}":\n    stale_after_days: 30\n    tests_below: 40\n'
    )
    data = sheet(projects_grid({"project_id": PROJECT_A, "stale_after_days": "7"}))
    merged = apply_sheet(base, data)
    assert merged.projects[PROJECT_A].stale_after_days == 7
    assert merged.projects[PROJECT_A].tests_below == 40


def test_a_project_only_in_the_sheet_is_added_and_a_yaml_only_project_stays() -> None:
    base = yaml_config(f'projects:\n  "{PROJECT_A}":\n    mode: paused\n')
    data = sheet(projects_grid({"project_id": PROJECT_B, "mode": "excluded"}))
    merged = apply_sheet(base, data)
    assert merged.mode_for(PROJECT_A) == "paused"
    assert merged.mode_for(PROJECT_B) == "excluded"


def test_every_project_column_is_applied() -> None:
    row = {
        "project_id": PROJECT_A,
        "name": "Shop (ignored)",
        "mode": "Active",
        "high_priority_audit_types": "audit.security, audit.tech-health",
        "stale_after_days": "20",
        "high_aged_after_days": "21",
        "reminder_every_days": "3",
        "tooling_window_days": "90",
        "ai_readiness_below": "60",
        "tests_below": "0",
    }
    project = apply_sheet(yaml_config(), sheet(projects_grid(row))).projects[PROJECT_A]
    assert project.mode == "active"
    assert project.high_priority_audit_types == ("audit.security", "audit.tech-health")
    assert (project.stale_after_days, project.high_aged_after_days) == (20, 21)
    assert (project.reminder_every_days, project.tooling_window_days) == (3, 90)
    assert (project.ai_readiness_below, project.tests_below) == (60, 0)


def test_a_row_with_only_an_id_changes_nothing() -> None:
    base = yaml_config()
    assert apply_sheet(base, sheet(projects_grid({"project_id": PROJECT_A}))) == base


def test_globals_set_dotted_paths_over_the_yaml() -> None:
    base = yaml_config("schedule:\n  every_minutes: 180\ndigest:\n  hour_utc: 8\n")
    data = sheet(
        globals_=globals_grid(
            ("schedule.every_minutes", "60"),
            ("digest.weekday", "Friday"),
            ("messages.language", "ru"),
            ("high.high_priority_audit_types", "audit.security,audit.tech-health"),
            ("tooling_gaps.audit_types", "audit.dead-code"),
            ("scores.tests_below", "50"),
            ("defaults.stale_after_days", ""),
        )
    )
    merged = apply_sheet(base, data)
    assert merged.schedule.every_minutes == 60
    assert merged.digest.weekday == "friday"
    assert merged.digest.hour_utc == 8
    assert merged.messages.language == "ru"
    assert merged.high.high_priority_audit_types == ("audit.security", "audit.tech-health")
    assert merged.tooling_gaps.audit_types == ("audit.dead-code",)
    assert merged.scores.tests_below == 50
    assert merged.defaults.stale_after_days == 14


def test_sheet_values_never_touch_the_sections_the_sheet_may_not_set() -> None:
    merged = apply_sheet(yaml_config("triage:\n  enabled: true\n"), sheet())
    assert merged.triage.enabled is True


def test_blank_rows_and_surplus_trailing_cells_are_ignored() -> None:
    projects = (
        ("project_id", "mode", "", ""),
        (PROJECT_A, "paused", "", ""),
        (),
        ("", "", "", ""),
    )
    merged = apply_sheet(yaml_config(), sheet(projects=projects))
    assert merged.mode_for(PROJECT_A) == "paused"


def test_list_keys_are_exactly_the_allowed_keys_holding_tuples() -> None:
    tuple_keys = {
        key for key in allowed_global_keys() if "tuple" in str(_annotation_of(key)).lower()
    }
    assert tuple_keys == LIST_GLOBAL_KEYS


def _annotation_of(key: str) -> object:
    section, name = key.split(".")
    model = RadarConfig.model_fields[section].annotation
    assert model is not None
    return model.model_fields[name].annotation


def test_allowed_keys_cover_the_global_sections_and_exclude_secrets_and_ops() -> None:
    keys = allowed_global_keys()
    assert {"schedule.every_minutes", "digest.weekday", "messages.language"} <= keys
    assert {"high.clear_after_clean_runs", "tooling_gaps.window_days"} <= keys
    assert not any(key.startswith(("triage.", "ops.", "sheet.")) for key in keys)
    assert "high.audit_types" not in keys


# --- the whole sheet is valid or nothing applies -----------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            sheet(projects_grid({"project_id": PROJECT_A, "stale_after_days": "0"})),
            "projects row 2 column stale_after_days",
        ),
        (
            sheet(projects_grid({"project_id": PROJECT_A, "tests_below": "101"})),
            "projects row 2 column tests_below",
        ),
        (
            sheet(projects_grid({"project_id": PROJECT_A, "tooling_window_days": "3"})),
            "projects row 2 column tooling_window_days",
        ),
        (
            sheet(projects_grid({"project_id": PROJECT_A, "reminder_every_days": "weekly"})),
            "projects row 2 column reminder_every_days",
        ),
        (
            sheet(projects_grid({"project_id": PROJECT_A, "stale_after_days": "7.5"})),
            "projects row 2 column stale_after_days",
        ),
        (
            sheet(projects_grid({"project_id": PROJECT_A, "mode": "sleeping"})),
            "projects row 2 column mode",
        ),
        (
            sheet(
                projects_grid({"project_id": PROJECT_A, "high_priority_audit_types": "security"})
            ),
            "projects row 2 column high_priority_audit_types",
        ),
        (
            sheet(
                projects_grid(
                    {"project_id": PROJECT_A, "mode": "paused"},
                    {"project_id": PROJECT_B, "stale_after_days": "-1"},
                )
            ),
            "projects row 3 column stale_after_days",
        ),
        (
            sheet(globals_=globals_grid(("schedule.every_minutes", "7"))),
            "globals row 2 column value",
        ),
        (
            sheet(globals_=globals_grid(("digest.hour_utc", "24"))),
            "globals row 2 column value",
        ),
        (
            sheet(globals_=globals_grid(("digest.weekday", "someday"))),
            "globals row 2 column value",
        ),
        (
            sheet(globals_=globals_grid(("messages.language", "de"))),
            "globals row 2 column value",
        ),
        (
            sheet(globals_=globals_grid(("high.high_priority_audit_types", ","))),
            "globals row 2 column value",
        ),
    ],
)
def test_a_bad_value_invalidates_the_whole_sheet_and_names_tab_row_and_column(
    data: SheetData, expected: str
) -> None:
    error = invalid(yaml_config(), data)
    assert expected in str(error)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            sheet(projects_grid({"project_id": PROJECT_A}, columns=("project_id", "colour"))),
            "projects row 1 column (header): unknown column",
        ),
        (
            sheet(projects_grid({"project_id": PROJECT_A}, columns=("mode", "name"))),
            "projects row 1 column project_id: required column is missing",
        ),
        (
            sheet(projects_grid(columns=("project_id", "mode", "mode"))),
            "projects row 1 column mode: column is listed twice",
        ),
        (
            sheet(projects_grid({"project_id": PROJECT_A}), globals_=((),)),
            "globals row 1 column key: required column is missing",
        ),
        (
            sheet(globals_=(("key", "value", "note"),)),
            "globals row 1 column (header): unknown column",
        ),
        (
            sheet(projects=()),
            "projects row 1 column project_id: the tab is empty",
        ),
        (
            sheet(
                projects_grid(
                    {"project_id": PROJECT_A}, {"project_id": PROJECT_A, "mode": "paused"}
                )
            ),
            "projects row 3 column project_id: duplicate project id (first at row 2)",
        ),
        (
            sheet(projects_grid({"mode": "paused"})),
            "projects row 2 column project_id: must not be empty",
        ),
        (
            sheet(projects=(("project_id", "mode"), (PROJECT_A, "paused", "stray"))),
            "projects row 2 column (no header): a cell has no column header",
        ),
        (
            sheet(globals_=globals_grid(("triage.enabled", "true"))),
            "globals row 2 column key: unknown key or a key the sheet may not set",
        ),
        (
            sheet(globals_=globals_grid(("ops.failures_before_alert", "2"))),
            "globals row 2 column key: unknown key or a key the sheet may not set",
        ),
        (
            sheet(globals_=globals_grid(("sheet.enabled", "false"))),
            "globals row 2 column key: unknown key or a key the sheet may not set",
        ),
        (
            sheet(globals_=globals_grid(("digest.colour", "red"))),
            "globals row 2 column key: unknown key or a key the sheet may not set",
        ),
        (
            sheet(globals_=globals_grid(("", "x"))),
            "globals row 2 column key: key is empty",
        ),
        (
            sheet(globals_=globals_grid(("digest.hour_utc", "4"), ("digest.hour_utc", "5"))),
            "globals row 3 column key: digest.hour_utc: key is listed twice (first at row 2)",
        ),
    ],
)
def test_structure_errors_name_tab_row_and_column(data: SheetData, expected: str) -> None:
    assert expected in str(invalid(yaml_config(), data))


def test_all_problems_are_reported_together() -> None:
    data = sheet(
        projects_grid(
            {"project_id": PROJECT_A, "stale_after_days": "0"},
            {"project_id": PROJECT_B, "tests_below": "500"},
        ),
        globals_grid(("digest.hour_utc", "99")),
    )
    error = invalid(yaml_config(), data)
    assert len(error.problems) == 3
    text = str(error)
    assert "projects row 2" in text
    assert "projects row 3" in text
    assert "globals row 2" in text


def test_errors_never_carry_a_cell_value() -> None:
    secret = "hunter2-not-a-number"
    for data in (
        sheet(projects_grid({"project_id": PROJECT_A, "stale_after_days": secret})),
        sheet(projects_grid({"project_id": PROJECT_A, "mode": secret})),
        sheet(globals_=globals_grid(("digest.hour_utc", secret))),
        sheet(globals_=globals_grid((secret, "1"))),
        sheet(projects_grid({"project_id": PROJECT_A, "high_priority_audit_types": secret})),
    ):
        assert secret not in str(invalid(yaml_config(), data))


def test_an_invalid_sheet_applies_nothing() -> None:
    base = yaml_config()
    data = sheet(
        projects_grid({"project_id": PROJECT_A, "mode": "paused"}),
        globals_grid(("digest.hour_utc", "99")),
    )
    with pytest.raises(SheetInvalid):
        apply_sheet(base, data)
    assert base.mode_for(PROJECT_A) == "active"


def test_a_sheet_that_breaks_a_cross_field_rule_is_reported_at_the_cell() -> None:
    error = invalid(yaml_config(), sheet(globals_=globals_grid(("schedule.every_minutes", "7"))))
    assert "must divide 1440" in str(error)
