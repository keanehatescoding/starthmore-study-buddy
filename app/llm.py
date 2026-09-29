"""Minimal OpenAI-compatible chat client (stdlib only).

One client covers OpenAI, DeepSeek, OpenRouter, Ollama, and Gemini's
OpenAI-compatible endpoint — only base_url/api_key/model differ (.env).
"""

from __future__ import annotations

import email.utils
import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


class LLMError(RuntimeError):
    pass


class QuotaExhaustedError(LLMError):
    """Repeated 429s: the quota is gone, not a transient blip. Callers
    should stop the run (work is resumable) instead of grinding chunks."""


RETRYABLE = {429, 500, 502, 503, 504}
BACKOFF = [5, 15, 30, 60, 90]
MAX_RETRY_AFTER = 120


def _retry_after(err: urllib.error.HTTPError) -> int | None:
    """Seconds the server asked us to wait: Retry-After is either
    delay-seconds or an HTTP-date (RFC 9110 §10.2.3)."""
    value = (getattr(err, "headers", None) or {}).get("Retry-After")
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        seconds = int(value)
    else:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:  # obsolete date forms without a zone are GMT
            when = when.replace(tzinfo=timezone.utc)
        seconds = math.ceil((when - datetime.now(timezone.utc)).total_seconds())
    return min(max(seconds, 0), MAX_RETRY_AFTER)


def parse_json_content(content: str) -> dict:
    """The model's reply as a JSON object. Without JSON mode, models wrap it
    in ```json fences or prose (which may hold braces of its own), so fall
    back to the largest complete {...} object found anywhere in the text."""
    for strict in (True, False):  # strict=False: raw control chars in strings
        try:
            parsed = json.loads(content, strict=strict)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    best, best_len = None, 0
    for strict in (True, False):
        decoder = json.JSONDecoder(strict=strict)
        pos = content.find("{")
        while pos != -1:
            try:
                parsed, end = decoder.raw_decode(content, pos)
            except json.JSONDecodeError:
                pos = content.find("{", pos + 1)
                continue
            if isinstance(parsed, dict) and end - pos > best_len:
                best, best_len = parsed, end - pos
            pos = content.find("{", end)  # skip past this object
        if best is not None:
            return best
    raise ValueError("no JSON object in reply")


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 180):
        if not api_key:
            raise LLMError("LLM API key is empty — set LLM_API_KEY in .env")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # Some OpenAI-compatible servers (Ollama, Gemini's shim) 400 on
        # response_format; the first such 400 turns it off for this client.
        self.json_mode = True

    def _request(self, system: str, user: str, temperature: float):
        payload = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        return urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

    def complete_json(self, system: str, user: str, temperature: float = 0.2) -> dict:
        last_error = None
        quota_hits = 0
        attempt = 0
        while attempt < len(BACKOFF):
            delay = BACKOFF[attempt]
            try:
                req = self._request(system, user, temperature)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode())
                break
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code == 400 and self.json_mode:
                    self.json_mode = False  # retry now, without JSON mode
                    continue
                if e.code not in RETRYABLE:
                    raise LLMError(f"chat completion failed: {e}") from e
                if e.code == 429:
                    quota_hits += 1
                    if quota_hits >= 3:
                        raise QuotaExhaustedError(
                            f"LLM quota exhausted (3 consecutive 429s): {e}"
                        ) from e
                    hint = _retry_after(e)
                    if hint is not None:  # the server's backoff replaces ours
                        delay = hint
                else:
                    quota_hits = 0  # a 5xx is transient, not quota
            except Exception as e:  # network blip — retry
                last_error = e
                quota_hits = 0
            attempt += 1
            if attempt < len(BACKOFF):
                time.sleep(delay)
        else:
            raise LLMError(f"chat completion failed after retries: {last_error}") from last_error
        try:
            return parse_json_content(body["choices"][0]["message"]["content"])
        except Exception as e:
            raise LLMError(f"bad LLM response: {e}\n{str(body)[:500]}") from e
