# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

QOTD generates a daily quote using OpenAI's API and posts it to Discord via a webhook. Quotes are generated with a "Quote Dealer" noir persona defined in `system_prompt.py`. A scheduler posts at a random time between 06:00–23:59, with a ~15% chance of a bonus second quote later the same day.

## Running the Project

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in credentials
python main.py         # send a regular quote immediately
python main.py --bonus # send a bonus quote with in-character acknowledgement
```

**Scheduling on the Pi (cron):**
```bash
chmod +x scheduler.sh
crontab -e
# Add:
0 6 * * * /path/to/qotd/scheduler.sh
```

`scheduler.sh` sleeps until a random time in the 06:00–23:59 window, runs `main.py`, then has a ~15% chance of sleeping again and running `main.py --bonus`.

## Environment Variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI API authentication |
| `ADMIN_WEBHOOK` | Discord webhook — currently the active send target in `main.py` (swap to `QOTD_WEBHOOK` for production) |
| `QOTD_WEBHOOK` | Primary quote channel webhook |
| `ALOE_WEBHOOK` | Secondary channel webhook |

## Architecture

**`main.py`** — Entry point with two functions:
- `send_to_discord(webhook, content)` — POSTs to a Discord webhook URL; returns `True` on HTTP 204
- `get_quote_of_the_day(input, bonus=False)` — Calls OpenAI and returns the quote string; when `bonus=True`, appends a user message prompting the dealer to acknowledge the bonus delivery in character

Uses the `/v1/responses` endpoint (not `/v1/chat/completions`). Response parsed as `data["output"][0]["content"][0]["text"]`. OpenAI call has a 30s timeout.

**`scheduler.sh`** — Cron-driven scheduler. Uses `python3` to generate random sleep durations (avoids bash `$RANDOM` 32767 limit). Runs the venv Python at `.venv/bin/python3`.

**`system_prompt.py`** — Defines the "Quote Dealer" persona imported by `main.py`. Edit this to change quote tone, style, or output format rules.
