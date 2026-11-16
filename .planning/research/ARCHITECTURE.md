# Architecture Research

**Domain:** Scheduled monitoring/alerting pipeline (poll -> snapshot -> pure rules -> LLM triage -> dedup -> notify), Python, file-based state
**Researched:** 2026-11-16
**Confidence:** MEDIUM overall. The pipeline shape and the crash/replay design are established engineering patterns (MEDIUM, no single authoritative source). Telegram and Claude CLI facts are HIGH (official docs fetched this session). Guard's data shape is unknown (LOW), so every Guard-facing field below is marked `ASSUMPTION`.

## Standard Architecture

Systems of this kind are a **one-shot batch pipeline run by an external timer**, not a daemon. Each run is a pure-core "functional core, imperative shell": I/O at the edges, a pure decision core in the middle, and one state object that is the single source of truth for "what have we already told people".

Two changes to `docs/design/v0.md` are recommended (details below):

1. **Dedup runs before triage**, not after. Claude is called only for messages that will actually be sent, and its output can never influence dedup keys or whether something is sent.
2. **The dedup ledger doubles as the outbox.** "Alerted" is a field on the finding record that is written only after Telegram acknowledges. A failed send leaves no mark, so the next run re-plans the same message. No separate queue is needed.

### System Overview

```
 external timer (systemd timer / cron)  ->  `enji-guard-radar run` (one-shot, flock'd)

┌──────────────────────────── SHELL (I/O, behind ports) ────────────────────────────┐
│  GuardSource        Triager            Notifier          StateStore      Clock     │
│  (Guard MCP)        (Claude)           (Telegram)        (state/*.json)  (now())   │
└────────┬───────────────┬──────────────────┬─────────────────┬──────────────────────┘
         │ Snapshot      │ TriageResult|None│ SendResult      │ State
┌────────▼───────────────▼──────────────────▼─────────────────▼──────────────────────┐
│                          pipeline.run_once  (orchestration only)                    │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                         CORE (pure functions, table-tested)                          │
│                                                                                      │
│  detect ──► reconcile ──► plan ──► [triage] ──► render ──► [send] ──► apply_delivery │
│  rules      ledger diff   what to   Claude       text,      Telegram    mark alerted │
│  over       NEW/PERSIST/  send &    explains     chunking,  per chunk   after ack    │
│  Snapshot   ESCALATED/    why       (optional)   fallback                            │
│  + history  REOPENED/                                                                │
│             RESOLVED                                                                 │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Pure? | Communicates With |
|-----------|----------------|-------|-------------------|
| `GuardSource` (port) + `GuardMcpSource` (adapter) | Fetch raw project state from Guard MCP, **normalize into domain `Snapshot`** (anti-corruption layer). Returns per-project `Ok` or `Failed`, never raises for a single project | No (I/O) | Guard MCP; returns `Snapshot` |
| `rules/` | Stateless-ish detection: `(ProjectSnapshot, HistoryView, Thresholds, now) -> list[Finding]`. One module per rule, registered in a list | Yes | Reads `Snapshot`, `HistoryView` |
| `ledger` (reconcile) | Diff current findings against the persisted ledger. Classify each as NEW / PERSISTING / ESCALATED / REOPENED / RESOLVED. Update first/last seen, episodes, runs seen. Never touches projects whose fetch failed | Yes | `State`, `Finding`s |
| `planner` | Decide what goes out this run: immediate alerts, reminders, resolved notes, weekly digest, radar-health. Applies dedup, reminder interval, quiet hours, digest schedule | Yes | `State`, changes, config, `now` |
| `Triager` (port) + `ClaudeCliTriager` (adapter) | Given the *planned* items plus a snapshot slice, return `TriageResult` or `None`. Owns prompt, subprocess/SDK call, JSON parse | No (I/O) | Claude; returns typed model |
| `triage` (validate/merge) | Cross-validate Claude output against the request (keys subset, no duplicates, length caps). Per-project fallback to rule-only | Yes | `TriageResult`, planned items |
| `render` | Build final text from planned items + optional triage. Escaping, redaction, chunking to <= 4096 chars, worst-first ordering | Yes | Planned items, triage |
| `Notifier` (port) + `TelegramNotifier` (adapter) | Send one message; classify outcome as `Delivered` / `RetryLater` / `Permanent` / `InDoubt` | No (I/O) | Telegram Bot API |
| `StateStore` (port) + `FileStateStore` (adapter) | `load()`, `save()`, `lock()`. Atomic replace, schema version, corruption handling | No (I/O) | `state/` directory |
| `config` | Env/`.env` for secrets; YAML for thresholds, per-project overrides, schedule. Pydantic-validated, secrets are `SecretStr` | Yes (parse) | Env, `config.yaml` |
| `pipeline.run_once` | Wire the above in order; own the commit points; build the `RunReport` and exit code | Orchestration | All |
| `cli` | `run`, `run --dry-run`, `probe`, `digest --force`; builds adapters or fakes from flags | Shell | `pipeline` |

### Recommended Project Structure

Matches the existing scaffold (`src/enji_guard_radar/`, entry point `enji_guard_radar:main`).

```
src/enji_guard_radar/
├── __init__.py          # main() delegates to cli.main
├── cli.py               # argparse; builds Deps (real or fake) from flags
├── config.py            # Settings (env), RadarConfig (yaml), Thresholds
├── model.py             # Snapshot, ProjectSnapshot, RunInfo, GuardFinding, Finding, Severity
├── ledger.py            # State, FindingRecord, Change; reconcile() (pure)
├── rules/
│   ├── __init__.py      # RULES registry: list[Rule]
│   ├── stale_runs.py    # one file per rule; each exports detect()
│   └── ...
├── planner.py           # plan() -> Plan (pure); dedup, reminders, digest due, quiet hours
├── triage.py            # TriageItem/TriageResult models, validate_against(), merge()
├── render.py            # render(plan, triage|None) -> list[OutboundMessage]; redact()
├── pipeline.py          # run_once(deps, mode) -> RunReport
├── ports.py             # Protocols: GuardSource, Triager, Notifier, StateStore, Clock
└── adapters/
    ├── guard_mcp.py     # real Guard MCP client + normalizer
    ├── guard_fixture.py # replays recorded JSON; used by tests, dry-run, offline dev
    ├── claude_cli.py    # `claude -p` adapter
    ├── telegram.py      # Bot API client + outcome classification
    ├── console.py       # PrintNotifier for dry-run
    └── file_store.py    # atomic JSON store + flock
