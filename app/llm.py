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
_model = os.getenv("LLM_MODEL", "gpt-4o-mini")

def call_llm(system_prompt: str, user_prompt: str, json_mode: bool = True) -> str:
    """Call LLM via direct HTTP request (Defaults to OpenAI)."""
    global _key_idx
    last_err = None
    start_time = time.time()

    if not _keys:
        log.error("No API keys configured in environment (OPENAI_API_KEY or LLM_API_KEYS).")
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
                "max_tokens": 1500,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            }
            
            base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1/chat/completions")
            resp = requests.post(
                base_url,
                json=payload,
                headers=headers,
                timeout=30,
                verify=False
            )
            
            if resp.status_code == 429:
                raise Exception("Rate limit hit")
            if resp.status_code != 200:
                raise Exception(f"API Error {resp.status_code}: {resp.text}")

            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
            log.info(f"LLM call OK (model={_model}, key=...{key[-6:]})")
            return text

        except Exception as e:
            last_err = e
            key_str = f"key=...{key[-6:]}"
            log.warning(f"LLM attempt failed ({key_str}): {e}")
            _key_idx += 1  # Rotate to next key
            time.sleep(1)

    log.error(f"LLM retries exhausted after 20s. Last error: {last_err}")
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


# ==========================================
# Specialized Prompts
# ==========================================

def generate_overview_narrative(data: dict) -> str:
    sys_prompt = """You are a top-tier wealth manager summarizing a portfolio overview.
Given the portfolio metrics (Value, Gain, Return %, Health Score), provide a concise 1-2 sentence executive summary of the portfolio's current state.
Return JSON: {"narrative": "..."}"""
    
    user_prompt = f"Portfolio Metrics:\n{json.dumps(data, indent=2)}\nGenerate the JSON output."
    result = call_llm_json(sys_prompt, user_prompt)
    if result and isinstance(result, dict):
        return result.get("narrative", "")
    return ""


def generate_performance_narrative(data: dict) -> dict:
    sys_prompt = """You are a wealth manager analyzing portfolio performance.
Analyze the provided performance data (top performers, worst performers, total return).
Provide a concise JSON output with:
{
    "performance_summary": "1 sentence on overall return vs invested capital.",
    "top_performer_insight": "1 sentence highlighting the best winner and its contribution.",
    "concern_areas": ["concern 1", "concern 2"]
}"""
    user_prompt = f"Performance Data:\n{json.dumps(data, indent=2)}\nGenerate the JSON output."
    result = call_llm_json(sys_prompt, user_prompt)
    return result if isinstance(result, dict) else {}


def generate_risk_narrative(data: dict) -> dict:
    sys_prompt = """You are a risk management analyst reviewing a portfolio.
Analyze the provided risk metrics (HHI, Sector Concentration, Top Holdings).
If this is a Family account, specifically analyze cross-account overlaps.
Provide a concise JSON output with:
{
    "risk_summary": "1-2 sentences on diversification and overall risk level.",
    "key_risks": ["risk 1", "risk 2"],
    "recommendations": ["rec 1", "rec 2"]
}"""
    user_prompt = f"Risk Data:\n{json.dumps(data, indent=2)}\nGenerate the JSON output."
    result = call_llm_json(sys_prompt, user_prompt)
    return result if isinstance(result, dict) else {}


def generate_insights(base_insights: list[dict], context_data: dict) -> list[dict]:
    sys_prompt = """You are an AI financial advisor generating targeted alerts.
You will be given a set of basic algorithmic insights and overall portfolio context.
Your job is to enhance them, select the 4 most important ones, and format them beautifully.
Types must be exactly one of: 'danger', 'warning', 'success', 'info'.
Title should be 2-3 words. Description should be a crisp 1 sentence explanation with the relevant percentage/number.
Return JSON:
{
    "insights": [
        {"type": "success", "title": "...", "description": "..."}, ...
    ]
}"""
    user_prompt = f"Portfolio Context:\n{json.dumps(context_data, indent=2)}\n\nAlgorithmic Insights:\n{json.dumps(base_insights, indent=2)}\nGenerate the JSON output."
    result = call_llm_json(sys_prompt, user_prompt)
    if result and isinstance(result, dict) and "insights" in result:
        return result["insights"]
    return base_insights
