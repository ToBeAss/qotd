"""preview.py — see the system work right now, without waiting for the scheduler.

Exercises the real OpenAI API on demand. Prints by default; pass --post to
actually send to Discord. Examples:

  python preview.py quote                 # a real Dealer quote, printed
  python preview.py quote --persona plug  # force a persona
  python preview.py quote --persona postman --post   # and send it

  python preview.py interaction           # direct + generate a full scene, printed
  python preview.py interaction --post --fast   # post it, skip the real delays

  python preview.py pipeline              # run planner + dispatch with everything
                                          #   forced due now (true end-to-end)
  python preview.py pipeline --interaction --dry   # force an interaction, mock sends

Only --post (and pipeline without --dry) needs webhooks. Plain previews need just
OPENAI_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(override=True)

import dispatch
import llm
import memory
import planner
import storyteller
from registry import load_registry

STATE_DIR = Path(__file__).resolve().parent / "state"


def _now(reg):
    return datetime.now(ZoneInfo(reg.location.timezone))


# --- quote --------------------------------------------------------------------
def cmd_quote(args) -> None:
    reg = load_registry()
    key = args.persona or "dealer"
    ch = reg[key]
    now = _now(reg)
    mem = memory.load()

    facts = {
        "date_human": f"{now:%A}, {now.day} {now:%B %Y}",
        "time": now.strftime("%H:%M"),
        "sunset": "—",
        "light": "unknown",
        "days_since_last": memory.days_since_last(mem, key, now.date()),
        "hour_unusual": False,
    }
    block = dispatch.render_context_block(facts, memory.recent_quotes(mem, key))

    print(f"--- {ch.name} ---")
    quote = llm.generate_from_prompt(ch.prompt, block)
    print(quote)

    if args.remember:
        memory.record_post(mem, key, quote, now)
        memory.save(mem)
        print("\n[remembered — run again to see anti-repetition push off this]")

    if args.post:
        if not ch.webhook:
            print(f"\n[!] {ch.webhook_env} not set; cannot post")
            return
        ok = dispatch.post_to_discord(ch.webhook, quote)
        print(f"\n[posted: {ok}]")


# --- interaction --------------------------------------------------------------
def cmd_interaction(args) -> None:
    reg = load_registry()
    now = _now(reg)
    weekday = now.strftime("%A")

    if args.cast:
        eligible = [k.strip() for k in args.cast.split(",") if k.strip()]
        bad = [k for k in eligible if k not in reg.keys()]
        if bad:
            print(f"[!] unknown character(s): {bad}; valid: {list(reg.keys())}")
            return
    else:
        eligible = [ch.key for ch in reg if ch.day_weight(now.weekday()) > 0]
        if len(eligible) < 2:
            eligible = [ch.key for ch in reg]  # ignore day rules for a forced preview

    scene = storyteller.direct(eligible, weekday)
    lines = storyteller.generate_lines(reg, scene["scene"], scene["medium"], scene["turns"])
    storyteller.assign_delays(lines, scene["medium"], random.Random())

    print(f"--- scene [{scene['medium']} / {scene['time_of_day']}] ---")
    print(f"(narrator) {scene['scene']}\n")
    for ln in lines:
        print(f"{reg[ln['character']].name}: {ln['line']}   (+{ln['delay_after']}s)")

    if args.post:
        print("\n[posting...]")
        if reg.storyteller_webhook:
            dispatch.post_to_discord(reg.storyteller_webhook, scene["scene"])
            time.sleep(0 if args.fast else (3 if scene["medium"] == "irl" else 10))
        else:
            print("[STORYTELLER_WEBHOOK not set; scene not posted]")
        for i, ln in enumerate(lines):
            ch = reg[ln["character"]]
            if not ch.webhook:
                print(f"[!] {ch.webhook_env} not set; skipping")
                continue
            dispatch.post_to_discord(ch.webhook, ln["line"])
            if i != len(lines) - 1:
                time.sleep(0 if args.fast else ln["delay_after"])
        print("[done]")


# --- pipeline (true end-to-end) ----------------------------------------------
def cmd_pipeline(args) -> None:
    reg = load_registry()
    tz = ZoneInfo(reg.location.timezone)
    now = datetime.now(tz)
    today = date.fromisoformat(args.date) if args.date else now.date()
    rng = random.Random()

    if args.interaction:
        eligible = [ch.key for ch in reg if ch.day_weight(today.weekday()) > 0]
        if len(eligible) < 2:
            eligible = [ch.key for ch in reg]
        plan = planner.build_interaction_plan(
            reg, today, tz, rng, eligible,
            planner._hhmm(reg.quiet_start), planner._hhmm(reg.quiet_end),
        )
    else:
        plan = planner.build_plan(reg, today, tz, rng)

    # Force everything due right now.
    if plan["kind"] == "interaction":
        plan["start_at"] = now.isoformat()
    else:
        for e in plan["entries"]:
            e["fire_at"] = now.isoformat()
            if "facts" in e:  # keep the previewed context block coherent
                e["facts"]["time"] = now.strftime("%H:%M")

    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / f"plan-{today.isoformat()}.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[plan kind={plan['kind']}, {len(plan['entries'])} entr{'y' if len(plan['entries'])==1 else 'ies'}, forced due now]")

    if args.dry:
        # Make webhooks look present so the real guards pass, then capture instead
        # of sending. setdefault leaves any real webhooks alone (the mock still
        # intercepts the send, so nothing actually goes out).
        for ch in reg:
            os.environ.setdefault(ch.webhook_env, "dry")
        if reg.storyteller_webhook_env:
            os.environ.setdefault(reg.storyteller_webhook_env, "dry")
        captured = []
        dispatch.post_to_discord = lambda w, c: (captured.append(c) or True)
        dispatch.run()
        print("--- would post (dry) ---")
        for c in captured:
            print(" ->", c)
    else:
        dispatch.run()
        print("[dispatch ran — check your Discord channel(s)]")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("quote", help="generate one quote")
    q.add_argument("--persona", choices=["dealer", "plug", "postman"])
    q.add_argument("--post", action="store_true", help="send to Discord")
    q.add_argument("--remember", action="store_true", help="record to memory (watch anti-repetition across runs)")
    q.set_defaults(func=cmd_quote)

    i = sub.add_parser("interaction", help="direct + generate a full scene")
    i.add_argument("--cast", help="comma-separated keys to force, e.g. postman,dealer")
    i.add_argument("--post", action="store_true", help="send to Discord")
    i.add_argument("--fast", action="store_true", help="skip the real inter-line delays when posting")
    i.set_defaults(func=cmd_interaction)

    pl = sub.add_parser("pipeline", help="planner + dispatch, everything forced due now")
    pl.add_argument("--interaction", action="store_true", help="force an interaction day")
    pl.add_argument("--date", help="plan for a specific day (YYYY-MM-DD), e.g. a weekday")
    pl.add_argument("--dry", action="store_true", help="mock sends, print what would post")
    pl.set_defaults(func=cmd_pipeline)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()