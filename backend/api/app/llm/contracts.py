"""Provider-independent request and response contracts for text inference."""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class LLMMessage(BaseModel):
    """A single text-only chat message."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    role: Literal["system", "developer", "user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class LLMRequest(BaseModel):
    """Bounded inference request; provider credentials are never part of it."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    model: str = Field(min_length=1, max_length=200)
    messages: tuple[LLMMessage, ...] = Field(min_length=1, max_length=100)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=1024, gt=0, le=32_768)
    response_format: Literal["json_object"] | None = None
    response_schema: dict[str, object] | None = None
    thinking_enabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)


class LLMUsage(BaseModel):
    """Provider-reported token counts; absent values remain unknown."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class LLMResponse(BaseModel):
    """Normalized model output with provider/model provenance."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    content: str
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    finish_reason: str | None = Field(default=None, max_length=100)
    usage: LLMUsage = Field(default_factory=LLMUsage)
    request_id: str | None = Field(default=None, max_length=200)


class LLMProvider(Protocol):
    """Narrow asynchronous inference port used by domain/application services."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate text without exposing provider-specific response objects."""
        ...
