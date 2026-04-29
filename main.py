import os
import requests
from dotenv import load_dotenv
from system_prompt import SYSTEM_PROMPT

load_dotenv()

def send_to_discord(webhook: str, content: str):
    data = {
        "content": content
    }

    try:
        response = requests.post(webhook, json=data, timeout=10)  # Add timeout

        if response.status_code == 204:
            print("Message sent to Discord successfully.")
            return True
        else:
            print(f"Failed to send message. Status code: {response.status_code}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"Discord webhook error: {e}")
        return False

def get_quote_of_the_day(input: str):
    url = "https://api.openai.com/v1/responses"

    payload = {
        "model": "gpt-5.4-mini",
        "input": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            }
        ]
    }

    headers = {
        "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()

    data = response.json()

    return data["output"][0]["content"][0]["text"]

send_to_discord(os.getenv("QOTD_WEBHOOK"), get_quote_of_the_day(SYSTEM_PROMPT))
