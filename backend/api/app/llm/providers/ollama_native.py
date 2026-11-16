"""Native Ollama chat adapter for thinking controls and schema-constrained output."""

import httpx
from pydantic import SecretStr, ValidationError

from app.llm.contracts import LLMRequest, LLMResponse, LLMUsage


class OllamaNativeLLMProvider:
    """Use Ollama's /api/chat API so Qwen thinking mode is actually controllable."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr | None = None,
        timeout_seconds: int = 120,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._client = client or httpx.AsyncClient(trust_env=False)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if not isinstance(request, LLMRequest):
            raise TypeError("request must be an LLMRequest.")
        payload: dict[str, object] = {
            "model": request.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_output_tokens,
            },
        }
        if request.response_schema is not None:
            payload["format"] = request.response_schema
        elif request.response_format == "json_object":
            payload["format"] = "json"
        if request.thinking_enabled is not None:
            payload["think"] = request.thinking_enabled
        headers = {"Accept": "application/json"}
        if self._api_key is not None and self._api_key.get_secret_value().strip():
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        try:
            response = await self._client.post(
                f"{self._base_url}/api/chat",
                json=payload,
                headers=headers,
                timeout=request.timeout_seconds or self._timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            message = data["message"]
            content = message["content"]
            if not isinstance(content, str) or not content.strip():
                raise TypeError("Ollama returned no user-facing content.")
            prompt_tokens = data.get("prompt_eval_count")
            output_tokens = data.get("eval_count")
            usage = LLMUsage(
                input_tokens=prompt_tokens,
                output_tokens=output_tokens,
                total_tokens=(
                    prompt_tokens + output_tokens
                    if isinstance(prompt_tokens, int) and isinstance(output_tokens, int)
                    else None
                ),
            )
            return LLMResponse(
                content=content.strip(),
                provider="ollama",
                model=data.get("model") or request.model,
                finish_reason=data.get("done_reason"),
                usage=usage,
                request_id=None,
            )
        except (
            httpx.HTTPError,
            ValueError,
            AttributeError,
            KeyError,
            TypeError,
            ValidationError,
        ) as exc:
            raise RuntimeError(
                "Configured Ollama chat request failed or returned an invalid response."
            ) from exc
