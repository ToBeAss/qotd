"""preview.py — see the system work right now, without waiting for the scheduler.

Exercises the real OpenAI API on demand. Prints by default; pass --post to
actually send to Discord. Examples:

  python preview.py quote                 # a real Dealer quote, printed
  python preview.py quote --persona plug  # force a persona
  python preview.py quote --persona postman --post   # and send it
  python preview.py quote --persona plug --mode remix --quote-id seneca-brevity-01
  python preview.py quote --persona postman --dealer-absent   # allow covering
  python preview.py quote --model gpt-5.4-mini --effort low   # A/B a model

  python preview.py interaction           # direct + generate a full scene, printed
  python preview.py interaction --material      # force a scene built from recent posts
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
import quotes
import storyteller
from registry import STORYTELLER_KEY, load_registry

STATE_DIR = Path(__file__).resolve().parent / "state"


def _now(reg):
    return datetime.now(ZoneInfo(reg.location.timezone))


# --- quote --------------------------------------------------------------------
def cmd_quote(args) -> None:
    reg = load_registry()
    llm.configure(model=args.model, reasoning_effort=args.effort)
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
    # Same entry the planner would write, then any forced overrides.
    entry = {"character": key, "facts": facts}
    planner.assign_quotes(reg, [entry], mem, now.date(), random.Random())
    if args.mode:
        entry["mode"] = args.mode
        if args.mode == "original":
            entry.pop("quote_id", None)
    if args.quote_id:
        entry["quote_id"] = args.quote_id
        if not args.mode:  # an explicit quote means this character's bank mode
            entry["mode"] = "remix" if ch.quote_source == "remix" else "bank"
    if entry.get("mode") in ("bank", "remix") and not entry.get("quote_id"):
        bank = quotes.load_bank(reg.quote_bank)
        if bank:
            entry["quote_id"] = quotes.select(
                bank, memory.quote_usage(mem), now.date(), reg.quote_cooldown_days, random.Random()
            ).id

    print(f"--- {ch.name} [{entry.get('mode', 'original')}"
          f"{' ' + entry['quote_id'] if entry.get('quote_id') else ''}] ---")
    quote, bank_record = dispatch.generate_post(
        entry, ch, reg, mem, now, absent=True if args.dealer_absent else None
    )
    print(quote)

    if args.remember:
        memory.record_post(mem, key, quote, now)
        if bank_record:
            memory.record_bank_post(mem, key, bank_record, quote, now)
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
    if args.slip and storyteller.DEALER not in eligible:
        print(f"[!] --slip needs the Dealer in today's cast {eligible}")
        return
    if args.closer and args.closer not in eligible:
        print(f"[!] closer {args.closer!r} isn't in today's cast {eligible}")
        return

    # The planner's own path: real memory for the material block and recent
    # premises, and the material roll unless --material / --no-material forces it.
    mem = memory.load()
    plan = planner.build_interaction_plan(
        reg, now.date(), now.tzinfo, random.Random(), eligible,
        planner._hhmm(reg.quiet_start), planner._hhmm(reg.quiet_end),
        mem, from_material=args.material, closer=args.closer,
        medium=args.medium, time_of_day=args.time, slip=args.slip,
    )

    print(f"--- scene [{plan['medium']} / {plan['time_of_day']}] ---")
    print(f"premise:       {plan['premise']}")
    print(f"from_material: {plan['from_material']}")
    print(f"seed:          {plan['seed']}")
    print(f"closer:        {plan['closer']}")
    print(f"slip_turn:     {plan['slip_turn']}")
    lines = [e for e in plan["entries"] if e["character"] != STORYTELLER_KEY]
    for e, beat in zip(lines, plan["beats"]):
        print(f"  beat ({e['character']}): {beat}")
    print(f"\n(narrator) {plan['scene']}\n")
    for e in lines:
        print(f"{reg[e['character']].name}: {e['line']}   (+{e['delay_after']}s)")

    if args.remember:
        memory.record_premise(
            mem, plan["date"], plan["premise"], seed=plan["seed"], closer=plan["closer"],
            medium=plan["medium"], time_of_day=plan["time_of_day"], slip_turn=plan["slip_turn"],
        )
        memory.save(mem)
        print("\n[premise remembered — the director will avoid it next time]")

    if args.post:
        print("\n[posting...]")
        for i, e in enumerate(plan["entries"]):
            if e["character"] == STORYTELLER_KEY:
                wh = reg.storyteller_webhook
                if not wh:
                    print("[STORYTELLER_WEBHOOK not set; scene not posted]")
                    continue
            else:
                wh = reg[e["character"]].webhook
                if not wh:
                    print(f"[!] {reg[e['character']].webhook_env} not set; skipping")
                    continue
            dispatch.post_to_discord(wh, e["line"])
            if i != len(plan["entries"]) - 1:
                time.sleep(0 if args.fast else e["delay_after"])
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
    q.add_argument("--remember", action="store_true", help="record to memory, incl. quote_usage (watch anti-repetition across runs)")
    q.add_argument("--quote-id", help="use this bank quote (bank/remix modes)")
    q.add_argument("--mode", choices=["remix", "original"], help="force the Plug's mode")
    q.add_argument("--dealer-absent", action="store_true", help="force the Dealer-absent flag on")
    q.add_argument("--model", help="override the registry model for this run")
    q.add_argument("--effort", help="override reasoning effort for this run (e.g. low, high)")
    q.set_defaults(func=cmd_quote)

    i = sub.add_parser("interaction", help="direct + generate a full scene")
    i.add_argument("--cast", help="comma-separated keys to force, e.g. postman,dealer")
    i.add_argument("--post", action="store_true", help="send to Discord")
    i.add_argument("--fast", action="store_true", help="skip the real inter-line delays when posting")
    i.add_argument("--material", dest="material", action="store_true", default=None,
                   help="force a scene built from recent posts (default: the normal roll)")
    i.add_argument("--no-material", dest="material", action="store_false",
                   help="force a fresh office situation (material as background only)")
    i.add_argument("--closer", help="force who speaks the last line (must be in the cast)")
    i.add_argument("--medium", choices=sorted(storyteller.MEDIA), help="force the medium")
    i.add_argument("--slip", dest="slip", action="store_true", default=None,
                   help="force a Dealer act slip (default: the normal roll)")
    i.add_argument("--no-slip", dest="slip", action="store_false",
                   help="keep the Dealer in his act throughout")
    i.add_argument("--time", choices=["morning", "lunch", "afternoon", "evening", "night"],
                   help="force the time of day")
    i.add_argument("--remember", action="store_true", help="record the premise to memory")
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