"""Deterministic composite cache key builder (Section 15).

Cache key incorporates all parameters that impact translation output:
1. Source text (masked_source_text)
2. Model identifier & hash (if available)
3. Quantization
4. Inference settings (temperature, max_tokens)
5. Prompt format version (`pfml3-v1`)
6. Source/Target language & Japanese style (`form`: `plain` vs `polite`)
7. Active glossary entries matched for this segment
8. Reference context / history pairs
9. Protected item signatures (`{v1}` -> original text)
10. Adapter version
"""

from __future__ import annotations

import hashlib
import json

from backend.domain.models import GlossaryEntry, ProtectedItem
from backend.translation.prompts.pfml3 import HistoryPair, filter_active_glossary


def compute_translation_cache_key(
    masked_source_text: str,
    model_name: str,
    model_hash: str = "",
    quantization: str = "Q4_K_M",
    temperature: float = 0.0,
    prompt_version: str = "pfml3-v1",
    source_lang: str = "en",
    target_lang: str = "ja",
    form: str = "plain",
    glossary_entries: list[GlossaryEntry] | None = None,
    context: str | None = None,
    history: list[HistoryPair] | None = None,
    protected_items: list[ProtectedItem] | None = None,
    adapter_version: str = "1.0.0",
) -> str:
    """Compute a deterministic SHA-256 hex key that invalidates whenever any relevant factor changes."""
    active_glossary = filter_active_glossary(masked_source_text, glossary_entries)
    glossary_canonical = sorted(
        [(g.source.strip(), g.target.strip()) for g in active_glossary],
        key=lambda x: x[0],
    )
    history_canonical = [
        {"src": h.source_text.strip(), "tgt": h.target_text.strip()}
        for h in (history or [])
    ]
    protected_canonical = [
        {"ph": p.placeholder, "kind": p.kind.value, "orig": p.original_text}
        for p in (protected_items or [])
    ]

    key_payload = {
        "masked_source_text": masked_source_text.strip(),
        "model_name": model_name,
        "model_hash": model_hash,
        "quantization": quantization,
        "temperature": round(float(temperature), 4),
        "prompt_version": prompt_version,
        "source_lang": source_lang.lower(),
        "target_lang": target_lang.lower(),
        "form": form.lower(),
        "glossary": glossary_canonical,
        "context": (context or "").strip(),
        "history": history_canonical,
        "protected_items": protected_canonical,
        "adapter_version": adapter_version,
    }
    serialized = json.dumps(key_payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
