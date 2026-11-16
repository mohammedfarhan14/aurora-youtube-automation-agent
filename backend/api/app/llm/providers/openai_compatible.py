"""OpenAI Chat Completions-compatible adapter (including local Ollama).

The base URL is trusted operator configuration, never model- or tenant-supplied.
The adapter does not perform retries or paid-provider fallback; orchestration
must make those decisions explicitly and record them.
"""

import re
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr, ValidationError

from app.llm.contracts import LLMRequest, LLMResponse, LLMUsage


class LLMProviderError(RuntimeError):
    """Sanitized inference failure that never includes response bodies or secrets."""


class OpenAICompatibleLLMProvider:
    """Call an explicitly configured OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr | None = None,
        provider_id: str = "openai-compatible",
        reasoning_effort: str | None = None,
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "LLM base URL must be an HTTP(S) URL without credentials, query, or fragment."
            )
        if timeout_seconds <= 0 or timeout_seconds > 600:
            raise ValueError("LLM timeout must be greater than 0 and at most 600 seconds.")
        if not provider_id or len(provider_id) > 80:
            raise ValueError("LLM provider identifier must contain 1 to 80 characters.")

        self._provider_id = provider_id
        if reasoning_effort not in {None, "none", "low", "medium", "high", "xhigh"}:
            raise ValueError("LLM reasoning effort is unsupported.")
        self._reasoning_effort = reasoning_effort
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        """Close only the HTTP client created and owned by this adapter."""
        if self._owns_client:
            await self._client.aclose()

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if not isinstance(request, LLMRequest):
            raise TypeError("request must be an LLMRequest.")

        headers = {"Accept": "application/json"}
        if self._api_key is not None:
            secret = self._api_key.get_secret_value()
            if secret:
                headers["Authorization"] = f"Bearer {secret}"

        payload = {
            "model": request.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "aurora_story_draft",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        elif request.response_format is not None:
            payload["response_format"] = {"type": request.response_format}
        if self._reasoning_effort is not None:
            payload["reasoning_effort"] = self._reasoning_effort

        try:
            response = await self._client.post(
                self._endpoint,
                json=payload,
                headers=headers,
                timeout=request.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMProviderError("Configured LLM provider request failed.") from exc

        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message["content"]
            if not isinstance(content, str):
                raise TypeError("Expected text content.")
            # Ollama's Qwen3 OpenAI-compatible endpoint may include its private
            # reasoning in-band as <think>…</think>. Keep only the user-facing
            # answer; a truncated reasoning block is not a usable completion.
            if re.search(r"</think\s*>", content, flags=re.IGNORECASE):
                # Some Ollama versions omit the opening token but retain the
                # closing marker, so discard everything before the last close.
                content = re.split(r"</think\s*>", content, flags=re.IGNORECASE)[-1]
            elif re.search(r"<think\b", content, flags=re.IGNORECASE):
                raise TypeError("Provider returned an incomplete reasoning block.")
            content = content.strip()
            if not content:
                raise TypeError("Provider returned no user-facing answer.")
            usage_data = data.get("usage") or {}
            usage = LLMUsage(
                input_tokens=usage_data.get("prompt_tokens"),
                output_tokens=usage_data.get("completion_tokens"),
                total_tokens=usage_data.get("total_tokens"),
            )
            return LLMResponse(
                content=content,
                provider=self._provider_id,
                model=data.get("model") or request.model,
                finish_reason=choice.get("finish_reason"),
                usage=usage,
                request_id=data.get("id"),
            )
        except (AttributeError, KeyError, IndexError, TypeError, ValidationError) as exc:
            raise LLMProviderError("Configured LLM provider returned an invalid response.") from exc
