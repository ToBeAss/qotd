"""Storyteller — the interaction director.

Two jobs, both at plan time:

1. direct(): one LLM call that sets a scene — situation, premise (who wants what,
   and what's in the way), medium, time of day, the ordered turns (who speaks
   each beat) and one intent per turn. Returns validated structured data.
2. generate_lines(): sequential per-character generation. Each line is produced by
   that character's own full persona prompt with the running transcript, the
   premise and its own beat, so voices stay isolated and can't blend. The whole
   exchange is written upfront and frozen into the plan; dispatch only posts.

The director is handed material from memory (each character's last couple of
posts) and the recent premises, so scenes can be about something real and don't
repeat. Structure is decided in code, not left to the model: pick_seed() chooses
the scene's seed (not one of the last few used), pick_closer() who speaks last
(not the previous scene's closer) and pick_setting() the medium and time of day
(weighted, not the previous scene's exact pair). The planner also rolls whether
the Dealer's act slips; the director picks which of his turns (slip_turn), and
generate_lines gives his lines the ACT instruction except that one. Lines may be exactly "..." when a beat calls
for silence; a line that narrates an action instead of speaking is retried once.
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any

import llm
import memory
import quotes
from registry import Registry

MEDIA = {"irl", "messaging"}
TIMES = {"morning", "lunch", "afternoon", "evening", "night"}

# Inter-line gaps (seconds). irl is spoken and fast; messaging is texting and slow.
IRL_DELAY = (4, 12)
MESSAGING_DELAY = (25, 75)

# Premise is a single sentence; a little slack over the prompt's ~250.
PREMISE_MAX = 300

# Material block: posts per character, and the length each is cut to.
MATERIAL_POSTS = 2
MATERIAL_CHARS = 200

# How many recent premises the director is told not to repeat.
RECENT_PREMISES_SHOWN = 5

# The director reasons over material, premises and beats; it can outlast llm's
# 30s default. It runs at plan time (07:00), so waiting longer costs nothing.
DIRECTOR_TIMEOUT = 90.0

# A seed isn't reused within this many recent scenes.
SEED_COOLDOWN = 3

log = logging.getLogger("qotd")

# The Dealer's scenes run on his act (noir as a bit), with an optional slip.
DEALER = "dealer"


@dataclass(frozen=True)
class Seed:
    text: str
    needs: frozenset[str] = frozenset()   # cast keys that must be available
    min_cast: int = 2
    material: bool = False                # only when the scene builds on material
    media: frozenset[str] = frozenset(MEDIA)   # media the seed can play in


# The shape of a scene: code picks one per scene (see pick_seed). The director
# builds the premise in its spirit. Keys are stable; they're stored in memory.
SEEDS: dict[str, Seed] = {
    "objects-to-post": Seed(
        "Someone objects to one of the others' recent posts and wants it "
        "acknowledged, retracted, or credited.",
        material=True,
    ),
    "remix-defence": Seed(
        'The Plug defends a remix as "basically the same thing"; the Dealer takes '
        "it personally.",
        needs=frozenset({"plug", "dealer"}),
        material=True,
    ),
    "settle-a-bet": Seed(
        "Two of them need the third to settle a bet, and the third won't play along.",
        min_cast=3,
    ),
    "unnoticed-win": Seed(
        "Someone wants the others to notice a small win. They don't, or they notice "
        "the wrong thing.",
    ),
    "postman-refuses": Seed(
        "The Postman is asked to deliver or say something he won't.",
        needs=frozenset({"postman"}),
    ),
    "dealer-trivial": Seed(
        "The Dealer wants to be taken seriously about something trivial; nobody does.",
        needs=frozenset({"dealer"}),
    ),
    "has-to-ask": Seed(
        "Someone wants a favour, a day off, or the last of something, and has to ask.",
    ),
}


class StorytellerError(RuntimeError):
    pass


_DIRECTOR_PROMPT = """You direct a tiny recurring comedy about three characters \
who run a "quote of the day" operation together. Occasionally they interact \
instead of posting a quote. Your job is to set ONE short scene that is about \
something.

The cast:
- dealer: noir, theatrical, mysterious, the canonical lead. Takes himself a little \
seriously.
- plug: chronically-online Gen-Z, brainrot, lighthearted, overshares, never mean. \
Remixes real quotes into his own register.
- postman: calm, plain, literal. Delivers real quotes with a dry note. The anchor \
who undercuts: deadpan, and the one who says the fact that ends the argument.

