"""Storyteller — the interaction director.

Two jobs, both at plan time:

1. direct(): one LLM call that sets a scene — situation, medium, time of day, and
   the ordered turns (who speaks each beat). Returns validated structured data.
2. generate_lines(): sequential per-character generation. Each line is produced by
   that character's own full persona prompt with the running transcript, so voices
   stay isolated and can't blend. The whole exchange is written upfront and frozen
   into the plan; dispatch only posts.

Lines may be exactly "..." — a real, intentional beat (the Postman staying
unbothered) that posts and enters the transcript the next speaker reacts to.
"""

from __future__ import annotations

import json
import random
from typing import Any

import llm
from registry import Registry

MEDIA = {"irl", "messaging"}
TIMES = {"morning", "lunch", "afternoon", "evening", "night"}

# Inter-line gaps (seconds). irl is spoken and fast; messaging is texting and slow.
IRL_DELAY = (4, 12)
MESSAGING_DELAY = (25, 75)


class StorytellerError(RuntimeError):
    pass


_DIRECTOR_PROMPT = """You direct a tiny recurring comedy about three characters \
who run a "quote of the day" operation together. Occasionally they interact \
instead of just posting a quote. Your job is to set ONE short scene.

The cast:
- dealer: noir, theatrical, mysterious, the canonical lead. Takes himself a little \
seriously.
- plug: chronically-online Gen-Z, brainrot, lighthearted, overshares, never mean.
- postman: calm, plain, observational. The still point — funniest when unbothered.

Output STRICT JSON and nothing else (no prose, no markdown fences):
{
  "scene": "<one or two sentences setting the situation; this is posted as the \
scene-setting line the audience reads before the dialogue>",
  "medium": "irl" | "messaging",
  "time_of_day": "morning" | "lunch" | "afternoon" | "evening" | "night",
  "turns": ["<character key>", ...]
}

Rules:
- 3 to 5 turns. Three is the floor (a three-beat joke); go longer when the cast \
and scene support it. Hard max 5. Vary it — don't default to the same length.
- A character may speak more than once.
- Only cast characters listed as available below.
- medium and time_of_day must FIT the scene: a lunch-room run-in is "irl" at \
"lunch"; a late-night group chat is "messaging" at "night".
- Keep the friction small and mundane. The comedy is in restraint and contrast, \
not in everyone being witty. A jammed printer, a late delivery, one of them \
rehearsing too hard — small. Three people performing at once is exhausting; let \
someone be flat, bored, or barely paying attention.
- Home base is their workplace: the quote room / office where the daily quote \
gets made. Most scenes happen there — it's the recurring set, and that \
familiarity is the point. Vary what's *happening* in the room rather than \
relocating every time. Venture out only occasionally (a café run, the walk in).
- Within the office, don't lean on equipment breaking (printers, toner, markers). \
That's one situation among many and wears out fast; the room has more going on \
than malfunctioning machines.
- Mix the medium: some scenes are just the three of them texting.
- The Postman is a good anchor — include him when you can, doing very little.
- Write scenes in the spirit of these seeds, but invent fresh ones — do not copy, \
and don't lean on any single one:
  * A disagreement about whether today's quote is any good.
  * One of them is in an unreasonable mood; the others react.
  * Some small thing in the room — a sticky note, a humming light, a squeaky \
chair, a stapler — becomes a whole debate, far past what it deserves.
  * The Dealer is theatrical about something trivial; Plug narrates it, the \
Postman deadpans.
  * They're waiting on each other, or on the day to start.
  * A tiny disagreement that escalates pointlessly and resolves with a shrug.
  * Someone shares a small win, or a small complaint.
  * A half-asleep group chat at an odd hour about nothing in particular.
  * The same little prompt lands on all three completely differently.
"""


def direct(available: list[str], weekday_name: str, *, model: str | None = None) -> dict[str, Any]:
    """Ask the director for a scene. Validates against the available cast."""
    if len(available) < 2:
        raise StorytellerError(f"need >=2 available characters, got {available}")

    note = (
        f"Today is {weekday_name}. Available cast: {', '.join(available)}."
        + ("" if "postman" in available else " (The Postman is off today.)")
    )
    raw = llm.generate(
        [{"role": "user", "content": f"{note}\n\nSet today's scene."}],
        instructions=_DIRECTOR_PROMPT,
        model=model,
        max_output_tokens=400,
    )
    scene = _parse_scene(raw)
    _validate_scene(scene, available)
    return scene