tests/
├── fakes.py             # FakeGuard, FakeTriager, FakeNotifier(script outcomes), MemoryStore, FixedClock
├── test_rules_*.py      # table tests
├── test_ledger.py  test_planner.py  test_render.py  test_triage.py
├── test_pipeline_*.py   # end-to-end with fakes, incl. crash-injection
└── fixtures/guard/      # sanitized recorded Guard payloads (from `probe`)
```

### Structure Rationale

- **`ports.py` is `typing.Protocol`, not ABCs.** Structural typing keeps fakes trivial and works with mypy strict. Name ports by intent (`GuardSource.fetch_snapshot`, `Notifier.send`), as `AGENTS.md` requires, not by vendor.
- **Core never imports `adapters/`.** Enforce with an import-direction test (a 10-line test that greps imports) so the rule survives AI-written PRs.
- **Core is synchronous.** The MCP Python client is async; `GuardMcpSource` hides that behind `asyncio.run` so nothing async leaks into the pure core.
- **`rules/` is a package with a registry list.** Adding a rule is one new file plus one line; rules never call each other.

## Data Models

All models are frozen Pydantic v2 (`pydantic` is already a dependency). All datetimes are timezone-aware UTC; the adapter rejects naive ones. Pure functions take `now` as a parameter and never call `datetime.now()`.

### Snapshot (what Guard says right now)

```python
class RunInfo(BaseModel, frozen=True):
    run_id: str                      # ASSUMPTION: Guard exposes a stable run id
    finished_at: datetime
    status: str                      # ASSUMPTION

class GuardFinding(BaseModel, frozen=True):   # raw issue as Guard reports it
    guard_id: str | None             # ASSUMPTION: stable id; else adapter derives a fingerprint
    category: str                    # e.g. "security", "quality"
    severity: Severity               # normalized: low | medium | high | critical
    title: str
    first_detected_at: datetime | None  # ASSUMPTION: prefer Guard's own date for age
    status: Literal["open", "fixed", "ignored"]

class Signals(BaseModel, frozen=True):         # all optional: Guard shape unknown
    harness_score: float | None
    has_guard_md: bool | None
    ai_readiness: float | None
    test_signal: float | None

class ProjectSnapshot(BaseModel, frozen=True):
    project_id: str
    name: str
    last_run: RunInfo | None         # None = never ran
    findings: tuple[GuardFinding, ...]
    signals: Signals

class ProjectFetch(BaseModel, frozen=True):
    project_id: str
    snapshot: ProjectSnapshot | None   # None iff fetch failed
    error: str | None                  # sanitized, no secrets

class Snapshot(BaseModel, frozen=True):
    taken_at: datetime
    projects: tuple[ProjectFetch, ...]
```

Rules never see raw MCP JSON. The adapter owns mapping, unit normalization (scores to a documented scale) and timestamp parsing. This is the single place that changes when Guard's real shape is known.

### Finding (what the rules concluded)

```python
class Finding(BaseModel, frozen=True):
    key: str                 # project_id + rule + subject; STABLE, see below
    project_id: str
    rule: str                # "stale_runs", "unaddressed_high_risk", ...
    subject: str             # "-" for project-level; guard_id/fingerprint for per-issue
    severity: Severity       # rule-assigned, drives routing
    route: Literal["immediate", "digest"]   # rule table decides, never Claude
    text: str                # deterministic one-line rule-only description
    evidence: Mapping[str, str | int | float | bool]   # facts: days_stale=14, age_days=31