These scenes are backstage. Their posting voices are registers they perform in; \
here they talk as themselves. The Dealer is the exception: his noir is a bit he \
commits to. Whether it slips in this scene is decided for you (see below).

Today there is no quote post: this scene replaces it. Never reference "today's \
quote" or a quote being posted today. Their past posts are fair game.

Output STRICT JSON and nothing else (no prose, no markdown fences):
{
  "scene": "<one or two sentences setting the situation; this is posted as the \
scene-setting line the audience reads before the dialogue>",
  "premise": "<one sentence: who wants what, and what's in the way. Not posted.>",
  "medium": "irl" | "messaging",
  "time_of_day": "morning" | "lunch" | "afternoon" | "evening" | "night",
  "turns": ["<character key>", ...],
  "beats": ["<intent for turn 1>", "<intent for turn 2>", ...],
  "slip_turn": <index into turns of the Dealer's slip, or null>
}

Rules:
- Small stakes, real want. Someone wants something small and specific; someone \
or something is in the way; the scene turns once and lands. After reading it, \
you should be able to say in one sentence what happened.
- Contrast over wit: not everyone is funny, and three people performing at once \
is exhausting. But a flat line still has to land on something: undercut, side \
with the wrong party, state the fact that ends the argument. Never a dead end.
- 3 to 5 turns. Three is the floor (a three-beat joke); go longer when the cast \
and premise support it. Hard max 5. Vary it — don't default to the same length.
- A character may speak more than once. Only cast characters listed as available.
- You're given this scene's seed and its closer. Build the premise in the \
seed's spirit — don't copy it. The closer speaks the final turn, and that last \
beat closes or deflates the premise.
- beats: one per turn, same order and length as turns. Each beat says WHAT the \
line accomplishes ("concedes, but claims it was his idea", "points out the date \
on the letter"). Never HOW it's said ("flatly", "without a speech") — their \
voices handle that — and never a physical action ("smooths the sign", "takes \
off his glasses"). Physical business belongs in the scene line only. A beat may \
call for silence ("says nothing") when silence is the point.
- slip_turn: only when told the Dealer slips. Give the dealer at least two \
turns, and set slip_turn to the 0-based index of one of his turns after his \
first: the line where his act drops and he's briefly, plainly sincere. Write \
that beat as what the line accomplishes, like any other. Otherwise null.
- You're given the medium and time of day. Return them unchanged and make the \
scene fit them: "irl" is face to face, "messaging" is the three of them in a \
group chat, wherever they each are.
- Home base is their workplace: the quote room / office. Most scenes happen \
there — it's the recurring set, and that familiarity is the point. Vary what's \
*happening* in the room rather than relocating every time. Venture out only \
occasionally (a café run, the walk in).
- Within the office, don't lean on equipment breaking (printers, toner, markers). \
That's one situation among many and wears out fast.
- The Postman is a good anchor — include him when you can.
"""

_MATERIAL_BUILD = (
    "Build this scene from the material above: the premise must reference "
    "something concrete in it (a specific post, a remix, an author, a delivery)."
)
_MATERIAL_BACKGROUND = (
    "The material above is background only. Invent a fresh office situation; the "
    "material must not be the premise."
)


# --- Material -----------------------------------------------------------------
def _plain(text: str) -> str:
    text = re.sub(r"[*_`]", "", text)
    return re.sub(r"\s*\n\s*", " / ", text).strip()


def _cut(text: str, limit: int = MATERIAL_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def material_block(reg: Registry, mem: dict) -> str:
    """Each character's last MATERIAL_POSTS posts, newest first, as plain text.
    Bank-based posts are resolved through recent_bank so the author is named.
    Empty string when nobody has posted yet."""
    bank = None
    sections = []
    for ch in reg:
        posts = memory.recent_quotes(mem, ch.key)[-MATERIAL_POSTS:][::-1]  # newest last in memory
        if not posts:
            continue
        records = {b.get("posted"): b for b in (mem.get(ch.key) or {}).get("recent_bank", [])}
        last = (mem.get(ch.key) or {}).get("last_posted")

        lines = []
        for i, posted in enumerate(posts):
            rec = records.get(posted)
            if rec and bank is None:
                bank = quotes.load_bank(reg.quote_bank) or {}
            q = bank.get(rec["quote_id"]) if rec and bank else None
            if rec and q and rec.get("remix"):
                text = f'{ch.key} remixed {q.author}: "{_cut(rec["remix"])}"'
            elif rec and q:
                text = (
                    f'{ch.key} delivered {q.author}: "{_cut(q.text)}" '
                    f'with the note "{_cut(rec.get("framing", ""))}"'
                )
            else:
                text = f"{ch.key} posted: {_cut(_plain(posted))}"
            if i == 0 and last:
                text = f"({last[:10]}) {text}"
            lines.append(f"- {text}")
        sections.append("\n".join(lines))
    return "\n".join(sections)


# --- Structure ----------------------------------------------------------------
def pick_seed(
    available: list[str],
    recent: list[dict],
    rng: random.Random,
    from_material: bool,
) -> str:
    """A seed the cast can play (needs, min_cast, material), not used in the last
    SEED_COOLDOWN scenes. Falls back to any playable seed if all are recent."""
    avail = set(available)
    playable = [
        key for key, s in SEEDS.items()
        if s.needs <= avail and len(avail) >= s.min_cast and (from_material or not s.material)
    ]
    used = {e.get("seed") for e in recent[-SEED_COOLDOWN:]}
    return rng.choice([k for k in playable if k not in used] or playable)


def pick_closer(available: list[str], recent: list[dict], rng: random.Random) -> str:
    """Who speaks last: anyone available except the previous scene's closer."""
    previous = recent[-1].get("closer") if recent else None
    return rng.choice([k for k in available if k != previous] or list(available))


