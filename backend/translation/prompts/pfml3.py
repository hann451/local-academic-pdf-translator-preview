"""PLaMo 3 Translate PFML3 prompt builder and context budget manager (Section 9 & 10).

Implements the exact official PFML3 specification verified from `pfnet/plamo-3-translate-2b`:
- Begins with `<|plamo:tag|>dataset<|plamo:msg|>translation`
- Optional `<|plamo:tag|>context<|plamo:msg|>...`
- Optional prior input/output pairs for immediate local context
- Optional `<|plamo:tag|>glossary<|plamo:index|>{src}<|plamo:item|>{tgt}...`
- Target `<|plamo:tag|>input<|plamo:key|>lang<|plamo:val|>en<|plamo:msg|>{text}`
- Generation prefix `<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>ja<|plamo:key|>form<|plamo:val|>{plain|polite}<|plamo:msg|>`
- Prevents duplicate `<|plamo:bos|>` tokens when passed to llama.cpp.
- Enforces token budget (default 8192 context, model max 32768) by trimming context first,
  then splitting long paragraphs at sentence boundaries without ever breaking `{vN}` placeholders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.domain.models import GlossaryEntry

PLAMO_STOP_TOKENS: list[str] = ["<|plamo:tag|>", "<|plamo:bos|>"]
PLAMO_MODEL_MAX_TOKENS: int = 32768
DEFAULT_CONTEXT_BUDGET_TOKENS: int = 8192

_PLACEHOLDER_TOKEN_RE = re.compile(r"\{v\d+\}")
_SPECIAL_TOKEN_RE = re.compile(r"<\|plamo:[a-z_]+\|>")


@dataclass(frozen=True)
class HistoryPair:
    source_text: str
    target_text: str


@dataclass
class PromptBudgetPlan:
    prompt: str
    estimated_input_tokens: int
    reserved_output_tokens: int
    total_budget_tokens: int
    active_glossary: list[GlossaryEntry] = field(default_factory=list)
    included_history_count: int = 0
    context_included: bool = False
    was_context_trimmed: bool = False


def estimate_token_count(text: str) -> int:
    """Conservative token estimator for English/Japanese/PFML3 without requiring external network download.

    Counts PFML3 special tokens as 1 token each, CJK characters as ~1.2 tokens,
    and ASCII words/punctuation conservatively.
    """
    if not text:
        return 0
    specials = _SPECIAL_TOKEN_RE.findall(text)
    stripped = _SPECIAL_TOKEN_RE.sub(" ", text)
    cjk_chars = len(re.findall(r"[\u3000-\u9fff\uff00-\uffef]", stripped))
    ascii_part = re.sub(r"[\u3000-\u9fff\uff00-\uffef]", " ", stripped)
    # Approximate 1 token per 3.5 ASCII chars + 1.2 per CJK char + special tokens
    ascii_tokens = max(1, int(len(ascii_part) / 3.5)) if ascii_part.strip() else 0
    return len(specials) + int(cjk_chars * 1.2) + ascii_tokens + 4


def strip_plamo_control_tokens(text: str) -> str:
    """Remove trailing or leading PLaMo stop/BOS tokens from generated output or history."""
    cleaned = text
    # Remove leading BOS if present
    while cleaned.startswith("<|plamo:bos|>"):
        cleaned = cleaned[len("<|plamo:bos|>") :]
    # Cut off at the first stop token if the server returned it in the text payload
    for stop_tok in PLAMO_STOP_TOKENS:
        idx = cleaned.find(stop_tok)
        if idx != -1:
            cleaned = cleaned[:idx]
    return cleaned.strip()


def filter_active_glossary(
    source_text: str,
    glossary_entries: list[GlossaryEntry] | None,
) -> list[GlossaryEntry]:
    """Select only glossary terms that appear in `source_text` (case-insensitive substring/word match)."""
    if not glossary_entries or not source_text:
        return []
    lower_src = source_text.lower()
    matched: list[GlossaryEntry] = []
    seen_sources: set[str] = set()
    for entry in glossary_entries:
        term = entry.source.strip()
        if not term:
            continue
        key = term.lower()
        if key in seen_sources:
            continue
        if key in lower_src:
            matched.append(entry)
            seen_sources.add(key)
    return matched


def build_pfml3_prompt(
    source_text: str,
    source_lang: str = "en",
    target_lang: str = "ja",
    form: str = "plain",
    context: str | None = None,
    history: list[HistoryPair] | None = None,
    glossary_entries: list[GlossaryEntry] | None = None,
    include_bos: bool = False,
) -> str:
    """Construct raw PFML3 prompt string.

    By default `include_bos=False` because llama.cpp automatically prepends `<|plamo:bos|>`,
    preventing duplicate BOS tokens (Section 9).
    """
    if not source_text or not source_text.strip():
        raise ValueError("source_text for PFML3 prompt must not be empty")
    if form not in ("plain", "polite"):
        raise ValueError(f"Invalid Japanese form style: {form!r}. Expected 'plain' or 'polite'.")

    # Ensure source_text itself does not accidentally contain raw <|plamo:...|>, escaping if present
    safe_source = source_text.replace("<|plamo:", "< |plamo:")
    parts: list[str] = []
    if include_bos:
        parts.append("<|plamo:bos|>")

    parts.append("<|plamo:tag|>dataset<|plamo:msg|>translation")

    if context and context.strip():
        safe_ctx = strip_plamo_control_tokens(context).replace("<|plamo:", "< |plamo:")
        if safe_ctx:
            parts.append(f"<|plamo:tag|>context<|plamo:msg|>{safe_ctx}")

    if history:
        for pair in history:
            s_clean = strip_plamo_control_tokens(pair.source_text).replace("<|plamo:", "< |plamo:")
            t_clean = strip_plamo_control_tokens(pair.target_text).replace("<|plamo:", "< |plamo:")
            if s_clean and t_clean:
                parts.append(
                    f"<|plamo:tag|>input<|plamo:key|>lang<|plamo:val|>{source_lang}<|plamo:msg|>{s_clean}"
                    f"<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>{target_lang}<|plamo:msg|>{t_clean}"
                )

    active_glossary = filter_active_glossary(source_text, glossary_entries)
    if active_glossary:
        parts.append("<|plamo:tag|>glossary")
        for g in active_glossary:
            s_term = g.source.strip().replace("<|plamo:", "< |plamo:")
            t_term = g.target.strip().replace("<|plamo:", "< |plamo:")
            parts.append(f"<|plamo:index|>{s_term}<|plamo:item|>{t_term}")

    parts.append(
        f"<|plamo:tag|>input<|plamo:key|>lang<|plamo:val|>{source_lang}<|plamo:msg|>{safe_source}"
    )

    if target_lang == "ja":
        parts.append(
            f"<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>ja"
            f"<|plamo:key|>form<|plamo:val|>{form}<|plamo:msg|>"
        )
    else:
        parts.append(
            f"<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>{target_lang}<|plamo:msg|>"
        )

    return "".join(parts)


def plan_prompt_within_budget(
    source_text: str,
    context_budget_tokens: int = DEFAULT_CONTEXT_BUDGET_TOKENS,
    model_max_tokens: int = PLAMO_MODEL_MAX_TOKENS,
    source_lang: str = "en",
    target_lang: str = "ja",
    form: str = "plain",
    context: str | None = None,
    history: list[HistoryPair] | None = None,
    glossary_entries: list[GlossaryEntry] | None = None,
) -> PromptBudgetPlan:
    """Build PFML3 prompt while enforcing token budget across body, context, glossary, and output reservation.

    If the combined token estimate exceeds the effective budget:
    1. Oldest history pairs are dropped one by one.
    2. Reference context is dropped if still over budget.
    3. Raises ValueError if the single paragraph alone (plus reserved output) exceeds budget,
       signaling the caller to split the paragraph with `split_paragraph_preserving_placeholders`.
    """
    effective_budget = min(context_budget_tokens, model_max_tokens)
    src_tokens = estimate_token_count(source_text)
    reserved_output = max(256, min(4096, int(src_tokens * 2.2) + 64))

    active_glossary = filter_active_glossary(source_text, glossary_entries)
    candidate_history = list(history or [])
    candidate_context = context
    was_trimmed = False

    while True:
        prompt = build_pfml3_prompt(
            source_text=source_text,
            source_lang=source_lang,
            target_lang=target_lang,
            form=form,
            context=candidate_context,
            history=candidate_history,
            glossary_entries=active_glossary,
            include_bos=False,
        )
        in_tokens = estimate_token_count(prompt)
        if in_tokens + reserved_output <= effective_budget:
            return PromptBudgetPlan(
                prompt=prompt,
                estimated_input_tokens=in_tokens,
                reserved_output_tokens=reserved_output,
                total_budget_tokens=effective_budget,
                active_glossary=active_glossary,
                included_history_count=len(candidate_history),
                context_included=bool(candidate_context and candidate_context.strip()),
                was_context_trimmed=was_trimmed,
            )

        # Step 1: Drop oldest history item
        if candidate_history:
            candidate_history.pop(0)
            was_trimmed = True
            continue

        # Step 2: Drop optional context string
        if candidate_context:
            candidate_context = None
            was_trimmed = True
            continue

        # Step 3: Even bare source + glossary + output reservation exceeds budget
        raise ValueError(
            f"Paragraph token requirement ({in_tokens} input + {reserved_output} reserved output) "
            f"exceeds budget ({effective_budget}). Paragraph must be split."
        )


def split_paragraph_preserving_placeholders(
    text: str,
    max_segment_chars: int = 600,
) -> list[str]:
    """Split a long paragraph into sentence-level chunks without cutting inside `{vN}` placeholders
    or common academic abbreviations (e.g., 'Fig.', 'Eq.', 'et al.', 'i.e.', 'e.g.').
    """
    stripped = text.strip()
    if not stripped:
        return []
    if len(stripped) <= max_segment_chars:
        return [stripped]

    # Temporarily protect common abbreviations from sentence boundary splitting
    protected_abbrevs = [
        "Fig.",
        "Figs.",
        "Eq.",
        "Eqs.",
        "Tab.",
        "Sec.",
        "Ref.",
        "Refs.",
        "et al.",
        "i.e.",
        "e.g.",
        "vs.",
        "cf.",
        "Dr.",
        "Prof.",
    ]
    working = stripped
    abbrev_map: dict[str, str] = {}
    for idx, ab in enumerate(protected_abbrevs):
        marker = f"__ABBREV_{idx}__"
        if ab in working:
            abbrev_map[marker] = ab
            working = working.replace(ab, marker)

    # Split on sentence-ending punctuation followed by whitespace
    raw_sentences = re.split(r"(?<=[.!?])\s+", working)
    sentences: list[str] = []
    for s in raw_sentences:
        restored = s
        for marker, ab in abbrev_map.items():
            restored = restored.replace(marker, ab)
        if restored.strip():
            sentences.append(restored.strip())

    # If a single sentence is still longer than max_segment_chars, split on clause boundaries (; or , )
    # while strictly avoiding cutting any `{vN}` token
    chunks: list[str] = []
    current = ""

    for sent in sentences:
        if not current:
            if len(sent) <= max_segment_chars:
                current = sent
            else:
                sub_parts = _split_long_sentence_safely(sent, max_segment_chars)
                chunks.extend(sub_parts[:-1])
                current = sub_parts[-1]
        elif len(current) + 1 + len(sent) <= max_segment_chars:
            current = f"{current} {sent}"
        else:
            chunks.append(current)
            if len(sent) <= max_segment_chars:
                current = sent
            else:
                sub_parts = _split_long_sentence_safely(sent, max_segment_chars)
                chunks.extend(sub_parts[:-1])
                current = sub_parts[-1]

    if current:
        chunks.append(current)

    return chunks


def _split_long_sentence_safely(sentence: str, max_chars: int) -> list[str]:
    """Split an overlong single sentence on whitespace outside `{vN}` placeholders."""
    tokens = re.findall(r"\{v\d+\}|\S+", sentence)
    segments: list[str] = []
    cur_tokens: list[str] = []
    cur_len = 0

    for tok in tokens:
        add_len = len(tok) + (1 if cur_tokens else 0)
        if cur_tokens and cur_len + add_len > max_chars:
            segments.append(" ".join(cur_tokens))
            cur_tokens = [tok]
            cur_len = len(tok)
        else:
            cur_tokens.append(tok)
            cur_len += add_len

    if cur_tokens:
        segments.append(" ".join(cur_tokens))

    return segments
