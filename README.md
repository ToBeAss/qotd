# QOTD — Quote of the Day

A small Discord bot that posts a daily quote, written by one of three recurring
characters who each have their own voice, posting habits, and — once in a while —
conversations with each other. Runs on a Raspberry Pi Zero.

It's deliberately more than a quote-poster: the characters have persistent memory
(so they don't repeat themselves), schedule themselves by personality (the Plug
owns weekends, the Postman skips them), and occasionally break the routine to talk
to each other in short, narrated scenes.

## The cast

- **The Quote Dealer** — noir, spare, the canonical voice. Posts in the evenings,
  tracking actual dusk.
- **The Quote Plug** — chronically online, brainrot, covers when the Dealer's off.
  Never before noon; lives for the weekend.
- **The Quote Postman** — plain, honest, observational. Working hours only, never
  on weekends.

## How it works

Two cron jobs. A **planner** runs each morning and decides the whole day — who
posts, when (a random time inside each character's own rhythm), and the rare
interaction days — writing it all to a plan file. A **dispatch** process wakes
every minute and posts whatever's due, generating the quote at send time with the
character's recent quotes injected so it stays fresh.

```
planner.py  (07:00)        → state/plan-YYYY-MM-DD.json
dispatch.py (every minute) → reads the plan, generates, posts to Discord
```

Quotes come from the OpenAI API; posts go out through Discord webhooks (one per
character). No other external services.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # then fill in your keys/webhooks
```

`.env` needs an `OPENAI_API_KEY` and the three character webhooks. The admin and
storyteller webhooks are optional. See `.env.example`.

### Try it without waiting for the scheduler

```bash
python preview.py quote                       # a real quote, printed
python preview.py interaction --cast plug,dealer   # a full scene
python preview.py pipeline --dry              # the whole pipeline, nothing sent
```

### Run it for real (on the Pi)

```cron
0 7 * * *  cd /path/to/qotd && /path/to/qotd/.venv/bin/python planner.py
* * * * *  cd /path/to/qotd && /path/to/qotd/.venv/bin/python dispatch.py
```

## Tuning

Almost everything lives in `registry.yaml` — the model, posting weights per day
and hour, quiet hours, interaction frequency, and location (for the dusk
calculation). Each character's voice is a markdown file in `agents/`. Adding a
character is one yaml block, one markdown file, and one webhook.

## More

- `CLAUDE.md` — architecture notes and working guidance.
- `HANDOVER.md` — full design rationale, every decision, and the deferred roadmap.
