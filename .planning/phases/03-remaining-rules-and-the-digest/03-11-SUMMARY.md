# 03-11 summary: weekly digest in the run

Done: the digest step in `pipeline.py` (decide, build, render, deliver, mark after every acknowledgement),
`DigestStatus` plus `RunReport.digest` and `digest_messages`, deferral on an unlisted Guard, the CLI
`follow-ups` line, `QUIET_DIGEST` and `Sim(digest_on=...)`, `tests/test_digest_pipeline.py` (tracer through
`cli.main` and the edge table), the runbook sections, the Phase 3 data notes and the tooling-gap idea.

Judged by intent: the plan's "skip" row assumes a failed first attempt can be skipped later, but
`decide_digest` (03-03) returns not-due for an install with no digest history. The skip and
deferred-then-skipped tests therefore start from a week whose digest was sent.

Left for the operator (human checkpoints, not attempted): the dry-run preview and the private test-chat
check in `docs/deploy.md` (Phase 3 operator steps 4 and 5).
