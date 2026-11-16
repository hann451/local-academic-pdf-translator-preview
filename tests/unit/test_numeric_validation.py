"""Unit tests for numeric value extraction and NUMERIC_VALUE_MISMATCH validation."""

import re

import pytest

from backend.translation.validation.validator import (
    normalize_numeric_strings,
    validate_translation_output,
)

# Historical regex pattern prior to the atomic unit-aware fix (kept here strictly to
# pin the backtracking truncation bug in a regression test).
_LEGACY_NUMBER_TOKEN_RE = re.compile(
    r"(?<![a-zA-Z_{])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][-+]?\d+)?(?![a-zA-Z_}])"
)


def test_legacy_regex_backtracking_reproduction() -> None:
    """Reproduce the old backtracking truncation bug and verify the current fix resolves it."""
    # Under the legacy regex, trailing unit 'm' fails (?![a-zA-Z_}]) so backtracking
    # retreats before the final digit/fraction and succeeds at the decimal point or prefix:
    assert _LEGACY_NUMBER_TOKEN_RE.findall("1.6m") == ["1"]
    assert _LEGACY_NUMBER_TOKEN_RE.findall("0.17m") == ["0.1"]
    assert _LEGACY_NUMBER_TOKEN_RE.findall("0.5m") == ["0"]
    assert _LEGACY_NUMBER_TOKEN_RE.findall("2m") == []
    # Without 0-9 in the negative lookbehind, the legacy pattern also leaked digits from subscripts:
    assert _LEGACY_NUMBER_TOKEN_RE.findall("t10") == ["0"]

    assert normalize_numeric_strings("1.6m") == ["1.6"]
    assert normalize_numeric_strings("0.17m") == ["0.17"]
    assert normalize_numeric_strings("0.5m") == ["0.5"]
    assert normalize_numeric_strings("2m") == ["2"]
    assert normalize_numeric_strings("t10") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.6m", ["1.6"]),
        ("0.17m", ["0.17"]),
        ("0.5m", ["0.5"]),
        ("2m", ["2"]),
        ("1.6 m", ["1.6"]),
        ("0.17 m", ["0.17"]),
        ("0.5 m", ["0.5"]),
        ("2 m", ["2"]),
        ("The distance is 1.6.", ["1.6"]),
        ("The width is 1.6m.", ["1.6"]),
        ("長さ2m、幅1.6m、高さ0.17m、側面0.5m）", ["2", "1.6", "0.17", "0.5"]),
        ("2.4GHz and 5GHz bands (20MHz, 10ms, 50Hz)", ["2.4", "5", "20", "10", "50"]),
        ("2.4 GHz and 5 GHz bands (20 MHz, 10 ms)", ["2.4", "5", "20", "10"]),
        ("0.25m2 and 0.5cm3 at 100Mbps", ["0.25", "0.5", "100"]),
        ("-3.5m and +1.2e-4 and 1.5e-3m", ["-3.5", "0.00012", "0.0015"]),
        ("1,000 and 1,234.50 and 1.50", ["1000", "1234.5", "1.5"]),
        ("1,234,567", ["1234567"]),
        ("1.6m x 0.17m (0.25m2)", ["1.6", "0.17", "0.25"]),
        ("between 1.0 and 2.5m; range 1.0-3.5m", ["1", "2.5", "1", "3.5"]),
    ],
)
def test_normalize_numeric_strings_unit_attached_and_spaced(
    text: str,
    expected: list[str],
) -> None:
    """Direct extraction assertions for unit-attached, spaced, signed, decimal, and sci numbers."""
    assert normalize_numeric_strings(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a1b", []),
        ("a12.5b", []),
        ("v2.0", []),
        ("{v1}", []),
        ("{v12}", []),
        ("t0", []),
        ("at0", []),
        ("t10", []),
        ("x_1", []),
        ("a_{1.2}", []),
        ("12b", []),
        ("0x1f", []),
        ("203d631b", []),
        ("3D", []),
        ("1st", []),
        ("Section 2.4.1", []),
        ("A 0.25 {v1} region [1, 2] spans 1.6m.", ["0.25", "1.6"]),
        ("In Eq.(4) and Eq.(6), {v1} for at0 uses step 0.5s.", ["4", "6", "0.5"]),
    ],
)
def test_normalize_numeric_strings_prevents_false_positives(
    text: str,
    expected: list[str],
) -> None:
    """Identifiers, placeholders, hex strings, ordinals, and subscripts must not produce bogus numbers."""
    assert normalize_numeric_strings(text) == expected


