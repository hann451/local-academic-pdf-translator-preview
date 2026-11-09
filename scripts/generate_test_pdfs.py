"""Synthetic Academic Test PDF Suite Generator (Section 18 & 23).

Generates copyright-free, deterministic academic PDFs covering all required test cases:
1. `single_col_math.pdf`: 1-column paper with title, abstract, headings, inline math (`x_i^2`, Greek α, β, γ),
   isolated display equations (sum, integral, fraction, matrix, equation numbers `(1)`, `(2)`), and citations `[1, 2]`.
2. `two_col_figures_tables_2p.pdf`: 2-page, 2-column paper with vector figure, caption, table grid,
   cross-column and cross-page paragraphs, and references section.
3. `rotated_and_cropbox.pdf`: PDF with page rotation (90 deg) and custom CropBox/MediaBox margins.
4. `scanned_like_no_text.pdf`: Image-only (scanned-equivalent) PDF with zero extractable text.
5. `日本語 空白 テスト論文 (2026) 数式検証.pdf`: Japanese + space filename with formulas to verify Windows path handling.
6. `corrupted_sample.pdf`: Invalid/corrupted file for negative preflight testing.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


def generate_single_col_math_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter  # 612 x 792 pt

    # Title
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, height - 72, "Convergence Analysis of Stochastic Gradient Descent")

    # Heading 1: Abstract
    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, height - 108, "Abstract")

    # Body Paragraph 1 with inline math, Greek letters, subscripts, numbers, and citations
    c.setFont("Times-Roman", 10.5)
    c.drawString(
        72,
        height - 128,
        "In this paper, we propose the energy function E(x_i) = α_i + β_i for all samples [1, 2].",
    )
    c.drawString(
        72,
        height - 143,
        "We demonstrate that the loss function L(θ_t) converges to 0.0025 with learning rate η = 0.01.",
    )

    # Isolated Equation 1 (Fraction, Integral, Sum, Equation Number)
    c.setFont("Times-Italic", 11.5)
    c.drawString(
        150,
        height - 185,
        "∫_0^∞ f(x) dx = ∑_{i=1}^n (α_i / β_i) + √(x_1^2 + x_2^2)    (1)",
    )
    # Draw fraction bar & equation box frame lines
    c.setLineWidth(0.8)
    c.line(145, height - 193, 465, height - 193)

    # Heading 2: Methodology
    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, height - 228, "1. Introduction")

    # Body Paragraph 2
    c.setFont("Times-Roman", 10.5)
    c.drawString(
        72,
        height - 248,
        "The gradient descent update θ_{t+1} = θ_t - η∇L(θ_t) satisfies the bound in [3].",
    )
    c.drawString(
        72,
        height - 263,
        "More details are available at https://example.org/paper-bridge and doi:10.1000/xyz123.",
    )

    # Isolated Equation 2 (Matrix / Linear system representation)
    c.setFont("Courier-Bold", 11)
    c.drawString(
        160,
        height - 305,
        "[ a_11  a_12 ] [ x_1 ] = [ b_1 ]    (2)",
    )

    # Vector Figure + Caption
    c.setStrokeColorRGB(0.1, 0.2, 0.6)
    c.setFillColorRGB(0.9, 0.94, 1.0)
    c.rect(140, height - 420, 320, 75, fill=1, stroke=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 9)
    c.drawString(215, height - 385, "Architecture Diagram Box (Untranslated Inside)")

    c.setFont("Times-Roman", 10)
    c.drawString(
        110,
        height - 440,
        "Figure 1: Overview of the energy function and the gradient descent update [1].",
    )

    # References section (Non-translated by default)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, height - 480, "References")
    c.setFont("Times-Roman", 9.5)
    c.drawString(
        72,
        height - 500,
        "[1] A. Smith and B. Jones, Stochastic Optimization Methods, Journal of ML, 2024.",
    )
    c.save()


def generate_english_fraction_equation_pdf(path: Path) -> None:
    """Create an English-labeled stacked fraction with a vector bar and equation number."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=letter)
    _, height = letter

    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, height - 85, "Abstract")

    c.setFont("Times-Roman", 10.5)
    c.drawString(72, height - 110, "The energy function converges to zero.")
    c.drawString(72, height - 680, "The loss function converges to zero.")

    c.setFont("Times-Roman", 11)
    numerator_left = 200
    formula_text = c.beginText()
    formula_text.setTextOrigin(72, height - 405)
    formula_text.textOut("Detection Count Ratio =")
    formula_text.setTextOrigin(numerator_left, height - 398.25)
    formula_text.textOut("Detected Intrusions")
    c.drawText(formula_text)
    c.setLineWidth(0.8)
    c.line(numerator_left, height - 405, 310, height - 405)
    denominator_text = c.beginText()
    denominator_text.setTextOrigin(numerator_left + 5, height - 418.25)
    denominator_text.textOut("Actual Intrusions")
    denominator_text.setTextOrigin(325, height - 405)
    denominator_text.textOut("(7)")
    c.drawText(denominator_text)

    c.save()