def _parse_scene(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise StorytellerError(f"no JSON object in director output: {raw[:200]}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise StorytellerError(f"director JSON parse failed: {exc}: {raw[:200]}")


def _validate_scene(scene: dict[str, Any], available: list[str]) -> None:
    for field in ("scene", "medium", "time_of_day", "turns"):
        if field not in scene:
            raise StorytellerError(f"scene missing '{field}'")
    if scene["medium"] not in MEDIA:
        raise StorytellerError(f"bad medium: {scene['medium']}")
    if scene["time_of_day"] not in TIMES:
        raise StorytellerError(f"bad time_of_day: {scene['time_of_day']}")
    turns = scene["turns"]
    if not isinstance(turns, list) or not (3 <= len(turns) <= 5):
        raise StorytellerError(f"turns must be a list of 3-5: {turns}")
    avail = set(available)
    bad = [t for t in turns if t not in avail]
    if bad:
        raise StorytellerError(f"turns cast unavailable characters: {bad}")


_TURN_TEMPLATE = """You're in a live {medium_desc} with the others. This is NOT a \
quote delivery — your usual output format and "deliver a quote" rules do not apply \
here. Just talk, in your established voice.

Scene: {scene}

Conversation so far:
{transcript}

It's your turn ({name}). Reply with a single short line in character — one or two \
sentences, said the way you'd actually say it.{grounding}{hint} This is a real \
conversation, not a writers' room: don't force a punchline. It's fine to be flat, \
brief, bored, or unbothered — most real lines are. You may reply with exactly \
"..." if silence is the truest response. Output only your line: no name prefix, no \
surrounding quotation marks, no markdown."""

_GROUNDING = (
    " You speak first, and the audience can't see the scene description — only the "
    "messages. So let your line quietly reveal the situation through how you react "
    "to it (what's happening, roughly where), without narrating or stating the "
    "obvious to someone standing right there."
)

# Per-character nudges that apply ONLY in conversation, not in quote delivery.
# The Postman especially drifts aphoristic here; in dialogue he should be plain.
_INTERACTION_HINTS: dict[str, str] = {
    "postman": (
        " In conversation you're blunter and more literal than in your quotes — "
        "plain facts, dry, grounded, a little deadpan. Say the obvious thing flatly. "
        "Save the gentle wisdom for the quotes; here you don't philosophise."
    ),
    "dealer": (
        " You can be theatrical, but land it in one short line — you're talking, "
        "not delivering a quote."
    ),
    "plug": " You're the live-commentator: reactive, quick, terminally online.",
}


def generate_lines(
    reg: Registry, scene: str, medium: str, turns: list[str], *, model: str | None = None
) -> list[dict[str, Any]]:
    medium_desc = "face-to-face conversation" if medium == "irl" else "group chat"
    transcript: list[tuple[str, str]] = []
    lines: list[dict[str, Any]] = []

    for i, key in enumerate(turns):
        ch = reg[key]
        convo = "\n".join(f"{reg[k].name}: {t}" for k, t in transcript) or "(nothing yet)"
        prompt = _TURN_TEMPLATE.format(
            medium_desc=medium_desc,
            scene=scene,
            transcript=convo,
            name=ch.name,
            grounding=_GROUNDING if i == 0 else "",
            hint=_INTERACTION_HINTS.get(key, ""),
        )
        raw = llm.generate(
            [{"role": "user", "content": prompt}],
            instructions=ch.prompt,
            model=model,
            max_output_tokens=200,
        )
        line = _clean_line(raw, ch.name)
        transcript.append((key, line))
        lines.append({"character": key, "line": line})

    return lines


def _clean_line(raw: str, name: str) -> str:
    line = raw.strip()
    if line.startswith("```"):
        line = line.strip("`").strip()
    # drop a leading "Name:" if the model added one
    if line.lower().startswith(name.lower() + ":"):
        line = line[len(name) + 1 :].strip()
    # strip wrapping quotes/bold, but never mangle a bare "..."
    if line != "...":
        if line.startswith("**") and line.endswith("**") and len(line) > 4:
            line = line[2:-2].strip()
        if len(line) >= 2 and line[0] in "\"'“" and line[-1] in "\"'”":
            line = line[1:-1].strip()
    return line or "..."


def assign_delays(lines: list[dict[str, Any]], medium: str, rng: random.Random) -> None:
    """Set delay_after (seconds) on each line — the gap before the next one. Last
    line gets 0. Mildly length-scaled so longer lines read as taking longer."""
    lo, hi = IRL_DELAY if medium == "irl" else MESSAGING_DELAY
    for i, ln in enumerate(lines):
        if i == len(lines) - 1:
            ln["delay_after"] = 0
            continue
        base = rng.randint(lo, hi)
        base += min(len(ln["line"]) // 40, (hi - lo) // 2)
        ln["delay_after"] = base