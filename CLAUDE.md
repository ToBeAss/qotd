# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What This Project Does

QOTD generates daily quotes with OpenAI and posts them to Discord via webhooks.
Quotes come from a small cast of characters, each with its own voice and its own
posting rhythm. A **planner** decides the day's schedule once each morning; a
**dispatch** process fires each post when its time arrives. The characters behave
like people with habits: when they show up, how often, and what they sound like
all vary by who they are.

## Characters

Each character is a voice file in `agents/` plus a config block in
`registry.yaml`. The Dealer is the canonical voice; the other two cover his quiet
days.

| Key | Name | Voice | Posting habits |
|---|---|---|---|
| `dealer` | The Quote Dealer | Noir, dark, mysterious, strangely comforting. Voice, not theme — never about "dealing." | Evenings, tracking actual dusk. Rare liminal dawn/midnight appearances. Slight overall favour. |
| `plug` | The Quote Plug | Brainrot, lighthearted, terminally online, never mean. | Never before noon. Builds toward the weekend, hates Mondays. |
| `postman` | The Quote Postman | Honest, plain, observational. Specific, never a fridge magnet. | Working hours only (08–16), front-loaded. Never on weekends. |

To retune a character: edit its `agents/<key>.md` for voice, or its `day_weights`
and `hours` in `registry.yaml` for timing. Adding a character is one yaml block,
one `agents/<key>.md`, and one webhook env var. Nothing else hard-codes the cast.

## Architecture

```
registry.yaml          # single source of truth: identity, webhook mapping,
                        #   day/hour weights, location
agents/
  _common.md           # house rules + context-block instruction (shared)
  dealer.md  plug.md  postman.md   # voice only
registry.py            # loads yaml + composes each prompt (voice + house rules)
llm.py                 # OpenAI Responses API call (raw requests, no SDK)
memory.py              # state/memory.json: last_posted, recent_quotes, post_count
obs.py                 # shared logger + throttled admin-channel error reporting
planner.py             # 07:00 cron: decides the day, writes a plan file
dispatch.py            # */5 cron: fires due posts, generates, posts, records
state/                 # plan-*.json, memory.json, admin_throttle.json
```

### Planner (`planner.py`)

Runs at 07:00, before the 07:30 posting window opens. Pure decision, no network.

- **Selection** is two-stage. Stage 1 picks exactly one character (weighted by the
  day, with a recency penalty so the rotation doesn't streak) — this is the floor
  of one post per day. Stage 2 rolls an independent low chance for each *other*
  eligible character to also post. Result is 1–3 posts, averaging ~1.33 on
  weekdays. A `day_weight` of 0 means the character never posts that day (Postman
  on weekends).
- **Timing** samples an hour from the character's own distribution, then a uniform
  minute, clamped to the global quiet window (07:30–00:30). The Dealer's evening
  weights are shifted at runtime to track civil dusk (via `astral`), so he skews
  late in summer and active-all-evening in winter, with a low daytime floor for
  rare early appearances.
- **Frozen facts**: each entry stores the context the character may riff on — date,
  weekday, planned time, sunset, light, days since last post, and whether the hour
  is unusual for them. These are frozen at plan time; dispatch does not recompute.
- Writes `state/plan-YYYY-MM-DD.json` and prunes plans older than 7 days.

A post planned for the late tail fires after midnight, so it carries a `fire_at`
on the next calendar date but still lives in the planning day's file.

Testing flags: `--date YYYY-MM-DD`, `--seed N`, `--force`, `--print`.

### Dispatch (`dispatch.py`)

Runs every 5 minutes. Scans plan files for entries that are due (`fire_at <= now`)
and unsent. For each: renders the context block from the frozen facts plus the
character's live `recent_quotes` (injected as anti-repetition negatives),
generates the quote, posts to the character's webhook, flips `sent`, and records
the post to memory.

Idempotent — the `sent` flag is the whole contract, so a reboot mid-day simply
resumes. A failing entry retries on later ticks; after `MAX_ATTEMPTS` it settles
so a persistent failure can't retry all day.

## The Context Block

Each generation is handed a small block the character *may* react to, in voice,
rarely:

```
<context>
Today is Monday, 15 June 2026. Local time 08:04. Sunset 22:33 — still light.
You last posted 3 days ago.
This is an unusual hour for you to appear.
</context>
```

The instruction to use it lives in `agents/_common.md`. This replaces every
boolean flag with facts the character interprets itself — "traffic was awful,"
"bet you didn't expect me this early," "been a while" all fall out of the same
mechanism. Recent quotes are appended as a `<recent>` block to discourage repeats.

## Running

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then fill in credentials

python planner.py --print       # plan today and show it
python dispatch.py              # fire anything currently due
```

**Cron on the Pi:**

```cron
0  7 * * *  cd /path/to/qotd && /path/to/qotd/.venv/bin/python planner.py
*/5 * * * *  cd /path/to/qotd && /path/to/qotd/.venv/bin/python dispatch.py
```

## State Files

- `state/plan-YYYY-MM-DD.json` — the day's decided schedule and per-entry `sent`
  flags. Pruned after 7 days. The audit trail for what was scheduled and why.
- `state/memory.json` — per character: `last_posted`, `recent_quotes` (last 15),
  `post_count`.
- `state/admin_throttle.json` — rate-limit bookkeeping for admin error posts.

## Environment Variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI authentication. |
| `DEALER_WEBHOOK` / `PLUG_WEBHOOK` / `POSTMAN_WEBHOOK` | One Discord webhook per character. Set avatar/username in Discord. |
| `ADMIN_WEBHOOK` | Optional. Errors are posted here, rate-limited. Blank = log only. |

## Logging

`logs/qotd.log` — rotating, 512 KB × 3 backups. Full application log: generation
attempts, webhook results, tracebacks. Errors also go to `ADMIN_WEBHOOK` when set,
throttled by `obs.py` (same error suppressed for an hour, hard cap of 5 admin
posts per rolling hour) so a failure loop can't flood the channel.