def generate_two_col_figures_tables_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter

    # --- Page 1 ---
    # Spanning Title
    c.setFont("Helvetica-Bold", 15)
    c.drawString(72, height - 60, "Two-Column Layout and Cross-Page Preservation Study")

    # Left Column (x: 54..290)
    c.setFont("Helvetica-Bold", 11.5)
    c.drawString(54, height - 100, "1. Introduction")
    c.setFont("Times-Roman", 10)
    c.drawString(54, height - 120, "In this paper, we propose a method")
    c.drawString(54, height - 134, "for α_k = β_k + 1 in two columns [1].")

    c.setFont("Times-Italic", 10.5)
    c.drawString(85, height - 170, "E_total = ∑_{k=1}^N γ_k x_k^2    (1)")

    c.setFont("Times-Roman", 10)
    c.drawString(54, height - 205, "As shown in Figure 1, the loss function")
    c.drawString(54, height - 219, "converges to 0.05 with threshold 100.")

    # Vector Figure in Left Column
    c.setStrokeColorRGB(0.2, 0.5, 0.2)
    c.rect(60, height - 310, 210, 65, fill=0, stroke=1)
    c.line(60, height - 310, 270, height - 245)
    c.setFont("Times-Roman", 9.5)
    c.drawString(54, height - 328, "Figure 1: Convergence curve in column 1.")

    # Right Column (x: 322..558)
    c.setFont("Helvetica-Bold", 11.5)
    c.drawString(322, height - 100, "2. Methodology")
    c.setFont("Times-Roman", 10)
    c.drawString(322, height - 120, "We propose the gradient descent update")
    c.drawString(322, height - 134, "where θ_t denotes parameter state [2].")

    # Table grid in Right Column
    c.setStrokeColorRGB(0, 0, 0)
    c.rect(322, height - 240, 220, 60, fill=0, stroke=1)
    c.line(322, height - 210, 542, height - 210)
    c.line(432, height - 240, 432, height - 180)
    c.setFont("Times-Roman", 9.5)
    c.drawString(322, height - 258, "Table 1: Experimental Results summary.")

    c.setFont("Times-Roman", 10)
    c.drawString(322, height - 295, "This paragraph continues to the next")
    c.drawString(322, height - 309, "page for all active evaluation metrics.")

    c.showPage()

    # --- Page 2 ---
    c.setFont("Helvetica-Bold", 11.5)
    c.drawString(54, height - 80, "3. Experimental Results")
    c.setFont("Times-Roman", 10)
    c.drawString(54, height - 100, "The energy function satisfies the bound")
    c.drawString(54, height - 114, "with learning rate 0.001 on page 2 [1].")

    c.setFont("Helvetica-Bold", 11.5)
    c.drawString(322, height - 80, "4. Conclusion")
    c.setFont("Times-Roman", 10)
    c.drawString(322, height - 100, "In this paper, we demonstrate that")
    c.drawString(322, height - 114, "the loss function converges to 0.0.")

    c.save()


