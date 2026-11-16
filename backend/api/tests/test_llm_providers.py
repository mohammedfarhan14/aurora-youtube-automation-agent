import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.llm import (
    AnthropicMessagesLLMProvider,
    LLMMessage,
    LLMProviderError,
    LLMRequest,
    MockLLMProvider,
    OllamaNativeLLMProvider,
    OpenAICompatibleLLMProvider,
)


def make_request() -> LLMRequest:
    return LLMRequest(
        model="qwen2.5:7b",
        messages=(
            LLMMessage(role="system", content="Return a concise answer."),
            LLMMessage(role="user", content="What is a solar cell?"),
        ),
        temperature=0.1,
        max_output_tokens=128,
        response_format="json_object",
        timeout_seconds=37,
    )


def test_request_contract_is_strict_and_bounded():
    with pytest.raises(ValidationError):
        LLMMessage(role="user", content="hello", tool_call="shell")

    with pytest.raises(ValidationError):
        LLMRequest(
            model="local-model",
            messages=(LLMMessage(role="user", content="hello"),),
            max_output_tokens=100_000,
        )


def test_mock_provider_is_deterministic_and_reports_mock_provenance():
    provider = MockLLMProvider("fixed test output")

    first = asyncio.run(provider.generate(make_request()))
    second = asyncio.run(provider.generate(make_request()))

    assert first == second
    assert first.content == "fixed test output"
    assert first.provider == "mock"
    assert first.model == "qwen2.5:7b"
    assert first.usage.total_tokens == 0


def test_mock_provider_rejects_empty_configured_response():
    with pytest.raises(ValueError, match="cannot be empty"):
        MockLLMProvider("  ")


def test_openai_compatible_adapter_sends_normalized_request_and_api_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(
            200,
            json={
                "id": "req-123",
                "model": "qwen2.5:7b",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "<think>private reasoning</think>\nA solar cell converts light."
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 8,
                    "total_tokens": 20,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleLLMProvider(
        base_url="http://localhost:11434/v1/",
        api_key=SecretStr("local-test-key"),
        reasoning_effort="none",
        client=client,
    )

    result = asyncio.run(provider.generate(make_request()))
    asyncio.run(provider.aclose())

    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["authorization"] == "Bearer local-test-key"
    assert captured["body"]["model"] == "qwen2.5:7b"
    assert captured["body"]["max_tokens"] == 128
    assert captured["body"]["reasoning_effort"] == "none"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["messages"][1]["content"] == "What is a solar cell?"
    assert captured["timeout"]["read"] == 37
    assert result.content == "A solar cell converts light."
    assert result.provider == "openai-compatible"
    assert result.request_id == "req-123"
    assert result.usage.total_tokens == 20
    assert not client.is_closed


def test_openai_compatible_adapter_sends_json_schema_when_requested():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"title":"ok"}'}, "finish_reason": "stop"}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleLLMProvider(
        base_url="http://localhost:11434/v1",
        client=client,
    )
    request = make_request().model_copy(
        update={
            "response_format": None,
            "response_schema": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
        }
    )

    asyncio.run(provider.generate(request))
    asyncio.run(client.aclose())

    assert captured["body"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "aurora_story_draft",
            "strict": True,
            "schema": request.response_schema,
        },
    }


def test_ollama_native_adapter_uses_thinking_toggle_and_schema():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "qwen3:4b",
                "message": {"role": "assistant", "content": '{"title":"Moon illusion"}'},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 20,
                "eval_count": 8,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaNativeLLMProvider(
        base_url="http://ollama:11434",
        client=client,
    )
    request = make_request().model_copy(
        update={
            "response_format": None,
            "response_schema": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
            "thinking_enabled": False,
        }
    )

    result = asyncio.run(provider.generate(request))
    asyncio.run(client.aclose())

    assert captured["url"] == "http://ollama:11434/api/chat"
    assert captured["body"]["think"] is False
    assert captured["body"]["format"] == request.response_schema
    assert captured["body"]["stream"] is False
    assert captured["body"]["options"]["num_predict"] == 128
    assert result.finish_reason == "stop"
    assert result.usage.total_tokens == 28


