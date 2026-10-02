from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """Vendor-neutral request passed to every model adapter."""

    prompt: str
    system_prompt: str = ""
    response_schema: Mapping[str, Any] | None = None
    schema_name: str = "response"
    temperature: float = 0.1
    max_output_tokens: int = 4096
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Normalized model response with safe operational metadata."""

    content: str
    provider: str
    model: str
    latency_ms: int
    usage: Mapping[str, Any] = field(default_factory=dict)


class AIProvider(Protocol):
    name: str
    model: str

    def generate(self, request: GenerationRequest) -> GenerationResult: ...


class AIError(RuntimeError):
    """Base error for the provider-agnostic AI layer."""


class AIConfigurationError(AIError):
    """Raised when no usable provider configuration exists."""


class AIProviderError(AIError):
    """A sanitized provider failure safe to route and log."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
        self.retry_after = retry_after


class AIRetryableProviderError(AIProviderError):
    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retry_after: float | None = None,
    ):
        super().__init__(
            provider,
            message,
            retryable=True,
            retry_after=retry_after,
        )


class AIRateLimitError(AIRetryableProviderError):
    """The provider rejected the request because its quota is exhausted."""


class AITimeoutError(AIRetryableProviderError):
    """The provider did not complete within the per-attempt timeout."""


class AIInvalidResponseError(AIRetryableProviderError):
    """The provider returned empty or malformed structured output."""


class AIUnavailableError(AIError):
    """Every provider in the selected route failed or exceeded the deadline."""
