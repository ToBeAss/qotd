"""Shared LLM call — OpenAI Responses API over raw requests (no SDK, Pi-friendly).

Used by dispatch (quote generation) and the storyteller (scene + lines). The old
get_quote_of_the_day in main.py is replaced by generate_from_prompt here.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

DEFAULT_MODEL = "gpt-5.4-mini"
OPENAI_URL = "https://api.openai.com/v1/responses"

# Module defaults, settable from the registry via configure(). Kept here so call
# sites don't all need to thread model/temperature through.
_DEFAULTS: dict[str, Any] = {"model": DEFAULT_MODEL, "temperature": None}


def configure(*, model: str | None = None, temperature: float | None = None) -> None:
    if model:
        _DEFAULTS["model"] = model
    if temperature is not None:
        _DEFAULTS["temperature"] = temperature


class LLMError(RuntimeError):
    pass


def generate(
    input_messages: list[dict[str, Any]],
    *,
    instructions: str | None = None,
    model: str | None = None,
    timeout: float = 30.0,
    max_output_tokens: int | None = None,
    reasoning: dict[str, Any] | None = None,
    temperature: float | None = None,
) -> str:
    """Call the Responses API and return the assembled text.

    `instructions` carries the persona/system prompt (kept separate from input,
    matching the SAM provider pattern). `input_messages` is the turn content —
    e.g. a single user message holding the context block.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMError("OPENAI_API_KEY is not set")

    model = model or _DEFAULTS["model"]
    temp = temperature if temperature is not None else _DEFAULTS["temperature"]

    payload: dict[str, Any] = {"model": model, "input": input_messages}
    if instructions:
        payload["instructions"] = instructions
    if max_output_tokens:
        payload["max_output_tokens"] = max_output_tokens
    if reasoning:
        payload["reasoning"] = reasoning
    if temp is not None:
        payload["temperature"] = temp

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    resp = requests.post(OPENAI_URL, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    return _extract_text(resp.json())


def generate_from_prompt(
    system_prompt: str,
    context_block: str | None = None,
    **kwargs: Any,
) -> str:
    """Convenience for single-shot persona generation: persona as instructions,
    context block (or a minimal nudge) as the user turn."""
    user_content = context_block or "Deliver today's quote."
    return generate(
        [{"role": "user", "content": user_content}],
        instructions=system_prompt,
        **kwargs,
    )


def _extract_text(data: dict[str, Any]) -> str:
    """Pull text out of a Responses payload defensively.

    The output array can interleave reasoning items with the message item, so we
    don't index blindly — we walk for the message and concatenate its text parts.
    """
    convenience = data.get("output_text")
    if isinstance(convenience, str) and convenience.strip():
        return convenience.strip()

    parts: list[str] = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for chunk in item.get("content", []):
            if chunk.get("type") in ("output_text", "text") and "text" in chunk:
                parts.append(chunk["text"])

    text = "".join(parts).strip()
    if not text:
        raise LLMError(f"no text found in response: {json.dumps(data)[:300]}")
    return text