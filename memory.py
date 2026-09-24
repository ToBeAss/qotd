"""Per-character memory — a single state/memory.json.

Holds last_posted (for the days-since context fact and anti-clustering),
recent_quotes (anti-repetition), and post_count (worldbuilding seed). Planner
reads it; dispatch writes it on a successful post.

recent_quotes always holds the posted text, as it always has. Bank-based posts
also get a structured record in recent_bank ({"quote_id", "framing"|"remix",
"posted"}) so the LLM-authored part can be told apart from the bank quote, and
spec-B cross-references can find quote ids. The top-level quote_usage
({quote_id: "YYYY-MM-DD"}) is shared across characters and drives the cooldown.
The top-level recent_premises ([{"date", "premise", "seed", "closer", "medium",
"time_of_day", "slip_turn"}], newest last) holds completed interactions so the director
doesn't repeat a premise and code doesn't repeat a seed, closer or setting.
Older entries lack some of these keys.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).resolve().parent / "state"
MEMORY_PATH = STATE_DIR / "memory.json"

RECENT_QUOTES_CAP = 15
RECENT_PREMISES_CAP = 10

# Overused-word avoidance: a word in at least this many distinct recent posts is
# flagged, up to this many words.
OVERUSED_MIN_POSTS = 3
OVERUSED_CAP = 8

# Function words long enough to pass the 4-letter floor. Deliberately short.
STOPWORDS = frozenset("""
    about after again also always another away back been before being both came
    come could does done down each even ever every from gets getting going have
    here into just keep know less like made make many more most much must never
    next nothing once only other over same should some still such than that their
    them then there these they thing things this those through today very want
    were what when where which while will with would your yours
""".split())


def load() -> dict[str, Any]:
    if MEMORY_PATH.exists():
        return json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    return {}


def save(mem: dict[str, Any]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    MEMORY_PATH.write_text(
        json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _entry(mem: dict[str, Any], key: str) -> dict[str, Any]:
    return mem.get(key) or {"last_posted": None, "recent_quotes": [], "post_count": 0}


def days_since_last(mem: dict[str, Any], key: str, today: date) -> int | None:
    """Calendar days between last post and `today`. None if never posted."""
    e = mem.get(key)
    if not e or not e.get("last_posted"):
        return None
    last = datetime.fromisoformat(e["last_posted"]).date()
    return (today - last).days


def recent_quotes(mem: dict[str, Any], key: str) -> list[str]:
    return _entry(mem, key).get("recent_quotes", [])


def recent_authored(mem: dict[str, Any], key: str) -> list[str]:
    """The LLM-written part of each recent post: the framing or remix for a bank
    post, the whole text otherwise (including old string-only entries)."""
    authored = {
        b["posted"]: b.get("framing") or b.get("remix") or ""
        for b in _entry(mem, key).get("recent_bank", [])
        if b.get("posted")
    }
    return [authored.get(q, q) for q in recent_quotes(mem, key)]


def overused_words(
    mem: dict[str, Any], key: str, ignore: set[str] | None = None
) -> list[str]:
    """Words of 4+ letters that show up in OVERUSED_MIN_POSTS or more distinct
    recent posts (LLM-authored text only), most frequent first, capped. A plural
    folds into its singular when both appear ("wounds" counts as "wound")."""
    ignore = ignore or set()
    posts = []
    for text in recent_authored(mem, key):
        text = re.sub(r"[*_~`>#]", " ", text.lower().replace("’", "'"))
        posts.append({w for w in re.findall(r"[a-z]+(?:'[a-z]+)*", text)})

    vocab = set().union(*posts) if posts else set()
    counts: Counter[str] = Counter()
    for words in posts:
        kept = set()
        for w in words:
            if len(w) < 4 or "'" in w or w in STOPWORDS or w in ignore:
                continue
            if w.endswith("s") and not w.endswith("ss") and w[:-1] in vocab:
                w = w[:-1]
            kept.add(w)
        counts.update(kept)

    frequent = [(w, n) for w, n in counts.items() if n >= OVERUSED_MIN_POSTS]
    frequent.sort(key=lambda wn: (-wn[1], wn[0]))
    return [w for w, _ in frequent[:OVERUSED_CAP]]


def quote_usage(mem: dict[str, Any]) -> dict[str, str]:
    return mem.get("quote_usage", {})


def recent_premises(mem: dict[str, Any], n: int = 5) -> list[str]:
    """The last n interaction premises, oldest first."""
    return [p["premise"] for p in mem.get("recent_premises", [])[-n:] if p.get("premise")]


def recent_scenes(mem: dict[str, Any]) -> list[dict[str, Any]]:
    """All recent_premises entries, oldest first (seed/closer may be missing)."""
    return list(mem.get("recent_premises", []))


def record_premise(
    mem: dict[str, Any], day: str, premise: str, **details: str | None
) -> dict[str, Any]:
    """Record a completed interaction (day is "YYYY-MM-DD"). `details` are the
    structure choices (seed, closer, medium, time_of_day, slip_turn); missing
    ones are left out, as in entries written before they existed."""
    rp = list(mem.get("recent_premises", []))
    entry = {"date": day, "premise": premise}
    entry.update({k: v for k, v in details.items() if v is not None and v != ""})
    rp.append(entry)
    mem["recent_premises"] = rp[-RECENT_PREMISES_CAP:]
    return mem


def record_post(
    mem: dict[str, Any], key: str, quote: str, when: datetime
) -> dict[str, Any]:
    """Record a successful quote post. `when` is the actual fire time (aware)."""
    e = _entry(mem, key)
    e["last_posted"] = when.isoformat()
    rq = list(e.get("recent_quotes", []))
    rq.append(quote)
    e["recent_quotes"] = rq[-RECENT_QUOTES_CAP:]
    e["post_count"] = int(e.get("post_count", 0)) + 1
    mem[key] = e
    return mem


def record_bank_post(
    mem: dict[str, Any], key: str, record: dict[str, Any], posted: str, when: datetime
) -> dict[str, Any]:
    """Alongside record_post for a bank-based post: store the structured record
    ({"quote_id", "framing"|"remix"}) and mark the quote used, shared across the
    cast, so the cooldown applies to everyone."""
    e = _entry(mem, key)
    rb = list(e.get("recent_bank", []))
    rb.append({**record, "posted": posted})
    e["recent_bank"] = rb[-RECENT_QUOTES_CAP:]
    mem[key] = e
    mem.setdefault("quote_usage", {})[record["quote_id"]] = when.date().isoformat()
    return mem


def record_appearance(
    mem: dict[str, Any], key: str, when: datetime
) -> dict[str, Any]:
    """Record an interaction appearance: counts toward last_posted/post_count for
    anti-clustering and the days-since fact, but is NOT added to recent_quotes —
    conversational lines aren't quotes and shouldn't suppress future quotes."""
    e = _entry(mem, key)
    e["last_posted"] = when.isoformat()
    e["post_count"] = int(e.get("post_count", 0)) + 1
    mem[key] = e
    return mem