def generate_continuation_edges_pdf(path: Path) -> None:
    """Create separate physical text blocks for cross-column and cross-page prose."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter

    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, height - 52, "Synthetic Continuation Boundary Cases")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(54, height - 90, "Abstract")

    c.setFont("Times-Roman", 10)
    c.drawString(
        54,
        height - 650,
        "The estimator E_n = sum_i x_i / n remains stable when",
    )
    c.drawString(
        322,
        height - 120,
        "the sample size increases without bound, as shown in Eq. (1).",
    )
    c.drawString(
        322,
        height - 650,
        "The resulting bound depends on",
    )
    c.showPage()

    c.setFont("Times-Roman", 10)
    c.drawString(
        54,
        height - 110,
        "the sequence and converges to zero.",
    )
    c.drawString(322, height - 220, "This independent paragraph ends here.")
    c.showPage()

    c.setFont("Times-Roman", 10)
    c.drawString(72, height - 115, "A one-column page begins with a complete sentence.")
    c.drawString(
        72,
        height - 650,
        "The single-column estimate remains stable when",
    )
    c.showPage()

    c.setFont("Times-Roman", 10)
    c.drawString(72, height - 110, "the noise variance decreases.")
    c.drawString(
        72,
        height - 170,
        "RSSI and CSI measurements remain stable across the evaluation.",
    )
    c.save()


def generate_rotated_and_cropbox_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_rl = path.with_suffix(".rl_tmp.pdf")
    c = canvas.Canvas(str(tmp_rl), pagesize=letter)
    width, height = letter
    c.setFont("Times-Roman", 11)
    c.drawString(
        72,
        height - 100,
        "In this paper, we propose the energy function E(x_i) = α_i + β_i [1].",
    )
    c.setFont("Times-Italic", 11.5)
    c.drawString(
        140,
        height - 150,
        "E_rot(x) = ∑_{i=1}^n α_i x_i^2    (1)",
    )
    c.save()

    try:
        doc = pymupdf.open(str(tmp_rl))
        page = doc[0]
        page.set_cropbox(pymupdf.Rect(36, 36, 576, 756))
        page.set_rotation(90)
        doc.save(str(path))
        doc.close()
    finally:
        tmp_rl.unlink(missing_ok=True)


def generate_scanned_like_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (600, 800), color=(250, 250, 250))
    draw = ImageDraw.Draw(img)
    draw.rectangle([50, 80, 550, 200], outline=(40, 40, 40), width=2)
    draw.line([60, 120, 520, 120], fill=(80, 80, 80), width=3)
    draw.line([60, 150, 480, 150], fill=(80, 80, 80), width=3)

    tmp_png = path.with_suffix(".tmp.png")
    img.save(tmp_png)
    try:
        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)
        page.insert_image(pymupdf.Rect(36, 36, 576, 756), filename=str(tmp_png))
        doc.save(str(path))
        doc.close()
    finally:
        tmp_png.unlink(missing_ok=True)


def generate_all_test_fixtures(base_dir: str | Path | None = None) -> dict[str, Path]:
    out_dir = (
        Path(base_dir).resolve()
        if base_dir
        else (Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "generated")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    single_col = out_dir / "single_col_math.pdf"
    english_fraction = out_dir / "english_fraction_equation.pdf"
    two_col = out_dir / "two_col_figures_tables_2p.pdf"
    rotated = out_dir / "rotated_and_cropbox.pdf"
    continuations = out_dir / "continuation_edges.pdf"
    scanned = out_dir / "scanned_like_no_text.pdf"
    jp_space = out_dir / "日本語 空白 テスト論文 (2026) 数式検証.pdf"
    corrupted = out_dir / "corrupted_sample.pdf"

    generate_single_col_math_pdf(single_col)
    generate_english_fraction_equation_pdf(english_fraction)
    generate_two_col_figures_tables_pdf(two_col)
    generate_continuation_edges_pdf(continuations)
    generate_rotated_and_cropbox_pdf(rotated)
    generate_scanned_like_pdf(scanned)
    generate_single_col_math_pdf(jp_space)
    corrupted.write_bytes(b"NOT_A_VALID_PDF_FILE_CONTENT_12345")

    return {
        "single_col_math": single_col,
        "english_fraction_equation": english_fraction,
        "two_col_figures_tables_2p": two_col,
        "continuation_edges": continuations,
        "rotated_and_cropbox": rotated,
        "scanned_like_no_text": scanned,
        "japanese_space_filename": jp_space,
        "corrupted_sample": corrupted,
    }


if __name__ == "__main__":
    generated = generate_all_test_fixtures()
    for key, p in generated.items():
        print(f"[Generated] {key}: {p} ({p.stat().st_size} bytes)")
