from __future__ import annotations

from typing import Any, Optional

from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings

# NOTE: OpenAI SDK is optional for local/dev runs.
# If not installed or API key missing, callers should handle the raised error and fallback to heuristics.
try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


_client: Optional[Any] = None


def _get_client() -> Any:
    global _client
    if _client is not None:
        return _client
    if OpenAI is None:
        raise ValueError("openai package is not installed. Install 'openai' to enable LLM features.")
    if not settings.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is missing. Please set it in .env")
    _client = OpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def generate_text(system: str, user: str, *, temperature: float = 0.5, json_object: bool = False) -> str:
    """Generate text from OpenAI chat.

    If json_object=True, the API is requested to return a valid JSON object.
    The model may still occasionally return invalid JSON; callers must validate.
    """

    client = _get_client()

    kwargs: dict[str, Any] = {}
    if json_object:
        kwargs["response_format"] = {"type": "json_object"}

    resp = client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        **kwargs,
    )

    return (resp.choices[0].message.content or "").strip()
