# Snapmaker U1 Scheduler

Want to start printing in the morning at a preset time? No problem!
Just confirm the bed is empty and clean, set the date and time, choose what to print, 
and schedule that! It starts to print for you.

## Description

Starts prints on a Snapmaker U1 at a time you choose, but only if someone has
confirmed the bed is empty and clean and the printer hasn't printed since.
It runs on an always-on box on your home network.

## Set up

1. Give the printer a DHCP reservation in your router so its address never changes.
2. On the home box (with Docker): clone this repository, `cp .env.example .env`, edit `.env`.
3. `docker compose up -d --build`, then open `http://<home-box>:8000`.
4. Keep `DRY_RUN=true` until the printer probe
   (`docs/superpowers/plans/2026-10-14-u1-printer-probe.md`) says GO in its findings note and
   its "Changes needed" are applied. A dry run runs every check and logs what it would send,
   without starting anything, but it cannot show whether the printer honours the head
   mapping, whether a remote start needs a touch on the screen, or whether the job history
   records touchscreen prints. Only then set `DRY_RUN=false` and run `docker compose up -d`
   again.

## Telegram (optional, send-only)

1. In Telegram, message @BotFather, send `/newbot`, and copy the token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<token>/getUpdates` and copy `message.chat.id`.
4. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`, run `docker compose up -d`
   (a plain restart doesn't re-read `.env`), and press "Send Telegram test message" on the
   dashboard.

The app only sends messages. Nothing written to the bot can affect the printer.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `PRINTER_HOST` | required | Printer IP or name |
| `APP_PASSWORD` | required | Password for the web page |
| `TZ` | `UTC` | Time zone for entering and showing times |
| `DRY_RUN` | `false` | Run all checks but never start a print |
| `PRINTER_PORT` | `7125` | Moonraker port |
| `PRINTER_API_KEY` | empty | Only if your printer requires one |
| `OFFLINE_GRACE_MINUTES` | `10` | How late a job may still start if the printer was briefly offline |
| `START_VERIFY_SECONDS` | `90` | How long to wait for the printer to report printing |
| `REMINDER_MINUTES` | `60` | Remind this long before a job whose bed isn't confirmed |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | Both needed to enable Telegram |
| `APP_BASE_URL` | empty | Link added to Telegram messages |
| `SECRET_KEY` | generated | Session signing key (stored in `/data` if unset) |

## How the safety rule works

- "Bed is empty and clean" can only be confirmed while the printer is idle.
- Any print that starts afterwards voids the confirmation, whether it was started from the
  touchscreen, the Snapmaker app or a scheduled job. Every start attempt by a scheduled
  job uses it up, even if the start fails.
- If the app stopped watching the printer for more than 2 minutes, it checks the printer's
  job history for that time. If it can't prove the printer stayed idle, it voids the
  confirmation.
- A job never starts more than `OFFLINE_GRACE_MINUTES` late.

## Development

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install -e '.[dev]'
pytest
```

Integration tests against a simulated U1: see `tests/integration/README.md`.
