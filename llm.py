"""Shared LLM call — OpenAI Responses API over raw requests (no SDK, Pi-friendly).

Used by dispatch (quote generation) and the storyteller (scene + lines). The old
get_quote_of_the_day in main.py is replaced by generate_from_prompt here.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

DEFAULT_MODEL = "gpt-5.4-mini"

# A dropped connection or timeout (SSLError is a ConnectionError) is retried once
# after this many seconds. HTTP errors (400, 429, ...) never are: a retry won't
# fix a bad request or an empty quota. The planner has no per-minute retry and a
# scene chains several calls, so one network blip shouldn't cost the day.
NETWORK_RETRY_DELAY = 15.0

log = logging.getLogger("qotd")
OPENAI_URL = "https://api.openai.com/v1/responses"

# Module defaults, settable from the registry via configure(). Kept here so call
# sites don't all need to thread model/temperature through.
_DEFAULTS: dict[str, Any] = {"model": DEFAULT_MODEL, "temperature": None, "reasoning_effort": None}


def configure(
    *,
    model: str | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
) -> None:
    if model:
        _DEFAULTS["model"] = model
    if temperature is not None:
        _DEFAULTS["temperature"] = temperature
    if reasoning_effort:
        _DEFAULTS["reasoning_effort"] = reasoning_effort


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
    elif _DEFAULTS["reasoning_effort"]:
        payload["reasoning"] = {"effort": _DEFAULTS["reasoning_effort"]}
    if temp is not None:
        payload["temperature"] = temp

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for attempt in (1, 2):
        try:
            resp = requests.post(OPENAI_URL, headers=headers, json=payload, timeout=timeout)
            break
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == 2:
                raise
            log.warning(
                "OpenAI request failed (%s: %s); retrying in %.0fs",
                type(exc).__name__, exc, NETWORK_RETRY_DELAY,
            )
            time.sleep(NETWORK_RETRY_DELAY)
    if not resp.ok:
        # Same HTTPError raise_for_status() would raise, but with OpenAI's error
        # code in the message: 429 is both insufficient_quota and rate_limit_exceeded.
        raise requests.HTTPError(_describe_error(resp), response=resp)
    data = resp.json()
    # Reasoning models count reasoning toward max_output_tokens, so a response can
    # finish "incomplete" with no text at all. Name it rather than "no text found".
    if data.get("status") == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason", "unknown")
        raise LLMError(f"response incomplete ({reason}) from {model}")
    return _extract_text(data)


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


def _describe_error(resp: requests.Response) -> str:
    """'429 insufficient_quota: You exceeded...' from an OpenAI error body, or the
    status plus a truncated raw body when it isn't the expected JSON."""
    try:
        err = resp.json()["error"]
        code = err.get("code") or err.get("type") or "error"
        return f"{resp.status_code} {code}: {str(err.get('message', ''))[:200]}"
    except (ValueError, KeyError, TypeError, AttributeError):
        return f"{resp.status_code} {resp.reason}: {resp.text[:200]}"


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