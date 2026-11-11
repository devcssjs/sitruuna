"""The AI connector. Switch between Gemini (free) and Claude (paid) with
LLM_PROVIDER in your .env file. Nothing else in the code needs to change.
"""
from __future__ import annotations

import base64
import re
import time

from .config import Settings


class AIError(Exception):
    pass


class QuotaExhausted(AIError):
    """The AI's daily free limit (or paid credit) is used up. Stop and wait."""

    def __init__(self, msg: str, retry_seconds: int | None = None):
        super().__init__(msg)
        self.retry_seconds = retry_seconds


class BaseLLM:
    name = "base"

    def __init__(self, settings: Settings):
        self.s = settings
        self.gap = float(settings.get("ai", "seconds_between_calls", default=6))
        self._last = 0.0

    def ask(self, system: str, prompt: str, files: list[tuple[str, str, bytes]]) -> str:
        """Send text (+ optional PDFs/images) and get the AI's reply as text.
        Waits between calls and retries when the provider says 'slow down'."""
        for attempt in range(4):
            wait = self.gap - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                return self._call(system, prompt, files)
            except Exception as e:
                if _is_quota_exhausted(e):
                    raise QuotaExhausted(f"{self.name}: daily limit reached", _retry_seconds(e)) from e
                if _is_rate_limit(e) and attempt < 3:
                    time.sleep(20 * (attempt + 1))
                    continue
                raise AIError(f"{self.name}: {e}") from e
        raise AIError(f"{self.name}: too many retries")

    def _call(self, system, prompt, files) -> str:
        raise NotImplementedError


class GeminiLLM(BaseLLM):
    name = "gemini"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        if not settings.gemini_key:
            raise AIError("GEMINI_API_KEY is empty in .env")
        from google import genai

        self.client = genai.Client(api_key=settings.gemini_key)
        self.model = settings.get("ai", "gemini_model", default="gemini-3.5-flash-lite")

    def _call(self, system, prompt, files):
        from google.genai import types

        parts = [types.Part.from_bytes(data=data, mime_type=mime) for _, mime, data in files]
        parts.append(prompt)
        resp = self.client.models.generate_content(
            model=self.model,
            contents=parts,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0,
                response_mime_type="application/json",
            ),
        )
        return resp.text or ""


class ClaudeLLM(BaseLLM):
    name = "claude"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        if not settings.anthropic_key:
            raise AIError("ANTHROPIC_API_KEY is empty in .env")
        import anthropic

        self.client = anthropic.Anthropic(api_key=settings.anthropic_key)
        self.model = settings.get("ai", "claude_model", default="claude-haiku-5-5")
        self.gap = min(self.gap, 1.0)  # paid API doesn't need long waits

    def _call(self, system, prompt, files):
        content = []
        for _, mime, data in files:
            b64 = base64.standard_b64encode(data).decode()
            kind = "document" if mime == "application/pdf" else "image"
            content.append({"type": kind, "source": {"type": "base64", "media_type": mime, "data": b64}})
        content.append({"type": "text", "text": prompt})
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def make_llm(settings: Settings) -> BaseLLM:
    if settings.llm_provider == "claude":
        return ClaudeLLM(settings)
    if settings.llm_provider == "gemini":
        return GeminiLLM(settings)
    raise AIError(f"LLM_PROVIDER must be 'gemini' or 'claude', got '{settings.llm_provider}'")


def _is_rate_limit(e: Exception) -> bool:
    text = str(e).lower()
    code = getattr(e, "status_code", None) or getattr(e, "code", None)
    return code in (429, 529, 503) or "429" in text or "resource_exhausted" in text or "overloaded" in text


def _is_quota_exhausted(e: Exception) -> bool:
    """Daily/credit limit (not the short 'too many per minute' kind)."""
    t = str(e).lower()
    return ("perday" in t or "per day" in t or "exceeded your current quota" in t
            or "credit balance is too low" in t or "insufficient_quota" in t)


def _retry_seconds(e: Exception) -> int | None:
    m = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+)s", str(e))
    return int(m.group(1)) if m else None
