# Deploying the radar unattended

Operator runbook: run the radar against real Guard on a schedule. The code is verified
offline; every step below that touches the host, Guard or Telegram is done by the operator.
Design background: `docs/design/v0.md`; what Guard returns: `docs/guard-data-notes.md`.

There are two ways to run it:

- **Docker Compose (primary).** One container runs `enji-guard-radar serve`, which polls on the
  schedule in the radar config (`schedule.every_minutes`). The schedule lives in git with the
  config; any host with Docker runs it the same way.
- **systemd or cron (no Docker).** A timer or cron job starts `enji-guard-radar run` once per
  slot. All sections after "Docker Compose" below describe this path.

Do not run both on one state: the state lock keeps two polls from overlapping, but the radar
would poll twice per slot.

## Docker Compose

Needs Docker Engine with Compose v2 and network access to build the image (uv installs the
locked dependencies) and at run time (Guard, Telegram).

1. Make Docker start at boot. A container with `restart: unless-stopped` comes back after a
   reboot only if the Docker service itself starts:

   ```
   sudo systemctl enable --now docker
   ```

   A container stopped with `docker compose stop` or `down` stays stopped after a reboot.

2. Get the code and the config. The config is part of your checkout and goes into git:

   ```
   git clone <repository-url> enji-guard-radar && cd enji-guard-radar
   cp config/radar.example.yaml config/radar.yaml
   ```

   Edit `config/radar.yaml`. The schedule is `schedule.every_minutes` (default 180): polls run at
   multiples of it counted from 00:00 UTC (180 gives 00:00, 03:00, 06:00, ...); the number must
   divide 1440. Check `ops.failures_before_alert` and `messages.language` too. The file holds no
   secrets.

