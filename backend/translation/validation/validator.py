"""Strict post-translation validator and retry/fallback classifier (Section 11).

Checks all 10 required quality criteria:
1. Empty output or generation length cutoff (`finish_reason == "length"`).
2. Missing, duplicated, or altered protection placeholders (`{v1}`, `{v2}`, ...).
3. Missing citation reference markers (`[1]`, `[2, 3]`, etc.).
4. Abnormal character or phrase repetition loops.
5. Residual PLaMo special tokens (`<|plamo:tag|>`, `<|plamo:msg|>`, `<|plamo:bos|>`).
6. Likely untranslated source text returned verbatim.
7. Numeric value discrepancies (distinguishing formatting like `1,000` vs `1000` from value change).
8. Abnormally short translation relative to source length.
9. Meta-explanation contamination (e.g., "翻訳結果:", "Here is the translation:").

CRITICAL RULE:
Never silently delete unmatched or broken placeholders to mark a block as "repaired".
If retries (up to 2) and sentence re-splitting cannot resolve an Error-level issue,
the block MUST fall back to original source text + original PDF elements and be listed
in ValidationIssues (`completed_with_warnings`).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from backend.domain.models import IssueSeverity, ProtectedItem, ProtectedItemKind

TRANSLATION_VALIDATION_ISSUE_CODES: frozenset[str] = frozenset(
    {
        "EMPTY_OUTPUT",
        "TRUNCATED_BY_LENGTH",
        "SPECIAL_TOKEN_RESIDUE",
        "PLACEHOLDER_ALTERED",
        "PLACEHOLDER_MISSING",
        "PLACEHOLDER_DUPLICATED",
        "PLACEHOLDER_HALLUCINATED",
        "REFERENCE_MARKER_MISSING",
        "ABNORMAL_REPETITION",
        "EXPLANATION_CONTAMINATION",
        "UNTRANSLATED_SOURCE_RETURNED",
        "ABNORMALLY_SHORT_TRANSLATION",
        "NUMERIC_VALUE_MISMATCH",
    }
)

_PLACEHOLDER_RE = re.compile(r"\{v\d+\}")
_PLACEHOLDER_CANDIDATE_RE = re.compile(
    r"[\{\｛\[\［][^\{\｛\[\［\}\｝\]\］\r\n]{1,32}[\}\｝\]\］]"
)
_NFKC_PLACEHOLDER_VARIANT_RE = re.compile(
    r"^[\{\[]\s*v\s*[0-9]+[A-Za-z0-9_]*\s*[\}\]]$",
    re.IGNORECASE,
)
_PLAMO_SPECIAL_RE = re.compile(r"<\|plamo:[^|>]+\|>")
_CITATION_BRACKET_RE = re.compile(r"\[\d+(?:\s*[-,–]\s*\d+)*\]")
# Common ASCII unit suffixes attached immediately after numbers in technical prose
# (e.g., "1.6m", "0.17m", "2.4GHz", "10ms", "0.25m2"). Single lowercase hex digits
# (a-f) and ordinal/identifier letters are intentionally omitted so tokens like
# "1a", "12b", "0x1f", "203d", "1st", "3D" are not mistaken for quantities.
_MEASUREMENT_UNIT_TOKENS: tuple[str, ...] = tuple(
    sorted(
        (
            "m", "mm", "cm", "km", "um", "nm", "pm", "fm",
            "m2", "m3", "cm2", "cm3", "mm2", "mm3", "km2", "km3", "um2", "nm2",
            "ft", "ft2", "ft3", "in", "in2", "in3", "yd", "mi",
            "g", "mg", "kg", "ug", "ng", "pg", "t", "kt", "Mt", "Gt", "oz", "lb", "lbs",
            "s", "ms", "us", "ns", "ps", "fs", "min", "h", "hr", "hrs", "sec", "secs",
            "day", "days", "wk", "wks", "yr", "yrs",
            "Hz", "kHz", "MHz", "GHz", "THz",
            "V", "mV", "uV", "kV", "MV", "GV",
            "A", "mA", "uA", "nA", "kA", "MA",
            "W", "mW", "uW", "nW", "kW", "MW", "GW", "TW",
            "Wh", "kWh", "MWh", "mAh", "VA", "kVA", "MVA", "var", "Var",
            "F", "mF", "uF", "nF", "pF", "H", "mH", "uH", "nH",
            "T", "mT", "uT", "nT", "Wb", "K", "J", "kJ", "MJ", "GJ", "eV", "keV", "MeV", "GeV", "TeV",
            "Pa", "hPa", "kPa", "MPa", "GPa", "bar", "mbar", "kbar", "atm", "Torr", "torr", "mmHg", "psi",
            "N", "kN", "MN", "mol", "mmol", "umol", "nmol", "IU",
            "L", "mL", "uL", "nL", "dL", "kL", "ml", "ul", "nl", "dl", "kl",
            "ohm", "ohms", "mOhm", "kOhm", "MOhm", "Bq", "Gy", "Sv", "lm", "lx", "cd",
            "dB", "dBm", "dBi", "dBc", "dBV", "dBu", "dBA",
            "cal", "kcal", "ppm", "ppb", "ppt", "pct", "wt", "vol",
            "deg", "rad", "sr", "fps", "rpm", "mph", "kph", "kmph",
            "bps", "kbps", "Mbps", "Gbps", "Tbps",
            "KB", "MB", "GB", "TB", "PB", "KiB", "MiB", "GiB", "TiB", "PiB", "bit", "bits", "byte", "bytes",
            "dpi", "ppi", "px", "pt", "pc", "em", "rem",
        ),
        key=lambda u: (-len(u), u),
    )
)
_MEASUREMENT_UNIT_SUFFIX_RE = "|".join(re.escape(u) for u in _MEASUREMENT_UNIT_TOKENS)
# Atomic number core prevents regex backtracking from truncating partial numbers
# (e.g., "1.6m" -> "1" or "0.17m" -> "0.1") when followed by non-delimiter text.
_NUMBER_TOKEN_RE = re.compile(
    r"(?<![a-zA-Z0-9_{.])"
    r"(?>[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?)"
    rf"(?=(?:(?:{_MEASUREMENT_UNIT_SUFFIX_RE})(?![a-zA-Z0-9_}}]))|(?!\.\d)(?![a-zA-Z0-9_}}]))"
)
_EXPLANATION_PREFIX_RE = re.compile(
    r"^(?:翻訳(?:結果|文)?\s*[:：]|以下(?:が|は).*?(?:翻訳|訳文)です[。:：]?|Here is the translation\s*:|Translation\s*:)",
    re.IGNORECASE,
)


@dataclass
class ValidationFinding:
    code: str
    severity: IssueSeverity
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class TranslationValidationResult:
    is_valid: bool
    has_warnings: bool
    should_retry: bool
    recommend_resplit: bool
    findings: list[ValidationFinding] = field(default_factory=list)

    @property
    def error_codes(self) -> list[str]:
        return [f.code for f in self.findings if f.severity == IssueSeverity.ERROR]

    @property
    def warning_codes(self) -> list[str]:
        return [f.code for f in self.findings if f.severity == IssueSeverity.WARNING]


def normalize_numeric_strings(text: str) -> list[str]:
    """Extract numeric values from text (excluding `{vN}` placeholders and `[N]` citations)
    and normalize formatting (`1,000` -> `1000`, `1.50` -> `1.5`) so formatting changes
    are distinguished from actual value changes (Section 11).
    """
    # Strip placeholders and bracketed citations first
    cleaned = _PLACEHOLDER_RE.sub(" ", text)
    cleaned = _CITATION_BRACKET_RE.sub(" ", cleaned)
    raw_nums = _NUMBER_TOKEN_RE.findall(cleaned)
    normalized: list[str] = []
    for tok in raw_nums:
        no_commas = tok.replace(",", "")
        try:
            val = float(no_commas)
            if val.is_integer() and "e" not in no_commas.lower():
                normalized.append(str(int(val)))
            else:
                normalized.append(f"{val:.6g}")
        except ValueError:
            continue
    return normalized


def find_placeholder_variant_tokens(text: str) -> list[str]:
    """Extract exact or malformed placeholder-like bracketed tokens via token-level NFKC.

    Only each candidate bracketed token itself is NFKC-normalized for detection (`｛ｖ１｝`,
    `{ v1 }`, `［Ｖ１］`, mixed half/fullwidth braces, etc.); surrounding prose and
    mathematical Unicode expressions are never globally normalized or altered.
    """
    if not text:
        return []
    variants: list[str] = []
    for match in _PLACEHOLDER_CANDIDATE_RE.finditer(text):
        raw_tok = match.group(0)
        nfkc_tok = unicodedata.normalize("NFKC", raw_tok)
        if _NFKC_PLACEHOLDER_VARIANT_RE.match(nfkc_tok):
            variants.append(raw_tok)
    return variants


def validate_translation_output(
    source_text: str,
    masked_source_text: str,
    translated_text: str | None,
    protected_items: list[ProtectedItem] | None = None,
    finish_reason: str | None = None,
) -> TranslationValidationResult:
    """Validate a single block's translated text against all Section 11 criteria."""
    findings: list[ValidationFinding] = []
    protected_items = protected_items or []

    # 1. Empty output check
    if translated_text is None or not translated_text.strip():
        findings.append(
            ValidationFinding(
                code="EMPTY_OUTPUT",
                severity=IssueSeverity.ERROR,
                message="翻訳出力が空です。",
            )
        )
        return TranslationValidationResult(
            is_valid=False,
            has_warnings=False,
            should_retry=True,
            recommend_resplit=False,
            findings=findings,
        )

    out = translated_text.strip()

    # 2. Truncated output check
    if finish_reason == "length":
        findings.append(
            ValidationFinding(
                code="TRUNCATED_BY_LENGTH",
                severity=IssueSeverity.ERROR,
                message="生成トークン上限に達したため訳文が途中で打ち切られました。",
                details={"finish_reason": finish_reason},
            )
        )

    # 3. Special token residue check
    special_matches = _PLAMO_SPECIAL_RE.findall(out)
    if special_matches:
        findings.append(
            ValidationFinding(
                code="SPECIAL_TOKEN_RESIDUE",
                severity=IssueSeverity.ERROR,
                message=f"PLaMo特殊制御トークンが訳文中に残存しています: {special_matches}",
                details={"tokens": special_matches},
            )
        )

    # 4. Placeholder integrity check (missing, duplicated, altered/fullwidth, hallucinated)
    expected_placeholders = [item.placeholder for item in protected_items]
    if not expected_placeholders:
        expected_placeholders = _PLACEHOLDER_RE.findall(masked_source_text)

    actual_exact_placeholders = _PLACEHOLDER_RE.findall(out)
    all_placeholder_variants = find_placeholder_variant_tokens(out)
    src_literal_variants = find_placeholder_variant_tokens(source_text or "")
    remaining_src_variants = list(src_literal_variants)
    altered_variants: list[str] = []
    for tok in all_placeholder_variants:
        if _PLACEHOLDER_RE.fullmatch(tok):
            continue
        if tok in remaining_src_variants:
            remaining_src_variants.remove(tok)
        else:
            altered_variants.append(tok)

    # Check if fullwidth (`｛ｖ１｝`), whitespace (`{ v1 }`), or bracket-corrupted placeholders occurred
    if altered_variants:
        findings.append(
            ValidationFinding(
                code="PLACEHOLDER_ALTERED",
                severity=IssueSeverity.ERROR,
                message="保護記号（プレースホルダー）の書式が改変されています（全角化・空白混入等）。",
                details={
                    "expected": expected_placeholders,
                    "found_raw": all_placeholder_variants,
                    "altered": altered_variants,
                },
            )
        )

    missing_phs = [p for p in expected_placeholders if out.count(p) == 0]
    if missing_phs:
        findings.append(
            ValidationFinding(
                code="PLACEHOLDER_MISSING",
                severity=IssueSeverity.ERROR,
                message=f"保護記号が訳文から欠落しています: {missing_phs}",
                details={"missing": missing_phs, "expected": expected_placeholders},
            )
        )

    duplicated_phs = [p for p in set(expected_placeholders) if out.count(p) > expected_placeholders.count(p)]
    if duplicated_phs:
        findings.append(
            ValidationFinding(
                code="PLACEHOLDER_DUPLICATED",
                severity=IssueSeverity.ERROR,
                message=f"保護記号が重複して出力されています: {duplicated_phs}",
                details={"duplicated": duplicated_phs},
            )
        )

    hallucinated_phs = [p for p in set(actual_exact_placeholders) if p not in expected_placeholders]
    if hallucinated_phs:
        findings.append(
            ValidationFinding(
                code="PLACEHOLDER_HALLUCINATED",
                severity=IssueSeverity.ERROR,
                message=f"原文に存在しない保護記号が混入しています: {hallucinated_phs}",
                details={"hallucinated": hallucinated_phs},
            )
        )

    # 5. Citation reference number check (`[1]`, `[2, 3]` when not already wrapped as placeholder)
    expected_citations = _CITATION_BRACKET_RE.findall(masked_source_text)
    if expected_citations:
        missing_cites = [c for c in expected_citations if c not in out]
        if missing_cites:
            findings.append(
                ValidationFinding(
                    code="REFERENCE_MARKER_MISSING",
                    severity=IssueSeverity.ERROR,
                    message=f"文献参照番号が訳文から欠落しています: {missing_cites}",
                    details={"missing_citations": missing_cites},
                )
            )

    # 6. Abnormal repetition loop check
    if _has_abnormal_repetition(out):
        findings.append(
            ValidationFinding(
                code="ABNORMAL_REPETITION",
                severity=IssueSeverity.ERROR,
                message="訳文中に異常な文字またはフレーズの反復（生成ループ）が検出されました。",
            )
        )

    # 7. Meta-explanation contamination check
    if _EXPLANATION_PREFIX_RE.search(out):
        findings.append(
            ValidationFinding(
                code="EXPLANATION_CONTAMINATION",
                severity=IssueSeverity.ERROR,
                message="訳文以外の説明文（「翻訳結果:」等）の混入が検出されました。",
            )
        )

    # 8. Unmodified source text returned check
    # Ignore short proper-noun headings, equation-only strings, or strings with few alphabetic words
    alpha_words = re.findall(r"[a-zA-Z]{3,}", _PLACEHOLDER_RE.sub(" ", masked_source_text))
    if len(alpha_words) >= 5 and out.strip().lower() == masked_source_text.strip().lower():
        findings.append(
            ValidationFinding(
                code="UNTRANSLATED_SOURCE_RETURNED",
                severity=IssueSeverity.ERROR,
                message="翻訳されず原文がそのまま返却されました。",
            )
        )

    # 9. Abnormally short translation check
    src_no_ph = _PLACEHOLDER_RE.sub("", masked_source_text).strip()
    out_no_ph = _PLACEHOLDER_RE.sub("", out).strip()
    if len(src_no_ph) >= 80 and len(out_no_ph) < max(8, int(len(src_no_ph) * 0.12)):
        findings.append(
            ValidationFinding(
                code="ABNORMALLY_SHORT_TRANSLATION",
                severity=IssueSeverity.ERROR,
                message=f"原文長（{len(src_no_ph)}文字）に対し訳文が異常に短すぎます（{len(out_no_ph)}文字）。一部欠落の可能性があります。",
                details={"source_chars": len(src_no_ph), "output_chars": len(out_no_ph)},
            )
        )

    # 10. Numeric value consistency check (Warning level unless critical)
    src_nums = normalize_numeric_strings(masked_source_text)
    out_nums = normalize_numeric_strings(out)
    if src_nums:
        # Set-membership check intentionally preserves the existing multiplicity behavior
        # (detecting dropped duplicate occurrences of the same value is tracked separately).
        missing_nums = [n for n in src_nums if n not in out_nums]
        if missing_nums:
            findings.append(
                ValidationFinding(
                    code="NUMERIC_VALUE_MISMATCH",
                    severity=IssueSeverity.WARNING,
                    message=f"原文中の数値が訳文で変化または欠落している可能性があります: {missing_nums}",
                    details={"source_numbers": src_nums, "output_numbers": out_nums},
                )
            )

    has_errors = any(f.severity == IssueSeverity.ERROR for f in findings)
    has_warnings = any(f.severity == IssueSeverity.WARNING for f in findings)
    recommend_resplit = any(
        f.code in ("TRUNCATED_BY_LENGTH", "ABNORMAL_REPETITION", "ABNORMALLY_SHORT_TRANSLATION")
        for f in findings
    )

    return TranslationValidationResult(
        is_valid=not has_errors,
        has_warnings=has_warnings,
        should_retry=has_errors,
        recommend_resplit=recommend_resplit,
        findings=findings,
    )


def _has_abnormal_repetition(text: str) -> bool:
    """Detect pathological character or n-gram repetition loops in LLM output."""
    # Same character repeated >= 12 times consecutively (excluding periods/spaces in leader lines)
    if re.search(r"([^\s.…・])\1{11,}", text):
        return True
    # Any 6+ char substring repeated >= 4 times consecutively
    if re.search(r"(.{6,30})\1{3,}", text):
        return True
    return False
