"""Geometry- and morphology-aware PDF line-break dehyphenation for LLM input.

Only soft mid-word splits occurring at physical PDF visual line boundaries are
candidates for joining in `Block.llm_input_text`. Original `Block.source_text`,
PDF coordinates (`Block.bbox`), inline math spans, and `Block.masked_source_text`
remain completely unaltered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

PREPROCESSING_VERSION_DEHYPHENATED = "line-hyphen-v1"
PREPROCESSING_VERSION_RAW = "raw-v0"

_MATH_FONT_TOKENS = (
    "cmmi",
    "cmsy",
    "cmex",
    "txsys",
    "txexs",
    "txmia",
    "mtmi",
    "mtsyn",
    "math",
    "symbol",
    "stix",
    "cambria math",
)

# Bound word-ending fragments or inflectional suffixes that never stand alone
# as the right-hand component of an English hyphenated compound.
_BOUND_RIGHT_FRAGMENTS: frozenset[str] = frozenset(
    {
        "tion",
        "tions",
        "sion",
        "sions",
        "ance",
        "ances",
        "ence",
        "ences",
        "ment",
        "ments",
        "ness",
        "ity",
        "ities",
        "ous",
        "ious",
        "eous",
        "tively",
        "cally",
        "nally",
        "tally",
        "rally",
        "ology",
        "ologies",
        "ical",
        "ular",
        "able",
        "ible",
        "ably",
        "ibly",
        "ing",
        "ings",
        "ize",
        "ise",
        "izing",
        "ising",
        "ized",
        "ised",
        "eign",
        "ods",
        "od",
        "ponent",
        "ponents",
        "nent",
        "nents",
        "minum",
        "tween",
        "hicle",
        "hicles",
        "alyzing",
        "alyzed",
        "alyze",
        "erating",
        "erated",
        "culated",
        "culate",
        "lating",
        "lated",
        "mating",
        "mated",
        "trusion",
        "trusions",
        "clusively",
        "stantial",
        "stantially",
        "sequent",
        "sequently",
        "ference",
        "ferences",
        "mission",
        "missions",
        "duction",
        "ductions",
        "lar",
        "angular",
        "cess",
        "cessing",
        "ficult",
        "tenna",
        "tennas",
        "sur",
        "eter",
        "eters",
        "tical",
        "metric",
        "metrical",
        "metry",
    }
)

# Solid closed English words that happen to decompose into apparent subwords
# (e.g. `per-formed` -> `performed`, `under-side` -> `underside`, `wire-less` -> `wireless`).
_KNOWN_SOLID_WORDS: frozenset[str] = frozenset(
    {
        "foreign",
        "variances",
        "variance",
        "variations",
        "variation",
        "periods",
        "period",
        "components",
        "component",
        "between",
        "performed",
        "perform",
        "performing",
        "performs",
        "underside",
        "wireless",
        "information",
        "intrusions",
        "intrusion",
        "aluminum",
        "vehicles",
        "vehicle",
        "within",
        "without",
        "therefore",
        "however",
        "moreover",
        "meanwhile",
        "throughout",
        "whereas",
        "hereinafter",
        "background",
        "foreground",
        "framework",
        "network",
        "software",
        "hardware",
        "database",
        "baseline",
        "outcome",
        "output",
        "input",
        "overview",
        "feedback",
        "subcarrier",
        "subcarriers",
        "understand",
        "understanding",
        "together",
        "another",
        "become",
        "became",
        "becomes",
        "because",
        "before",
        "afterward",
        "always",
        "although",
        "already",
        "otherwise",
        "furthermore",
        "research",
        "result",
        "results",
        "remain",
        "remains",
        "require",
        "requires",
        "required",
        "represent",
        "represents",
        "represented",
        "respectively",
        "previous",
        "process",
        "processing",
        "provide",
        "provides",
        "provided",
        "predict",
        "predicted",
        "preserve",
        "preserving",
        "subsequent",
        "subsequently",
        "interface",
        "internal",
        "interval",
        "introduce",
        "introduced",
        "subtract",
        "substantial",
        "substantially",
        "surround",
        "surrounding",
        "rectangular",
        "experimental",
        "measurements",
        "measurement",
        "approximately",
        "alternatively",
        "respectively",
        "environment",
        "operating",
        "simulating",
        "detection",
        "analyzing",
        "diameter",
        "vertical",
        "equation",
        "amplitude",
        "normalized",
        "average",
        "movement",
        "exclusively",
        "protection",
        "symmetric",
        "symmetrical",
        "electronics",
    }
)

# Prefixes commonly used in hyphenated academic/technical compounds; never join
# blindly when paired with a non-fragment right-hand token.
_HYPHENATED_COMPOUND_PREFIXES: frozenset[str] = frozenset(
    {
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "zero",
        "half",
        "full",
        "self",
        "non",
        "semi",
        "quasi",
        "pseudo",
        "anti",
        "multi",
        "ultra",
        "cross",
        "counter",
        "ex",
        "pre",
        "post",
        "co",
        "sub",
        "inter",
        "intra",
        "hyper",
        "hypo",
        "well",
        "ill",
        "real",
        "front",
        "back",
        "left",
        "right",
        "high",
        "low",
        "open",
        "fine",
        "coarse",
        "user",
        "task",
        "case",
        "time",
        "ground",
        "vehicle",
        "deep",
        "short",
        "long",
        "wide",
        "narrow",
        "large",
        "small",
        "single",
        "double",
        "dual",
        "triple",
        "hard",
        "soft",
        "fast",
        "slow",
    }
)

# Standalone English words that commonly participate in hyphenated compounds
# across line wraps (kept hyphenated when paired with another compound word).
_STANDALONE_COMPOUND_WORDS: frozenset[str] = _HYPHENATED_COMPOUND_PREFIXES | frozenset(
    {
        "dimensional",
        "series",
        "power",
        "energy",
        "cost",
        "rate",
        "step",
        "scale",
        "band",
        "pass",
        "stop",
        "end",
        "ended",
        "wise",
        "free",
        "proof",
        "like",
        "only",
        "specific",
        "driven",
        "oriented",
        "aware",
        "dependent",
        "independent",
        "effective",
        "efficient",
        "sensitive",
        "intensive",
        "preserving",
        "guided",
        "assisted",
        "enabled",
        "related",
        "focused",
        "centered",
        "aligned",
        "weighted",
        "bounded",
        "valued",
        "ordered",
        "domain",
        "space",
        "field",
        "state",
        "frame",
        "size",
        "range",
        "width",
        "length",
        "depth",
        "point",
        "line",
        "node",
        "edge",
        "layer",
        "block",
        "mask",
        "grid",
        "mesh",
        "map",
        "set",
        "list",
        "tree",
        "graph",
        "flow",
        "path",
        "link",
        "port",
        "host",
        "file",
        "text",
        "word",
        "char",
        "code",
        "type",
        "mode",
        "form",
        "rule",
        "norm",
        "mean",
        "data",
        "study",
        "making",
        "distance",
        "known",
        "defined",
        "trained",
        "supervised",
        "based",
        "shaped",
        "side",
        "phase",
        "level",
        "model",
        "signal",
        "noise",
        "teacher",
        "student",
        "encoder",
        "decoder",
        "policy",
        "shot",
        "linear",
        "nonlinear",
        "static",
        "dynamic",
        "local",
        "global",
        "source",
        "crafted",
        "established",
        "grained",
        "term",
        "hop",
        "planar",
        "entrant",
        "photon",
        "abelian",
        "newton",
        "top",
        "bottom",
        "up",
        "down",
        "in",
        "out",
        "on",
        "off",
        "to",
        "by",
        "at",
        "of",
        "for",
        "with",
        "from",
        "good",
        "bad",
        "best",
        "new",
        "old",
        "area",
        "zone",
        "view",
        "head",
        "tail",
        "body",
        "part",
        "unit",
        "item",
        "pair",
        "load",
        "read",
        "write",
        "blue",
        "green",
        "red",
        "black",
        "white",
        "object",
        "objects",
        "detection",
        "camera",
        "thermal",
    }
)

_LEADING_QUOTE_RE = re.compile(r'^[("“‘\'\[{]*')
_TRAILING_PUNCT_RE = re.compile(r'[.,;:!?)}\]"”’\']*$')


@dataclass(frozen=True)
class LineBreakHyphenEdit:
    """Verified soft line-break hyphen join offset in `Block.source_text`."""

    line_index: int
    hyph_idx: int
    space_idx: int
    token_start: int
    token_end: int
    left_token: str
    right_token: str
    joined_word: str
    reason: str


@dataclass(frozen=True)
class DocumentTokenInventory:
    """In-line intact words and in-line hyphenated compounds seen in a document."""

    intact_words: frozenset[str]
    hyphenated_compounds: frozenset[str]


def build_document_token_inventory(line_texts: Iterable[str]) -> DocumentTokenInventory:
    """Collect single-line intact words and hyphenated compounds without crossing line breaks."""
    intact: set[str] = set()
    compounds: set[str] = set()
    for ln in line_texts:
        s = ln.strip()
        if not s:
            continue
        # Only inspect complete tokens before the line end (strip final soft hyphen if present)
        body = s[:-1] if s.endswith("-") else s
        for m in re.finditer(r"\b([A-Za-z]+-[A-Za-z]+(?:-[A-Za-z]+)*)\b", body):
            compounds.add(m.group(1).lower())
        for m in re.finditer(r"\b([A-Za-z]{3,})\b", body):
            # Exclude parts adjacent to hyphens so compound halves are not counted as intact
            start, end = m.start(1), m.end(1)
            prev_ch = body[start - 1] if start > 0 else ""
            next_ch = body[end] if end < len(body) else ""
            if prev_ch == "-" or next_ch == "-":
                continue
            intact.add(m.group(1).lower())
    return DocumentTokenInventory(
        intact_words=frozenset(intact),
        hyphenated_compounds=frozenset(compounds),
    )


def _is_math_or_script_span(span: dict, max_line_size: float) -> bool:
    font = str(span.get("font", "")).lower()
    if any(tok in font for tok in _MATH_FONT_TOKENS):
        return True
    flags = int(span.get("flags", 0))
    sz = float(span.get("size", 10.0))
    if (flags & 1) or (max_line_size > 0 and sz < max_line_size * 0.86):
        return True
    return False


def _line_spans_and_bbox(line: dict) -> tuple[list[dict], list[float]]:
    spans = [s for s in line.get("spans", []) if str(s.get("text", "")).strip()]
    bbox = line.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        return spans, [float(x) for x in bbox]
    s_boxes = [
        s["bbox"]
        for s in spans
        if isinstance(s.get("bbox"), (list, tuple)) and len(s["bbox"]) == 4
    ]
    if s_boxes:
        return spans, [
            min(float(b[0]) for b in s_boxes),
            min(float(b[1]) for b in s_boxes),
            max(float(b[2]) for b in s_boxes),
            max(float(b[3]) for b in s_boxes),
        ]
    return spans, [0.0, 0.0, 0.0, 0.0]


def _classify_hyphen_pair(
    left_token: str,
    right_token: str,
    inventory: DocumentTokenInventory | None,
) -> tuple[bool, str]:
    left_low = left_token.lower()
    right_low = right_token.lower()
    joined = left_low + right_low
    hyphenated = f"{left_low}-{right_low}"

    if inventory is not None:
        if hyphenated in inventory.hyphenated_compounds:
            return False, "compound_in_doc"
        if joined in inventory.intact_words:
            return True, "intact_word_in_doc"

    if joined in _KNOWN_SOLID_WORDS:
        return True, "known_solid_word"

    if right_low in _BOUND_RIGHT_FRAGMENTS:
        return True, "bound_right_fragment"

    # Protect known hyphenated compound pairs (e.g. `two-dimensional`, `real-time`, `time-series`)
    if left_low in _STANDALONE_COMPOUND_WORDS and right_low in _STANDALONE_COMPOUND_WORDS:
        return False, "standalone_compound_pair"

    if left_low in _HYPHENATED_COMPOUND_PREFIXES and len(right_low) >= 3:
        return False, "compound_prefix_preserve"

    return True, "non_standalone_morph_split"


def detect_line_break_hyphen_edits(
    visual_lines: list[dict],
    text_lines: list[str],
    block_bbox: list[float],
    full_text: str,
    inventory: DocumentTokenInventory | None = None,
) -> list[LineBreakHyphenEdit]:
    """Identify verified mid-word soft line-break hyphen splits using PDF line geometry and morphology."""
    if len(visual_lines) < 2 or len(visual_lines) != len(text_lines):
        return []

    non_empty_pairs = [
        (ln, txt.strip())
        for ln, txt in zip(visual_lines, text_lines)
        if txt.strip()
    ]
    if len(non_empty_pairs) < 2:
        return []

    # Verify exact alignment with full_text = " ".join(stripped lines)
    line_ranges: list[tuple[int, int]] = []
    cursor = 0
    for _, t_stripped in non_empty_pairs:
        start = cursor
        end = start + len(t_stripped)
        if full_text[start:end] != t_stripped:
            return []
        line_ranges.append((start, end))
        cursor = end + 1
    if cursor - 1 != len(full_text):
        return []

    parsed_meta = [_line_spans_and_bbox(ln) for ln, _ in non_empty_pairs]
    para_x0 = min(b[0] for _, b in parsed_meta)
    para_x1 = max([float(block_bbox[2])] + [b[2] for _, b in parsed_meta])

    edits: list[LineBreakHyphenEdit] = []
    for i in range(len(non_empty_pairs) - 1):
        spans_a, box_a = parsed_meta[i]
        spans_b, box_b = parsed_meta[i + 1]
        txt_a = non_empty_pairs[i][1]
        txt_b = non_empty_pairs[i + 1][1]
        if not spans_a or not spans_b:
            continue

        # Syntax pre-check on trailing ASCII '-' at end of physical line `i`
        if not txt_a.endswith("-") or len(txt_a) < 3:
            continue
        last_chunk_a = txt_a.split()[-1]
        if not last_chunk_a.endswith("-"):
            continue
        lead_strip = _LEADING_QUOTE_RE.match(last_chunk_a[:-1])
        lead_len = lead_strip.end() if lead_strip else 0
        left_raw = last_chunk_a[lead_len:-1]
        if not re.fullmatch(r"[A-Za-z]{2,}", left_raw):
            continue
        if not (left_raw.islower() or (left_raw[0].isupper() and left_raw[1:].islower())):
            continue

        # Syntax check on leading token at start of line `i + 1`
        first_chunk_b = txt_b.split()[0]
        trail_m = _TRAILING_PUNCT_RE.search(first_chunk_b)
        trail_len = len(trail_m.group(0)) if trail_m else 0
        right_core = first_chunk_b[: len(first_chunk_b) - trail_len] if trail_len else first_chunk_b
        if not re.fullmatch(r"[a-z]{2,}", right_core):
            continue

        # Span font and geometry checks
        sp_a = spans_a[-1]
        sp_b = spans_b[0]
        max_sz_a = max(float(s.get("size", 10.0)) for s in spans_a)
        max_sz_b = max(float(s.get("size", 10.0)) for s in spans_b)
        if _is_math_or_script_span(sp_a, max_sz_a) or _is_math_or_script_span(sp_b, max_sz_b):
            continue

        sz_a = float(sp_a.get("size", 10.0))
        sz_b = float(sp_b.get("size", 10.0))
        if max(sz_a, sz_b) <= 0 or min(sz_a, sz_b) / max(sz_a, sz_b) < 0.90:
            continue

        # Vertical adjacency and reading continuation
        dy = box_b[1] - box_a[1]
        if dy < sz_a * 0.4 or dy > max(sz_a * 2.2, 6.0):
            continue
        if box_b[0] - para_x0 > max(sz_a * 2.5, 18.0) and box_b[0] > box_a[0] + sz_a * 0.5:
            continue

        # Right-margin wrap evidence
        sb_box = sp_b.get("bbox")
        sb_txt = str(sp_b.get("text", "")).strip()
        next_tok_w = 0.0
        if isinstance(sb_box, (list, tuple)) and len(sb_box) == 4 and sb_txt:
            sb_w = max(0.0, float(sb_box[2]) - float(sb_box[0]))
            next_tok_w = sb_w * min(1.0, len(first_chunk_b) / max(1, len(sb_txt)))
        right_gap = para_x1 - box_a[2]
        wrapped_near_margin = right_gap <= max(20.0, sz_a * 2.2) or (
            box_a[2] + sz_a * 0.25 + next_tok_w >= para_x1 - 4.0
        )
        if not wrapped_near_margin:
            continue

        should_join, reason = _classify_hyphen_pair(left_raw, right_core, inventory)
        if not should_join:
            continue

        a_start, a_end = line_ranges[i]
        b_start, _ = line_ranges[i + 1]
        hyph_idx = a_end - 1
        space_idx = a_end
        if b_start != a_end + 1 or full_text[hyph_idx : space_idx + 1] != "- ":
            continue
        token_start = hyph_idx - len(left_raw)
        token_end = (a_end + 1) + len(right_core)
        if (
            full_text[token_start:hyph_idx] != left_raw
            or full_text[a_end + 1 : token_end] != right_core
        ):
            continue

        edits.append(
            LineBreakHyphenEdit(
                line_index=i,
                hyph_idx=hyph_idx,
                space_idx=space_idx,
                token_start=token_start,
                token_end=token_end,
                left_token=left_raw,
                right_token=right_core,
                joined_word=left_raw + right_core,
                reason=reason,
            )
        )
    return edits


def project_line_break_edits_to_masked(
    source_text: str,
    masked_source_text: str,
    orig_to_masked: dict[int, int] | None,
    edits: list[LineBreakHyphenEdit],
) -> str | None:
    """Apply line-break hyphen joins onto `masked_source_text` strictly when char mapping is intact.

    Returns `None` if no edit applies or if character correspondence is lost across edits.
    """
    if not edits or orig_to_masked is None:
        return None

    verified_spans: list[tuple[int, int]] = []
    for ed in edits:
        if ed.token_start < 0 or ed.token_end > len(source_text):
            continue
        if source_text[ed.hyph_idx : ed.space_idx + 1] != "- ":
            continue
        # Every character of left_token + "- " + right_token must remain unmasked & contiguous
        if not all(pos in orig_to_masked for pos in range(ed.token_start, ed.token_end)):
            continue
        m_positions = [orig_to_masked[pos] for pos in range(ed.token_start, ed.token_end)]
        expected_len = ed.token_end - ed.token_start
        if m_positions[-1] - m_positions[0] + 1 != expected_len:
            continue
        if m_positions != list(range(m_positions[0], m_positions[-1] + 1)):
            continue
        m_hyph = orig_to_masked[ed.hyph_idx]
        m_space = orig_to_masked[ed.space_idx]
        if m_space != m_hyph + 1:
            continue
        if masked_source_text[m_hyph : m_space + 1] != "- ":
            continue
        m_tok_start = orig_to_masked[ed.token_start]
        m_tok_end = orig_to_masked[ed.token_end - 1] + 1
        if (
            masked_source_text[m_tok_start:m_hyph] != ed.left_token
            or masked_source_text[m_space + 1 : m_tok_end] != ed.right_token
        ):
            continue
        verified_spans.append((m_hyph, m_space))

    if not verified_spans:
        return None

    out = masked_source_text
    for m_hyph, m_space in sorted(verified_spans, reverse=True):
        out = out[:m_hyph] + out[m_space + 1 :]
    return out if out != masked_source_text else None
