from __future__ import annotations

import os
import time
from contextlib import suppress

import httpx
from google import genai
from google.genai import errors, types

from .base import (
    AIConfigurationError,
    AIInvalidResponseError,
    AIProviderError,
    AIRateLimitError,
    AIRetryableProviderError,
    AITimeoutError,
    GenerationRequest,
    GenerationResult,
)


# Retained as an import-compatible alias for older callers.
GeminiConfigurationError = AIConfigurationError


class GeminiProvider:
    """Google Gemini adapter implementing Yiyara's neutral provider contract."""

    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 30.0,
    ):
        api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise AIConfigurationError("GEMINI_API_KEY is not configured")

        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.client = self._create_client(timeout_seconds)

    def _create_client(self, timeout_seconds: float):
        return genai.Client(
            api_key=self.api_key,
            http_options=types.HttpOptions(
                timeout=max(1, round(timeout_seconds * 1000)),
                # Keep retry policy in AIGateway so fallbacks have one bounded budget.
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def generate(self, request: GenerationRequest) -> GenerationResult:
        config_args = {
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
        }
        if request.system_prompt:
            config_args["system_instruction"] = request.system_prompt
        if request.response_schema:
            config_args.update(
                response_mime_type="application/json",
                response_json_schema=request.response_schema,
            )

        client = self.client
        close_client = False
        if request.timeout_seconds < self.timeout_seconds:
            # A fallback may start near the shared gateway deadline. Use a
            # shorter-lived client so this SDK call cannot overrun that budget.
            client = self._create_client(request.timeout_seconds)
            close_client = True

        started_at = time.monotonic()
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=request.prompt,
                config=types.GenerateContentConfig(**config_args),
            )
        except errors.APIError as exc:
            self._raise_api_error(exc)
        except httpx.TimeoutException as exc:
            raise AITimeoutError("gemini", "Gemini request timed out") from exc
        except httpx.TransportError as exc:
            raise AIRetryableProviderError(
                "gemini",
                "Gemini transport failed",
            ) from exc
        finally:
            if close_client:
                with suppress(Exception):
                    client.close()

        content = response.text
        if not isinstance(content, str) or not content.strip():
            raise AIInvalidResponseError(
                "gemini",
                "Gemini returned an empty response",
            )

        usage_metadata = getattr(response, "usage_metadata", None)
        usage = {}
        if usage_metadata:
            usage = {
                "input_tokens": getattr(usage_metadata, "prompt_token_count", None),
                "output_tokens": getattr(
                    usage_metadata,
                    "candidates_token_count",
                    None,
                ),
                "total_tokens": getattr(usage_metadata, "total_token_count", None),
            }
            usage = {key: value for key, value in usage.items() if value is not None}

        return GenerationResult(
            content=content.strip(),
            provider=self.name,
            model=self.model,
            latency_ms=round((time.monotonic() - started_at) * 1000),
            usage=usage,
        )

    @staticmethod
    def _raise_api_error(exc: errors.APIError) -> None:
        code = getattr(exc, "code", None)
        if code == 429:
            raise AIRateLimitError("gemini", "Gemini rate limit exceeded") from exc
        if code in {408, 409, 425} or (
            isinstance(code, int) and code >= 500
        ):
            raise AIRetryableProviderError(
                "gemini",
                f"Gemini temporarily unavailable ({code})",
            ) from exc
        raise AIProviderError(
            "gemini",
            f"Gemini rejected the request ({code or 'unknown'})",
        ) from exc
