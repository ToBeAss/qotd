"""Quote bank — real quotes with genuine attribution, in data/quotes.jsonl.

Code owns real-quote text and attribution; the LLM never writes a real quote
from memory. One JSON object per line:

    {"id": "seneca-brevity-01", "text": "...", "author": "Seneca", "source": "..."}

id, text and author are required; source is optional. Candidates live in
data/quotes.candidates.jsonl until reviewed — moving a line into the bank is the
review gate. Usage (for the cooldown) is memory["quote_usage"], shared across
characters and recorded only on a successful post.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import obs


@dataclass(frozen=True)
class Quote:
    id: str
    text: str
    author: str
    source: str | None = None


def load_bank(path: Path | None) -> dict[str, Quote] | None:
    """Parse the bank at use time. Returns {id: Quote}, or None when there's no
    usable bank (missing file or no valid lines) — callers then fall back to
    original mode. Invalid lines are skipped with a single report per load."""
    if path is None or not path.exists():
        obs.report_error(f"quote bank not found: {path}")
        return None

    bank: dict[str, Quote] = {}
    skipped: list[str] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            q = Quote(
                id=str(obj["id"]).strip(),
                text=str(obj["text"]).strip(),
                author=str(obj["author"]).strip(),
                source=str(obj["source"]).strip() if obj.get("source") else None,
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            skipped.append(f"{n} (invalid)")
            continue
        if not (q.id and q.text and q.author):
            skipped.append(f"{n} (empty field)")
        elif q.id in bank:
            skipped.append(f"{n} (duplicate id {q.id!r})")
        else:
            bank[q.id] = q

    if skipped:
        obs.report_error(f"quote bank {path.name}: skipped line(s) {', '.join(skipped)}")
    if not bank:
        obs.report_error(f"quote bank {path.name} has no usable quotes")
        return None
    return bank


def select(
    bank: dict[str, Quote],
    usage: dict[str, str],
    today: date,
    cooldown_days: int,
    rng: random.Random,
    exclude: set[str] | None = None,
) -> Quote:
    """Uniform random among quotes not used within the cooldown. If none are
    eligible, the least-recently-used one (and a report). `exclude` keeps two
    entries in the same plan from drawing the same quote."""
    cutoff = today - timedelta(days=cooldown_days)
    exclude = exclude or set()
    pool = [q for q in bank.values() if q.id not in exclude] or list(bank.values())

    fresh = [
        q for q in pool
        if q.id not in usage or date.fromisoformat(usage[q.id]) <= cutoff
    ]
    if fresh:
        return rng.choice(fresh)

    obs.report_error("quote bank exhausted within cooldown")
    return min(pool, key=lambda q: usage[q.id])