3. Create `.env` next to `compose.yaml` (it is git-ignored and mounted read-only into the
   container as `/run/radar.env`):

   ```
   cp .env.example .env
   ```

   Fill `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (a private test chat first) and, if you use
   it, `TELEGRAM_OPS_CHAT_ID`. The container runs as uid 10001, so the file must be readable by
   it, and also by you: `docker compose` itself reads `.env` from the project folder, so a file
   your user cannot read fails every compose command with `open .../.env: permission denied`:

   ```
   sudo chown "$USER":10001 .env && sudo chmod 0640 .env
   ```

   Tried on a real host on 2026-11-24 (`root:10001` failed for the reason above).

4. Build the image:

   ```
   docker compose build
   ```

5. Log in to Guard once. The login lands in the state volume (`/state/guard-auth.json`, mode
   0600) and every refresh rotates it there:

   ```
   docker compose run --rm radar auth --state-dir /state
   ```

   Open the printed address in a browser, approve, then paste the redirect address into the
   hidden prompt.

6. Run `doctor` before the first start. Every line must be PASS (SKIP is fine for `timer` and,
   while triage is off, for `triage`); the `schedule` line shows the interval and the next slot:

   ```
   docker compose run --rm radar doctor --config /config/radar.yaml --state-dir /state --env-file /run/radar.env
   ```

7. Start it and watch the log. This step is required: `docker compose run --rm` (used for
   `auth` and `doctor` above) starts a one-off container that is removed when it exits, so no
   service exists and `restart: unless-stopped` has nothing to restart until `up -d` creates it:

   ```
   docker compose up -d
   docker compose ps          # radar: Up
   docker compose logs -f radar
   ```

   Expect `serve: started ...`, then for each run `serve: run start slot=... reason=... next_slot=...`
   and `serve: run finish exit=... duration=...s next_slot=...`. `exit` is 0 for a clean poll
   and 1 for a poll that needs attention (for example Guard unavailable); the loop continues
   either way. A run that takes longer than a slot logs a warning with the skipped slots; they
   are not made up.

### Day to day

- **Update:** `git pull && docker compose up -d --build`.
- **Change the schedule or any config:** edit `config/radar.yaml`, then
  `docker compose restart radar`. Every run re-reads the config, except `schedule`, which is read
  once when `serve` starts, so a schedule change needs the restart.
- **Stop:** `docker compose stop radar`. It lets a running poll finish (up to two minutes);
  state is written atomically, so even a forced stop leaves a valid state file.
- **Run by hand while `serve` runs:** `docker compose run --rm radar run --config /config/radar.yaml --state-dir /state --env-file /run/radar.env`
  is safe: the state lock makes the second of two overlapping polls exit as "locked". The
  `/state` volume is shared. Add `--dry-run` to preview without sending.
- **Catch-up:** when `serve` starts and no poll has finished since the latest slot (the
  container or host was down), it polls once at once, then continues on the slot grid. "Last
  poll" is the modification time of `state.json`, which every finished poll rewrites. A fresh
  state volume polls once at start; that first poll only records the current findings and sends
  no alerts. Copying a state directory without keeping its modification times can cause one
  extra or one skipped catch-up poll.
- **State:** the named volume `radar-state` holds `state.json`, its `.bak`, `radar.lock`,
  `guard-auth.json` and the triage cost file. Look at it with
  `docker compose run --rm --entrypoint ls radar -la /state`. To use a host directory instead,
  follow the comment in `compose.yaml` (the directory must be writable by uid 10001). Back up
  the state by copying the volume's files; never copy `guard-auth.json` elsewhere or commit it.
- **Re-login:** when the ops chat says the Guard login expired, run the `auth` command from step
  5 again, then `docker compose restart radar`.
- **Triage:** the image has no `claude` CLI, so keep `triage.enabled: false` in Docker; with it
  on, `doctor` reports `claude not found on PATH`.

## Install (no Docker)

The rest of this runbook is the systemd/cron path for hosts without Docker. It uses
`enji-guard-radar run` started by a timer, so the schedule is the timer's, not
`schedule.every_minutes` (that setting only drives `serve`; keep the timer interval and the
digest window in step by hand).

The files in `deploy/` assume a checkout at `/opt/enji-guard-radar`, a system user
`enji-guard-radar`, config and secrets in `/etc/enji-guard-radar/` and state in
`/var/lib/enji-guard-radar`. If your host differs, change those paths in the unit files, in
`deploy/cron.example` and in the commands below, keeping them consistent: `--state-dir` must
equal the unit's `StateDirectory`.

```
sudo useradd --system --home-dir /opt/enji-guard-radar --shell /usr/sbin/nologin enji-guard-radar
sudo git clone <repository-url> /opt/enji-guard-radar
cd /opt/enji-guard-radar
sudo uv sync --frozen --no-dev
sudo chown -R root:root /opt/enji-guard-radar
```

`--frozen` installs exactly what the reviewed `uv.lock` pins. The timer runs the installed
entry point `/opt/enji-guard-radar/.venv/bin/enji-guard-radar`, never `uv run`, so nothing is
resolved or downloaded at run time.

## Configuration and secrets

1. Copy the example config and edit it:
   `sudo install -D -m 0644 config/radar.example.yaml /etc/enji-guard-radar/radar.yaml`.
   It holds no secrets. Check `ops.failures_before_alert` (default 3). To get every Telegram
   message (group alerts, reminders, digest and the ops chat) in Russian, set
   `messages.language: ru` (default `en`); logs, `doctor` and CLI errors stay English, and
   changing the language re-sends nothing.
2. Create `/etc/enji-guard-radar/radar.env`, plain `KEY=VALUE` lines, mode 0600, owned by the
   service user (`sudo install -m 0600 -o enji-guard-radar -g enji-guard-radar /dev/null /etc/enji-guard-radar/radar.env`):

   ```
   TELEGRAM_BOT_TOKEN=<bot token>
   TELEGRAM_CHAT_ID=<a private test chat first>
   TELEGRAM_OPS_CHAT_ID=<ops chat>
   ```

3. Do not put any Guard token in `radar.env`. The unit files have no `EnvironmentFile` and no
   secret: the radar reads `radar.env` itself through `--env-file`.

Tokens, codes and the pasted redirect address never go into chat, tickets, shell history or
logs.

## Guard login (auth)

The radar logs in to Guard with its own login, once, interactively:

```
sudo -u enji-guard-radar /opt/enji-guard-radar/.venv/bin/enji-guard-radar auth --state-dir /var/lib/enji-guard-radar
```

Open the printed address in a browser, approve, then paste the redirect address into the
hidden prompt. Record only the non-secret line the command prints. The login lands in
`/var/lib/enji-guard-radar/guard-auth.json` (mode 0600, inside the 0700 state directory) and
every refresh rotates it there. This file, not `radar.env`, holds the rotating refresh token:
it changes on every refresh, so it cannot live in an env file (decision D-07 and the one
exception AGENTS.md names to "secrets come from env"). Never copy it, never commit it.

## Doctor

```
sudo -u enji-guard-radar /opt/enji-guard-radar/.venv/bin/enji-guard-radar doctor --state-dir /var/lib/enji-guard-radar --env-file /etc/enji-guard-radar/radar.env
```

Every line must be PASS (SKIP is fine for the timer hint, and for the `triage` line while triage is
off). Fix any FAIL before going on. Add `--config /etc/enji-guard-radar/radar.yaml` to see the
config-based lines for your real config, the `triage` line included.

## Probe and fixture review

Record what real Guard returns into a scratch directory, never straight into `fixtures/guard`:

```
sudo -u enji-guard-radar /opt/enji-guard-radar/.venv/bin/enji-guard-radar probe --state-dir /var/lib/enji-guard-radar --config /etc/enji-guard-radar/radar.yaml --out /tmp/guard-probe
```

`--out` must be a new or empty directory (the default is `fixtures/guard-real`). Review the
sanitised files and diff them against `fixtures/guard` by hand, then bring anything worth
keeping in through a branch and a normal review. Nothing is added to git automatically.

## Dry-run review and poll cost

```
sudo -u enji-guard-radar /opt/enji-guard-radar/.venv/bin/enji-guard-radar run --dry-run --config /etc/enji-guard-radar/radar.yaml --state-dir /var/lib/enji-guard-radar --env-file /etc/enji-guard-radar/radar.env
```

This reads real Guard, prints what would be sent and writes no `state.json`. It does keep the
Guard login current: a rotated refresh token is saved to `guard-auth.json` even in a dry-run.
The last line is the poll cost, for example
`poll cost: 25 calls (get_audit_report 11, list_audit_runs 6, list_projects 1, list_repositories 7), 0 failed, 41.2 s, 8 projects, 12 repositories`.
Every real run logs the same line to the journal. Use it to settle the cadence: the default is
every 3 hours; the run must finish well inside `TimeoutStartSec=15min`.

## Live test-chat check

With `TELEGRAM_CHAT_ID` pointing at the private test chat:

1. Run once without `--dry-run`: it baselines current conditions and sends nothing.
2. Copy the config to a scratch file with a lowered threshold (for example a small
   `stale_after_days` on one project), run again with `--config <scratch>`: one real alert
   arrives in the test chat.
3. Run the same command again: nothing is sent (the alert is already recorded).

## Timer

```
sudo install -m 0644 deploy/systemd/enji-guard-radar.service deploy/systemd/enji-guard-radar.timer /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/enji-guard-radar.service /etc/systemd/system/enji-guard-radar.timer
systemd-analyze calendar '*-*-* 00/3:00:00'
sudo systemctl daemon-reload
sudo systemctl enable --now enji-guard-radar.timer
systemctl list-timers enji-guard-radar.timer
journalctl -u enji-guard-radar
```

`systemd-analyze calendar` checks the `OnCalendar` syntax, which was not verified when the
file was written: it must print the next elapse times. The service is `Type=oneshot` with
`TimeoutStartSec=15min`: a oneshot unit has no start timeout by default and `RuntimeMaxSec`
does not apply to it, so without it a hung run would stay active forever and the timer would
never start another. `Persistent=true` runs a missed slot once after the host was off. Exit
code 1 (a failed poll or an undelivered message) shows the unit as failed in
`systemctl --failed`; that is intended.

## Switching to the executive group

After you have reviewed the test-chat alerts and a few runs in the journal, put the group's
chat id in `TELEGRAM_CHAT_ID` in `radar.env`, run `doctor` (it checks the bot is a member) and
wait for the next timer run. Alerts already in `state.json` are not re-sent to the new chat.

## Reminders

An open High finding that nobody has dealt with is sent again as a reminder: a separate
message with the title `Radar reminder · N open` and one line per open High series: name,
audit type, finding count and age, for example:

```
<b>Radar reminder · 2 open</b>
🔴 <b>Project A</b> · security High 1 · 8d
🔴 <b>Project B</b> · tests High 5 · 22d
```

The age counts from the radar's first sight of the series, not from when Guard found it. A
reminder carries no Claude line.

A series is reminded every `reminders.every_days` days (default 7), counted from the last
alert or reminder of that series, and never earlier. If the count rises and the series is
alerted again, the clock restarts from that alert. There is no cap: the series is reminded
every interval for as long as it stays open. All series due in one run share one message.

Never reminded: stale or never-ran projects (they appear in the weekly digest), and series
that were only baselined and never alerted (they also appear in the digest). A series that
closed, or whose latest High count is 0, is not reminded either.

A reminder is recorded only after Telegram acknowledges it, so a failed send is retried on the
next run. After a fatal send error (for example a rejected bot token) no reminder is tried in
that run, and the run exits with code 1. `run --dry-run` prints the reminders that would be
sent and writes nothing.

Set the interval with `reminders.every_days` in `/etc/enji-guard-radar/radar.yaml`, from 1 to
90 days.

## Weekly digest

Once a week the group gets one digest, even when nothing is open. It goes out on the first
timer run at or after the planned moment: `digest.weekday` and `digest.hour_utc`, in UTC. The
default is Wednesday 05:00 UTC (11:00 UTC+6), so with the 3-hour timer the 06:00 UTC run sends
it.

The window is `digest.grace_hours` (default 24). A digest that Telegram did not acknowledge is
retried on each run inside the window. After the window the week is skipped: the state records
it once, the journal gets one WARNING line, and nothing is made up later. The items that were
resolved in the meantime stay pending for the next digest. To see a skipped week, run
`journalctl -u enji-guard-radar` and look for "digest for the week of". A window shorter than
the timer interval can skip a week without a single attempt; `doctor` warns about it.

While Guard is unavailable (the project listing failed, or every active project could not be
read) a due digest is deferred and logged, never built from a failed poll. The first
successful run inside the window sends it. A project that could not be read in an otherwise
successful poll does not defer it: the digest counts such projects in its text ("N projects
could not be read in that poll").

What it contains, worst first by type and then by age: open High series, stale or never-ran
projects, low scores, tooling gaps, one line per item. A High series that was open in each of
the last `recurring.after_runs` runs ends with `recurring N runs`. At most `digest.max_items`
items are listed, then "+N more open items not shown." Items resolved since the previous digest
follow under `Resolved · N`, each shown once. Every digest shows the time of the last
successful Guard poll and ends with the footer "Ages count from the radar's first sight." For
example:

```
<b>Radar weekly · 3 open</b>
Last poll: 2026-11-18 06:00 UTC
🔴 <b>Project A</b> · security High 2 · 16d · recurring 3 runs
🟡 <b>Project B</b> · no scan 21d
🟡 <b>Project C</b> · tests 40/100 · 8d
Resolved · 1
<b>Project D</b> · security closed
Ages count from the radar's first sight.
```

When nothing is open it is a single all-clear line:
`Radar weekly · nothing open · last poll 2026-11-18 06:00 UTC`. The digest counts only: it shows names, counts, scores,
dates and audit types, no finding titles. It includes items the group already saw as alerts
and items that were only baselined.

`run --dry-run` prints the digest in the same way and writes nothing.

## Triage

Triage adds one short Claude line under a project's High alert line (at most one per project,
the most urgent): a priority and a likely cause. It never
decides whether to alert (the rules do) and a failed or invalid Claude reply leaves the plain
rule message. It ships off. Claude gets only counts, audit types and finding titles of the
alert being sent; nothing from Guard is stored.

1. Create the token with `claude setup-token`, on a machine where you are logged in to Claude.
   Never paste it into chat, tickets or shell history.
2. Put it in `/etc/enji-guard-radar/radar.env` (mode 0600, see "Configuration and secrets") as
   `CLAUDE_CODE_OAUTH_TOKEN=<token>`. The `claude` binary must be on the `PATH` of the service
   user.
3. Enable it in `/etc/enji-guard-radar/radar.yaml` with `triage.enabled: true`. The other
   `triage` keys keep their defaults (see `config/radar.example.yaml`).
4. Run `doctor` with the config:
   `sudo -u enji-guard-radar /opt/enji-guard-radar/.venv/bin/enji-guard-radar doctor --state-dir /var/lib/enji-guard-radar --env-file /etc/enji-guard-radar/radar.env --config /etc/enji-guard-radar/radar.yaml`.
   Expect `PASS  triage: on, backend claude_cli, binary found, token set; calls 0, failed 0`.
   Doctor makes no Claude call. A `FAIL` names what is missing: the `claude` binary or
   `CLAUDE_CODE_OAUTH_TOKEN`. A `WARN` with `calls N, failed N` means every call today failed;
   look at the journal.
5. Read one dry-run with triage on before going live:
   `sudo -u enji-guard-radar /opt/enji-guard-radar/.venv/bin/enji-guard-radar run --dry-run --config /etc/enji-guard-radar/radar.yaml --state-dir /var/lib/enji-guard-radar --env-file /etc/enji-guard-radar/radar.env`.
   When a fresh High alert exists it makes one real Claude call, prints the alert with the
   Claude line and writes no state. The group message has one line per project (total High
   count, number of audit types, oldest age; no repo or audit names), and the Claude line sits
   under it, for example:

   ```
   🔴 <b>Project A</b> · High 4 in 2 audits · 16d
      p1 · Injection flaw in order search
   ```

6. Read the two journal lines every call writes (`journalctl -u enji-guard-radar`):
   `triage call: items N, outcome ..., seconds ..., cost_usd ..., input_tokens ..., output_tokens ...`
   (`unknown` where the CLI reported nothing) and
   `triage today (UTC): N calls, M failed, $X`. An `outcome` other than `ok` names why the
   call failed. The per-day numbers live in `triage-cost.json` in the state directory.
7. In the private test chat check that the 3-space indent of the Claude line survives.
   ASSUMPTION (D-10): Telegram keeps leading spaces. If it does not, drop the indent in the
   `alert.triage_line` entry of the message catalog (`src/enji_guard_radar/texts.py`).
8. Turn it off with `triage.enabled: false`. The next run makes no Claude call and the alerts
   are the same without the extra line.

## Phase 3 operator steps

1. After the first real run, list the recorded scores (run as the service user):
   `sudo -u enji-guard-radar python3 -c 'import json; [print(s["action_key"], s["score"]) for s in json.load(open("/var/lib/enji-guard-radar/state.json"))["scores"]]'`
2. Choose thresholds from that spread and set `scores.ai_readiness_below` and
   `scores.tests_below` in `/etc/enji-guard-radar/radar.yaml`, optionally per project. A value
   of 0 turns the rule off for that project. Run `doctor` and read the "score rules" line.
   Thresholds only ever add lines to the digest, never alerts.
3. Tooling gaps: a gap is stated once the radar has read the repository's whole run list, or
   has watched the repository for `tooling_gaps.window_days`. For repositories with long run
   histories the first gaps therefore appear that many days after the first run.
   ASSUMPTION: this evidence rule is right; revisit it after real data exists.
4. Preview the digest without sending: copy the state directory to a scratch directory, delete
   the `digest` entry in the scratch `state.json` when that week was already handled, then run
   `sudo -u enji-guard-radar /opt/enji-guard-radar/.venv/bin/enji-guard-radar run --dry-run --now 2026-11-18T06:00:00Z --config /etc/enji-guard-radar/radar.yaml --state-dir <scratch> --env-file /etc/enji-guard-radar/radar.env`
   with `--now` set to a Wednesday 06:00 UTC.
5. Live check in the private test chat (`TELEGRAM_CHAT_ID` still pointing at it): run once
   without `--dry-run` and with `--now` set to a Wednesday 06:00 UTC. Expect one digest. Run
   the identical command again and expect nothing.
6. Only then point `TELEGRAM_CHAT_ID` at the group.
7. Check the 60-day tooling window and the thresholds again after real data exists. This is a
   gap-closure step, not part of this phase.

## Failure drill

Prove the ops chat works before relying on it:

1. Move `guard-auth.json` aside and let `failures_before_alert` runs happen
   (`sudo systemctl start enji-guard-radar.service`). Expect exactly one ops message saying the
   Guard login is missing or expired, and no further message while it stays broken.
2. Move the file back and run once more. Expect one recovery message in the ops chat.

## Re-auth and revocation

When the ops chat says the login expired or was revoked (or `doctor` fails the Guard check),
run the `auth` command above again; it replaces `guard-auth.json`. To revoke the radar's
access, revoke it on the Guard side and delete `guard-auth.json`.

## Cron alternative

If the host has no systemd, use `deploy/cron.example`. Cron has no catch-up and no failure
hook, so check the journal or mail by hand. The command is bounded by `timeout 15m`.
Do not wrap it in `flock` on the radar's own lock file (`radar.lock` in the state directory):
the radar takes that lock itself, so the wrapper would make every run exit 0 as "locked"
without doing anything.
