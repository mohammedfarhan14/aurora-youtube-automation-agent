"""Deterministic inference provider for tests and local development."""

from app.llm.contracts import LLMRequest, LLMResponse, LLMUsage


class MockLLMProvider:
    """Return a fixed response without network access or hidden model behavior."""

    def __init__(
        self,
        response_text: str = "Deterministic mock response. No factual research was performed.",
    ) -> None:
        if not isinstance(response_text, str) or not response_text.strip():
            raise ValueError("Mock response text cannot be empty.")
        self._response_text = response_text

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if not isinstance(request, LLMRequest):
            raise TypeError("request must be an LLMRequest.")
        return LLMResponse(
            content=self._response_text,
            provider="mock",
            model=request.model,
            finish_reason="stop",
            usage=LLMUsage(input_tokens=0, output_tokens=0, total_tokens=0),
            request_id=None,
        )
