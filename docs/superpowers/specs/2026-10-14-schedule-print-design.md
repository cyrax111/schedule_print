# Scheduled Printing for Snapmaker U1 — Design

**Date:** 2026-10-14
**Status:** Approved in brainstorming; awaiting written-spec review
**Source idea:** `raw/idea.md`

## 1. Purpose

Start a print on a Snapmaker U1 at a chosen day and time, with a chosen
G-code file and colour, but only when it is safe: a person has confirmed the
bed is empty and clean, and the printer has not printed since that
confirmation. Scheduling, confirming and watching printer status all happen in
a web browser.

### Decisions made with the user

| Topic | Decision |
|---|---|
| Model input | Pre-sliced G-code (sliced in Snapmaker Orca as today). No slicing in the app. |
| Colour | For each colour the file uses, the user picks one of the 4 loaded heads. Expected material and colour are re-verified at start time. |
| Host | Always-on home box (Raspberry Pi / NAS / home server) running Docker on the printer's LAN. |
| Access | Home network (or the user's own VPN) only, protected by one shared password. |
| Scheduling | Several one-off jobs. No recurrence. |
| Notifications | In-page notification feed plus send-only Telegram messages. No commands from Telegram. |
| Stack | Python, FastAPI, SQLite, server-rendered Jinja2 pages with htmx. |
| Approach | One service that watches the printer continuously and fails closed when it cannot prove the printer stayed idle. |
| Video | Wanted later; a separate sub-project. v1 only leaves room for it. |

### Assumptions (stated, not yet verified)

- One printer.
- A job whose conditions fail at its time is skipped, never started late.
- Stock U1 firmware exposes Moonraker on port 7125 to LAN clients (verified by
  community probes on firmware 1.4.1.6; must be re-checked on the user's
  firmware in sub-project 0).

## 2. Research summary

The U1 runs Snapmaker's forks of Klipper, Moonraker and Fluidd
(github.com/Snapmaker/u1-klipper, u1-moonraker, u1-fluidd). Moonraker serves
the LAN on port 7125; Fluidd on port 80. Community projects read and control
the printer on stock firmware with no printer-side changes.

| Need | U1 / Moonraker capability |
|---|---|
| Live status | `GET /printer/objects/query`, websocket `printer.objects.subscribe` on `print_stats`, `virtual_sdcard`, `extruder`..`extruder3`, `heater_bed`, `toolhead`, `exception_manager`, `pause_resume`, `webhooks` |
| Loaded filament per head | `print_task_config`: `filament_type[N]`, `filament_vendor[N]`, `filament_color_rgba[N]` (RRGGBBAA), `filament_exist[N]`, `extruder_map_table`; `filament_detect` for RFID detail |
| Head numbering | Printhead #1–#4 = T0–T3 = objects `extruder`, `extruder1`, `extruder2`, `extruder3` = channel 0–3 |
| Files | `GET /server/files/list?root=gcodes`, `GET /server/files/metadata?filename=` (slicer metadata, thumbnails), `POST /server/files/upload` with `print=false` |
| Colour → head mapping | G-code macro `SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=<logical> MAP_EXTRUDER=<head>`; read back via `GET_PRINT_EXTRUDER_MAP` / `extruder_map_table`. Refused while printing or paused. |
| Start | `POST /printer/print/start?filename=` and/or the U1 macro `SDCARD_PRINT_FILE_WITH_PARAMETERS` (exact sequence unverified) |
| Job history | Moonraker `GET /server/history/list?since=` (presence on the U1 fork unverified) |
| Camera (for later) | Websocket `camera.start_monitor` refreshes `/server/files/camera/monitor.jpg` (stock, ~1 fps); Extended Firmware adds WebRTC/RTSP 1080p. Printer records its own timelapses. |

Prior art:
- **u1hub** (dlgambill/u1hub): Node hub for U1 farms with a dispatch queue.
  Broader than this idea, and has no confirmation-validity rule.
- **snapmaker-u1-toolkit** (bbolinger): Telegram flow with a human-approved
  start gate. Its `references/` notes are the best API reference found.
- **u1-companion** (zkasuran): Home Assistant integration plus **u1sim**, a
  fake Klippy that runs Snapmaker's real Moonraker in Docker. Used here for
  integration tests.

## 3. Decomposition

| # | Sub-project | Kind | Output |
|---|---|---|---|
| 0 | Printer probe | Spike | Answers to the probe questions (§11) and recorded real responses saved as test fixtures. No product code kept. |
| 1 | Scheduler core (v1) | Build | Everything in this spec. |
| 2 | Video | Later; own brainstorm, spec and plan | Live view and/or recordings. |

Sub-project 0 runs first. If it shows the API is closed on the user's
firmware, or that a remote start needs touchscreen interaction, stop and
revisit this design before building v1.

## 4. v1 scope

In:
- Status page: printer state, progress, temperatures, current file, filament
  in each head, online/offline.
- Files: list printer G-code with thumbnail, estimated time and colours used;
  upload a `.gcode` without starting it.
- Schedule one-off jobs (file, local date/time, head per colour); edit or
  cancel pending jobs.
- Bed confirmation: confirm, see validity, withdraw.
- Start-time gate that starts or skips with a visible reason.
- Notifications (§9): in-page feed and send-only Telegram messages.
- One shared password.
- Docker image and compose file for the home box.

Out: slicing, Telegram commands or buttons, browser push notifications
(need HTTPS), recurring jobs, multiple printers, video and camera, internet
exposure, user accounts.

## 5. Architecture

Single Python 3.12 process. FastAPI serves pages and fragments; background
asyncio tasks run the printer watcher, the scheduler and Telegram delivery;
SQLite stores state.

```
 Browser (htmx, polls status every 2 s)
    │
 ┌──▼───────────── web ──────────────┐     ┌──── auth ────┐
 │ pages + partials, form handling   │◄────┤ password →   │
 └──┬──────────┬──────────┬──────────┘     │ signed cookie│
    │          │          │                └──────────────┘
 ┌──▼───┐  ┌───▼──────┐  ┌▼─────────────┐
 │ jobs │  │ bedcheck │  │ printer      │ ◄── only code that knows
 │ +    │─►│ (confirm │◄─┤ client +     │     Moonraker / U1 details
 │ gate │  │ + ledger)│  │ watcher      │──► Moonraker :7125 (U1)
 └──┬───┘  └───┬──────┘  └──────┬───────┘
    │          │                 │ events
 ┌──▼──────────▼─────────────────▼──┐
 │ notify: feed (SQLite) + Telegram │──► api.telegram.org (outbound only)
 └──────────────────────────────────┘
```

### 5.1 Units

**printer** — the only unit that knows Moonraker or U1 specifics.
- `PrinterClient` (HTTP): `status() -> PrinterStatus`, `files() -> list[FileInfo]`,
  `file_meta(name) -> FileMeta`, `upload(name, bytes)`,
  `loaded_filaments() -> list[HeadFilament]` (4 entries),
  `apply_head_map(map) -> None` (applies then reads back; raises on mismatch),
  `start_print(filename) -> None`,
  `history(since) -> list[HistoryJob] | None` (`None` = history unavailable).
- `PrinterWatcher` (websocket): keeps one subscription open, reconnects with
  backoff (1 s doubling to 30 s), keeps the latest `PrinterStatus` in memory,
  and publishes events: `connected`, `disconnected`, `print_started(filename, at)`,
  `print_finished(filename, at, result)`.
- A print start is a `print_stats.state` transition into `printing` from any
  other state.
- "Idle" means: connected, Klippy `ready`, `print_stats.state` in
  {`standby`, `complete`, `cancelled`}, not paused, no active exception.
  Exact U1 state values are confirmed in sub-project 0.

**bedcheck** — owns the confirmation rule (§6).
- `confirm() -> Confirmation` (refused unless the printer is idle),
  `current() -> Confirmation | None`, `withdraw()`,
  `validity(now) -> Valid | Invalid(reason)`, `consume(job_id)`.
- Subscribes to printer events and records `print_events` and `watch_periods`.

**jobs** — job storage, the gate and the runner.
- Store: `schedule()`, `edit()`, `cancel()`, `list()`, `next_due()`.
- `gate(snapshot) -> Start | Skip(reason)`: a pure function over a
  `GateSnapshot` (printer reachability and status, confirmation validity,
  current file info, loaded filaments, the job). No I/O.
- Runner: sleeps until the next due job or 30 s, whichever is sooner; wakes
  early when jobs change.

**notify** — records notifications and delivers them (§9).
- `notify(kind, text, job_id=None)`: writes a `notifications` row, then
  queues Telegram delivery if configured. Never raises to the caller, so a
  notification problem cannot affect scheduling.
- A delivery worker sends queued rows to Telegram with retries.

**web** — routes and Jinja2 templates; htmx fragments for live parts.

**auth** — `APP_PASSWORD` from the environment; signed session cookie
(`SameSite=Strict`, `HttpOnly`); POSTs also check the `Origin` header; five
wrong passwords lock login for 60 s.

### 5.2 Supporting pieces

- `Clock` interface injected everywhere time is read, so tests use a fake clock.
- Settings from environment variables: `PRINTER_HOST` (required),
  `PRINTER_PORT=7125`, `PRINTER_API_KEY` (optional), `APP_PASSWORD`
  (required), `SECRET_KEY` (generated into the data directory on first run if
  unset), `TZ`, `DRY_RUN=false`, `OFFLINE_GRACE_MINUTES=10`,
  `START_VERIFY_SECONDS=90`, `DATA_DIR=/data`, `REMINDER_MINUTES=60`,
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (both optional; Telegram is off
  unless both are set), `APP_BASE_URL` (optional; used for links in
  Telegram messages).
- Schema migrations: numbered SQL files applied in order, tracked by SQLite
  `PRAGMA user_version`.

### 5.3 Room for video (sub-project 2)

A future recorder unit subscribes to `print_started` and `print_finished` and
adds its own page. Camera access will live in its own module, not in
`PrinterClient`. Compose layout allows adding a relay container (go2rtc or
MediaMTX) next to the app.

## 6. Bed-confirmation rule

At most one confirmation is active; it belongs to the printer, not to a job.

**Creating:** allowed only when the printer is online and idle. A new
confirmation replaces the previous one (the previous one ends with reason
`replaced`).

**Ending:**

| Event | Result |
|---|---|
| A print start is observed (any source: touchscreen, Snapmaker app, Orca, this app outside a job) | Void: `printed since confirmation` |
| A scheduled job reaches the start step, whether or not the start succeeds | Consumed by that job |
| User withdraws | Void: `withdrawn` |
| On (re)connect, the printer is printing or paused | Void: `printed since confirmation` |

**Validity:** `validity(now)` returns `Valid` only for an active confirmation
that passes the coverage check below. Otherwise it returns `Invalid` with
`no confirmation` (none was ever made, or the latest one was consumed by a
job) or with the latest confirmation's end reason (`printed since
confirmation`, `couldn't verify the printer stayed idle`, `withdrawn`).

**Coverage check** (inside `validity(now)`): every moment from `confirmed_at`
to `now` must be accounted for.
1. Times inside a `watch_period` (connected and watching) are covered.
2. Each uncovered gap longer than 2 minutes is checked against
   `history(since=gap_start)`: covered if no job started inside the gap.
   Gaps of 2 minutes or less are covered (a U1 print cannot start and finish
   that fast, and a print still running would be seen on reconnect).
3. If history returns `None` or any job started inside a gap, the result is
   `Invalid("couldn't verify the printer stayed idle")` or
   `Invalid("printed since confirmation")` respectively, and the confirmation
   is voided with that reason.

**Watch periods:** a period opens on `connected` and its `last_seen_at` is
updated every 30 s and on every event; it closes on `disconnected` or process
exit. On start-up any open period is closed at its `last_seen_at`.

## 7. Data model (SQLite)

```
confirmations(id, confirmed_at, ended_at NULL, end_reason NULL,
              consumed_by_job_id NULL)
print_events(id, at, kind['started'|'finished'], filename,
             result NULL, source['live'|'history'])
watch_periods(id, began_at, last_seen_at, closed INTEGER)
jobs(id, filename, file_size, file_modified, scheduled_at_utc,
     head_map_json, status, outcome_reason NULL, reminder_sent INTEGER,
     created_at, decided_at NULL)
notifications(id, at, kind, text, job_id NULL, read INTEGER,
              telegram_status['off'|'pending'|'sent'|'failed'],
              telegram_attempts, telegram_error NULL)
```

- `head_map_json`: list of `{logical, head, expected_type, expected_rgb}`, one
  per colour the file uses. Expectations are copied from the head's loaded
  filament when the job is created or edited.
- `status`: `pending`, `starting`, `started`, `skipped`, `failed`, `cancelled`.
- All timestamps stored in UTC; shown in `TZ`.

## 8. Start-time flow

When a job is due, the runner builds a fresh `GateSnapshot` and calls `gate`.
Checks run in order; the first failure decides.

| # | Check | Skip reason |
|---|---|---|
| 1 | Printer reachable. If not, retry every 30 s until `OFFLINE_GRACE_MINUTES` after the scheduled time. | `printer offline` |
| 2 | Printer idle and without exception | `printer busy` / `printer in error: <message>` |
| 3 | `bedcheck.validity(now)` is `Valid` | `no confirmation` / `printed since confirmation` / `couldn't verify the printer stayed idle` / `withdrawn` |
| 4 | File exists with the same size and modified time | `file missing` / `file changed since scheduling` |
| 5 | Every mapped head has `filament_exist`, the expected type (case-insensitive) and the exact expected RGB | `head <n>: expected <colour> <type>, found <colour> <type>` |

On `Start`:
1. Set job `starting`; `bedcheck.consume(job_id)`.
2. `apply_head_map` (applies and reads back).
3. `start_print(filename)`.
4. Wait up to `START_VERIFY_SECONDS` for the watcher to report
   `print_started` for that filename → `started`; otherwise
   `failed: printer did not start`.

Any exception in steps 2–3 → `failed: <message>`. The confirmation stays
consumed in every failure after step 1.

**Dry run:** with `DRY_RUN=true` the gate runs normally, but on `Start`
nothing is sent to the printer, the confirmation is not consumed, and the job
ends `skipped: dry run (all checks passed)` with the planned head map and
start call logged.

**Edge cases**
- On start-up, `pending` jobs more than `OFFLINE_GRACE_MINUTES` overdue →
  `skipped: missed (app not running)`. Never started late. Jobs overdue by
  less than that are evaluated normally.
- On start-up, jobs in `starting` → `failed: interrupted`. Never retried.
- Two jobs close together: the first consumes the confirmation, so the second
  skips unless the user confirms again. The schedule form warns when a new
  job's time is earlier than the previous job's time plus its estimated
  print time.
- Nonexistent local times (spring-forward gap) are rejected by the form;
  ambiguous times (fall-back) use the first occurrence and show the UTC
  offset.

## 9. Notifications

**Events**

| Kind | When | Example text |
|---|---|---|
| `job_started` | Job reaches `started` | "Started benchy.gcode (head 1 red PLA)" |
| `job_skipped` | Job reaches `skipped` | "Skipped benchy.gcode at 02:00: no confirmation" |
| `job_failed` | Job reaches `failed` | "Failed benchy.gcode: printer did not start" |
| `print_finished` | Watcher sees any print end | "benchy.gcode finished: complete" |
| `reminder` | `REMINDER_MINUTES` before a pending job, if `validity(now)` is not `Valid`; sent at most once per job (`reminder_sent`, reset when the job's time is edited). A job created or edited inside the window is checked right away. | "benchy.gcode is due at 02:00 and the bed is not confirmed" |
| `confirmation_voided` | A confirmation is voided while at least one job is pending. Not sent for `withdrawn` or `replaced`, since the user did those, nor when the voiding happens during a job's gate check, since that job's `job_skipped` message carries the same reason. | "Bed confirmation voided: printed since confirmation. 2 jobs waiting." |

**Web feed:** every notification is stored. The page header shows an unread
count; the dashboard lists the latest five; `/notifications` lists all, with
"mark all read". Read state is global (one shared password, no per-user
state). Rows whose Telegram delivery failed show the error.

**Telegram (send-only):**
- `POST https://api.telegram.org/bot<token>/sendMessage` with `chat_id` and
  plain `text` (no parse mode, so no escaping issues), plus a link to
  `APP_BASE_URL` when set.
- Delivery worker retries failed sends up to 5 times (10 s, 30 s, 1 min,
  5 min, 15 min), then marks the row `failed`. Undelivered rows are resumed
  after a restart.
- The bot token never appears in logs or stored errors: error text is
  redacted before it is saved, since HTTP errors include the request URL.
- The app reads no updates from Telegram, so nothing in the chat can affect
  the printer.
- Setup: create a bot with @BotFather, send it any message, read the chat ID
  from `getUpdates`, set both environment variables, then press "Send
  Telegram test message" on the dashboard.

## 10. Pages

- **Dashboard (`/`)** — status fragment refreshed every 2 s: state, progress,
  temperatures, current file, four head swatches (type + colour), online or
  offline. Bed panel: "Confirmed at 21:40 · valid" with Withdraw, or the end
  reason and the Confirm button (disabled with an explanation when the
  printer is not idle). Next upcoming job. Latest five notifications and a
  "Send Telegram test message" button when Telegram is configured.
- **Files (`/files`)** — printer files with thumbnail, estimated time and
  colours used; upload form; "Schedule" action per file.
- **Schedule (`/jobs/new`, `/jobs/<id>/edit`)** — file, local date and time,
  and one head selector per colour used. Default per colour: the loaded head
  with the same material and nearest colour. Overlap warning.
- **Jobs (`/jobs`)** — upcoming (edit, cancel) and history (status, reason,
  times).
- **Notifications (`/notifications`)** — full feed, mark all read.
- **Login (`/login`)** — one password field.

Pages work at phone width. Every page header shows the unread count.

## 11. Probe questions (sub-project 0)

Answered against the user's printer, read-only except Q8, which uses one
small supervised print.

1. Firmware version. Is Moonraker on 7125 reachable from the home box without
   an API key? If not, how is a key obtained?
2. Do websocket subscriptions deliver all objects in §2? Record payloads in
   idle, printing, paused, complete and error states.
3. Which `print_stats.state` values and U1 machine-state values appear in each
   of those states?
4. Is `/server/history/list` enabled, and does it list prints started from the
   touchscreen and from the Snapmaker app?
5. For a multi-colour Orca-sliced file, which metadata fields give each
   colour's RGB, type and used length? If none, can the G-code header comments
   (`; filament_colour =`, `; filament used [mm] =`) be read instead?
6. Does `POST /server/files/upload` with `print=false` store without starting?
7. Does `SET_PRINT_EXTRUDER_MAP` + `POST /printer/print/start` honour the map,
   or is `SDCARD_PRINT_FILE_WITH_PARAMETERS` needed? With which parameters?
8. Does a remote start need any touchscreen interaction (dialogs, filament
   checks)? How long until `print_stats.state` becomes `printing`?

Outputs: a short findings note, recorded JSON payloads under
`tests/fixtures/real_u1/`, and any changes to `PrinterClient` constants.

## 12. Testing

- **Unit:** `gate` as a table of snapshots → decisions (every row of §8);
  coverage logic (§6) including short gaps, long gaps with and without history,
  history unavailable, reconnect while printing; head suggestion; time-zone
  and DST handling; auth lockout; which events produce which notifications
  (§9), including the reminder window and the once-per-job rule; token
  redaction.
- **Telegram delivery:** against a fake Telegram API (httpx mock transport):
  success, retry schedule, final failure, resume after restart, Telegram off
  when either variable is unset. No test ever calls the real Telegram API.
- **Integration:** `PrinterClient`, `PrinterWatcher` and full scheduler runs
  against u1sim with Snapmaker's real Moonraker via `docker-compose.test.yml`,
  with a fake clock.
- **Contract:** the real payloads from sub-project 0 are compared field by
  field with u1sim's, so the simulator cannot drift from the real printer
  unnoticed.
- **Safety fence:** the test configuration refuses any `PRINTER_HOST` other
  than the simulator's.
- **Acceptance (manual, real printer):** confirm the bed, schedule a small
  print 5 minutes ahead, watch it start and get the Telegram message; then
  schedule another without confirming, get the reminder, and watch it skip
  with `no confirmation`.

## 13. Deployment

- `Dockerfile` (python:3.12-slim) and `docker-compose.yml` with one `app`
  service, a `./data:/data` volume and the settings from §5.2.
- Printer needs a DHCP reservation so its address does not change.
- Runs on a Raspberry Pi 4/5 or any home server. An SSD is recommended once
  video recording is added.

## 14. Risks

| Risk | Mitigation |
|---|---|
| Internet or Telegram outage hides a skipped job | Every notification is also in the web feed; retries resume after restart |
| Bot token leaks through logs | Redaction before logging or storing errors; covered by a unit test |
| A firmware update closes or changes the API | Sub-project 0 probe; all U1 details isolated in `printer`; contract tests against recorded payloads |
| Remote start needs touchscreen interaction | Probe Q8; stop and redesign if so |
| Job history unavailable | Design already fails closed; cost is skipped jobs after app downtime |
| Colour metadata missing from Moonraker | Fallback to G-code header comments (probe Q5) |
| u1sim differs from real printer | Contract tests; manual acceptance on the real printer |
