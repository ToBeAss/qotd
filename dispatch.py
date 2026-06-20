"""Dispatch — runs every 5 minutes.

Scans plan files for entries that are due (fire_at <= now) and unsent, renders
the context block from the frozen facts plus live recent-quotes, generates the
quote, posts it to the character's webhook, flips `sent`, and records the post to
memory. Idempotent: the `sent` flag is the whole contract, so a reboot mid-day
just resumes. The planned time is used verbatim — no clock recomputation.
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

import llm
import memory
import obs
from registry import STORYTELLER_KEY, load_registry

load_dotenv(override=True)  # override: dotenv cache can serve stale values

STATE_DIR = Path(__file__).resolve().parent / "state"
LOCK_PATH = STATE_DIR / "dispatch.lock"
INTERACTION_LOG = STATE_DIR / "interactions.log"

# Give up on an entry after this many failed attempts so a persistent failure
# (dead API, bad webhook) doesn't retry every 5 minutes all day.
MAX_ATTEMPTS = 3

log = obs.get_logger()
report_error = obs.report_error


# --- Context block ------------------------------------------------------------
def render_context_block(facts: dict, recent: list[str]) -> str:
    lines = [
        f"Today is {facts['date_human']}. Local time {facts['time']}. "
        f"Sunset {facts['sunset']} — {facts['light']}."
    ]
    dsl = facts.get("days_since_last")
    if dsl is None:
        lines.append("This is your first time posting.")
    else:
        lines.append(f"You last posted {dsl} day{'s' if dsl != 1 else ''} ago.")
    if facts.get("hour_unusual"):
        lines.append("This is an unusual hour for you to appear.")

    block = "<context>\n" + "\n".join(lines) + "\n</context>"

    if recent:
        listed = "\n".join(f"- {q}" for q in recent)
        block += (
            "\n\n<recent>\nYou've delivered these recently — don't echo them in "
            f"wording or idea:\n{listed}\n</recent>"
        )
    return block


# --- Discord ------------------------------------------------------------------
def post_to_discord(webhook: str, content: str) -> bool:
    try:
        resp = requests.post(webhook, json={"content": content}, timeout=10)
    except requests.exceptions.RequestException as exc:
        report_error(f"discord request failed: {exc}")
        return False
    if resp.status_code == 204:
        return True
    report_error(
        f"discord webhook rejected: status={resp.status_code} body={resp.text[:300]}"
    )
    return False


# --- Core loop ----------------------------------------------------------------
def _due_plan_files() -> list[Path]:
    return sorted(STATE_DIR.glob("plan-*.json"))


def fire_entry(entry: dict, reg, mem: dict) -> tuple[bool, bool]:
    """Generate + post one due entry.

    Returns (settled, mem_changed). `settled` means the entry should be marked
    sent — either it posted, or it exhausted its attempts.
    """
    key = entry["character"]
    ch = reg.characters.get(key)
    if ch is None:
        report_error(f"unknown character in plan: {key!r}; skipping")
        return True, False  # never resolvable — settle it

    if not ch.webhook:
        report_error(f"{key}: {ch.webhook_env} not set; cannot post")
        entry["attempts"] = entry.get("attempts", 0) + 1
        return entry["attempts"] >= MAX_ATTEMPTS, False

    fire_at = datetime.fromisoformat(entry["fire_at"])
    block = render_context_block(entry["facts"], memory.recent_quotes(mem, key))

    try:
        quote = llm.generate_from_prompt(ch.prompt, block)
    except Exception as exc:
        report_error(f"{key}: generation failed: {exc}")
        entry["attempts"] = entry.get("attempts", 0) + 1
        return entry["attempts"] >= MAX_ATTEMPTS, False

    if not post_to_discord(ch.webhook, quote):
        entry["attempts"] = entry.get("attempts", 0) + 1
        return entry["attempts"] >= MAX_ATTEMPTS, False

    memory.record_post(mem, key, quote, fire_at)
    log.info("posted %s (fire_at=%s)", key, entry["fire_at"])
    return True, True


def _write_plan(path: Path, plan: dict) -> None:
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")


def _append_interaction_log(plan: dict, tz: ZoneInfo) -> None:
    """Append the completed exchange to the worldbuilding substrate (JSON lines)."""
    record = {
        "logged_at": datetime.now(tz).isoformat(timespec="seconds"),
        "date": plan["date"],
        "scene": plan.get("scene"),
        "medium": plan.get("medium"),
        "transcript": [
            {"character": e["character"], "line": e["line"]}
            for e in plan["entries"]
            if e["character"] != STORYTELLER_KEY
        ],
    }
    try:
        STATE_DIR.mkdir(exist_ok=True)
        with INTERACTION_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        report_error(f"could not append interaction log: {exc}")


def play_interaction(plan: dict, path: Path, reg, mem: dict, now: datetime, tz: ZoneInfo) -> bool:
    """Play out a due interaction within this one invocation: post each unsent
    line, checkpoint to disk after each (crash recovery), sleep the gap. Returns
    whether memory changed. The dispatch lock keeps a second tick from racing us.
    """
    if now < datetime.fromisoformat(plan["start_at"]):
        return False
    entries = plan["entries"]
    if all(e.get("sent") for e in entries):
        return False

    start = datetime.fromisoformat(plan["start_at"])
    mem_changed = False
    remaining = [e for e in entries if not e.get("sent")]

    for idx, entry in enumerate(remaining):
        is_last = idx == len(remaining) - 1

        # Narrator scene line — posted via the storyteller webhook if one is set,
        # otherwise quietly skipped (the narrator is optional).
        if entry["character"] == STORYTELLER_KEY:
            wh = reg.storyteller_webhook
            if not wh:
                entry["sent"] = True
                _write_plan(path, plan)
                continue
            if not post_to_discord(wh, entry["line"]):
                return mem_changed
            entry["sent"] = True
            _write_plan(path, plan)
            log.info("interaction %s posted scene", plan["date"])
            if not is_last:
                time.sleep(entry.get("delay_after", 0))
            continue

        ch = reg.characters.get(entry["character"])
        if ch is None or not ch.webhook:
            report_error(f"interaction line {entry['id']}: cannot post ({entry['character']})")
            entry["sent"] = True  # settle so it can't wedge the rest forever
            _write_plan(path, plan)
            continue

        if not post_to_discord(ch.webhook, entry["line"]):
            # leave unsent; the next tick resumes here (lock prevents overlap)
            return mem_changed

        entry["sent"] = True
        memory.record_appearance(mem, entry["character"], start)
        mem_changed = True
        _write_plan(path, plan)  # checkpoint: a crash now resumes at the next line
        log.info("interaction %s posted %s", plan["date"], entry["character"])

        if not is_last:
            time.sleep(entry.get("delay_after", 0))

    if all(e.get("sent") for e in entries):
        _append_interaction_log(plan, tz)
    return mem_changed


def run() -> None:
    reg = load_registry()
    tz = ZoneInfo(reg.location.timezone)
    now = datetime.now(tz)
    mem = memory.load()
    mem_changed = False

    for path in _due_plan_files():
        plan = json.loads(path.read_text(encoding="utf-8"))

        if plan.get("kind") == "interaction":
            mem_changed |= play_interaction(plan, path, reg, mem, now, tz)
            continue

        plan_changed = False
        for entry in plan["entries"]:
            if entry.get("sent"):
                continue
            if datetime.fromisoformat(entry["fire_at"]) > now:
                continue

            settled, changed = fire_entry(entry, reg, mem)
            mem_changed |= changed
            if settled:
                entry["sent"] = True
                plan_changed = True
            elif "attempts" in entry:
                plan_changed = True  # persist the bumped attempt counter

        if plan_changed:
            _write_plan(path, plan)

    if mem_changed:
        memory.save(mem)


@contextlib.contextmanager
def _dispatch_lock():
    """Exclusive lock so a long interaction playout can't be raced by the next
    cron tick. flock auto-releases on process death, so no stale locks. On
    platforms without fcntl (e.g. Windows dev), runs unlocked."""
    STATE_DIR.mkdir(exist_ok=True)
    try:
        import fcntl
    except ImportError:
        yield True
        return

    fd = open(LOCK_PATH, "w")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another dispatch holds the lock; skipping this tick")
            yield False
            return
        yield True
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def main() -> None:
    with _dispatch_lock() as acquired:
        if acquired:
            run()


if __name__ == "__main__":
    main()