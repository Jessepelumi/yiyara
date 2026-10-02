from __future__ import annotations

import time
from typing import Any, Mapping

import httpx

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


class OpenAICompatibleProvider:
    """Adapter for Groq, OpenRouter, Ollama, and compatible chat APIs."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        api_key: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        extra_body: Mapping[str, Any] | None = None,
        structured_mode: str = "json_schema",
    ):
        if not base_url:
            raise AIConfigurationError(f"{name} base URL is not configured")
        if not model:
            raise AIConfigurationError(f"{name} model is not configured")
        if not api_key and name != "ollama":
            raise AIConfigurationError(f"{name} API key is not configured")
        if structured_mode not in {"json_schema", "json_object", "prompt"}:
            raise AIConfigurationError(
                f"{name} structured mode must be json_schema, json_object, or prompt"
            )

        self.name = name
        self.model = model
        self.structured_mode = structured_mode
        self.extra_body = dict(extra_body or {})
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **dict(extra_headers or {}),
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
        )

    def generate(self, request: GenerationRequest) -> GenerationResult:
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            **self.extra_body,
        }
        if request.response_schema:
            if self.structured_mode == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.schema_name,
                        "strict": False,
                        "schema": request.response_schema,
                    },
                }
            elif self.structured_mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
            else:
                messages[-1]["content"] += (
                    "\n\nReturn JSON only, matching this JSON Schema:\n"
                    f"{request.response_schema}"
                )

        started_at = time.monotonic()
        try:
            response = self.client.post(
                "chat/completions",
                json=payload,
                timeout=request.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise AITimeoutError(self.name, f"{self.name} request timed out") from exc
        except httpx.TransportError as exc:
            raise AIRetryableProviderError(
                self.name,
                f"{self.name} transport failed",
            ) from exc

        if response.status_code >= 400:
            self._raise_for_status(response)

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AIInvalidResponseError(
                self.name,
                f"{self.name} returned an invalid response envelope",
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise AIInvalidResponseError(
                self.name,
                f"{self.name} returned an empty response",
            )

        usage = data.get("usage") if isinstance(data, dict) else None
        return GenerationResult(
            content=content.strip(),
            provider=self.name,
            model=self.model,
            latency_ms=round((time.monotonic() - started_at) * 1000),
            usage=usage if isinstance(usage, dict) else {},
        )

    def _raise_for_status(self, response: httpx.Response) -> None:
        status_code = response.status_code
        if status_code == 429:
            raise AIRateLimitError(
                self.name,
                f"{self.name} rate limit exceeded",
                retry_after=self._retry_after(response),
            )
        if status_code in {408, 409, 425} or status_code >= 500:
            raise AIRetryableProviderError(
                self.name,
                f"{self.name} temporarily unavailable ({status_code})",
            )
        raise AIProviderError(
            self.name,
            f"{self.name} rejected the request ({status_code})",
        )

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None
