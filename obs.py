"""Observability — shared logging plus throttled admin-channel error reporting.

Dispatch (and later the storyteller) report failures through report_error. It
logs in full to logs/qotd.log and posts a short notice to ADMIN_WEBHOOK, but the
post is throttled: dispatch runs every 5 minutes as a fresh process, so a
persistent failure would otherwise spam the channel on every tick. The throttle
state lives in a file so it survives across invocations.

  - Same error: suppressed for ADMIN_COOLDOWN_S after it's first sent.
  - Any errors: hard ceiling of ADMIN_MAX_PER_HOUR posts in a rolling hour.

Suppressed errors are still logged to file in full — only the Discord post is
held back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"
THROTTLE_PATH = STATE_DIR / "admin_throttle.json"

ADMIN_COOLDOWN_S = 3600        # suppress a repeat of the same error for an hour
ADMIN_MAX_PER_HOUR = 5         # global ceiling on admin posts per rolling hour

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """The shared 'qotd' logger — rotating file + stderr. Configured once."""
    global _logger
    if _logger is not None:
        return _logger
    LOG_DIR.mkdir(exist_ok=True)
    log = logging.getLogger("qotd")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        fh = RotatingFileHandler(LOG_DIR / "qotd.log", maxBytes=512_000, backupCount=3)
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(sh)
    _logger = log
    return log


def _load_throttle() -> dict:
    try:
        return json.loads(THROTTLE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"sigs": {}, "recent": []}


def _save_throttle(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    try:
        THROTTLE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception as exc:
        get_logger().error("could not write admin throttle: %s", exc)


def _signature(msg: str) -> str:
    return hashlib.sha1(msg.encode("utf-8", "ignore")).hexdigest()[:12]


def _allow_admin_post(msg: str, now: float) -> bool:
    """Decide whether this error may go to the admin channel, and record it if so."""
    state = _load_throttle()
    sigs: dict[str, float] = state.get("sigs", {})
    recent = [ts for ts in state.get("recent", []) if now - ts < 3600]

    sig = _signature(msg)
    last = sigs.get(sig)
    if last is not None and now - last < ADMIN_COOLDOWN_S:
        return False  # same error, still cooling down
    if len(recent) >= ADMIN_MAX_PER_HOUR:
        return False  # global hourly cap hit

    sigs[sig] = now
    recent.append(now)
    # forget signatures we no longer need to dedupe against
    sigs = {s: ts for s, ts in sigs.items() if now - ts < ADMIN_COOLDOWN_S * 24}
    _save_throttle({"sigs": sigs, "recent": recent})
    return True


def _post_admin(msg: str) -> None:
    url = os.getenv("ADMIN_WEBHOOK")
    if not url:
        return
    try:
        requests.post(url, json={"content": f"⚠️ QOTD: {msg}"[:1900]}, timeout=10)
    except Exception as exc:
        get_logger().error("admin webhook post failed: %s", exc)


def report_error(msg: str) -> None:
    """Log an error in full and post it to the admin channel if the throttle allows."""
    get_logger().error(msg)
    if _allow_admin_post(msg, time.time()):
        _post_admin(msg)
