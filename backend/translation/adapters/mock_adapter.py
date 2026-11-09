"""Deterministic Mock Translation Adapter for CI, Unit, and Integration Testing (Section 0.6, 17, 18).

Always marks `is_mock=True` so mock test runs are strictly distinguished from real
PLaMo + llama.cpp executions.
Supports fault injection (missing placeholders, truncation, connection drop, repetition)
to verify retry and fallback-to-original guarantees.
"""

from __future__ import annotations

import re
from typing import Callable

from backend.domain.models import GlossaryEntry
from backend.translation.adapters.base import (
    TranslationAdapter,
    TranslationAdapterResponse,
    TranslationConnectionError,
)
from backend.translation.prompts.pfml3 import (
    HistoryPair,
    plan_prompt_within_budget,
    strip_plamo_control_tokens,
)


class MockPlamoAdapter(TranslationAdapter):
    adapter_version: str = "mock-1.0.0"
    prompt_version: str = "pfml3-v1"
    is_mock: bool = True

    def __init__(
        self,
        model_name: str = "mock-plamo-3-translate-2b",
        quantization: str = "MOCK",
        fault_injector: Callable[[str, int], tuple[str, str] | None] | None = None,
    ) -> None:
        self.model_name = model_name
        self.quantization = quantization
        self.fault_injector = fault_injector
        self.call_count = 0
        self.call_Count_by_source: dict[str, int] = {}

    def check_health(self) -> dict[str, str | bool | None]:
        return {
            "status": "connected_mock",
            "is_mock": True,
            "model_name": self.model_name,
            "quantization": self.quantization,
            "detail": "モック翻訳アダプター（実モデル未接続・テスト用）",
        }

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
        self.call_count += 1
        attempt = self.call_Count_by_source.get(source_text, 0) + 1
        self.call_Count_by_source[source_text] = attempt

        budget_plan = plan_prompt_within_budget(
            source_text=source_text,
            context_budget_tokens=context_budget_tokens,
            source_lang=source_lang,
            target_lang=target_lang,
            form=form,
            context=context,
            history=history,
            glossary_entries=glossary_entries,
        )

        if self.fault_injector is not None:
            fault = self.fault_injector(source_text, attempt)
            if fault is not None:
                raw_out, reason = fault
                if reason == "connection_error":
                    raise TranslationConnectionError("Simulated llama-server connection drop")
                return TranslationAdapterResponse(
                    translated_text=strip_plamo_control_tokens(raw_out),
                    raw_output=raw_out,
                    prompt_used=budget_plan.prompt,
                    model_name=self.model_name,
                    quantization=self.quantization,
                    finish_reason=reason,
                    is_mock=True,
                    prompt_tokens=budget_plan.estimated_input_tokens,
                    completion_tokens=len(raw_out) // 2 + 1,
                )

        # Deterministic rule-based academic translation preserving {vN}, [N], numbers, and glossary
        ja_text = self._synthesize_japanese(
            source_text,
            form=form,
            active_glossary=budget_plan.active_glossary,
        )
        raw_with_stop = f"{ja_text}<|plamo:tag|>"
        cleaned = strip_plamo_control_tokens(raw_with_stop)

        return TranslationAdapterResponse(
            translated_text=cleaned,
            raw_output=raw_with_stop,
            prompt_used=budget_plan.prompt,
            model_name=self.model_name,
            quantization=self.quantization,
            finish_reason="stop",
            is_mock=True,
            prompt_tokens=budget_plan.estimated_input_tokens,
            completion_tokens=len(cleaned),
        )

    def _synthesize_japanese(
        self,
        source_text: str,
        form: str,
        active_glossary: list[GlossaryEntry],
    ) -> str:
        working = source_text.strip()
        # Apply active glossary substitutions (case-insensitive)
        for entry in active_glossary:
            pattern = re.compile(re.escape(entry.source), re.IGNORECASE)
            working = pattern.sub(entry.target, working)

        # Replace common academic English phrases while keeping all {vN}, [N], and numbers intact
        replacements = [
            (r"\bAbstract\b", "概要"),
            (r"\bIntroduction\b", "1. はじめに"),
            (r"\bMethodology\b", "2. 手法"),
            (r"\bExperimental Results\b", "3. 実験結果"),
            (r"\bConclusion\b", "4. 結論"),
            (r"\bFigure\b", "図"),
            (r"\bFig\.", "図"),
            (r"\bTable\b", "表"),
            (r"\bWe propose\b", "本研究では提案する："),
            (r"\bIn this paper,?\b", "本論文では、"),
            (r"\bsatisfies\b", "を満たす："),
            (r"\bwhere\b", "ここで"),
            (r"\bdenotes\b", "を表す："),
            (r"\bconverges to\b", "へ収束する："),
            (r"\bwith learning rate\b", "学習率"),
            (r"\band threshold\b", "および閾値"),
            (r"\bas shown in\b", "に示すように"),
            (r"\bdemonstrates that\b", "であることを実証する："),
            (r"\bthe energy function\b", "エネルギー関数"),
            (r"\bthe gradient descent update\b", "勾配降下法の更新式"),
            (r"\bthe loss function\b", "損失関数"),
            (r"\bfor all\b", "すべての"),
        ]
        for pat, rep in replacements:
            working = re.sub(pat, rep, working, flags=re.IGNORECASE)

        ending = "です。" if form == "polite" else "である。"
        if working.endswith("."):
            working = working[:-1] + ending
        elif working == source_text.strip():
            working = working + "（訳）"

        return working