def test_openai_compatible_adapter_supports_local_endpoint_without_api_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "local response"}, "finish_reason": "stop"}],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleLLMProvider(
        base_url="http://localhost:11434/v1",
        client=client,
    )

    result = asyncio.run(provider.generate(make_request()))
    asyncio.run(client.aclose())

    assert captured["authorization"] is None
    assert result.content == "local response"
    assert result.usage.total_tokens is None


def test_huggingface_router_compatible_chat_returns_provider_provenance():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Hosted response"}, "finish_reason": "stop"}]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleLLMProvider(
        base_url="https://router.huggingface.co/v1",
        api_key=SecretStr("hf-test-token"),
        provider_id="huggingface-inference-providers",
        client=client,
    )
    result = asyncio.run(provider.generate(make_request()))
    asyncio.run(client.aclose())

    assert captured["url"] == "https://router.huggingface.co/v1/chat/completions"
    assert result.provider == "huggingface-inference-providers"


def test_anthropic_messages_adapter_normalizes_messages_and_usage():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_test_123",
                "type": "message",
                "role": "assistant",
                "model": "claude-test-model",
                "content": [
                    {"type": "thinking", "thinking": "private reasoning"},
                    {"type": "text", "text": "A solar cell converts light."},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 8},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicMessagesLLMProvider(
        base_url="https://api.anthropic.com/v1",
        api_key=SecretStr("anthropic-test-key"),
        client=client,
    )
    result = asyncio.run(provider.generate(make_request()))
    asyncio.run(client.aclose())

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "anthropic-test-key"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["body"]["system"] == "Return a concise answer."
    assert captured["body"]["messages"] == [{"role": "user", "content": "What is a solar cell?"}]
    assert "temperature" not in captured["body"]
    assert result.provider == "anthropic"
    assert result.content == "A solar cell converts light."
    assert result.usage.total_tokens == 20


def test_anthropic_adapter_rejects_noncanonical_api_hosts():
    with pytest.raises(ValueError, match="api.anthropic.com"):
        AnthropicMessagesLLMProvider(
            base_url="https://attacker.example/v1",
            api_key=SecretStr("test-key"),
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///etc/passwd",
        "http://user:password@localhost:11434/v1",
        "http://localhost:11434/v1?token=secret",
        "http://localhost:11434/v1#fragment",
    ],
)
def test_adapter_rejects_invalid_base_urls(base_url):
    with pytest.raises(ValueError, match="base URL"):
        OpenAICompatibleLLMProvider(base_url=base_url)


def test_adapter_rejects_non_positive_or_excessive_timeouts():
    with pytest.raises(ValueError, match="timeout"):
        OpenAICompatibleLLMProvider(base_url="http://localhost:11434/v1", timeout_seconds=0)
    with pytest.raises(ValueError, match="timeout"):
        OpenAICompatibleLLMProvider(base_url="http://localhost:11434/v1", timeout_seconds=601)


def test_provider_http_failure_is_sanitized_and_does_not_expose_body():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, text="private upstream response body")
        )
    )
    provider = OpenAICompatibleLLMProvider(
        base_url="http://localhost:11434/v1",
        client=client,
    )

    with pytest.raises(LLMProviderError, match="request failed") as error:
        asyncio.run(provider.generate(make_request()))

    assert "private upstream response body" not in str(error.value)
    asyncio.run(client.aclose())


def test_provider_rejects_malformed_response_without_leaking_payload():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": []}))
    )
    provider = OpenAICompatibleLLMProvider(
        base_url="http://localhost:11434/v1",
        client=client,
    )

    with pytest.raises(LLMProviderError, match="invalid response"):
        asyncio.run(provider.generate(make_request()))

    asyncio.run(client.aclose())


def test_adapter_closes_only_a_client_it_owns():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    provider = OpenAICompatibleLLMProvider(
        base_url="http://localhost:11434/v1",
        client=client,
    )

    asyncio.run(provider.aclose())

    assert not client.is_closed
    asyncio.run(client.aclose())