```

**Key stability is the most load-bearing decision.** `key` must contain only identity, never volatile values. No line numbers, no counts, no ages, no Claude text, no severity. Severity lives on the record so a change is detected as ESCALATED rather than as a new finding. If Guard has no stable finding id, the adapter must derive a fingerprint from stable attributes (category + normalized location/title) and that choice needs real data (research flag).

### State (what the radar remembers)

```python
class AlertMark(BaseModel, frozen=True):
    alerted_at: datetime
    alerted_severity: Severity
    last_reminder_at: datetime | None
    send_count: int

class FindingRecord(BaseModel, frozen=True):
    key: str
    project_id: str; rule: str; subject: str
    status: Literal["open", "resolved"]
    severity: Severity                  # latest
    first_seen_at: datetime             # of the current episode
    last_seen_at: datetime
    resolved_at: datetime | None
    episodes: int                       # times it has been opened (recurring signal)
    runs_seen: tuple[str, ...]          # distinct Guard run_ids, bounded (e.g. last 20)
    alerted: AlertMark | None
    resolved_noted: bool                # "resolved" note already sent for this episode

class ProjectMemory(BaseModel, frozen=True):
    project_id: str
    last_run_id: str | None
    last_signals: Signals | None        # for "worsening" comparisons
    last_ok_at: datetime | None

class RadarHealth(BaseModel, frozen=True):
    guard_consecutive_failures: int
    health_alerted: bool

class State(BaseModel, frozen=True):
    schema_version: int
    findings: Mapping[str, FindingRecord]
    projects: Mapping[str, ProjectMemory]
    last_digest_at: datetime | None
    health: RadarHealth
```

Why `runs_seen` stores **Guard run ids, not radar poll timestamps**: the radar may poll hourly while Guard runs weekly. "Same finding keeps coming back across runs" must count distinct Guard runs, otherwise 24 polls of one run would look like 24 recurrences. Reconcile adds a run id to a set, which also makes reconcile idempotent under replay.

### Previous-vs-current diff and aging

No full previous snapshot needs persisting. The ledger **is** the compact previous state. Reconcile is a pure function of `(State, Snapshot, findings, now)`:

| Change | Condition | Effect |
|--------|-----------|--------|
| NEW | key not in ledger | create record, `first_seen=now` (or Guard's own date if provided), `episodes=1` |
| PERSISTING | key open in ledger and present now | `last_seen=now`, add run id; age = `now - first_seen` |
| ESCALATED | present, severity > `alerted.alerted_severity` | eligible for re-alert (this is how "new or worsening security" works) |
| REOPENED | key resolved in ledger, present now | `episodes += 1`, new `first_seen`, clear `alerted`; feeds the "recurring" rule |
| RESOLVED | key open in ledger, **project fetched OK this run**, absent now | `status=resolved`, `resolved_at=now`; emit "resolved" note only if it was alerted |

Rules:

- **Absence of data is not resolution.** If a project's fetch failed, reconcile skips it entirely. Treating a Guard outage as "everything fixed" then "everything is new again" is the classic alert-storm bug.
- **Age prefers Guard's own first-detected date.** Radar's `first_seen` only measures since the radar started watching and understates age. Use Guard's date when available and flag in the message evidence which source was used.
- **Two kinds of rules, one signature.** Snapshot rules (stale runs, harness, AI readiness, tests) need only the current project. History rules (recurring, aged high-risk, worsening security) also read a read-only `HistoryView` built from the ledger and `ProjectMemory`. Rules never read files; they receive history as an argument, so they stay table-testable.
- **Recurring needs a definition from the operator.** Two readings exist: (a) re-opened after being fixed (`episodes >= N` within a window), (b) present across N consecutive Guard runs (that is "unaddressed", not "recurring"). Recommend (a) for `recurring_findings`, and (b) is covered by `unaddressed_high_risk`. ASSUMPTION: confirm with operator.
- **Prune** resolved records after a retention window (e.g. 90 days) so the file stays small, but keep them long enough to cover the recurring window.

### Triage model

```python
class TriageItem(BaseModel, frozen=True, extra="forbid"):
    finding_key: str
    summary: Annotated[str, StringConstraints(max_length=240)]
    probable_cause: Annotated[str, StringConstraints(max_length=200)] | None
    priority: Literal["p1", "p2", "p3"]

class TriageResult(BaseModel, frozen=True, extra="forbid"):
    items: tuple[TriageItem, ...]
