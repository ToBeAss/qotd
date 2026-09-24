"""Dispatch — runs every minute.

Scans plan files for entries that are due (fire_at <= now) and unsent, renders
the context block from the frozen facts plus live recent-quotes, generates the
quote, posts it to the character's webhook, flips `sent`, and records the post to
memory. Idempotent: the `sent` flag is the whole contract, so a reboot mid-day
just resumes. The planned time is used verbatim — no clock recomputation.

An entry's `mode` (from the planner) says where the quote comes from: `original`
(the LLM writes it), `bank` (code inserts the bank quote, the LLM writes only the
delivery line) or `remix` (the LLM remixes the bank quote, code attributes it).
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

import llm
import memory
import obs
import quotes
from registry import STORYTELLER_KEY, load_registry

load_dotenv(override=True)  # override: dotenv cache can serve stale values

STATE_DIR = Path(__file__).resolve().parent / "state"
LOCK_PATH = STATE_DIR / "dispatch.lock"
INTERACTION_LOG = STATE_DIR / "interactions.log"

# Give up on an entry after this many failed attempts so a persistent failure
# (dead API, bad webhook) doesn't retry on every tick all day.
MAX_ATTEMPTS = 3

# The canonical voice. The others may mention covering for him only once he's
# been quiet for reg.cover_after_days.
CANONICAL_KEY = "dealer"

# A remix must stay within this length ratio of the original (characters).
REMIX_LEN_RATIO = (0.5, 2.5)

log = obs.get_logger()
report_error = obs.report_error


# --- Context block ------------------------------------------------------------
def render_context_block(
    facts: dict,
    recent: list[str],
    *,
    dealer_absent: bool | None = None,
    avoid: list[str] | None = None,
    task: str | None = None,
) -> str:
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
    if dealer_absent is True:
        lines.append(
            "The Dealer has gone quiet for a few days. You may mention covering for "
            "him if it fits — allowed, not required."
        )
    elif dealer_absent is False:
        lines.append(
            "The Dealer is around and posting as usual. Post as yourself: don't "
            "mention covering for him, standing in for him, or him being away."
        )

    block = "<context>\n" + "\n".join(lines) + "\n</context>"

    if recent:
        listed = "\n".join(f"- {q}" for q in recent)
        block += (
            "\n\n<recent>\nYou've delivered these recently — don't echo them in "
            f"wording or idea:\n{listed}\n</recent>"
        )
    if avoid:
        block += f"\n\nAvoid these words and images this time: {', '.join(avoid)}."
    if task:
        block += f"\n\n<task>\n{task}\n</task>"
    return block


def render_task(mode: str, quote: quotes.Quote | None) -> str:
    """The per-post instruction for a bank-sourced character, naming the mode
    his prompt file describes."""
    if mode == "bank":
        return (
            "Mode: delivery. Today's letter is a real quote. Code prints it under your "
            "line, exactly as written:\n"
            f'"{quote.text}" — {_attribution(quote)}\n'
            "Write only your one-line delivery framing. Don't quote, restate or "
            "rewrite it."
        )
    if mode == "remix":
        return (
            f'Mode: remix. The original:\n"{quote.text}" — {quote.author}\n'
            "Return only your remixed line."
        )
    return "Mode: original. Write your own, in your original-mode format."


# --- Rendering ----------------------------------------------------------------
def _attribution(quote: quotes.Quote) -> str:
    return f"{quote.author}, *{quote.source}*" if quote.source else quote.author


def render_bank_post(framing: str, quote: quotes.Quote) -> str:
    return f'*{framing}*\n**"{quote.text}"**\n— {_attribution(quote)}'


def render_remix_post(remix: str, quote: quotes.Quote) -> str:
    return f'**"{remix}"**\n— {quote.author} (remix)'


def _one_line(raw: str, quote: quotes.Quote) -> str:
    """Reduce a single-line reply to the bare line: first non-empty line, no
    markdown or wrapping quotes, and no attribution the model added itself."""
    line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    surname = quote.author.split()[-1].lower()
    tail = re.search(r"\s+[—–-]{1,2}\s*([^—–-]+)$", line)
    if tail and surname in tail.group(1).lower():
        line = line[: tail.start()]
    return line.strip().strip("*_").strip().strip('"“”').strip()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def remix_ok(remix: str, original: str) -> bool:
    if not remix or _norm(remix) == _norm(original):
        return False
    lo, hi = REMIX_LEN_RATIO
    return lo <= len(remix) / len(original) <= hi


# --- Generation ---------------------------------------------------------------
def dealer_absent(reg, mem: dict, day) -> bool:
    dsl = memory.days_since_last(mem, CANONICAL_KEY, day)
    return dsl is None or dsl >= reg.cover_after_days


def _bank_quote(reg, quote_id: str | None) -> quotes.Quote | None:
    bank = quotes.load_bank(reg.quote_bank)  # reports its own failures
    if bank is None:
        return None
    if quote_id not in bank:
        report_error(f"quote {quote_id!r} not in bank; falling back to original")
        return None
    return bank[quote_id]


def generate_post(
    entry: dict, ch, reg, mem: dict, when: datetime, *, absent: bool | None = None
) -> tuple[str, dict | None]:
    """Generate one entry's post. Returns (content, bank_record), where
    bank_record is {"quote_id", "framing"|"remix"} for a bank-based post and None
    otherwise. `absent` forces the dealer_absent flag (preview). Raises on
    generation failure."""
    mode = entry.get("mode", "original")
    quote = None
    if mode in ("bank", "remix"):
        quote = _bank_quote(reg, entry.get("quote_id"))
        if quote is None:
            mode = "original"

    # The Dealer never gets the flag; everyone else always does.
    if ch.key == CANONICAL_KEY or CANONICAL_KEY not in reg.characters:
        absent = None
    elif absent is None:
        absent = dealer_absent(reg, mem, when.date())
    names = {w for c in reg for w in c.name.lower().split()}

    def prompt(task_mode: str | None) -> str:
        return render_context_block(
            entry["facts"],
            memory.recent_quotes(mem, ch.key),
            dealer_absent=absent,
            avoid=memory.overused_words(mem, ch.key, names),
            task=render_task(task_mode, quote) if task_mode else None,
        )

    if mode == "bank":
        framing = _one_line(llm.generate_from_prompt(ch.prompt, prompt("bank")), quote)
        if not framing:
            raise llm.LLMError("empty delivery line")
        return render_bank_post(framing, quote), {"quote_id": quote.id, "framing": framing}

    if mode == "remix":
        block = prompt("remix")
        for _ in range(2):  # one retry, then fall back to his own original
            remix = _one_line(llm.generate_from_prompt(ch.prompt, block), quote)
            if remix_ok(remix, quote.text):
                return render_remix_post(remix, quote), {"quote_id": quote.id, "remix": remix}
            log.info("%s: remix failed checks: %r", ch.key, remix)
        log.warning("%s: remix of %s failed twice; posting an original", ch.key, quote.id)

    # Single-mode characters (the Dealer) get no task block at all.
    task_mode = "original" if ch.quote_source != "original" else None
    return llm.generate_from_prompt(ch.prompt, prompt(task_mode)), None


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

    try:
        quote, bank_record = generate_post(entry, ch, reg, mem, fire_at)
    except Exception as exc:
        report_error(f"{key}: generation failed: {exc}")
        entry["attempts"] = entry.get("attempts", 0) + 1
        return entry["attempts"] >= MAX_ATTEMPTS, False

    if not post_to_discord(ch.webhook, quote):
        entry["attempts"] = entry.get("attempts", 0) + 1
        return entry["attempts"] >= MAX_ATTEMPTS, False

    memory.record_post(mem, key, quote, fire_at)
    if bank_record:
        memory.record_bank_post(mem, key, bank_record, quote, fire_at)
    log.info("posted %s (fire_at=%s, mode=%s)", key, entry["fire_at"], entry.get("mode", "original"))
    return True, True


def _write_plan(path: Path, plan: dict) -> None:
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")


def _append_interaction_log(plan: dict, tz: ZoneInfo) -> None:
    """Append the completed exchange to the worldbuilding substrate (JSON lines)."""
    record = {
        "logged_at": datetime.now(tz).isoformat(timespec="seconds"),
        "date": plan["date"],
        "scene": plan.get("scene"),
        "premise": plan.get("premise"),
        "beats": plan.get("beats"),
        "from_material": plan.get("from_material"),
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
        if plan.get("premise"):  # older plans have none
            memory.record_premise(mem, plan["date"], plan["premise"])
            mem_changed = True
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