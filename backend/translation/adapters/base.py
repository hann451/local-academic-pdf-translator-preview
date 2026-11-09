"""Base interface for translation inference adapters (Section 9)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from backend.domain.models import GlossaryEntry
from backend.translation.prompts.pfml3 import HistoryPair


@dataclass
class TranslationAdapterResponse:
    translated_text: str
    raw_output: str
    prompt_used: str
    model_name: str
    quantization: str
    finish_reason: str = "stop"
    is_mock: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0


class TranslationConnectionError(RuntimeError):
    """Raised when the local inference server is unreachable or rejects connection."""


class SecurityPolicyViolationError(PermissionError):
    """Raised when an external translation endpoint is used without explicit opt-in."""


class TranslationAdapter(ABC):
    adapter_version: str = "1.0.0"
    prompt_version: str = "pfml3-v1"
    is_mock: bool = False

    @abstractmethod
    def translate_segment(
        self,
        source_text: str,
        source_lang: str = "en",
        target_lang: str = "ja",
        form: str = "plain",
        context: str | None = None,
        history: list[HistoryPair] | None = None,
        glossary_entries: list[GlossaryEntry] | None = None,
        context_budget_tokens: int = 8192,
    ) -> TranslationAdapterResponse:
        """Translate a single masked paragraph or sentence segment."""
        raise NotImplementedError

    @abstractmethod
    def check_health(self) -> dict[str, str | bool | None]:
        """Inspect inference backend availability, distinguishing unreachable vs model errors."""
        raise NotImplementedError
