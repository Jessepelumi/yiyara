"""Backward-compatible import for the generic OpenAI-compatible adapter."""

from .openai_compatible_provider import OpenAICompatibleProvider

ChatGPTProvider = OpenAICompatibleProvider

__all__ = ["ChatGPTProvider"]
