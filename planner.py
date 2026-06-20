"""Planner — runs once at 07:00 (before the 07:30 window opens).

Decides the whole day: who posts, at what (random) time inside their own
dusk-aware distribution and the global quiet window, and freezes the context
facts each post will riff on. Writes state/plan-YYYY-MM-DD.json. Generates no
quote text and contacts nothing — pure decision. Dispatch does the talking.

The plan is owned by the planning day even when a post fires after midnight: an
hour-0 entry gets a fire_at on the following calendar date but lives in today's
plan file.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from registry import Registry, Character, STORYTELLER_KEY, load_registry
import memory
import obs
import storyteller

STATE_DIR = Path(__file__).resolve().parent / "state"

# --- Tunables -----------------------------------------------------------------
# Stage-2 bonus probability per eligible non-guaranteed character. ~0.165 gives
# an expected ~1.33 messages/day on weekdays (3 eligible), lighter on weekends.
P_BONUS = 0.165

# Anti-clustering: days_since_last -> weight multiplier (default 1.0). Posted
# today (a straddle post) or yesterday gets damped so the rotation doesn't streak.
RECENCY_PENALTY: dict[int, float] = {0: 0.25, 1: 0.5}

# An hour counts as "unusual" for a character if it sits in the rarest 15% of
# that character's probability mass — licenses a "didn't expect me this early".
UNUSUAL_PCT = 0.15

# Fallback interaction chance if registry.yaml omits interaction_chance. The live
# value comes from the registry (reg.interaction_chance).
INTERACTION_CHANCE = 0.07

# Time-of-day labels (from the storyteller) -> candidate start-hour ranges. Each
# is clamped to the global quiet window before sampling.
INTERACTION_WINDOWS: dict[str, tuple[int, int]] = {
    "morning": (8, 11),
    "lunch": (11, 13),
    "afternoon": (13, 17),
    "evening": (18, 22),
    "night": (22, 24),
}

# Dealer evening hours (17–23) before civil dusk are damped to this floor weight
# — he can still surface pre-dark, but rarely. At/after dusk they keep full mass.
DEALER_PREDUSK_FLOOR = 1

# Plan files older than this are pruned each run. Dispatch only needs today +
# yesterday (the after-midnight straddle); the rest is audit buffer.
PLAN_RETENTION_DAYS = 7


# --- Sun ----------------------------------------------------------------------
def sun_for(reg: Registry, day: date, tz: ZoneInfo) -> tuple[datetime | None, float]:
    """Return (sunset_dt, dusk_hour_float). Falls back gracefully near the
    solstice where civil dusk may not resolve at this latitude."""
    try:
        from astral import LocationInfo
        from astral.sun import sun

        loc = reg.location
        li = LocationInfo("loc", "", loc.timezone, loc.lat, loc.lon)
        s = sun(li.observer, date=day, tzinfo=tz)
        sunset = s["sunset"]
        dusk = s.get("dusk", sunset)
        dusk_hour = dusk.hour + dusk.minute / 60.0
        return sunset, dusk_hour
    except Exception:
        # No resolvable dusk (white-night edge) or astral missing: assume late.
        return None, 23.0


# --- Hour distributions -------------------------------------------------------
def character_hours(ch: Character, dusk_hour: float) -> dict[int, int]:
    """The character's effective hour weights for today. Only the Dealer is
    dusk-adjusted; everyone else uses their static shape."""
    if ch.key != "dealer":
        return dict(ch.hours)
    adjusted: dict[int, int] = {}
    for hour, weight in ch.hours.items():
        if 17 <= hour <= 23 and hour < dusk_hour:
            adjusted[hour] = DEALER_PREDUSK_FLOOR  # not dark yet — rare
        else:
            adjusted[hour] = weight
    return adjusted


def unusual_hours(weights: dict[int, int]) -> set[int]:
    """The rarest hours whose combined probability mass is <= UNUSUAL_PCT."""
    total = sum(weights.values())
    if total <= 0:
        return set()
    marked: set[int] = set()
    acc = 0.0
    for hour in sorted(weights, key=lambda h: weights[h]):  # rarest first
        acc += weights[hour] / total
        if acc <= UNUSUAL_PCT:
            marked.add(hour)
        else:
            break
    return marked


# --- Quiet window -------------------------------------------------------------
def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def hour_allowed(hour: int, qs: tuple[int, int], qe: tuple[int, int]) -> bool:
    """Window runs qs..23 then 0..qe (qe is the next day past midnight)."""
    return hour >= qs[0] or hour <= qe[0]


def minute_bounds(hour: int, qs: tuple[int, int], qe: tuple[int, int]) -> tuple[int, int]:
    lo, hi = 0, 59
    if hour == qs[0]:
        lo = qs[1]
    if hour == qe[0]:
        hi = qe[1]
    return lo, hi


# --- Selection ----------------------------------------------------------------
def recency_factor(mem: dict, key: str, today: date) -> float:
    dsl = memory.days_since_last(mem, key, today)
    if dsl is None:
        return 1.0
    return RECENCY_PENALTY.get(dsl, 1.0)


def effective_weight(ch: Character, weekday: int, mem: dict, today: date) -> float:
    base = ch.day_weight(weekday)
    if base <= 0:
        return 0.0
    return base * recency_factor(mem, ch.key, today)


def select_characters(
    reg: Registry, weekday: int, mem: dict, today: date, rng: random.Random
) -> list[str]:
    eligible = [ch for ch in reg if ch.day_weight(weekday) > 0]
    if not eligible:
        return []

    # Stage 1 — one guaranteed post (the floor of 1/day).
    weights = [effective_weight(ch, weekday, mem, today) for ch in eligible]
    guaranteed = rng.choices(eligible, weights=weights, k=1)[0]
    chosen = [guaranteed.key]

    # Stage 2 — each other eligible character rolls an independent bonus.
    for ch in eligible:
        if ch.key == guaranteed.key:
            continue
        p = P_BONUS * recency_factor(mem, ch.key, today)
        if rng.random() < p:
            chosen.append(ch.key)

    return chosen


# --- Timestamp ----------------------------------------------------------------
def sample_fire_time(
    ch: Character,
    today: date,
    tz: ZoneInfo,
    hours: dict[int, int],
    qs: tuple[int, int],
    qe: tuple[int, int],
    rng: random.Random,
) -> tuple[datetime, int]:
    """Pick an allowed hour weighted by the character's distribution, a uniform
    minute inside that hour's allowed sub-range, and resolve the fire datetime
    (next calendar day for the post-midnight tail). Returns (fire_at, hour)."""
    allowed = {h: w for h, w in hours.items() if hour_allowed(h, qs, qe) and w > 0}
    if not allowed:  # registry shouldn't allow this, but never crash the planner
        allowed = {qs[0]: 1}

    hour = rng.choices(list(allowed), weights=list(allowed.values()), k=1)[0]
    lo, hi = minute_bounds(hour, qs, qe)
    minute = rng.randint(lo, hi)

    fire_date = today + timedelta(days=1) if hour <= qe[0] else today
    fire_at = datetime.combine(fire_date, time(hour, minute), tzinfo=tz)
    return fire_at, hour


# --- Facts --------------------------------------------------------------------
def freeze_facts(
    ch: Character,
    fire_at: datetime,
    hour: int,
    sunset: datetime | None,
    rare_hours: set[int],
    mem: dict,
    today: date,
) -> dict:
    if sunset is not None:
        sunset_str = sunset.strftime("%H:%M")
        light = "after dark" if fire_at >= sunset else "still light"
    else:
        sunset_str = "—"
        light = "unknown"

    return {
        "date_human": f"{today:%A}, {today.day} {today:%B %Y}",
        "weekday": today.strftime("%A"),
        "time": fire_at.strftime("%H:%M"),
        "sunset": sunset_str,
        "light": light,
        "days_since_last": memory.days_since_last(mem, ch.key, today),
        "hour_unusual": hour in rare_hours,
    }


# --- Build --------------------------------------------------------------------
def build_plan(reg: Registry, today: date, tz: ZoneInfo, rng: random.Random) -> dict:
    mem = memory.load()
    weekday = today.weekday()  # Mon=0
    qs, qe = _hhmm(reg.quiet_start), _hhmm(reg.quiet_end)
    sunset, dusk_hour = sun_for(reg, today, tz)

    # Rare interaction day: the cast talks instead of posting a quote. If the
    # storyteller or any line fails, fall back to a normal plan so the day isn't lost.
    if reg.interaction_chance > 0 and rng.random() < reg.interaction_chance:
        eligible = [ch.key for ch in reg if ch.day_weight(weekday) > 0]
        if len(eligible) >= 2:
            try:
                return build_interaction_plan(reg, today, tz, rng, eligible, qs, qe)
            except Exception as exc:
                obs.get_logger().warning(
                    "interaction planning failed, falling back to normal: %s", exc
                )

    chosen = select_characters(reg, weekday, mem, today, rng)

    entries = []
    for key in chosen:
        ch = reg[key]
        hours = character_hours(ch, dusk_hour)
        rare = unusual_hours(hours)
        fire_at, hour = sample_fire_time(ch, today, tz, hours, qs, qe, rng)
        entries.append({
            "id": key,
            "character": key,
            "kind": "quote",
            "fire_at": fire_at.isoformat(),
            "sent": False,
            "facts": freeze_facts(ch, fire_at, hour, sunset, rare, mem, today),
        })

    entries.sort(key=lambda e: e["fire_at"])
    return {
        "date": today.isoformat(),
        "weekday": today.strftime("%A"),
        "kind": "normal",
        "generated_at": datetime.now(tz).isoformat(timespec="seconds"),
        "entries": entries,
    }


def sample_interaction_start(
    time_of_day: str,
    today: date,
    tz: ZoneInfo,
    qs: tuple[int, int],
    qe: tuple[int, int],
    rng: random.Random,
) -> datetime:
    """Pick a concrete start time inside the storyteller's time-of-day window,
    clamped to the global quiet window."""
    lo, hi = INTERACTION_WINDOWS.get(time_of_day, (12, 14))
    candidates = [h for h in range(lo, hi) if hour_allowed(h, qs, qe)]
    if not candidates:
        candidates = [max(qs[0] + 1, 12)]
    hour = rng.choice(candidates)
    m_lo, m_hi = minute_bounds(hour, qs, qe)
    minute = rng.randint(m_lo, m_hi)
    return datetime.combine(today, time(hour, minute), tzinfo=tz)


def build_interaction_plan(
    reg: Registry,
    today: date,
    tz: ZoneInfo,
    rng: random.Random,
    eligible: list[str],
    qs: tuple[int, int],
    qe: tuple[int, int],
) -> dict:
    """Direct a scene, generate all lines sequentially, freeze them with delays.
    Raises on any failure so the caller can fall back to a normal plan."""
    scene = storyteller.direct(eligible, today.strftime("%A"), model=reg.interaction_model)
    lines = storyteller.generate_lines(
        reg, scene["scene"], scene["medium"], scene["turns"], model=reg.interaction_model
    )
    storyteller.assign_delays(lines, scene["medium"], rng)

    start_at = sample_interaction_start(scene["time_of_day"], today, tz, qs, qe, rng)

    # Narrator scene line goes first (posted via the storyteller webhook, if set),
    # then a short beat, then the dialogue.
    scene_gap = rng.randint(2, 5) if scene["medium"] == "irl" else rng.randint(8, 18)
    entries = [{
        "id": "scene",
        "character": STORYTELLER_KEY,
        "line": scene["scene"],
        "delay_after": scene_gap,
        "sent": False,
    }]
    entries += [
        {
            "id": f"int-{i}",
            "character": ln["character"],
            "line": ln["line"],
            "delay_after": ln["delay_after"],
            "sent": False,
        }
        for i, ln in enumerate(lines)
    ]
    return {
        "date": today.isoformat(),
        "weekday": today.strftime("%A"),
        "kind": "interaction",
        "generated_at": datetime.now(tz).isoformat(timespec="seconds"),
        "scene": scene["scene"],
        "medium": scene["medium"],
        "time_of_day": scene["time_of_day"],
        "start_at": start_at.isoformat(),
        "entries": entries,
    }


def plan_path(day: date) -> Path:
    return STATE_DIR / f"plan-{day.isoformat()}.json"


def write_plan(plan: dict, day: date) -> Path:
    STATE_DIR.mkdir(exist_ok=True)
    path = plan_path(day)
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def prune_old_plans(today: date, keep_days: int = PLAN_RETENTION_DAYS) -> int:
    """Delete plan-*.json older than keep_days. Returns count removed."""
    cutoff = today - timedelta(days=keep_days)
    removed = 0
    for f in STATE_DIR.glob("plan-*.json"):
        try:
            d = date.fromisoformat(f.stem.removeprefix("plan-"))
        except ValueError:
            continue
        if d < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan today's QOTD posts.")
    parser.add_argument("--date", help="Override planning day (YYYY-MM-DD), for testing.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing plan.")
    parser.add_argument("--seed", type=int, help="RNG seed, for reproducible testing.")
    parser.add_argument("--print", action="store_true", help="Print the plan to stdout.")
    args = parser.parse_args()

    reg = load_registry()
    tz = ZoneInfo(reg.location.timezone)
    today = date.fromisoformat(args.date) if args.date else datetime.now(tz).date()
    rng = random.Random(args.seed) if args.seed is not None else random.Random()

    path = plan_path(today)
    if path.exists() and not args.force:
        print(f"[planner] plan already exists for {today} (use --force to overwrite): {path}")
        return

    plan = build_plan(reg, today, tz, rng)
    write_plan(plan, today)
    pruned = prune_old_plans(today)

    if plan["kind"] == "interaction":
        cast = " → ".join(
            e["character"] for e in plan["entries"] if e["character"] != STORYTELLER_KEY
        )
        summary = f"INTERACTION @{plan['start_at'][11:16]} [{plan['medium']}]: {cast}"
    else:
        summary = ", ".join(
            f"{e['character']}@{e['fire_at'][11:16]}"
            + ("!" if e["facts"]["hour_unusual"] else "")
            for e in plan["entries"]
        ) or "(no posts)"
    print(f"[planner] {today} ({plan['weekday']}): {summary}")
    if pruned:
        print(f"[planner] pruned {pruned} old plan file(s)")

    if args.print:
        print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()