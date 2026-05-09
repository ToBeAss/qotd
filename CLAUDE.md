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
| `HEALTHCHECK_URL` | healthchecks.io ping URL (optional). Pinged by `scheduler.sh` on start/success/failure so an off-host service alerts when the Pi goes silent. |

## Logging

Both `main.py` and `scheduler.sh` write to `logs/` next to the scripts (gitignored).

- `logs/qotd.log` — application log from `main.py` (rotating, 512 KB × 3 backups). Includes OpenAI call attempts, Discord webhook results, and full tracebacks on failure.
- `logs/scheduler.log` — scheduler timeline: when cron fired, sleep durations, exit codes, healthchecks.io ping results. Also captures stderr from each `main.py` invocation.

Tail both during a run: `tail -f logs/scheduler.log logs/qotd.log`.

## Off-host heartbeat (healthchecks.io)

The Pi being silent is the failure mode that on-host logs can't catch. `scheduler.sh` pings healthchecks.io at three points:

- `${HEALTHCHECK_URL}/start` — when cron fires (before any sleep).
- `${HEALTHCHECK_URL}` — after a successful `main.py` run.
- `${HEALTHCHECK_URL}/fail` (with the tail of `scheduler.log` as the body) — after a failing run.

**One-time setup on healthchecks.io:**
1. Create a check named `qotd-daily`.
2. Schedule: period **1 day**, grace **20 hours**. (The script sends at a random time between 06:00–23:59, so consecutive successful pings can be up to ~42h apart in the worst case. Period 24h + grace 20h covers that.)
3. Add a Discord notification integration pointed at `ADMIN_WEBHOOK`.
4. Paste the ping URL into `.env` as `HEALTHCHECK_URL=`.

If `HEALTHCHECK_URL` is unset the pings are skipped silently — useful for local dev.

## Recovery (when the Pi goes silent)

If healthchecks.io alerts that the Pi missed its window — or you notice no quote landed — the script logs are not enough. Pi system logs are where the real evidence lives, and on a default Pi OS install they are wiped on reboot.

**One-time Pi setup (do this once after recovery so the *next* crash leaves a trail):**
```bash
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo systemctl restart systemd-journald
journalctl --disk-usage   # verify
```
Do **not** install `log2ram` — it does the opposite of what we want here (RAM-only logs are lost on reboot).

**Post-mortem cheat sheet (run when the Pi comes back):**
```bash
# Did it cleanly shut down or crash? Tail of the previous boot:
journalctl -b -1 -n 200 --no-pager

# Common Pi Zero killers — undervoltage, OOM, SD-card I/O errors, fs corruption:
dmesg | grep -i -E 'under-?voltage|throttl|oom|i/o error|ext4-fs error|hung_task'

# Disk full? Pi Zero SD cards die quietly when /var fills up:
df -h

# When did cron actually fire?
grep CRON /var/log/syslog | tail -50

# What did the scripts say last?
tail -100 ~/qotd/logs/scheduler.log
tail -100 ~/qotd/logs/qotd.log

# Reboot loop?
last -x | head -20
```

## Architecture

**`main.py`** — Entry point with two functions:
- `send_to_discord(webhook, content)` — POSTs to a Discord webhook URL; returns `True` on HTTP 204
- `get_quote_of_the_day(input, bonus=False)` — Calls OpenAI and returns the quote string; when `bonus=True`, appends a user message prompting the dealer to acknowledge the bonus delivery in character

Uses the `/v1/responses` endpoint (not `/v1/chat/completions`). Response parsed as `data["output"][0]["content"][0]["text"]`. OpenAI call has a 30s timeout.

**`scheduler.sh`** — Cron-driven scheduler. Uses `python3` to generate random sleep durations (avoids bash `$RANDOM` 32767 limit). Runs the venv Python at `.venv/bin/python3`.

**`system_prompt.py`** — Defines the "Quote Dealer" persona imported by `main.py`. Edit this to change quote tone, style, or output format rules.