```

Beyond schema validity, `triage.validate_against(request_keys, result)` enforces: every `finding_key` is in the request; no duplicates; strings contain no markup after escaping. **Priority only orders items inside a message.** It cannot create, suppress, or re-route an alert. Items Claude omitted get rule-only text individually (per-item fallback, not all-or-nothing).

## Architectural Patterns

### Pattern 1: Functional core, imperative shell with ports

**What:** All decisions (`detect`, `reconcile`, `plan`, `render`, `apply_delivery`) are pure functions over immutable models. `pipeline.run_once` is the only place that sequences I/O.
**When to use:** Always here. It is required by `AGENTS.md` and is what makes "no false alarms" testable: every alert decision is a table-test row.
**Trade-offs:** A little more plumbing (models, Protocols). In exchange, every failure mode below is testable with scripted fakes and no network.

```python
class Notifier(Protocol):
    def send(self, msg: OutboundMessage) -> SendResult: ...

@dataclass(frozen=True)
class Deps:
    guard: GuardSource
    triager: Triager | None
    notifier: Notifier
    store: StateStore
    clock: Clock
    config: RadarConfig

def run_once(deps: Deps) -> RunReport:
    with deps.store.lock():                       # flock: no overlapping runs
        state = deps.store.load()
        now = deps.clock.now()
        snap = deps.guard.fetch_snapshot()        # per-project Ok | Failed
        findings = detect(snap, state, deps.config, now)
        state, changes = reconcile(state, snap, findings, now)
        deps.store.save(state)                    # observations are idempotent: safe pre-send
        plan = plan_messages(state, changes, deps.config, now)
        triage = try_triage(deps.triager, plan)   # None on any failure -> rule-only
        for msg in render(plan, triage):          # disjoint finding sets per chunk
            result = deps.notifier.send(msg)
            if isinstance(result, Delivered):
                state = apply_delivery(state, msg, now)
                deps.store.save(state)            # commit point, once per acknowledged message
            else:
                log_send_failure(msg, result)
        return build_report(...)
```

### Pattern 2: Ledger as outbox (derived delivery)

**What:** "Needs sending" is *derived* from state (`finding open AND alerted is None`, or reminder due), never stored as a queue. `alerted` is written only by `apply_delivery` after an acknowledged send.
**When to use:** Low-volume alerting with a non-idempotent sink (Telegram has no idempotency key; the Bot API docs describe none for sends).
**Trade-offs:** No new files, replay is automatic, and a failed send retries on the next run with zero extra code. The cost is one narrow duplicate window, covered in the crash matrix.

### Pattern 3: One commit unit per outbound message

**What:** Each `OutboundMessage` carries the exact set of finding keys it reports. Chunks of one logical alert have disjoint key sets. Delivery of chunk 1 commits only chunk 1's keys.
**When to use:** Any time a message may be split (Telegram caps `text` at 1-4096 characters after entities parsing) or one of several sends may fail.
**Trade-offs:** If chunk 2 fails, chunk 1's findings are already marked and chunk 2's retry next run. Nothing lost, nothing doubled.

### Pattern 4: Atomic state file with generation backup

**What:** Write temp file in the same directory, `flush` + `os.fsync(file)`, `os.replace(tmp, path)`, then `fsync` the directory. Before replacing, keep the previous good file as `state.json.bak` (hard link or copy). Single JSON document, `schema_version` field, Pydantic-validated on load.
**When to use:** Always for the state file. At this scale (tens of projects, hundreds of findings) one file under 1 MB, rewritten whole, is simpler and safer than multiple files that can disagree.
**Trade-offs:** Whole-file rewrite is fine for this size. Multi-file layouts would need cross-file atomicity that POSIX does not give.

```python
def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)                       # atomic on POSIX, same filesystem
    dfd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(dfd)
    finally: os.close(dfd)
