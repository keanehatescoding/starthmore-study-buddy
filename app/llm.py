"""Minimal OpenAI-compatible chat client (stdlib only).

One client covers OpenAI, DeepSeek, OpenRouter, Ollama, and Gemini's
OpenAI-compatible endpoint — only base_url/api_key/model differ (.env).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


class QuotaExhaustedError(LLMError):
    """Repeated 429s: the quota is gone, not a transient blip. Callers
    should stop the run (work is resumable) instead of grinding chunks."""


RETRYABLE = {429, 500, 502, 503, 504}


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 180):
        if not api_key:
            raise LLMError("LLM API key is empty — set LLM_API_KEY in .env")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete_json(self, system: str, user: str, temperature: float = 0.2) -> dict:
        payload = json.dumps(
            {
                "model": self.model,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        last_error = None
        quota_hits = 0
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode())
                break
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code not in RETRYABLE:
                    raise LLMError(f"chat completion failed: {e}") from e
                if e.code == 429:
                    quota_hits += 1
                    if quota_hits >= 3:
                        raise QuotaExhaustedError(
                            f"LLM quota exhausted (3 consecutive 429s): {e}"
                        ) from e
                    try:  # honor server backoff hint
                        time.sleep(min(int(e.headers.get("Retry-After", 0)), 120))
                    except (TypeError, ValueError):
                        pass
                else:
                    quota_hits = 0  # a 5xx is transient, not quota
            except Exception as e:  # network blip — retry
                last_error = e
                quota_hits = 0
            time.sleep([5, 15, 30, 60, 90][attempt])
        else:
            raise LLMError(f"chat completion failed after retries: {last_error}") from last_error
        try:
            content = body["choices"][0]["message"]["content"]
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return json.loads(content, strict=False)  # raw control chars in strings
        except Exception as e:
            raise LLMError(f"bad LLM response: {e}\n{str(body)[:500]}") from e
