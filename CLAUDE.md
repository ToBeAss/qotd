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
`registry.yaml`. The Dealer is the canonical voice; the other two post as
themselves and may mention covering for him only when he's actually gone quiet
(see *Dealer absence* below).

| Key | Name | Voice | Quote source | Posting habits |
|---|---|---|---|---|
| `dealer` | The Quote Dealer | Noir, dark, mysterious, strangely comforting. Voice, not theme — never about "dealing." | `original` — writes his own quote + optional aside. | Evenings, tracking actual dusk. Rare liminal dawn/midnight appearances. Slight overall favour. |
| `plug` | The Quote Plug | Brainrot, lighthearted, terminally online, never mean. Grindset/motivational register. | `remix` — remixes a bank quote (`remix_ratio`, 0.7), otherwise his own original. | Never before noon. Builds toward the weekend, hates Mondays. |
| `postman` | The Quote Postman | Honest, plain, observational. Specific, never a fridge magnet. | `bank` — delivers a bank quote; writes only the delivery line. | Working hours only (08–16), front-loaded. Never on weekends. |

To retune a character: edit its `agents/<key>.md` for voice, its `day_weights`
and `hours` in `registry.yaml` for timing, or its `quote_source` / `remix_ratio`
for where its quotes come from (missing or unknown source = `original`). Adding a character is one yaml block,
one `agents/<key>.md`, and one webhook env var. Nothing else hard-codes the cast.

## Architecture

```
registry.yaml          # single source of truth: identity, webhook mapping,
                        #   day/hour weights, quote sources, location, llm config
agents/
  _common.md           # house rules + context-block instruction (shared)
  dealer.md  plug.md  postman.md   # voice + each character's modes/output format
data/
  quotes.jsonl         # the live quote bank (reviewed)
  quotes.candidates.jsonl  # unreviewed candidates; never read by code
registry.py            # loads yaml + composes each prompt (voice + house rules)
llm.py                 # OpenAI Responses API call (raw requests, no SDK)
quotes.py              # quote bank: load/validate, cooldown-aware selection
memory.py              # state/memory.json: last_posted, recent_quotes, post_count,
                        #   recent_bank, quote_usage, overused-word helper
obs.py                 # shared logger + throttled admin-channel error reporting
planner.py             # 07:00 cron: decides the day, writes a plan file
dispatch.py            # every-minute cron: fires due posts, generates, posts, records
preview.py             # manual: generate/preview a quote, scene or full pipeline
state/                 # plan-*.json, memory.json, admin_throttle.json
```

The model is `llm.model` in `registry.yaml`, with optional `reasoning_effort`.
Reasoning models (GPT-6) reject `temperature` with a 400, so it stays commented out;
variety comes from the prompt context, not sampling. They also count reasoning
toward `max_output_tokens`, so don't cap output with tokens — a cap makes the
response come back `incomplete` with no text (`llm.py` raises an `LLMError`
naming the reason). Control length through prompts and deterministic checks.

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
- **Quote source**: for bank-sourced characters, each entry gets a `mode`
  (`bank`, `remix` or `original`; the Plug rolls `remix_ratio`) and, for bank and
  remix, a `quote_id` drawn from the bank (distinct within the plan). No usable
  bank → `original` for that post, plus an admin report.
- Writes `state/plan-YYYY-MM-DD.json` and prunes plans older than 7 days.

A post planned for the late tail fires after midnight, so it carries a `fire_at`
on the next calendar date but still lives in the planning day's file.

Testing flags: `--date YYYY-MM-DD`, `--seed N`, `--force`, `--print`.

### Dispatch (`dispatch.py`)

Runs every minute. Scans plan files for entries that are due (`fire_at <= now`)
and unsent. For each: renders the context block from the frozen facts plus the
character's live `recent_quotes` (injected as anti-repetition negatives),
generates the quote, posts to the character's webhook, flips `sent`, and records
the post to memory.

How the post is built depends on the entry's `mode` (`generate_post`):

