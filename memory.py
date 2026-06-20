"""Per-character memory — a single state/memory.json.

Holds last_posted (for the days-since context fact and anti-clustering),
recent_quotes (anti-repetition), and post_count (worldbuilding seed). Planner
reads it; dispatch writes it on a successful post.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).resolve().parent / "state"
MEMORY_PATH = STATE_DIR / "memory.json"

RECENT_QUOTES_CAP = 15


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
