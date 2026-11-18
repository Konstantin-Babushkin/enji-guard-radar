# Deploying the radar unattended

Operator runbook for Phase 2: run the radar against real Guard on a timer. The code is
verified offline; every step below that touches the host, Guard or Telegram is done by the
operator (plan 02-11). Design background: `docs/design/v0.md`; what Guard returns:
`docs/guard-data-notes.md`.

The files in `deploy/` assume a checkout at `/opt/enji-guard-radar`, a system user
`enji-guard-radar`, config and secrets in `/etc/enji-guard-radar/` and state in
`/var/lib/enji-guard-radar`. If your host differs, change those paths in the unit files, in
`deploy/cron.example` and in the commands below, keeping them consistent: `--state-dir` must
equal the unit's `StateDirectory`.

## Install

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
   It holds no secrets. Check `ops.failures_before_alert` (default 3).
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

Every line must be PASS (SKIP is fine for the timer hint). Fix any FAIL before going on.

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
message titled "Reminder: High findings are still open". It lists each open High series with
its finding count, audit type, repository and how long the radar has seen it ("Seen by the
radar since ...").

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
projects, low scores, tooling gaps. A High series that was open in each of the last
`recurring.after_runs` runs carries a "Recurring" marker. At most `digest.max_items` items are
listed, then "+N more open items not shown." Items resolved since the previous digest follow,
each shown once. Every digest shows the time of the last successful Guard poll. When nothing
is open it is a single all-clear line. The digest counts only: it shows names, counts, scores,
dates and audit types, no finding titles. It includes items the group already saw as alerts
and items that were only baselined.

`run --dry-run` prints the digest in the same way and writes nothing.

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
