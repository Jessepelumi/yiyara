from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import lru_cache
from typing import Any, Callable, Mapping, TypeVar

from django.conf import settings
from django.utils.module_loading import import_string

from ai.providers.base import (
    AIConfigurationError,
    AIInvalidResponseError,
    AIProvider,
    AIProviderError,
    AIUnavailableError,
    GenerationRequest,
    GenerationResult,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


class AITask(StrEnum):
    GOAL_DECOMPOSITION = "goal_decomposition"
    PLAN_ITERATION = "plan_iteration"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 2
    min_delay_seconds: float = 0.5
    max_delay_seconds: float = 4.0
    total_timeout_seconds: float = 45.0

    def __post_init__(self):
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.min_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")
        if self.min_delay_seconds > self.max_delay_seconds:
            raise ValueError("min_delay_seconds cannot exceed max_delay_seconds")
        if self.total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds must be positive")


class AIGateway:
    """Routes tasks across providers with bounded retries and fallback."""

    def __init__(
        self,
        *,
        providers: Mapping[str, AIProvider],
        routes: Mapping[str, tuple[str, ...]],
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ):
        self.providers = dict(providers)
        self.routes = dict(routes)
        self.retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._random_uniform = random_uniform

    def generate_json(
        self,
        *,
        task: AITask | str,
        prompt: str,
        system_prompt: str,
        response_schema: Mapping[str, Any],
        schema_name: str,
        temperature: float = 0.1,
        max_output_tokens: int = 4096,
        timeout_seconds: float = 30.0,
        validator: Callable[[Any], T] | None = None,
    ) -> Any:
        request = GenerationRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            response_schema=response_schema,
            schema_name=schema_name,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )

        def parse_json(result: GenerationResult):
            try:
                value = json.loads(result.content)
            except json.JSONDecodeError as exc:
                raise AIInvalidResponseError(
                    result.provider,
                    f"{result.provider} returned malformed JSON",
                ) from exc
            if validator is None:
                return value
            try:
                return validator(value)
            except AIProviderError:
                raise
            except Exception as exc:
                raise AIInvalidResponseError(
                    result.provider,
                    f"{result.provider} returned JSON that failed validation",
                ) from exc

        return self._execute(task, request, parse_json)

    def generate_text(
        self,
        *,
        task: AITask | str,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.3,
        max_output_tokens: int = 2048,
        timeout_seconds: float = 30.0,
    ) -> str:
        request = GenerationRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        return self._execute(task, request, lambda result: result.content)

    def _execute(
        self,
        task: AITask | str,
        request: GenerationRequest,
        transform: Callable[[GenerationResult], T],
    ) -> T:
        task_name = task.value if isinstance(task, AITask) else str(task)
        route = self.routes.get(task_name, ())
        if not route:
            raise AIConfigurationError(f"No AI route configured for {task_name}")

        deadline = self._monotonic() + self.retry_policy.total_timeout_seconds
        failures: list[str] = []

        for provider_name in route:
            provider = self.providers.get(provider_name)
            if provider is None:
                failures.append(f"{provider_name}: not configured")
                continue

            for attempt in range(1, self.retry_policy.max_attempts + 1):
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise AIUnavailableError(
                        f"AI deadline exceeded for {task_name}"
                    )

                bounded_request = replace(
                    request,
                    timeout_seconds=min(request.timeout_seconds, remaining),
                )
                try:
                    result = provider.generate(bounded_request)
                    transformed = transform(result)
                    logger.info(
                        "AI request succeeded",
                        extra={
                            "ai_task": task_name,
                            "ai_provider": result.provider,
                            "ai_model": result.model,
                            "ai_attempt": attempt,
                            "ai_latency_ms": result.latency_ms,
                            "ai_usage": dict(result.usage),
                        },
                    )
                    return transformed
                except AIProviderError as exc:
                    failures.append(f"{provider_name}: {type(exc).__name__}")
                    logger.warning(
                        "AI provider attempt failed",
                        extra={
                            "ai_task": task_name,
                            "ai_provider": provider_name,
                            "ai_attempt": attempt,
                            "ai_error": type(exc).__name__,
                        },
                    )
                    if not exc.retryable or attempt >= self.retry_policy.max_attempts:
                        break
                    if (
                        exc.retry_after is not None
                        and exc.retry_after > self.retry_policy.max_delay_seconds
                    ):
                        break

                    delay = self._retry_delay(attempt, exc.retry_after)
                    if delay >= deadline - self._monotonic():
                        break
                    self._sleep(delay)
                except Exception:
                    failures.append(f"{provider_name}: unexpected error")
                    logger.exception(
                        "Unexpected AI adapter failure",
                        extra={
                            "ai_task": task_name,
                            "ai_provider": provider_name,
                            "ai_attempt": attempt,
                        },
                    )
                    break

        failure_summary = ", ".join(failures) or "no configured providers"
        raise AIUnavailableError(
            f"No AI provider completed {task_name} ({failure_summary})"
        )

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        exponential_cap = min(
            self.retry_policy.max_delay_seconds,
            self.retry_policy.min_delay_seconds * (2 ** (attempt - 1)),
        )
        jittered = self._random_uniform(0.0, exponential_cap)
        return max(jittered, retry_after or 0.0)


def _create_provider(name: str, config: Mapping[str, Any]) -> AIProvider:
    backend = config.get("BACKEND")
    if not backend:
        raise AIConfigurationError(f"{name} provider backend is not configured")
    provider_class = import_string(backend)
    options = dict(config.get("OPTIONS", {}))
    return provider_class(**options)


@lru_cache(maxsize=1)
def get_ai_gateway() -> AIGateway:
    routes = {
        str(task): tuple(provider_names)
        for task, provider_names in settings.AI_ROUTES.items()
    }
    referenced_names = dict.fromkeys(
        name for provider_names in routes.values() for name in provider_names
    )
    providers: dict[str, AIProvider] = {}
    for name in referenced_names:
        config = settings.AI_PROVIDERS.get(name)
        if not config:
            logger.warning("AI provider %s has no configuration", name)
            continue
        try:
            providers[name] = _create_provider(name, config)
        except AIConfigurationError as exc:
            logger.info("AI provider %s disabled: %s", name, exc)

    if not providers:
        raise AIConfigurationError("No configured AI provider is available")

    return AIGateway(
        providers=providers,
        routes=routes,
        retry_policy=RetryPolicy(
            max_attempts=settings.AI_RETRY_MAX_ATTEMPTS,
            min_delay_seconds=settings.AI_RETRY_MIN_SECONDS,
            max_delay_seconds=settings.AI_RETRY_MAX_SECONDS,
            total_timeout_seconds=settings.AI_TOTAL_TIMEOUT_SECONDS,
        ),
    )


def reset_ai_gateway() -> None:
    """Clear the process-local gateway, primarily for settings-based tests."""

    get_ai_gateway.cache_clear()