```

**Load policy (important):**

| Situation | Behaviour | Why |
|-----------|-----------|-----|
| File missing | First run: **baseline mode** (see Pitfalls). Do not send N immediate alerts for pre-existing findings | A cold start otherwise floods the executive group |
| File corrupt / fails validation | Try `.bak`; if that fails too, **abort the run non-zero without sending** | Silently resetting to empty state re-alerts everything |
| `schema_version` newer than code | Abort | Never downgrade-write |
| `schema_version` older | Migrate in pure function, write back | Forward-only migrations |

### Pattern 5: Single-flight lock

**What:** `fcntl.flock` (exclusive, non-blocking) on `state/.lock` for the whole run. A second invocation logs and exits 0.
**Why:** Overlapping runs (slow Guard plus short timer interval) are a far likelier source of double alerts than crashes. The lock is released by the OS when the process dies, so it never wedges.

### Pattern 6: Result types for I/O, not exceptions across the boundary

**What:** Adapters return values the core can branch on: `ProjectFetch(snapshot|error)`, `SendResult = Delivered | RetryLater(after) | Permanent(reason) | InDoubt`, `TriageResult | None`. Unexpected exceptions are caught at the adapter boundary and mapped, with secrets scrubbed.
**Trade-offs:** More types, but the failure matrix below becomes ordinary table tests.

### Pattern 7: Dry-run as dependency substitution, not an `if`

**What:** `--dry-run` swaps two adapters and nothing else: `Notifier -> PrintNotifier` (renders exactly what would be sent, to stdout) and `StateStore -> ReadOnlyStore` (`load` real, `save` logs "would save diff"). Same `run_once`, same code path, so dry-run output is by construction what the live run would send.
**Flags:**

| Flag | Effect |
|------|--------|
| `--dry-run` | Real Guard fetch (or fixtures), real rules and plan, PrintNotifier, no state writes |
| `--fixtures DIR` | Use `FixtureGuardSource` instead of MCP (offline dev, demos, replay of a bad day) |
| `--no-triage` | Force rule-only; dry-run default, so a dry-run costs nothing |
| `--triage` | Opt in to call Claude during dry-run to preview triaged text |
| `--now ISO8601` | Fixed clock for reproducing a scenario |
| `--state-dir DIR` | Point at a copy of production state for "what would happen tomorrow" |

Dry-run must still take the lock in shared mode or refuse to run while a live run holds it, so it never reads a half-written state.

### Pattern 8: Record real Guard data first (`probe`)

**What:** Because Guard's tools and shapes are unknown, ship a `probe` command early: list MCP tools, call the read tools, dump sanitized raw responses to `tests/fixtures/guard/`. The normalizer and rules are then written against recorded payloads, and `FixtureGuardSource` replays them.
**Why:** The PROJECT.md constraint says rules must be drafted against real Guard data, not guessed. A recorder turns that into a repeatable, reviewable artifact instead of a one-off manual copy.

## Data Flow

### Run Flow

```
timer ─► run_once
          │ lock + load State
          ▼
   GuardSource.fetch_snapshot ──► Snapshot (per-project Ok|Failed)
          ▼
   detect(rules)  ──► Findings (stable keys, severity, route, evidence)
          ▼
   reconcile(State, Snapshot, Findings)  ──► State' + Changes     ─► save State' (idempotent)
          ▼
   plan(State', Changes, config, now)  ──► Plan{immediate, reminders, resolved, digest?, health?}
          ▼           (empty plan ─► log "quiet run", exit 0; Claude NOT called)
   Triager.triage(Plan items + snapshot slice) ──► TriageResult | None
          ▼
   validate + merge  (per-item fallback to rule text)
          ▼
   render ──► [OutboundMessage{text, finding_keys}]  (<=4096 chars, redacted, worst-first)
          ▼
   for each msg:  Notifier.send ──Delivered──► apply_delivery ─► save State''
                              └─otherwise───► log, no state change (retry next run)
          ▼
   RunReport ─► exit code
```

### Failure Matrix

| Failure | Detection | Behaviour | Lost alert? | Double alert? |
|---------|-----------|-----------|-------------|---------------|
| Guard unreachable (all projects) | Every `ProjectFetch` failed | Skip reconcile/plan for them, state untouched, `guard_consecutive_failures += 1`. At N (default 3) emit **one** radar-health message, dedup'd like any finding; a recovery note when it clears | No: next successful run catches up | No |
| Guard partially down | Some `ProjectFetch` failed | Process OK projects normally; failed projects frozen (no resolve, no new). Log which | No | No |
| Guard returns data that fails normalization | Pydantic error in adapter | Treat as that project's `Failed` with sanitized error | No | No |
| Claude timeout / non-zero exit | Adapter returns `None` | Rule-only text for all items | No | No |
| Claude invalid JSON / schema fail | Pydantic `ValidationError` | One bounded retry, then rule-only | No | No |
| Claude references unknown finding key / duplicates | `validate_against` | Drop bad items; affected findings use rule text | No | No |
| Claude output contains markup or oversize text | Escaping and caps in `render` | Truncate, escape | No | No |
| Telegram 429 | `parameters.retry_after` in error response | If `retry_after` <= small bound (e.g. 30 s) sleep once and retry; else `RetryLater`, no state change | No: retried next run | No |
| Telegram 5xx / network error before request is sent | Connection error | `RetryLater` | No | No |
| Telegram timeout **after** request sent | Read timeout | `InDoubt`: treat as not delivered, log WARN `possible duplicate on retry` | No | **Possible** (unavoidable, see below) |
| Telegram 400 parse error | `ok:false`, 400 | Re-send once as plain text; never loop | No | No |
| Telegram 401/403/chat not found | `Permanent` | Abort run non-zero, loud log; state unchanged | No: fix config, next run sends | No |
| One chunk fails, others succeed | Per-message result | Only delivered chunks committed | No | No |
| State file corrupt | Load validation | `.bak` fallback, else abort without sending | No | No |
| Two runs overlap | `flock` | Second exits | No | No |

### Crash and Replay Safety

Telegram offers no idempotency key and the bot cannot query its own sent messages, so **exactly-once is impossible** and the design must choose a side. Choose **at-least-once**: a duplicate message is a mild annoyance; a lost high-risk alert defeats the project's core value. Duplicate exposure is narrowed to one window.

| Crash point | State on disk | Next run | Outcome |
|-------------|---------------|----------|---------|
| Before fetch / during fetch | Unchanged | Same as normal | Fine |
| After reconcile save, before any send | Observations saved, `alerted` unset | Reconcile is idempotent (run ids are a set); plan identical | Alert sent next run. No loss |
| During Claude call | As above | As above | Fine |
| After send acknowledged, before `apply_delivery` save | `alerted` unset | Plan re-emits the same alert | **One duplicate** (only window). Logged as WARN |
| After commit save | `alerted` set | Dedup suppresses | Fine |
| Mid-write of state file | Old file intact (atomic replace) | Loads old file | Equivalent to a crash before the commit |

Keep the duplicate window small: no work between `send` returning and `save` (no logging that can raise, no Claude, no second send). Optional hardening, deliberately **not** in v1: a pre-send `in_flight` intent marker, which would only turn "possible duplicate" into "possible loss", the worse trade.

Weekly digest follows the same rule: `last_digest_at` is written only after the digest is acknowledged. The digest is "due" if `now >= next_scheduled_slot(last_digest_at)`, so a missed week is sent at the next run instead of skipped.

## Key Data Flows

1. **New high-risk finding:** rule emits `Finding(route="immediate")` -> reconcile marks NEW -> planner puts it in `immediate` (no `alerted` mark) -> triage explains -> sent -> `AlertMark` committed -> later runs classify PERSISTING and the planner suppresses until the reminder interval.
2. **Aged / unaddressed:** rule reads `HistoryView` (age from Guard date or ledger) -> emits finding with `age_days` evidence -> planner decides immediate vs reminder vs digest-only using the rule's route and `last_reminder_at`.
3. **Escalation:** security finding present with higher severity than `alerted_severity` -> ESCALATED -> eligible again, sent once, `alerted_severity` raised.
4. **Resolution:** open record absent from an OK project -> RESOLVED -> short "resolved" note only if it had been alerted and `resolved_noted` is false.
5. **Weekly digest:** planner checks `last_digest_at` against schedule -> collects all open records sorted by severity then age descending -> render truncates to fit with "+N more" -> one message. An empty digest is a one-line all-clear (see Pitfalls on the dead-man's switch).

## Integration Points

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| Guard MCP | Deterministic client calls in `GuardMcpSource`, tool names from `probe` | URL/command/auth unknown: **ASSUMPTION**, blocks the real adapter. Core and rules can be built against fakes. Credentials from env, never logged |
| Claude | `ClaudeCliTriager` shelling out to `claude -p`, behind `Triager` | Documented flags (verified in official CLI reference): `-p`, `--output-format json`, `--json-schema`, `--model`, `--max-turns`, `--max-budget-usd`, `--no-session-persistence`, `--tools ""` (disable all tools), `--bare` (skips MCP servers, hooks, CLAUDE.md, auto memory). Use tools disabled and no MCP so Claude cannot reach Guard or anything else. **ASSUMPTION:** how `--bare` authenticates in the operator's setup, and whether `claude -p` vs the Agent SDK is chosen (design v0 open question). The port makes this swappable without touching core |
| Telegram | `POST https://api.telegram.org/bot<TOKEN>/sendMessage`, `chat_id`, `text` (1-4096 chars after entities parsing), optional `parse_mode`, `disable_notification` | Limits from FAQ: about 1 msg/s per chat, **20 msgs/min per group**, 429 returns `parameters.retry_after`. Token is in the URL path: see secrets pitfall |
| Scheduler | systemd timer or cron running the one-shot CLI | Not in code; ship a sample unit/crontab in `docs/`. Exit code non-zero on any failed send or permanent error |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| Core <-> adapters | `Protocol` ports, frozen models in and out | Core imports only `ports`/`model`; test enforces |
| Rules <-> state | `HistoryView` argument, read-only | Rules never touch the store |
| Planner <-> Claude | None: planner output is input to triage; triage output is only used in `render` | Claude cannot affect dedup keys, routing, or whether to send |
| Pipeline <-> store | `load` once, `save` after reconcile and after each delivery | Two commit kinds: observation (idempotent) and delivery (acknowledged) |
| Message <-> state | `OutboundMessage.finding_keys` | The only link between "what was sent" and "what to mark" |

## Anti-Patterns

### Anti-Pattern 1: Marking alerted before the send is acknowledged
**What people do:** Update dedup state, then send.
**Why it's wrong:** Any send failure becomes a permanently lost alert, the worst outcome here.
**Do this instead:** Commit `alerted` only in `apply_delivery` after `Delivered`.

### Anti-Pattern 2: Treating missing data as "resolved" or "empty"
**What people do:** A failed fetch yields an empty finding list, and reconcile resolves everything.
**Why it's wrong:** Recovery then re-alerts every finding as NEW.
**Do this instead:** `ProjectFetch` is `Ok | Failed`; reconcile freezes failed projects.

### Anti-Pattern 3: Claude in the decision path
**What people do:** Ask the LLM "is this worth alerting?" or build dedup keys from its summary.
**Why it's wrong:** Non-determinism produces false alarms and duplicates; violates `AGENTS.md`.
**Do this instead:** Rules and planner decide; Claude output is cosmetic and only consumed in `render`.

### Anti-Pattern 4: Volatile fields in the dedup key
**What people do:** Key on title text, line number, or severity.
**Why it's wrong:** A cosmetic change in Guard output re-alerts every finding.
**Do this instead:** Key on stable identity only; track severity on the record.

### Anti-Pattern 5: Hidden clock and I/O in "pure" functions
**What people do:** `datetime.now()` or env reads inside rules.
**Why it's wrong:** Untestable, flaky age/threshold boundaries.
**Do this instead:** Inject `now` and `Thresholds`; add boundary rows (exactly N days, N-1, N+1) to every table.

### Anti-Pattern 6: Long-running daemon with in-memory state
**What people do:** `while True: sleep()` with a cache.
**Why it's wrong:** Restart loses state, scheduler libraries add failure modes, drift.
**Do this instead:** One-shot process plus external timer plus file state.

### Anti-Pattern 7: Logging or alerting anything containing the Telegram URL
**What people do:** Log request URLs or `repr(exception)` from an HTTP client.
**Why it's wrong:** The bot token is part of the URL path, so a default INFO-level HTTP client log line or a traceback leaks it into logs.
**Do this instead:** Silence HTTP client loggers (set to WARNING), wrap adapter errors into token-free messages, and run a `redact()` pass over all log and outbound text for every known secret value. Test it by asserting the token never appears in captured logs or sent text.

### Anti-Pattern 8: Unvalidated Guard strings straight into the Claude prompt and message
**What people do:** Paste finding titles and evidence into prompts and Telegram text.
**Why it's wrong:** Guard data derives from repo content, so it is attacker-influenced input (prompt injection, markup injection).
**Do this instead:** Tools disabled and no MCP for the Claude call, strict output schema, length caps, escaping at render. Treat Guard text as data.

## Scaling Considerations

The real scaling axis is number of projects and findings, not users.

| Scale | Architecture Adjustments |
|-------|--------------------------|
| 1-50 projects, hundreds of findings | Exactly the design above. Single JSON under 1 MB, sequential fetch, one message per run |
| 50-500 projects | Fetch projects concurrently inside the adapter (bounded), keep core sync. Add per-project triage batching so one Claude call covers several projects. Digest truncation ("+N more") becomes mandatory |
| 500+ projects | Revisit "no database" (SQLite would still be file-based and transactional). Out of scope for v0 |

### Scaling Priorities

1. **First bottleneck:** Telegram message size and rate (4096 chars, 20 msgs/min per group), not compute. Coalesce to one message per run, chunk the digest, sleep >= 1 s between chunks.
2. **Second bottleneck:** Guard fetch latency with many projects. Bounded concurrency in the adapter; per-project failure isolation already exists.

## Suggested Build Order

Each phase ends in a runnable end-to-end slice (`make verify` green plus a demonstrable command). The ordering is driven by one constraint: **Guard access does not exist yet**, so everything that does not need it is built first against fakes, and the real adapter is isolated as one gated phase.

| # | Phase (vertical slice) | Delivers end-to-end | Depends on |
|---|------------------------|---------------------|------------|
| 0 | **Unblock Guard access (non-code, runs in parallel)** | Operator supplies MCP URL/command/auth, Telegram token and chat id. Result: ability to run `probe` | Operator |
| 1 | **Walking skeleton, dry-run** | `model.py`, `ports.py`, fakes, `FixtureGuardSource`, **stale-runs rule only** (needs just last-run timestamp, the most robust assumption), `render`, in-memory state, `PrintNotifier`, CLI `run --dry-run --fixtures`. Table tests incl. threshold boundaries and per-project override. Proves the architecture and import-direction test | None |
| 2 | **Durable state, dedup, real Telegram** | `FileStateStore` (atomic write, `.bak`, lock, schema version, corrupt handling), `ledger.reconcile`, `planner`, `apply_delivery`, `TelegramNotifier` with outcome classification, redaction, chunking. **Crash-injection tests** (scripted `FakeNotifier` outcomes plus a store that raises between send and save). Baseline mode for first run. Result: stale-run alerts reach a real group once and are not re-sent | 1; Telegram token |
| 3 | **Real Guard adapter + unattended schedule** | `probe` and fixture recorder, `GuardMcpSource` plus normalizer, per-project failure isolation, radar-health finding, `.env` loading, sample systemd timer/cron, exit codes, structured logs. Result: it runs on a schedule against real data | 2; **Guard access** |
| 4 | **Remaining rules, highest value first** | Each rule is its own slice with table tests and a fixture: unaddressed high-risk (with age) -> security (new/worsening, uses `ProjectMemory`) -> harness + `GUARD.md` -> AI readiness -> tests -> recurring (uses `episodes`). Rules are independent and can be parallel tasks. Rule definitions are finalized from real data | 3 |
| 5 | **Digest, reminders, quiet hours, resolved notes** | `planner` additions: weekly digest (worst first, age, truncation, all-clear one-liner), reminder interval, optional quiet-hours deferral, resolved note | 2 (ledger), 4 (content worth summarizing) |
| 6 | **Claude triage** | `Triager` port, `ClaudeCliTriager`, `--json-schema`, `validate_against`, per-item fallback, retry-once, injection hardening, `--max-turns`/`--max-budget-usd` caps. Pure addition: the rule-only message is already the shipped product, so triage failure can never regress it | Phase 2 minimum (needs `plan` and `render`); can run in parallel with 4/5 |

**Ordering rationale:**

- Phase 1 before everything: it fixes the seams that every later task (and every AI-implemented PR) must respect.
- Durable state and Telegram (Phase 2) come **before** the real Guard adapter because they are the highest-risk design (crash safety, no double/lost alerts) and do not need Guard. Stale-runs is demonstrable from fixtures.
- Claude is last because the rule-only path is the fallback and the baseline; adding triage later cannot change decisions, only text. If time is short, v1 ships valuable without it.
- The ledger is built in Phase 2 even though only one rule exists, because retrofitting `first_seen`/`episodes` after alerts are live loses history and risks a re-alert flood.

**Research flags for phases:**

- Phase 3: **needs deeper research** (Guard MCP tool names, auth, payload shape, stable finding id, run id, date fields). Everything in `model.py` marked ASSUMPTION is validated here; expect `GuardFinding`/`Signals` to change.
- Phase 4: **needs per-rule research** against real data (what "harness metric" is, scales and thresholds for AI readiness and tests, the meaning of "recurring").
- Phase 6: **light research** (Claude CLI auth under `--bare`, `claude -p` vs Agent SDK, exact `--json-schema` result envelope in `--output-format json`).
- Phases 1, 2, 5: standard patterns, unlikely to need research.

## Open Questions for the Operator (affect architecture)

1. **Cold start behaviour.** Recommend: first run with empty state seeds the ledger and sends a single "radar online, N open items" digest instead of N immediate alerts.
2. **Heartbeat.** PROJECT.md says silence means healthy, but a dead radar is also silent. Recommend the weekly digest always sends (one-line all-clear when nothing is open) as the dead-man's switch. Plus `state/meta last_success_at` for an external check later.
3. **Where radar-health alerts go.** The exec group should not see "Guard unreachable" noise. Recommend a separate optional `TELEGRAM_OPS_CHAT_ID`, falling back to the main group only after N consecutive failures.
4. **Reminder channel.** Recommend: reminders for high-risk items ride the next run's message at the set interval (default 7 days); everything else is only in the weekly digest.
5. **Quiet hours.** If adopted, defer (never drop): dedup state is untouched, so the alert is naturally planned again after the window.

## Sources

- Telegram Bot API, `sendMessage` (text 1-4096 characters after entities parsing, `parse_mode`, `disable_notification`) and `ResponseParameters.retry_after`: https://core.telegram.org/bots/api#sendmessage (HIGH, fetched)
- Telegram Bot FAQ, rate limits (about 1 msg/s per chat, 20 msgs/min per group, 429 on excess): https://core.telegram.org/bots/faq (HIGH, fetched)
- Telegram Bot API, error format (`ok:false`, `error_code`, optional `parameters`) and absence of any send idempotency mechanism: https://core.telegram.org/bots/api (HIGH, fetched)
- Claude Code CLI reference (`-p`, `--output-format`, `--json-schema`, `--tools`, `--bare`, `--max-turns`, `--max-budget-usd`, `--no-session-persistence`): https://code.claude.com/docs/en/cli-reference (HIGH, fetched)
- Functional core / imperative shell, ports and adapters, outbox/at-least-once delivery, write-temp-fsync-rename atomic file replacement, `flock` single-flight: established engineering patterns, synthesized from experience (MEDIUM, not tied to a single fetched source)
- Project context: `.planning/PROJECT.md`, `docs/design/v0.md`, `AGENTS.md`

---
*Architecture research for: scheduled Guard-to-Telegram monitoring pipeline*
*Researched: 2026-11-16*