- **`original`** (Dealer; Plug's original mode): the LLM writes the whole post.
- **`bank`** (Postman): the LLM returns only a one-line delivery framing. Code
  composes `*framing*`, the bank quote as `**"…"**`, then `— Author, *Source*`.
- **`remix`** (Plug): the LLM returns only the remixed line; code adds
  `— Author (remix)`. Deterministic checks (non-empty, not identical to the
  original, 0.5×–2.5× its length); one retry, then his `original` mode.

A bank quote that's gone from the bank by fire time falls back to `original`.
`quote_usage` is recorded only on a successful post.

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

After that come, when they apply:

- **Overused words**: `memory.overused_words` takes the character's last 15
  posts (LLM-authored text only: the Postman's framings, not the bank quotes),
  and flags 4+ letter words (minus stopwords and cast names) that appear in 3+
  distinct posts, top 8. Injected as `Avoid these words and images this time: …`.
- **`<task>`**: for bank-sourced characters, the mode, plus the quote to deliver
  or remix. The Dealer has no task block.

### Dealer absence

The only world state that colours a normal post. For the Plug and Postman,
dispatch computes `dealer_absent` at fire time: days since the Dealer's
`last_posted` ≥ `cover_after_days` (registry, default 3). False → a context line
tells them he's around and not to mention covering, standing in, or him being
away. True → a covering reference is allowed, not required.

## Quote Bank

`data/quotes.jsonl`, one object per line:

```json
{"id": "seneca-brevity-01", "text": "...", "author": "Seneca", "source": "On the Shortness of Life"}
```

`id` (unique, stable slug), `text` and `author` are required; `source` is
optional. **Code owns real-quote text and attribution — the LLM never writes a
real quote from memory.** The commonly known phrasing is fine; the attribution
must be genuine (no misattributed or apocryphal quotes, no lyrics, no poetry).

- **Review gate**: new candidates go in `data/quotes.candidates.jsonl`. Moving a
  line into `quotes.jsonl` is the review. Nothing reads the candidates file.
- Parsed at use time. Invalid lines and duplicate ids are skipped, with one admin
  report per load. Missing or empty bank → report, and the post falls back to
  `original`.
- **Cooldown**: `quote_cooldown_days` (365), shared across characters. Selection
  is uniform among quotes not used within the cooldown; if none are eligible, the
  least-recently-used one, plus a "quote bank exhausted" report.

## Running

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then fill in credentials

python planner.py --print       # plan today and show it
python dispatch.py              # fire anything currently due
```

`preview.py quote` runs the real generation path without posting (`--post` to
send). Flags: `--persona`, `--quote-id <id>`, `--mode remix|original` (Plug),
`--dealer-absent`, `--model <name>` / `--effort <level>` (A/B overrides for this
run), and `--remember` (the only way it writes memory or `quote_usage`).

**Cron on the Pi:**

```cron
0 7 * * *  cd /path/to/qotd && /path/to/qotd/.venv/bin/python planner.py
* * * * *  cd /path/to/qotd && /path/to/qotd/.venv/bin/python dispatch.py
```

Dispatch runs **every minute** (not every 5) so posts land on the planner's
random `fire_at` minute rather than rounding up to a 5-minute mark, which would
read as artificial. The flock in dispatch makes this safe — a slow generation or
an interaction playout that spans several ticks just causes the next ticks to
exit on the held lock. Idle ticks are a cheap cold-start that finds nothing due.

## State Files

- `state/plan-YYYY-MM-DD.json` — the day's decided schedule and per-entry `sent`
  flags. Pruned after 7 days. The audit trail for what was scheduled and why.
- `state/memory.json` — per character: `last_posted`, `recent_quotes` (last 15,
  the posted text), `post_count`, and `recent_bank` (last 15 bank-based posts:
  `{quote_id, framing|remix, posted}`). Top level: `quote_usage`
  (`{quote_id: "YYYY-MM-DD"}`, shared across characters).
- `state/admin_throttle.json` — rate-limit bookkeeping for admin error posts.

## Environment Variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI authentication. |
| `DEALER_WEBHOOK` / `PLUG_WEBHOOK` / `POSTMAN_WEBHOOK` | One Discord webhook per character. Set avatar/username in Discord. |
| `ADMIN_WEBHOOK` | Optional. Errors are posted here, rate-limited. Blank = log only. |
| `STORYTELLER_WEBHOOK` | Optional narrator. Posts the scene before an interaction's dialogue. Blank = no scene message. |

**Every cron entry point must call `load_dotenv(override=True)` at module top.**
Cron provides no environment, so a script that skips it runs without
`OPENAI_API_KEY` or `ADMIN_WEBHOOK` — failing, and unable to report that it
failed. `planner.py`, `dispatch.py` and `preview.py` each do this themselves. When
a fourth entry point arrives (e.g. the planned `announce.py`), that is the point
to introduce a shared `config.py` instead of copying the call again.

## Logging

`logs/qotd.log` — rotating, 512 KB × 3 backups. Full application log: generation
attempts, webhook results, tracebacks. Errors also go to `ADMIN_WEBHOOK` when set,
throttled by `obs.py` (same error suppressed for an hour, hard cap of 5 admin
posts per rolling hour) so a failure loop can't flood the channel.