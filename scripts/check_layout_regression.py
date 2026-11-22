"""CLI tool for running the 4 mechanical layout & math regression metrics on PDFs.

Usage examples:
    python scripts/check_layout_regression.py --pdf input.pdf
    python scripts/check_layout_regression.py --pdf input.pdf --mono-pdf out.ja.pdf --dual-pdf out.dual.pdf --report-json out.report.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.config import AppConfig
from backend.domain.models import Block, BlockStatus, LayoutRegressionReport, TranslationJobConfig
from backend.pdf.adapters.native_preserving_adapter import NativePreservingAdapter
from backend.pdf.coordinates import parse_page_range_spec
from backend.pdf.validation.layout_regression_metrics import evaluate_document_layout_regression
from backend.pdf.validation.preflight import inspect_pdf_preflight


def _configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _load_report_hints(report_json_path: Path | None) -> tuple[TranslationJobConfig, dict[str, str]]:
    if report_json_path is None or not report_json_path.exists():
        return TranslationJobConfig(), {}
    data = json.loads(report_json_path.read_text(encoding="utf-8"))
    prov = data.get("execution_provenance") or {}
    cfg = TranslationJobConfig(
        normalize_line_break_hyphens=bool(prov.get("normalize_line_break_hyphens", True)),
        protect_isolated_math_vars=bool(prov.get("protect_isolated_math_vars", True)),
    )
    status_by_suffix: dict[str, str] = {}
    applied = data.get("applied_block_revisions") or {}
    if isinstance(applied, dict):
        for blk_id, meta in applied.items():
            if isinstance(blk_id, str) and ":" in blk_id and isinstance(meta, dict):
                suffix = blk_id.split(":", 1)[1]
                st = meta.get("status")
                if isinstance(st, str):
                    status_by_suffix[suffix] = st
    return cfg, status_by_suffix


def _apply_status_hints(blocks: list[Block], status_by_suffix: dict[str, str]) -> None:
    if not status_by_suffix:
        return
    valid_statuses = {s.value: s for s in BlockStatus}
    for blk in blocks:
        if ":" not in blk.id:
            continue
        suffix = blk.id.split(":", 1)[1]
        hinted = status_by_suffix.get(suffix)
        if hinted in valid_statuses and blk.status != BlockStatus.PROTECTED_SKIP:
            blk.status = valid_statuses[hinted]


def _format_report_table(report: LayoutRegressionReport) -> str:
    lines: list[str] = []
    title = report.original_filename or report.document_id or "document"
    lines.append(f"=== Layout & Math Regression Report: {title} ({report.total_pages_evaluated} pages) ===")
    header = (
        f"{'Page':>4} | {'Formula(Img/Ext/Diff)':>21} | {'NumKeep(Page/Body)':>18} | "
        f"{'Cols/Inv':>8} | {'Align(Chk/Mis/Ovl/Inv/Gap)':>26} | {'Review':<8}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for p in report.pages:
        fc = p.formula_block_count
        f_str = f"{fc.image_formula_cluster_count}/{fc.extracted_formula_block_count}/{fc.count_diff:+d}"
        nm = p.numeric_preservation
        n_str = f"{nm.preservation_ratio:.3f}/{nm.body_blocks_masking.preservation_ratio:.3f}"
        co = p.column_order
        c_str = f"{co.detected_column_count}/{co.inversion_count}"
        al = p.protected_alignment
        if al is None:
            a_str = "-"
        else:
            a_str = (
                f"{al.checked_protected_blocks}/{len(al.misaligned_blocks)}/"
                f"{len(al.overlapping_pairs)}/{len(al.order_inversions)}/{len(al.abnormal_gaps)}"
            )
        rev = "FLAGGED" if p.needs_visual_review else "OK"
        lines.append(
            f"{p.display_page_number:>4} | {f_str:>21} | {n_str:>18} | {c_str:>8} | {a_str:>26} | {rev:<8}"
        )

    lines.append("-" * len(header))
    lines.append(
        f"Flagged pages ({len(report.flagged_pages)}): "
        f"{report.flagged_pages if report.flagged_pages else 'None'}"
    )
    for p in report.pages:
        if not p.needs_visual_review:
            continue
        lines.append(f"  [Page {p.display_page_number}] reasons: {', '.join(p.review_reasons)}")
        if p.numeric_preservation.missing_tokens:
            sample = p.numeric_preservation.missing_tokens[:10]
            lines.append(f"    missing numeric tokens (sample): {sample}")
        if p.numeric_preservation.body_blocks_masking.missing_tokens:
            lines.append(
                f"    body masking missing tokens: {p.numeric_preservation.body_blocks_masking.missing_tokens[:10]}"
            )
        if p.column_order.inversions:
            inv_desc = [
                f"{inv.prev_block_id}(c{inv.prev_col})->{inv.curr_block_id}(c{inv.curr_col})"
                for inv in p.column_order.inversions[:5]
            ]
            lines.append(f"    column inversions: {inv_desc}")
        if p.protected_alignment is not None and not p.protected_alignment.all_aligned:
            for m in p.protected_alignment.misaligned_blocks[:5]:
                lines.append(f"    misaligned[{m.target}] {m.block_id}: {m.reason}")
            for o in p.protected_alignment.overlapping_pairs[:5]:
                lines.append(
                    f"    overlap[{o.target}] {o.protected_block_id} vs {o.neighbor_block_id} ({o.relation}): "
                    f"v_ov={o.vertical_overlap_pt}pt area={o.overlap_area_pt2}"
                )
            for iv in p.protected_alignment.order_inversions[:5]:
                lines.append(
                    f"    order_inv[{iv.target}] {iv.protected_block_id} vs {iv.neighbor_block_id} ({iv.expected_relation})"
                )
            for gp in p.protected_alignment.abnormal_gaps[:5]:
                lines.append(
                    f"    abnormal_gap[{gp.target}] {gp.protected_block_id} ({gp.relation}): "
                    f"orig={gp.orig_gap_pt}pt out={gp.output_gap_pt}pt"
                )
    return "\n".join(lines)


def run_cli(argv: list[str] | None = None) -> tuple[LayoutRegressionReport, int]:
    parser = argparse.ArgumentParser(
        description="Run 4 mechanical layout & math regression metrics on an academic PDF."
    )
    parser.add_argument("--pdf", required=True, type=Path, help="Input original PDF path (read-only)")
    parser.add_argument("--mono-pdf", type=Path, default=None, help="Output monolingual PDF (.ja.pdf)")
    parser.add_argument("--dual-pdf", type=Path, default=None, help="Output dual bilingual PDF (.dual.pdf)")
    parser.add_argument("--report-json", type=Path, default=None, help="Optional job validation report JSON")
    parser.add_argument("--pages", type=str, default=None, help="1-indexed page range (e.g. '1,3-5')")
    parser.add_argument("--dpi", type=int, default=150, help="Rasterization DPI for Metric 1 (default: 150)")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional path to write JSON report")
    parser.add_argument(
        "--fail-on-flagged",
        action="store_true",
        help="Exit with code 2 if any page requires visual review",
    )
    args = parser.parse_args(argv)

    pdf_path = args.pdf.resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"Input PDF not found: {pdf_path}")

    job_cfg, status_hints = _load_report_hints(args.report_json)
    doc = inspect_pdf_preflight(pdf_path, original_filename=pdf_path.name)
    page_indices = parse_page_range_spec(args.pages, max(1, doc.page_count))

    with tempfile.TemporaryDirectory(prefix="pb_layout_check_") as tmp_dir:
        cfg = AppConfig(runtime_dir=Path(tmp_dir))
        adapter = NativePreservingAdapter(cfg)
        blocks = adapter.parse_and_extract_blocks(
            document=doc,
            page_indices=page_indices,
            job_config=job_cfg,
        )

    _apply_status_hints(blocks, status_hints)

    report = evaluate_document_layout_regression(
        original_pdf_path=pdf_path,
        blocks=blocks,
        page_indices=page_indices,
        mono_pdf_path=args.mono_pdf,
        dual_pdf_path=args.dual_pdf,
        document_id=doc.id,
        original_filename=doc.original_filename,
        dpi=args.dpi,
    )

    print(_format_report_table(report))

    if args.json_out is not None:
        out_path = args.json_out.resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Saved JSON report to: {out_path}")

    exit_code = 2 if (args.fail_on_flagged and report.flagged_pages) else 0
    return report, exit_code


def main(argv: list[str] | None = None) -> int:
    _configure_stdio_utf8()
    _, exit_code = run_cli(argv)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