def _weighted(weights: dict[str, float], rng: random.Random) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def pick_setting(
    reg: Registry,
    seed: str,
    recent: list[dict],
    rng: random.Random,
    allowed_times: set[str],
    *,
    medium: str | None = None,
    time_of_day: str | None = None,
) -> tuple[str, str]:
    """Roll (medium, time_of_day): medium from scene_medium_weights within the
    seed's media, then time from that medium's weights, limited to allowed_times
    (labels with an hour inside the quiet window). The previous scene's exact
    pair is avoided when anything else is possible. Either value can be forced."""
    previous = (recent[-1].get("medium"), recent[-1].get("time_of_day")) if recent else None

    if medium is None:
        media = {
            m: w for m, w in reg.scene_medium_weights.items()
            if m in SEEDS[seed].media and w > 0
        } or {m: 1.0 for m in SEEDS[seed].media}
        medium = _weighted(media, rng)

    if time_of_day is None:
        times = {
            t: w for t, w in reg.scene_time_weights.get(medium, {}).items()
            if t in TIMES and t in allowed_times and w > 0
        } or {t: 1.0 for t in sorted(allowed_times)}
        if previous and previous[0] == medium and len(times) > 1:
            times.pop(previous[1], None)
        time_of_day = _weighted(times, rng)

    return medium, time_of_day


