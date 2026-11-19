"""Triage request side: builder, prompt, schema, models and the shared fake. No Guard data."""

from enji_guard_radar.model import AlertKey, Severity
from enji_guard_radar.triage import (
    SYSTEM_PROMPT_EN,
    TriageCandidate,
    build_request,
    render_prompt,
)

HIGH, MEDIUM = Severity.HIGH, Severity.MEDIUM


def test_one_candidate_prompt() -> None:
    key = AlertKey(project_id="p1", level="high", action_key="audit.security")
    candidate = TriageCandidate(
        key=key,
        action_key="audit.security",
        high_count=1,
        age_days=3,
        titles=((HIGH, "F-1 SQL injection in order search"), (MEDIUM, "x")),
    )
    built = build_request([candidate], language="en", max_items=30)
    system, user = render_prompt(built.request, "n0nce")
    assert built.ids == {"i1": key}
    assert built.skipped == {}
    assert system is SYSTEM_PROMPT_EN
    assert user == (
        '<data id="n0nce">\n'
        '{"items":[{"item_id":"i1","audit_type":"audit.security","high_count":1,"age_days":3,'
        '"titles":["SQL injection in order search"]}]}\n'
        '</data id="n0nce">'
    )
