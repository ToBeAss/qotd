import os
import sys
import logging
import argparse
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv
from system_prompt import SYSTEM_PROMPT

load_dotenv()

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger("qotd")
log.setLevel(logging.INFO)
if not log.handlers:
    fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, "qotd.log"),
        maxBytes=512_000,
        backupCount=3,
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def send_to_discord(webhook: str, content: str):
    data = {"content": content}

    try:
        response = requests.post(webhook, json=data, timeout=10)

        if response.status_code == 204:
            log.info("discord webhook accepted message")
            return True
        else:
            log.error(
                "discord webhook rejected message: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            return False
    except requests.exceptions.RequestException:
        log.exception("discord webhook request failed")
        return False


def get_quote_of_the_day(input: str, bonus: bool = False):
    url = "https://api.openai.com/v1/responses"

    messages = [{"role": "system", "content": input}]
    if bonus:
        messages.append({
            "role": "user",
            "content": "The dealer is feeling generous today. Deliver a bonus quote, but first acknowledge the occasion with a short in-character line — something in the spirit of 'two for the price of one' but make it your own. Keep the acknowledgement brief and on-brand."
        })

    model = "gpt-5.4-mini"
    payload = {
        "model": model,
        "input": messages,
    }

    headers = {
        "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
        "Content-Type": "application/json"
    }

    log.info("calling openai (model=%s, bonus=%s)", model, bonus)
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data["output"][0]["content"][0]["text"]
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:500] if e.response is not None else ""
        log.error("openai returned http error: %s body=%s", e, body)
        raise
    except (KeyError, IndexError, ValueError):
        log.exception("openai response had unexpected shape")
        raise
    except requests.exceptions.RequestException:
        log.exception("openai request failed")
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bonus", action="store_true")
    args = parser.parse_args()

    missing = [v for v in ("OPENAI_API_KEY", "QOTD_WEBHOOK") if not os.getenv(v)]
    if missing:
        log.error("missing required env vars: %s", ", ".join(missing))
        sys.exit(2)

    log.info("run starting (bonus=%s)", args.bonus)
    try:
        quote = get_quote_of_the_day(SYSTEM_PROMPT, bonus=args.bonus)
        ok = send_to_discord(os.getenv("QOTD_WEBHOOK"), quote)
        if not ok:
            log.error("run finished with discord send failure")
            sys.exit(1)
        log.info("run finished ok")
    except Exception:
        log.exception("run failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
