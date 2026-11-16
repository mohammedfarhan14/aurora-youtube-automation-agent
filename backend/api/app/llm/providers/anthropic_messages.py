"""Native Anthropic Messages API adapter for the provider-neutral LLM contract."""

from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from app.llm.contracts import LLMMessage, LLMRequest, LLMResponse, LLMUsage
from app.llm.providers.openai_compatible import LLMProviderError


class AnthropicMessagesLLMProvider:
    """Call Anthropic's Messages API without exposing provider data or secrets."""

    API_VERSION = "2023-06-01"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.anthropic.com"
            or parsed.path.rstrip("/") != "/v1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Anthropic requires https://api.anthropic.com/v1.")
        if timeout_seconds <= 0 or timeout_seconds > 600:
            raise ValueError("LLM timeout must be greater than 0 and at most 600 seconds.")
        token = api_key.get_secret_value().strip()
        if not token:
            raise ValueError("Anthropic API key cannot be empty.")

        self._token = token
        self._endpoint = f"{base_url.rstrip('/')}/messages"
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
            follow_redirects=False,
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if not isinstance(request, LLMRequest):
            raise TypeError("request must be an LLMRequest.")

        system_parts = [
            message.content
            for message in request.messages
            if message.role in {"system", "developer"}
        ]
        messages = _anthropic_messages(request.messages)
        if not messages or messages[0]["role"] != "user" or messages[-1]["role"] != "user":
            raise ValueError("Anthropic messages must begin and end with a user turn.")

        payload: dict[str, object] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "messages": messages,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": self._token,
            "anthropic-version": self.API_VERSION,
        }
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
            blocks = data["content"]
            content = "".join(
                block["text"]
                for block in blocks
                if block.get("type") == "text" and isinstance(block.get("text"), str)
            )
            if not content:
                raise ValueError("Expected text content.")
            usage = data.get("usage") or {}
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            return LLMResponse(
                content=content,
                provider="anthropic",
                model=data.get("model") or request.model,
                finish_reason=data.get("stop_reason"),
                usage=LLMUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=(input_tokens + output_tokens)
                    if isinstance(input_tokens, int) and isinstance(output_tokens, int)
                    else None,
                ),
                request_id=data.get("id"),
            )
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMProviderError("Configured LLM provider returned an invalid response.") from exc


def _anthropic_messages(messages: tuple[LLMMessage, ...]) -> list[dict[str, str]]:
    """Drop top-level instructions and merge adjacent same-role turns."""
    result: list[dict[str, str]] = []
    for message in messages:
        if message.role in {"system", "developer"}:
            continue
        if message.role not in {"user", "assistant"}:
            raise ValueError("Anthropic messages support only user and assistant turns.")
        if result and result[-1]["role"] == message.role:
            result[-1]["content"] += "\n\n" + message.content
        else:
            result.append({"role": message.role, "content": message.content})
    return result
