import json
import logging
from typing import Any
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import time
import os
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# Read API keys from environment
_keys_str = os.getenv("OPENAI_API_KEY", os.getenv("LLM_API_KEYS", ""))
_keys = [k.strip() for k in _keys_str.split(",") if k.strip()]
_key_idx = 0
_model = os.getenv("LLM_MODEL", "gpt-4.1-mini")


def call_llm(system_prompt: str, user_prompt: str, json_mode: bool = True) -> str:
    """Call LLM via direct HTTP request."""
    global _key_idx
    last_err = None
    start_time = time.time()

    if not _keys:
        log.error("No API keys configured (OPENAI_API_KEY or LLM_API_KEYS).")
        return ""

    while time.time() - start_time < 20:
        key = _keys[_key_idx % len(_keys)]
        try:
            payload: dict[str, Any] = {
                "model": _model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 2000,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            }

            base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1/chat/completions")
            resp = requests.post(base_url, json=payload, headers=headers, timeout=30, verify=False)

            if resp.status_code == 429:
                raise Exception("Rate limit hit")
            if resp.status_code != 200:
                raise Exception(f"API Error {resp.status_code}: {resp.text}")

            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
            return text

        except Exception as e:
            last_err = e
            log.warning(f"LLM attempt failed (key=...{key[-6:]}): {e}")
            _key_idx += 1
            time.sleep(1)

    log.error(f"LLM retries exhausted. Last error: {last_err}")
    return ""


def call_llm_json(system_prompt: str, user_prompt: str) -> dict | list | None:
    text = call_llm(system_prompt, user_prompt, json_mode=True)
    if not text:
        return None
    try:
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        return json.loads(text.strip())
    except Exception as e:
        log.error(f"Failed to parse LLM JSON: {e}\n{text}")
        return None
