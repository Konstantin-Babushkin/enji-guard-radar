import json
from enji_guard_radar.model import AlertKey, Severity
from enji_guard_radar.triage import TriageCandidate, build_request, validate_reply

def cand(n):
    return TriageCandidate(key=AlertKey(project_id=f"p{n}", level="high"), action_key="audit.security",
        high_count=1, age_days=1, titles=((Severity.HIGH, f"Issue {n}"),))

built = build_request([cand(1), cand(2)], language="en", max_items=10)
def reply(*items):
    return json.dumps({"items": [{"item_id": i, "priority": p, "cause": c} for i, p, c in items]})
rows = {
    "good": reply(("i1", "P1", "SQL injection in order search"), ("i2", "p3", "Weak hashing")),
    "not_json": "nope",
    "schema": '{"items":[],"summary":"x"}',
    "ids": reply(("i1", "p1", "ok"), ("i1", "p2", "ok")),
    "digit": reply(("i1", "p1", "3 findings"), ("i2", "p2", "Weak hashing")),
    "missing": reply(("i1", "p1", "ok")),
    "deep": "[" * 65536,
}
for name, text in rows.items():
    v = validate_reply(built, text)
    print(name, sorted(k.project_id for k in v.accepted), v.failure.value if v.failure else None,
          sorted((k.value, n) for k, n in v.rejected.items()),
          sorted(i.priority for i in v.accepted.values()))
