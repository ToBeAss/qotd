# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

QOTD generates daily quotes using OpenAI's API and posts them to Discord via a webhook. Quotes come from a small cast of personas defined in `system_prompt.py` — see [Personas](#personas). Each weekday the scheduler delivers 2–3 messages spread across the day, each independently picking a persona by weight.

## Personas

`system_prompt.py` exports a `PERSONAS` dict and a `pick_persona()` weighted-random helper. Each invocation of `main.py` picks one persona (or `--persona <key>` forces one).

| Key | Name | Voice | Weight |
|---|---|---|---|
| `dealer` | The Quote Dealer | Noir, dark, mysterious — the canonical voice. | 60% |
| `plug` | The Quote Plug | Brainrot, lighthearted, chronically online. References 6-7 / 69 occasionally. Opens with a "covering for the Dealer" pre-line. | 25% |
| `postman` | The Quote Postman | Plain, calm, observational. Universal quotes. Opens with a mild "filling in for the Dealer" pre-line. | 15% |

To rebalance: change the `weight` values in `system_prompt.py`. Weights are integers and don't have to sum to 100.

## Running the Project

```bash
pip install -r requirements.txt
cp .env.example .env                              # then fill in credentials
python main.py                                    # weighted-random persona, posts to Discord
python main.py --persona plug                     # force a specific persona
python main.py --persona postman --dry-run        # print to stdout instead of posting (preview)
```

**Scheduling on the Pi (cron):**
```bash
chmod +x scheduler.sh
crontab -e
# Add:
0 6 * * * /path/to/qotd/scheduler.sh
```

`scheduler.sh` runs three time slots per day, each delivering one independent message (each picks a persona via `pick_persona()`):

- **Slot A** — random moment in 06:00–12:00 (guaranteed)
- **Slot B** — random moment in 12:00–18:00 (guaranteed)
- **Slot C** — random moment in 18:00–24:00 (30% chance)

Average ~2.3 messages/day. A slot whose window has already closed when cron fires (e.g. cron started late) is skipped — we don't fire two messages back-to-back.

## Environment Variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI API authentication |
| `DEALER_WEBHOOK` | Discord webhook for The Quote Dealer. Set webhook avatar/name in Discord. |
| `PLUG_WEBHOOK` | Discord webhook for The Quote Plug. Set webhook avatar/name in Discord. |
| `POSTMAN_WEBHOOK` | Discord webhook for The Quote Postman. Set webhook avatar/name in Discord. |
| `HEALTHCHECK_URL` | healthchecks.io ping URL (optional). Pinged by `scheduler.sh` on start/success/failure so an off-host service alerts when the Pi goes silent. |

**Setting up webhooks:** Create three Discord webhooks (one per persona) in your target channel. In Discord's webhook settings, set the avatar and username for each. Paste the webhook URLs into `.env`.

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
2. Schedule: period **12 hours**, grace **14 hours**. (Now that we deliver in three time bands, the worst-case gap between consecutive successful pings is roughly slot-A early one day → slot-B late next day, ~26h. Period 12h + grace 14h alerts within that window.)
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

**`main.py`** — Entry point. CLI flags: `--persona {dealer,plug,postman}` (force a persona; default is weighted random) and `--dry-run` (print to stdout instead of posting).
- `send_to_discord(webhook, content)` — POSTs to a Discord webhook URL; returns `True` on HTTP 204
- `get_quote_of_the_day(prompt)` — Calls OpenAI with the given system prompt and returns the quote string

Uses the `/v1/responses` endpoint (not `/v1/chat/completions`). Response parsed as `data["output"][0]["content"][0]["text"]`. OpenAI call has a 30s timeout.

**`scheduler.sh`** — Cron-driven scheduler. Pings healthchecks.io on `/start` / success / `/fail`. Uses `python3` to generate random sleep durations (avoids bash `$RANDOM` 32767 limit). Runs the venv Python at `.venv/bin/python3`. The 3-slot logic lives in `run_slot()` — call it with a label, window-start epoch, and window-end epoch.

**`system_prompt.py`** — Defines the persona cast (`PERSONAS` dict) and `pick_persona()`. Three personas: dealer / plug / postman. Edit prompts to change tone or rules, edit weights to rebalance frequency.