# --- Director -----------------------------------------------------------------
def direct(
    available: list[str],
    weekday_name: str,
    *,
    seed: str,
    closer: str,
    medium: str,
    time_of_day: str,
    slip: bool = False,
    material: str = "",
    from_material: bool = False,
    recent_premises: list[str] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Ask the director for a scene built on `seed` (a SEEDS key) that `closer`
    ends, set in `medium` at `time_of_day`. With `slip`, the Dealer gets 2+
    turns and the scene names his slip_turn. Validates against the cast, closer,
    setting and slip; on a parse or validation failure retries once, then raises
    StorytellerError."""
    if len(available) < 2:
        raise StorytellerError(f"need >=2 available characters, got {available}")
    if closer not in available:
        raise StorytellerError(f"closer {closer!r} not in available cast {available}")
    if slip and DEALER not in available:
        raise StorytellerError("a Dealer slip needs the Dealer in the cast")

    parts = [
        f"Today is {weekday_name}. Available cast: {', '.join(available)}."
        + ("" if "postman" in available else " (The Postman is off today.)")
    ]
    if material:
        parts.append(f"<material>\n{material}\n</material>")
        parts.append(_MATERIAL_BUILD if from_material else _MATERIAL_BACKGROUND)
    if recent_premises:
        listed = "\n".join(f"- {p}" for p in recent_premises[-RECENT_PREMISES_SHOWN:])
        parts.append(f"Recent premises — do not repeat these premises or their core joke:\n{listed}")
    parts.append(f"This scene's seed: {SEEDS[seed].text}")
    parts.append(f"Closer: {closer}. {closer} speaks the final turn.")
    parts.append(f'Setting: medium "{medium}", time_of_day "{time_of_day}".')
    if slip:
        parts.append(
            "Dealer slip: yes. Give the dealer at least two turns and set slip_turn "
            "to one of his turns after his first."
        )
    elif DEALER in available:
        parts.append("Dealer slip: no. He stays in his act throughout; slip_turn is null.")
    parts.append("Set today's scene.")
    prompt = "\n\n".join(parts)

    for attempt in (1, 2):
        raw = llm.generate(
            [{"role": "user", "content": prompt}],
            instructions=_DIRECTOR_PROMPT,
            model=model,
            timeout=DIRECTOR_TIMEOUT,
        )
        try:
            scene = _parse_scene(raw)
            _validate_scene(scene, available, closer, medium, time_of_day, slip)
            return scene
        except StorytellerError:
            if attempt == 2:
                raise


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


def _validate_scene(
    scene: dict[str, Any],
    available: list[str],
    closer: str,
    medium: str,
    time_of_day: str,
    slip: bool = False,
) -> None:
    for field in ("scene", "premise", "medium", "time_of_day", "turns", "beats"):
        if field not in scene:
            raise StorytellerError(f"scene missing '{field}'")
    if scene["medium"] not in MEDIA:
        raise StorytellerError(f"bad medium: {scene['medium']}")
    if scene["time_of_day"] not in TIMES:
        raise StorytellerError(f"bad time_of_day: {scene['time_of_day']}")
    if (scene["medium"], scene["time_of_day"]) != (medium, time_of_day):
        raise StorytellerError(
            f"setting must be {medium}/{time_of_day}, got {scene['medium']}/{scene['time_of_day']}"
        )
    premise = scene["premise"]
    if not isinstance(premise, str) or not premise.strip() or len(premise) > PREMISE_MAX:
        raise StorytellerError(f"premise must be a non-empty sentence: {premise!r:.100}")
    turns = scene["turns"]
    if not isinstance(turns, list) or not (3 <= len(turns) <= 5):
        raise StorytellerError(f"turns must be a list of 3-5: {turns}")
    avail = set(available)
    bad = [t for t in turns if t not in avail]
    if bad:
        raise StorytellerError(f"turns cast unavailable characters: {bad}")
    if turns[-1] != closer:
        raise StorytellerError(f"last turn must be the closer {closer!r}: {turns}")
    beats = scene["beats"]
    if (
        not isinstance(beats, list)
        or len(beats) != len(turns)
        or not all(isinstance(b, str) and b.strip() for b in beats)
    ):
        raise StorytellerError(f"beats must be {len(turns)} non-empty strings: {beats}")
    if not slip:
        scene["slip_turn"] = None  # code decides; ignore a stray index
        return
    st = scene.get("slip_turn")
    if (
        not isinstance(st, int) or isinstance(st, bool)
        or not 0 <= st < len(turns)
        or turns[st] != DEALER
        or DEALER not in turns[:st]
    ):
        raise StorytellerError(f"slip_turn must be a Dealer turn after his first: {st!r} in {turns}")


# --- Lines --------------------------------------------------------------------
_TURN_TEMPLATE = """You're in a live {medium_desc} with the others. This is NOT a \
quote delivery — your usual output format and "deliver a quote" rules do not apply \
here. This is backstage. Your posting voice is a register you perform in, not \
how you talk all the time.{yourself} There's no quote post today, so don't bring \
up "today's quote".

Scene: {scene}
What's going on: {premise}

Conversation so far:
{transcript}

It's your turn ({name}). Your job this line: {beat}

Reply with a single line in character, one or two sentences. Be specific to \
what's going on and say it the way you'd actually say it. The beat is what your \
line does; your voice decides how it sounds.\
{opening}{grounding}{hint}{closing}{retry} Reply with exactly "..." only if your job this line \
calls for silence. Output only your line, as spoken: no name prefix, no \
stage directions or actions in brackets, no surrounding quotation marks, no \
markdown."""

# Only when no narrator is configured: with one, the audience has already read the
# scene line before the dialogue starts.
_GROUNDING = (
    " You speak first, and the audience can't see the scene description — only the "
    "messages. So let your line quietly reveal the situation through how you react "
    "to it (what's happening, roughly where), without narrating or stating the "
    "obvious to someone standing right there."
)

_OPENING = (
    " Nobody has said anything yet. Don't respond to objections or answers that "
    "haven't been voiced."
)

_CLOSING = (
    " Your line closes the scene: resolve or deflate what's going on. A shrug "
    "counts only if it's about the premise."
)

_RETRY_SPOKEN = (
    " Your last attempt narrated an action instead of speaking. Give only the "
    "words you say out loud."
)

# Narrated actions: bracketed or starred ("(sighs)", "*smooths the sign*"), first
# person ("I smooth down the corner"), or verb-first ("Takes off his glasses").
# Checked on the first and last sentence of a line.
_ACTION_VERBS = (
    "adjust|clear|close|cross|drop|flip|fold|glance|grab|grin|hand|lean|lift|"
    "lower|nod|open|pat|peel|pick|place|pour|pull|push|put|raise|reach|roll|rub|"
    "set|shake|shrug|shut|sigh|sip|slide|slip|smile|smooth|stare|step|stick|stir|"
    "straighten|take|tap|tilt|toss|tuck|walk|wave|wipe"
)
_BRACKETED = re.compile(r"^\s*[*(\[][^*)\]]+[*)\]]|[*(\[][^*)\]]+[*)\]]\s*$")
_FIRST_PERSON_ACTION = re.compile(rf"^I (?:\w+ly )?(?:{_ACTION_VERBS})\b")
_VERB_FIRST_ACTION = re.compile(rf"^(?:\w+ly )?(?:{_ACTION_VERBS})e?s\b", re.I)

# Per-character nudges that apply ONLY in conversation, not in quote delivery.
# The Postman especially drifts aphoristic here; in dialogue he should be plain.
_INTERACTION_HINTS: dict[str, str] = {
    "postman": (
        " In conversation you're blunter and more literal than in your delivery "
        "notes — plain facts, dry, grounded, a little deadpan. Your plain line "
        "lands: it's the fact that settles the argument or deflates it, never a "
        "neutral acknowledgement. You don't philosophise."
    ),
    "plug": (
        " You're the live-commentator: reactive, quick, online. The slang is how "
        "you talk, not a rule: drop it when something actually matters."
    ),
}


# The Dealer's hint is per line: the act by default, the slip on slip_turn.
_DEALER_ACT = (
    " You're doing the bit: noir, clipped, low, a little too serious for the "
    "moment. Commit to it; you think it's working."
)
_DEALER_SLIP = (
    " On this line the act slips. For a moment you're just a plain, earnest, "
    "slightly dorky guy who actually cares about this. Don't announce it; let the "
    "line show it. You can scramble back into character at the end."
)


def _hint(key: str, i: int, slip_turn: int | None) -> str:
    if key == DEALER:
        return _DEALER_SLIP if i == slip_turn else _DEALER_ACT
    return _INTERACTION_HINTS.get(key, "")


def generate_lines(
    reg: Registry, scene: dict[str, Any], *, model: str | None = None
) -> list[dict[str, Any]]:
    """Write every line of a validated scene in order. The first speaker gets the
    grounding nudge only when there's no narrator to post the scene line."""
    medium_desc = "face-to-face conversation" if scene["medium"] == "irl" else "group chat"
    narrated = bool(reg.storyteller_webhook)
    turns = scene["turns"]
    transcript: list[tuple[str, str]] = []
    lines: list[dict[str, Any]] = []

    for i, key in enumerate(turns):
        ch = reg[key]
        convo = "\n".join(f"{reg[k].name}: {t}" for k, t in transcript) or "(nothing yet)"
        for attempt in (1, 2):  # one retry if the line narrates an action
            prompt = _TURN_TEMPLATE.format(
                medium_desc=medium_desc,
                scene=scene["scene"],
                premise=scene["premise"],
                transcript=convo,
                name=ch.name,
                beat=scene["beats"][i],
                yourself="" if key == DEALER else " Sound like yourself.",
                opening=_OPENING if i == 0 else "",
                grounding=_GROUNDING if i == 0 and not narrated else "",
                hint=_hint(key, i, scene.get("slip_turn")),
                closing=_CLOSING if i == len(turns) - 1 else "",
                retry=_RETRY_SPOKEN if attempt == 2 else "",
            )
            raw = llm.generate(
                [{"role": "user", "content": prompt}],
                instructions=ch.prompt,
                model=model,
            )
            line = _clean_line(raw, ch.name)
            if not narrates_action(line):
                break
            log.info("%s line narrates an action (attempt %d): %r", key, attempt, line)
        else:
            line = _BRACKETED.sub("", line).strip() or line  # keep the spoken part
        transcript.append((key, line))
        lines.append({"character": key, "line": line})

    return lines


def narrates_action(line: str) -> bool:
    """True if the line starts or ends by narrating an action instead of speech."""
    if line == "...":
        return False
    if _BRACKETED.search(line):
        return True
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", line.strip()) if s]
    return any(
        _FIRST_PERSON_ACTION.match(s) or _VERB_FIRST_ACTION.match(s)
        for s in {sentences[0], sentences[-1]}
    ) if sentences else False


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