def test_page2_block8_real_example_and_actual_numeric_mutations() -> None:
    """Verify page-2:block-8 verbatim output passes while real numeric changes/omissions still warn."""
    masked_src = (
        "Figure 1 shows an indoor measurement environ- ment where an aluminum plate "
        "simulates the under- side of a vehicle. The aluminum plate measures 2 m in length "
        "and 1.6 m in width and is supported by a 0.17- m-high styrene foam representing "
        "the vehicle\u2019s height. The power supply unit, assumed to have sides of 0.5 m, "
        "defines a 0.25 {v1} detection area. As shown in Figs. 2(a), (b), and (c), two "
        "L-shaped antennas surround the detection area. In contrast, three rectangular "
        "an- tennas are arranged in an equilateral triangle at the center. Figure 2(a) "
        "depicts an L-shaped antenna for transmission and a rectangular antenna for "
        "reception (L-Tx {v2} Re-Rx), while Fig. 2(b) shows a rectangular antenna for "
        "transmission and an L-shaped antenna for reception (Re-Tx {v3}). Antennas are "
        "placed on the floor as shown in Fig. 2(c), with measurements conducted in "
        "2.4 GHz and 5 GHz bands (20 MHz bandwidth). For each antenna arrangement and "
        "op- erating frequency band, measurements were also per- formed in the absence "
        "of the aluminum plate simulat- ing the vehicle. Thus, a total of eight "
        "measurement patterns were used."
    )
    valid_out = (
        "図1に、アルミニウム板で車両の下面を模した屋内測定環境を示す。"
        "アルミニウム板は長さ2m、幅1.6mであり、車両の高さに相当する高さ0.17mのスチレンフォームによって支持されている。"
        "側面が0.5mと想定される電源ユニットにより、0.25 {v1}の検出領域が画定される。"
        "図2(a)、(b)および(c)に示すように、検出領域の周囲を2本のL型アンテナが囲んでいる。"
        "対照的に、3本の矩形アンテナが中心部に正三角形に配置されている。"
        "図2(a)は送信用のL型アンテナと受信用の矩形アンテナ（L-Tx {v2} Re-Rx）を示し、"
        "図2(b)は送信用の矩形アンテナと受信用のL型アンテナ（Re-Tx {v3}）を示している。"
        "アンテナは図2(c)に示すように床面に設置され、2.4 GHz帯および5 GHz帯（帯域幅20 MHz）で測定が行われた。"
        "各アンテナ配置および動作周波数帯について、車両を模したアルミニウム板がない条件下でも測定を実施した。"
        "したがって、計8種類の測定パターンが用いられた。"
    )

    ok_res = validate_translation_output(masked_src, masked_src, valid_out)
    assert ok_res.is_valid is True
    assert "NUMERIC_VALUE_MISMATCH" not in ok_res.warning_codes

    # Mutating 1.6m -> 1.5m must trigger NUMERIC_VALUE_MISMATCH even with attached unit
    mutated_out = valid_out.replace("幅1.6m", "幅1.5m")
    mut_res = validate_translation_output(masked_src, masked_src, mutated_out)
    assert "NUMERIC_VALUE_MISMATCH" in mut_res.warning_codes
    mut_finding = next(f for f in mut_res.findings if f.code == "NUMERIC_VALUE_MISMATCH")
    assert "1.6" in mut_finding.details["source_numbers"]
    assert "1.6" not in mut_finding.details["output_numbers"]

    # Omitting 0.17m completely must also trigger NUMERIC_VALUE_MISMATCH
    dropped_out = valid_out.replace("高さ0.17m", "所定高さ")
    drop_res = validate_translation_output(masked_src, masked_src, dropped_out)
    assert "NUMERIC_VALUE_MISMATCH" in drop_res.warning_codes

    # Mutating signed or scientific unit-attached values must also be detected
    sci_src = "Offset is -3.5 m with step 1.5e-3 m."
    sci_bad = "オフセットは3.5m、ステップは1.5e-2mです。"
    sci_res = validate_translation_output(sci_src, sci_src, sci_bad)
    assert "NUMERIC_VALUE_MISMATCH" in sci_res.warning_codes


def test_numeric_validation_multiplicity_constraint_documented() -> None:
    """Existing check compares unique presence rather than occurrence count (kept out of scope here)."""
    src = "Two boards each measure 2 m and 2 m with height 0.5 m."
    # One occurrence of `2` dropped, while `2` still appears once in output
    out = "板の寸法は2m、高さは0.5mです。"
    assert normalize_numeric_strings(src) == ["2", "2", "0.5"]
    assert normalize_numeric_strings(out) == ["2", "0.5"]
    res = validate_translation_output(src, src, out)
    assert "NUMERIC_VALUE_MISMATCH" not in res.warning_codes
