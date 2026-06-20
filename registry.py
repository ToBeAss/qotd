"""Registry loader — the single entry point for character config.

Reads registry.yaml + the agents/*.md voice files and composes each character's
full prompt (voice + shared house rules). Everything downstream — planner,
dispatch, storyteller — consumes Character objects from here. Nothing else reads
the yaml or the markdown directly.

Retires the old system_prompt.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

import llm

REPO_ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = REPO_ROOT / "registry.yaml"
COMMON_PROMPT_PATH = REPO_ROOT / "agents" / "_common.md"

# Sentinel character key for the narrator's scene line in an interaction plan.
STORYTELLER_KEY = "_storyteller"


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    timezone: str


@dataclass(frozen=True)
class Character:
    key: str
    name: str
    webhook_env: str
    prompt: str                 # composed: voice + house rules
    day_weights: list[int]      # Mon..Sun, index 0 = Monday
    hours: dict[int, int]       # hour (0-23) -> base weight

    @property
    def webhook(self) -> str | None:
        """Resolved at access time so a missing env var fails loudly at send,
        not silently at load."""
        return os.getenv(self.webhook_env)

    def day_weight(self, weekday: int) -> int:
        """weekday: Monday=0 .. Sunday=6 (matches datetime.weekday())."""
        return self.day_weights[weekday]


@dataclass(frozen=True)
class Registry:
    location: Location
    quiet_start: str            # "HH:MM"
    quiet_end: str              # "HH:MM" — may wrap past midnight
    characters: dict[str, Character]
    storyteller_webhook_env: str | None = None
    interaction_model: str | None = None   # optional stronger model for interactions
    interaction_chance: float = 0.07

    @property
    def storyteller_webhook(self) -> str | None:
        if not self.storyteller_webhook_env:
            return None
        return os.getenv(self.storyteller_webhook_env)

    def __getitem__(self, key: str) -> Character:
        return self.characters[key]

    def __iter__(self):
        return iter(self.characters.values())

    def keys(self):
        return self.characters.keys()


def _compose_prompt(voice: str, common: str) -> str:
    voice = voice.strip()
    common = common.strip()
    return f"{voice}\n\n{common}" if common else voice


def load_registry(path: Path = REGISTRY_PATH) -> Registry:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    root = path.parent

    common = ""
    if COMMON_PROMPT_PATH.exists():
        common = COMMON_PROMPT_PATH.read_text(encoding="utf-8")

    characters: dict[str, Character] = {}
    for key, c in raw["characters"].items():
        voice = (root / c["prompt_file"]).read_text(encoding="utf-8")
        day_weights = list(c["day_weights"])
        if len(day_weights) != 7:
            raise ValueError(
                f"{key}: day_weights must have 7 entries (Mon..Sun), got {len(day_weights)}"
            )
        characters[key] = Character(
            key=key,
            name=c["name"],
            webhook_env=c["webhook_env"],
            prompt=_compose_prompt(voice, common),
            day_weights=day_weights,
            hours={int(h): int(w) for h, w in c["hours"].items()},
        )

    loc = raw["location"]
    qh = raw["quiet_hours"]

    llm_cfg = raw.get("llm", {})
    llm.configure(model=llm_cfg.get("model"), temperature=llm_cfg.get("temperature"))

    return Registry(
        location=Location(lat=loc["lat"], lon=loc["lon"], timezone=loc["timezone"]),
        quiet_start=qh["start"],
        quiet_end=qh["end"],
        characters=characters,
        storyteller_webhook_env=raw.get("storyteller_webhook_env"),
        interaction_model=llm_cfg.get("interaction_model"),
        interaction_chance=float(raw.get("interaction_chance", 0.07)),
    )


# Convenience for quick inspection: `python registry.py`
if __name__ == "__main__":
    reg = load_registry()
    print(f"location: {reg.location.lat},{reg.location.lon} ({reg.location.timezone})")
    print(f"quiet hours: {reg.quiet_start}–{reg.quiet_end}")
    for ch in reg:
        env_state = "set" if ch.webhook else f"UNSET ({ch.webhook_env})"
        print(
            f"  {ch.key:8} {ch.name:18} webhook={env_state} "
            f"days={ch.day_weights} hours={sorted(ch.hours)}"
        )
        print(f"           prompt: {len(ch.prompt)} chars")