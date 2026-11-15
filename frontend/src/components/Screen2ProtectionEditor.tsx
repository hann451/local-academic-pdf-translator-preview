/**
 * Screen 2: PDF.js Viewer & SVG Protection Region Editor (Section 6).
 *
 * Features:
 * - Renders PDF page via local PDF.js worker (`PdfPageCanvas`) with 1-indexed page navigation,
 *   zoom (`0.5x` .. `2.5x`), and user view rotation (`0°`, `90°`, `180°`, `270°`).
 * - Displays all 6 core block kinds (`paragraph`, `heading`/`title`, `isolated_formula`,
 *   `figure`, `table`, `reference`) with a comprehensive Legend that uses labels, icons,
 *   and stroke dash patterns in addition to color.
 * - Clearly distinguishes Auto-detected (`auto`), Manual (`manual`), and Fallback (`fallback_original`).
 * - Allows drag creation & deletion of manual protection regions (`do_not_translate`,
 *   `protect_as_formula`, `protect_as_figure`, `translate_as_body`), handling reverse drag,
 *   tiny accidental clicks, and out-of-bounds clamping.
 */

import React, { useEffect, useState } from "react";
import type {
  Block,
  BlockKind,
  BlockSplitPreviewResult,
  BlockSplitRole,
  BlockSplitSpec,
  Document,
  ProtectionMode,
  ProtectionRegion,
} from "../types/api";
import {
  backendToViewportBbox,
  getRotatedViewportDimensions,
  normalizeDragSelection,
  viewportToBackendBbox,
  type BBox4,
} from "../utils/coordinates";
import { displayPageToIndex0 } from "../utils/pageRange";
import { PdfPageCanvas } from "./PdfPageCanvas";

export interface Screen2Props {
  document: Document;
  pdfBytes: Uint8Array | null;
  blocks: Block[];
  protectionRegions: ProtectionRegion[];
  blockSplits?: BlockSplitSpec[];
  isParsing: boolean;
  saveStatus: "saved" | "saving" | "error";
  saveMessage: string | null;
  onReparseBlocks: () => Promise<void>;
  onAddProtectionRegion: (payload: {
    page_index: number;
    bbox: BBox4;
    mode: ProtectionMode;
    note?: string | null;
  }) => Promise<void>;
  onDeleteProtectionRegion: (regionId: string) => Promise<void>;
  onPreviewBlockSplit?: (payload: {
    page_index: number;
    requested_bbox: BBox4;
    include_equation_number: boolean;
  }) => Promise<BlockSplitPreviewResult>;
  onCreateBlockSplit?: (payload: {
    page_index: number;
    requested_bbox: BBox4;
    include_equation_number: boolean;
    note?: string | null;
    expected_protection_revision?: number | null;
    expected_parse_revision?: number | null;
    expected_parent_signature?: string | null;
  }) => Promise<unknown>;
  onDeleteBlockSplit?: (splitId: string) => Promise<void>;
  onProceedToStartJob: (options?: { allowPendingSplits?: boolean }) => Promise<void>;
  jobStartError?: string | null;
  onSetBlockOverride?: (
    blockId: string,
    override: "force_translate" | "force_protect" | null
  ) => Promise<void>;
  onReapplyAutoProtection?: () => Promise<void>;
}

interface BlockVisualStyle {
  labelJa: string;
  shortIcon: string;
  stroke: string;
  fill: string;
  dashArray: string;
}

const BLOCK_KIND_STYLES: Record<BlockKind, BlockVisualStyle> = {
  title: {
    labelJa: "タイトル",
    shortIcon: "[T]",
    stroke: "#1d4ed8",
    fill: "rgba(29, 78, 216, 0.08)",
    dashArray: "none",
  },
  heading: {
    labelJa: "見出し",
    shortIcon: "[§]",
    stroke: "#1d4ed8",
    fill: "rgba(29, 78, 216, 0.08)",
    dashArray: "none",
  },
  paragraph: {
    labelJa: "本文段落",
    shortIcon: "[¶]",
    stroke: "#0f766e",
    fill: "rgba(15, 118, 110, 0.07)",
    dashArray: "none",
  },
  caption: {
    labelJa: "キャプション",
    shortIcon: "[Cap]",
    stroke: "#0369a1",
    fill: "rgba(3, 105, 161, 0.08)",
    dashArray: "4 2",
  },
  isolated_formula: {
    labelJa: "独立数式 (保護)",
    shortIcon: "[∑]",
    stroke: "#7c3aed",
    fill: "rgba(124, 58, 237, 0.14)",
    dashArray: "6 3",
  },
  figure: {
    labelJa: "図・ベクター (保護)",
    shortIcon: "[▣]",
    stroke: "#b45309",
    fill: "rgba(180, 83, 9, 0.12)",
    dashArray: "8 3 2 3",
  },
  table: {
    labelJa: "表・グリッド (保護)",
    shortIcon: "[⊞]",
    stroke: "#be123c",
    fill: "rgba(190, 18, 60, 0.11)",
    dashArray: "3 3",
  },
  reference: {
    labelJa: "参考文献 (保護)",
    shortIcon: "[Ref]",
    stroke: "#475569",
    fill: "rgba(71, 85, 105, 0.10)",
    dashArray: "2 2",
  },
  header_footer: {
    labelJa: "ヘッダ/フッタ (保護)",
    shortIcon: "[H/F]",
    stroke: "#64748b",
    fill: "rgba(100, 116, 139, 0.08)",
    dashArray: "2 4",
  },
  unknown: {
    labelJa: "未分類",
    shortIcon: "[?]",
    stroke: "#6b7280",
    fill: "rgba(107, 114, 128, 0.08)",
    dashArray: "4 4",
  },
};

const PROTECTION_MODE_LABELS: Record<
  ProtectionMode,
  { label: string; icon: string; color: string; dash: string }
> = {
  do_not_translate: {
    label: "翻訳しない (DO_NOT_TRANSLATE)",
    icon: "[🔒不翻訳]",
    color: "#dc2626",
    dash: "5 2",
  },
  protect_as_formula: {
    label: "数式として保護 (PROTECT_AS_FORMULA)",
    icon: "[🔒∑数式]",
    color: "#7c3aed",
    dash: "7 3",
  },
  protect_as_figure: {
    label: "図として保護 (PROTECT_AS_FIGURE)",
    icon: "[🔒▣図]",
    color: "#d97706",
    dash: "9 3 2 3",
  },
  translate_as_body: {
    label: "本文として翻訳 (TRANSLATE_AS_BODY)",
    icon: "[✎本文指定]",
    color: "#059669",
    dash: "none",
  },
};

const ZOOM_SCALES = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5];
const ROTATION_OPTIONS: Array<0 | 90 | 180 | 270> = [0, 90, 180, 270];

const SPLIT_STATUS_LABELS: Record<
  BlockSplitSpec["applied_status"],
  { labelJa: string; badgeClass: string }
> = {
  applied: { labelJa: "✅ 適用中", badgeClass: "badge-success" },
  pending_ambiguous: { labelJa: "⚠ 適用保留（曖昧）", badgeClass: "badge-warning" },
  pending_missing: { labelJa: "⚠ 適用保留（対象消失）", badgeClass: "badge-warning" },
  pending_changed: { labelJa: "⚠ 適用保留（親内容変更）", badgeClass: "badge-warning" },
};

const SPLIT_ROLE_LABELS: Record<BlockSplitRole, { labelJa: string; shortTag: string }> = {
  top: { labelJa: "上の本文（翻訳対象）", shortTag: "✂上本文" },
  formula: { labelJa: "選択数式（原文保護）", shortTag: "✂数式保護" },
  bottom: { labelJa: "下の本文（翻訳対象）", shortTag: "✂下本文" },
};

export const Screen2ProtectionEditor: React.FC<Screen2Props> = ({
  document,
  pdfBytes,
  blocks,
  protectionRegions,
  blockSplits = [],
  isParsing,
  saveStatus,
  saveMessage,
  onReparseBlocks,
  onSetBlockOverride,
  onReapplyAutoProtection,
  onAddProtectionRegion,
  onDeleteProtectionRegion,
  onPreviewBlockSplit,
  onCreateBlockSplit,
  onDeleteBlockSplit,
  onProceedToStartJob,
  jobStartError = null,
}) => {
  const [displayPage, setDisplayPage] = useState<number>(1);
  const [scale, setScale] = useState<number>(1.0);
  const [userRotation, setUserRotation] = useState<0 | 90 | 180 | 270>(0);
  const [dragToolMode, setDragToolMode] = useState<"protection_region" | "split_formula">(
    "protection_region"
  );
  const [newRegionMode, setNewRegionMode] = useState<ProtectionMode>("do_not_translate");
  const [newRegionNote, setNewRegionNote] = useState<string>("");
  const [splitIncludeEqNum, setSplitIncludeEqNum] = useState<boolean>(true);
  const [splitNote, setSplitNote] = useState<string>("");
  const [splitPreview, setSplitPreview] = useState<BlockSplitPreviewResult | null>(null);
  const [isPreviewLoading, setIsPreviewLoading] = useState<boolean>(false);
  const [isConfirmingSplit, setIsConfirmingSplit] = useState<boolean>(false);
  const [splitActionError, setSplitActionError] = useState<string | null>(null);
  const [selectedRegionId, setSelectedRegionId] = useState<string | null>(null);
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(null);
  const [dragNotice, setDragNotice] = useState<string | null>(null);
  const [allowPendingSplitsStart, setAllowPendingSplitsStart] = useState<boolean>(false);

  const pendingSplits = blockSplits.filter((sp) =>
    sp.applied_status.startsWith("pending_")
  );
  const pendingSplitSignature = pendingSplits
    .map((sp) => `${sp.id}:${sp.applied_status}`)
    .join("|");

  useEffect(() => {
    setAllowPendingSplitsStart(false);
  }, [pendingSplitSignature]);

  const [dragStart, setDragStart] = useState<[number, number] | null>(null);
  const [dragCurrent, setDragCurrent] = useState<[number, number] | null>(null);

  const totalPages = Math.max(1, document.page_count);
  const safeDisplayPage = Math.max(1, Math.min(totalPages, displayPage));
  const currentPageIndex0 = displayPageToIndex0(safeDisplayPage, totalPages);

  const pageMeta = document.pages.find((p) => p.page_index === currentPageIndex0) || {
    document_id: document.id,
    page_index: currentPageIndex0,
    width_pt: 595,
    height_pt: 842,
    rotation_deg: 0 as const,
    media_box: [0, 0, 595, 842] as BBox4,
    crop_box: [0, 0, 595, 842] as BBox4,
    has_extractable_text: true,
    is_scanned_like: false,
    cid_ratio: 0,
    column_count: 1,
  };

  const dims = getRotatedViewportDimensions(
    pageMeta.width_pt,
    pageMeta.height_pt,
    scale,
    pageMeta.rotation_deg,
    userRotation,
    1
  );

  const toVp = (bbox: BBox4): BBox4 =>
    backendToViewportBbox(
      bbox,
      pageMeta.width_pt,
      pageMeta.height_pt,
      scale,
      pageMeta.rotation_deg,
      userRotation
    );

  const pageBlocks = blocks.filter((b) => b.page_index === currentPageIndex0);
  const pageRegions = protectionRegions.filter((r) => r.page_index === currentPageIndex0);
  const pageSplits = blockSplits.filter((s) => s.page_index === currentPageIndex0);

  const handlePageChange = (nextPage: number) => {
    setDisplayPage(nextPage);
    setSplitPreview(null);
    setSplitActionError(null);
    setDragNotice(null);
  };

  const requestSplitPreview = async (
    pageIndex: number,
    backendBbox: BBox4,
    includeEqNum: boolean
  ) => {
    if (!onPreviewBlockSplit) return;
    setIsPreviewLoading(true);
    setSplitActionError(null);
    try {
      const preview = await onPreviewBlockSplit({
        page_index: pageIndex,
        requested_bbox: backendBbox,
        include_equation_number: includeEqNum,
      });
      setSplitPreview(preview);
      if (preview.valid) {
        setDragNotice(
          `分割プレビューを取得しました（分割数: ${preview.parts.length}）。右パネルで内容を確認し、「分割を確定」を押してください。`
        );
      } else {
        setDragNotice(
          `選択範囲を分割できませんでした: ${preview.rejection_reason || preview.rejection_code || "不明な理由"}`
        );
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setSplitActionError(msg);
      setDragNotice(`分割プレビュー取得に失敗しました: ${msg}`);
    } finally {
      setIsPreviewLoading(false);
    }
  };

  const handleToggleSplitEqNum = async (includeEqNum: boolean) => {
    setSplitIncludeEqNum(includeEqNum);
    if (splitPreview && splitPreview.page_index === currentPageIndex0 && onPreviewBlockSplit) {
      await requestSplitPreview(
        splitPreview.page_index,
        splitPreview.requested_bbox,
        includeEqNum
      );
    }
  };

  const handleConfirmSplit = async () => {
    if (!splitPreview || !splitPreview.valid || !onCreateBlockSplit) return;
    setIsConfirmingSplit(true);
    setSplitActionError(null);
    try {
      await onCreateBlockSplit({
        page_index: splitPreview.page_index,
        requested_bbox: splitPreview.requested_bbox,
        include_equation_number: splitIncludeEqNum,
        note: splitNote.trim() || null,
        expected_protection_revision: splitPreview.protection_revision,
        expected_parse_revision: splitPreview.parse_revision,
        expected_parent_signature: splitPreview.parent_signature,
      });
      setSplitPreview(null);
      setSplitNote("");
      setDragNotice("選択範囲の数式保護と本文への分割を確定しました。");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setSplitActionError(msg);
    } finally {
      setIsConfirmingSplit(false);
    }
  };

  const handleMouseDown = (xPx: number, yPx: number) => {
    setDragStart([xPx, yPx]);
    setDragCurrent([xPx, yPx]);
    setDragNotice(null);
  };

  const handleMouseMove = (xPx: number, yPx: number) => {
    if (!dragStart) return;
    setDragCurrent([xPx, yPx]);
  };

  const handleMouseUp = async (xPx: number, yPx: number) => {
    if (!dragStart) return;
    const [sx, sy] = dragStart;
    setDragStart(null);
    setDragCurrent(null);

    const norm = normalizeDragSelection(
      sx,
      sy,
      xPx,
      yPx,
      dims.cssWidthPx,
      dims.cssHeightPx,
      8
    );

    if (!norm.valid || !norm.viewportBboxPx) {
      if (norm.rejectedReason === "too_small") {
        setDragNotice(
          "ドラッグ範囲が極小（8px未満）のためクリックと判定し、領域追加をスキップしました。"
        );
      } else {
        setDragNotice("ページ領域外のドラッグのため棄却しました。");
      }
      return;
    }

    const backendBbox = viewportToBackendBbox(
      norm.viewportBboxPx,
      pageMeta.width_pt,
      pageMeta.height_pt,
      scale,
      pageMeta.rotation_deg,
      userRotation
    );

    if (dragToolMode === "split_formula") {
      await requestSplitPreview(currentPageIndex0, backendBbox, splitIncludeEqNum);
      return;
    }

    if (norm.clampedOutOfBounds) {
      setDragNotice(
        `ページ境界外のドラッグをページ枠内へクランプして登録しました: [${backendBbox.join(", ")}] pt`
      );
    } else {
      setDragNotice(
        `手動保護領域を追加しました (Page ${safeDisplayPage}, [${backendBbox.join(", ")}] pt)`
      );
    }

    await onAddProtectionRegion({
      page_index: currentPageIndex0,
      bbox: backendBbox,
      mode: newRegionMode,
      note: newRegionNote.trim() || null,
    });
  };

  const activeDragRect: BBox4 | null =
    dragStart && dragCurrent
      ? [
          Math.max(0, Math.min(dims.cssWidthPx, Math.min(dragStart[0], dragCurrent[0]))),
          Math.max(0, Math.min(dims.cssHeightPx, Math.min(dragStart[1], dragCurrent[1]))),
          Math.max(0, Math.min(dims.cssWidthPx, Math.max(dragStart[0], dragCurrent[0]))),
          Math.max(0, Math.min(dims.cssHeightPx, Math.max(dragStart[1], dragCurrent[1]))),
        ]
      : null;

  return (
    <div className="screen-container" data-testid="screen2-protection-editor">
      {/* Toolbar */}
      <div className="card" style={{ marginBottom: "16px", padding: "12px 16px" }}>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "14px",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          {/* Page Navigation (1-indexed) */}
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <button
              type="button"
              className="btn btn-secondary"
              data-testid="prev-page-btn"
              disabled={safeDisplayPage <= 1}
              onClick={() => handlePageChange(Math.max(1, safeDisplayPage - 1))}
            >
              ◀ 前へ
            </button>
            <span data-testid="page-indicator" style={{ fontWeight: 600 }}>
              ページ {safeDisplayPage} / {totalPages}
            </span>
            <button
              type="button"
              className="btn btn-secondary"
              data-testid="next-page-btn"
              disabled={safeDisplayPage >= totalPages}
              onClick={() => handlePageChange(Math.min(totalPages, safeDisplayPage + 1))}
            >
              次へ ▶
            </button>
          </div>

          {/* Zoom & Rotation */}
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <label style={{ fontSize: "13px", fontWeight: 600 }}>
              倍率:
              <select
                data-testid="zoom-select"
                className="form-input"
                style={{ width: "auto", marginLeft: "6px", display: "inline-block" }}
                value={scale}
                onChange={(ev) => setScale(Number(ev.target.value))}
              >
                {ZOOM_SCALES.map((s) => (
                  <option key={s} value={s}>
                    {Math.round(s * 100)}%
                  </option>
                ))}
              </select>
            </label>

            <label style={{ fontSize: "13px", fontWeight: 600 }}>
              表示回転:
              <select
                data-testid="rotation-select"
                className="form-input"
                style={{ width: "auto", marginLeft: "6px", display: "inline-block" }}
                value={userRotation}
                onChange={(ev) =>
                  setUserRotation(Number(ev.target.value) as 0 | 90 | 180 | 270)
                }
              >
                {ROTATION_OPTIONS.map((r) => (
                  <option key={r} value={r}>
                    +{r}° (PDF元回転: {pageMeta.rotation_deg}°)
                  </option>
                ))}
              </select>
            </label>
          </div>

          {/* Save Status & Actions */}
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <span
              data-testid="protection-save-status"
              className={`badge ${
                saveStatus === "error"
                  ? "badge-error"
                  : saveStatus === "saving"
                  ? "badge-warning"
                  : "badge-success"
              }`}
            >
              {saveStatus === "saving"
                ? "保存・反映中..."
                : saveStatus === "error"
                ? "保存エラー"
                : `保存済み (rev ${document.protection_revision})`}
            </span>
            <button
              type="button"
              className="btn btn-secondary"
              data-testid="reparse-blocks-btn"
              disabled={isParsing}
              onClick={() => void onReparseBlocks()}
            >
              {isParsing ? "解析中..." : "保護領域を反映してブロック再解析"}
            </button>
            {onReapplyAutoProtection && (
              <button
                type="button"
                className="btn btn-secondary"
                data-testid="reapply-auto-protection-btn"
                disabled={isParsing}
                onClick={() => void onReapplyAutoProtection()}
              >
                初期非翻訳判定を再適用
              </button>
            )}
            <button
              type="button"
              className="btn btn-primary"
              data-testid="screen2-start-job-btn"
              disabled={pendingSplits.length > 0 && !allowPendingSplitsStart}
              onClick={() =>
                void onProceedToStartJob(
                  allowPendingSplitsStart ? { allowPendingSplits: true } : undefined
                )
              }
            >
              この設定で翻訳を開始 ▶
            </button>
          </div>
        </div>

        {pendingSplits.length > 0 && (
          <div
            role="alert"
            data-testid="pending-splits-job-warning"
            className="alert alert-warning"
            style={{ marginTop: "10px", fontSize: "13px" }}
          >
            <div>
              <strong>⚠ 適用保留中（pending）の分割設定が {pendingSplits.length} 件あります:</strong>{" "}
              対象ページ:{" "}
              {Array.from(new Set(pendingSplits.map((s) => `${s.page_index + 1}`))).join(", ")}。
              保留状態のまま翻訳を開始すると、該当箇所は数式分離されず元の親ブロックのまま処理されます。不要な場合は右側の「分割指定一覧」から削除・再設定してください。
            </div>
            <label
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
                marginTop: "6px",
                fontWeight: 600,
              }}
            >
              <input
                type="checkbox"
                data-testid="allow-pending-splits-checkbox"
                checked={allowPendingSplitsStart}
                onChange={(ev) => setAllowPendingSplitsStart(ev.target.checked)}
              />
              適用保留中の分割設定を除いてそのまま翻訳を開始することを了承する
            </label>
          </div>
        )}

        {jobStartError && (
          <div
            role="alert"
            data-testid="screen2-job-start-error"
            className="alert alert-error"
            style={{ marginTop: "10px", fontSize: "13px" }}
          >
            <strong>ジョブ開始エラー:</strong> {jobStartError}
          </div>
        )}

        {/* Legend (Color + Icon + Line Style + Source Distinction) */}
        <div
          data-testid="block-legend"
          style={{
            marginTop: "12px",
            paddingTop: "10px",
            borderTop: "1px solid #e2e8f0",
            display: "flex",
            flexWrap: "wrap",
            gap: "12px",
            fontSize: "12px",
          }}
        >
          <strong style={{ color: "#334155" }}>凡例（色＋記号＋線種）:</strong>
          {(
            [
              "paragraph",
              "heading",
              "isolated_formula",
              "figure",
              "table",
              "reference",
            ] as BlockKind[]
          ).map((k) => {
            const st = BLOCK_KIND_STYLES[k];
            return (
              <span
                key={k}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "4px",
                  padding: "2px 6px",
                  borderRadius: "4px",
                  border: `2px ${st.dashArray === "none" ? "solid" : "dashed"} ${st.stroke}`,
                  backgroundColor: st.fill,
                  color: "#0f172a",
                }}
              >
                <code>{st.shortIcon}</code> {st.labelJa}
              </span>
            );
          })}
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "4px",
              padding: "2px 6px",
              borderRadius: "4px",
              border: "2px dashed #dc2626",
              backgroundColor: "rgba(220, 38, 38, 0.12)",
            }}
          >
            <code>[🔒手動]</code> 手動保護領域 (Manual)
          </span>
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "4px",
              padding: "2px 6px",
              borderRadius: "4px",
              border: "2px dotted #ea580c",
              backgroundColor: "rgba(234, 88, 12, 0.15)",
            }}
          >
            <code>[⚠原文保持]</code> フォールバック (Fallback)
          </span>
        </div>
      </div>

      {(dragNotice || saveMessage) && (
        <div
          data-testid="screen2-notice-banner"
          className="alert alert-info"
          style={{ marginBottom: "12px", fontSize: "13px" }}
        >
          {dragNotice || saveMessage}
        </div>
      )}

      {/* Main Split View: Left = PDF + SVG Overlay, Right = Region Tools & List */}
      <div className="grid-viewer-sidebar">
        <div
          style={{
            overflow: "auto",
            backgroundColor: "#e2e8f0",
            padding: "16px",
            borderRadius: "8px",
            display: "flex",
            justifyContent: "center",
            minHeight: "520px",
          }}
        >
          <PdfPageCanvas
            pdfBytes={pdfBytes}
            displayPageNumber={safeDisplayPage}
            pageWidthPt={pageMeta.width_pt}
            pageHeightPt={pageMeta.height_pt}
            pageRotationDeg={pageMeta.rotation_deg}
            scale={scale}
            userViewRotationDeg={userRotation}
            onViewportMouseDown={handleMouseDown}
            onViewportMouseMove={handleMouseMove}
            onViewportMouseUp={(x, y) => void handleMouseUp(x, y)}
          >
            {/* 1. Render detected blocks */}
            {pageBlocks.map((blk) => {
              const vp = backendToViewportBbox(
                blk.bbox,
                pageMeta.width_pt,
                pageMeta.height_pt,
                scale,
                pageMeta.rotation_deg,
                userRotation
              );
              const x = vp[0];
              const y = vp[1];
              const w = Math.max(2, vp[2] - vp[0]);
              const h = Math.max(2, vp[3] - vp[1]);
              const st = BLOCK_KIND_STYLES[blk.kind] || BLOCK_KIND_STYLES.unknown;
              const isFallback = blk.status === "fallback_original";
              const isManualOverride = blk.protection_source === "manual";
              const isSelected = selectedBlockId === blk.id;
              const isSplitChild = Boolean(blk.split_role);
              const splitTag = blk.split_role
                ? SPLIT_ROLE_LABELS[blk.split_role]?.shortTag
                : null;
              const isManualProtected =
                blk.status === "protected_skip" && isManualOverride;
              const isManualTranslatable =
                blk.status !== "protected_skip" &&
                (isManualOverride || blk.user_protection_override === "force_translate");
              const sourceBadge = isFallback
                ? "⚠Fallback"
                : splitTag
                ? splitTag
                : isManualProtected
                ? "🔒Manual"
                : blk.status === "protected_skip"
                ? "Auto保護"
                : isManualTranslatable
                ? "翻訳(手動)"
                : "翻訳対象";

              return (
                <g
                  key={blk.id}
                  data-testid={`svg-block-${blk.id}`}
                  data-block-kind={blk.kind}
                  data-protection-source={blk.protection_source}
                  data-split-role={blk.split_role || ""}
                  onClick={(ev) => {
                    ev.stopPropagation();
                    setSelectedBlockId(blk.id);
                  }}
                >
                  <rect
                    x={x}
                    y={y}
                    width={w}
                    height={h}
                    fill={
                      isFallback
                        ? "rgba(234, 88, 12, 0.18)"
                        : blk.split_role === "formula"
                        ? "rgba(124, 58, 237, 0.15)"
                        : st.fill
                    }
                    stroke={
                      isFallback
                        ? "#ea580c"
                        : blk.split_role === "formula"
                        ? "#7c3aed"
                        : isManualProtected
                        ? "#dc2626"
                        : st.stroke
                    }
                    strokeWidth={isSelected ? 2.5 : isSplitChild ? 2 : 1.4}
                    strokeDasharray={isFallback ? "3 2" : st.dashArray}
                  />
                  <text
                    x={x + 3}
                    y={Math.max(11, y + 11)}
                    fontSize={10}
                    fontWeight={700}
                    fill="#0f172a"
                    style={{ pointerEvents: "none" }}
                  >
                    {st.shortIcon} #{blk.reading_order + 1} ({sourceBadge})
                  </text>
                </g>
              );
            })}

            {/* 2. Render manual protection regions */}
            {pageRegions.map((reg) => {
              const vp = backendToViewportBbox(
                reg.bbox,
                pageMeta.width_pt,
                pageMeta.height_pt,
                scale,
                pageMeta.rotation_deg,
                userRotation
              );
              const x = vp[0];
              const y = vp[1];
              const w = Math.max(4, vp[2] - vp[0]);
              const h = Math.max(4, vp[3] - vp[1]);
              const modeInfo = PROTECTION_MODE_LABELS[reg.mode];
              const isSelected = selectedRegionId === reg.id;

              return (
                <g
                  key={reg.id}
                  data-testid={`svg-manual-region-${reg.id}`}
                  onClick={(ev) => {
                    ev.stopPropagation();
                    setSelectedRegionId(reg.id);
                  }}
                >
                  <rect
                    x={x}
                    y={y}
                    width={w}
                    height={h}
                    fill="rgba(220, 38, 38, 0.14)"
                    stroke={modeInfo.color}
                    strokeWidth={isSelected ? 3 : 2}
                    strokeDasharray={modeInfo.dash}
                  />
                  <text
                    x={x + 4}
                    y={Math.max(12, y + 13)}
                    fontSize={11}
                    fontWeight={700}
                    fill={modeInfo.color}
                    style={{ pointerEvents: "none" }}
                  >
                    {modeInfo.icon} ({reg.source === "manual" ? "手動指定" : "自動"})
                  </text>
                </g>
              );
            })}

            {/* 3. Pending block split warnings (not currently matched uniquely) */}
            {pageSplits
              .filter((sp) => sp.applied_status !== "applied")
              .map((sp) => {
                const vp = backendToViewportBbox(
                  sp.snapped_bbox,
                  pageMeta.width_pt,
                  pageMeta.height_pt,
                  scale,
                  pageMeta.rotation_deg,
                  userRotation
                );
                return (
                  <rect
                    key={sp.id}
                    data-testid={`svg-pending-split-${sp.id}`}
                    x={vp[0]}
                    y={vp[1]}
                    width={Math.max(4, vp[2] - vp[0])}
                    height={Math.max(4, vp[3] - vp[1])}
                    fill="rgba(217, 119, 6, 0.12)"
                    stroke="#d97706"
                    strokeWidth={2}
                    strokeDasharray="6 3"
                  />
                );
              })}

            {/* 4. Interactive Split Preview Overlay */}
            {splitPreview &&
              splitPreview.page_index === currentPageIndex0 &&
              (splitPreview.valid ? (
                <g data-testid="svg-split-preview-group">
                  {splitPreview.parts.map((part) => {
                    const pvp = toVp(part.bbox);
                    const isFormulaPart = part.role === "formula";
                    return (
                      <g key={part.role} data-testid={`svg-split-part-${part.role}`}>
                        <rect
                          x={pvp[0]}
                          y={pvp[1]}
                          width={Math.max(4, pvp[2] - pvp[0])}
                          height={Math.max(4, pvp[3] - pvp[1])}
                          fill={
                            isFormulaPart
                              ? "rgba(124, 58, 237, 0.22)"
                              : "rgba(13, 148, 136, 0.14)"
                          }
                          stroke={isFormulaPart ? "#7c3aed" : "#0d9488"}
                          strokeWidth={2}
                          strokeDasharray={isFormulaPart ? "none" : "5 3"}
                        />
                        <text
                          x={pvp[0] + 4}
                          y={Math.max(12, pvp[1] + 12)}
                          fontSize={10}
                          fontWeight={700}
                          fill={isFormulaPart ? "#5b21b6" : "#115e59"}
                          style={{ pointerEvents: "none" }}
                        >
                          {part.will_protect ? "🔒" : "📝"} {part.label_ja}
                        </text>
                      </g>
                    );
                  })}
                  {splitPreview.snapped_bbox &&
                    (() => {
                      const svp = toVp(splitPreview.snapped_bbox);
                      return (
                        <rect
                          data-testid="svg-split-preview-snapped"
                          x={svp[0]}
                          y={svp[1]}
                          width={Math.max(4, svp[2] - svp[0])}
                          height={Math.max(4, svp[3] - svp[1])}
                          fill="none"
                          stroke="#6d28d9"
                          strokeWidth={2}
                          strokeDasharray="2 2"
                        />
                      );
                    })()}
                  {splitPreview.equation_number_candidate_bbox &&
                    (() => {
                      const evp = toVp(splitPreview.equation_number_candidate_bbox);
                      return (
                        <rect
                          data-testid="svg-split-preview-eqnum"
                          x={evp[0]}
                          y={evp[1]}
                          width={Math.max(4, evp[2] - evp[0])}
                          height={Math.max(4, evp[3] - evp[1])}
                          fill="rgba(217, 119, 6, 0.25)"
                          stroke="#d97706"
                          strokeWidth={2}
                        />
                      );
                    })()}
                </g>
              ) : (
                (() => {
                  const rvp = toVp(splitPreview.requested_bbox);
                  return (
                    <rect
                      data-testid="svg-split-preview-rejected"
                      x={rvp[0]}
                      y={rvp[1]}
                      width={Math.max(4, rvp[2] - rvp[0])}
                      height={Math.max(4, rvp[3] - rvp[1])}
                      fill="rgba(220, 38, 38, 0.14)"
                      stroke="#dc2626"
                      strokeWidth={2}
                      strokeDasharray="4 2"
                    />
                  );
                })()
              ))}

            {/* 5. Active drag preview rectangle */}
            {activeDragRect && (
              <rect
                data-testid="active-drag-rect"
                x={activeDragRect[0]}
                y={activeDragRect[1]}
                width={Math.max(1, activeDragRect[2] - activeDragRect[0])}
                height={Math.max(1, activeDragRect[3] - activeDragRect[1])}
                fill={
                  dragToolMode === "split_formula"
                    ? "rgba(124, 58, 237, 0.2)"
                    : "rgba(37, 99, 235, 0.18)"
                }
                stroke={dragToolMode === "split_formula" ? "#7c3aed" : "#2563eb"}
                strokeWidth={2}
                strokeDasharray="4 2"
              />
            )}
          </PdfPageCanvas>
        </div>

        {/* Right Sidebar: Drag Mode Configuration, Manual Regions List & Block Inspector */}
        <aside className="card" style={{ alignSelf: "start" }}>
          <h3 className="card-title" style={{ fontSize: "16px" }}>
            PDFドラッグ操作ツール
          </h3>

          <div
            data-testid="drag-tool-selector"
            style={{ display: "flex", gap: "6px", marginBottom: "10px", flexWrap: "wrap" }}
          >
            <button
              type="button"
              data-testid="drag-tool-protection-btn"
              className={`btn ${
                dragToolMode === "protection_region" ? "btn-primary" : "btn-secondary"
              }`}
              onClick={() => {
                setDragToolMode("protection_region");
                setSplitActionError(null);
              }}
            >
              手動保護領域を追加
            </button>
            <button
              type="button"
              data-testid="drag-tool-split-btn"
              className={`btn ${
                dragToolMode === "split_formula" ? "btn-primary" : "btn-secondary"
              }`}
              onClick={() => {
                setDragToolMode("split_formula");
                setDragNotice(
                  "分離対象の数式と式番号を左のPDFページ上でドラッグしてください（保存前にプレビュー表示されます）。"
                );
              }}
            >
              選択範囲を分離して原文保護
            </button>
          </div>

          {dragToolMode === "protection_region" ? (
            <>
              <p className="card-subtitle" style={{ fontSize: "12px" }}>
                左のPDFページ上をマウスでドラッグすると、指定モードの保護領域（PDF座標pt）を追加できます。
              </p>

              <div className="form-group">
                <label className="form-label" htmlFor="protection-mode-select">
                  ドラッグ追加時の保護モード
                </label>
                <select
                  id="protection-mode-select"
                  data-testid="protection-mode-select"
                  className="form-input"
                  value={newRegionMode}
                  onChange={(ev) => setNewRegionMode(ev.target.value as ProtectionMode)}
                >
                  <option value="do_not_translate">
                    🔒 翻訳しない (do_not_translate)
                  </option>
                  <option value="protect_as_formula">
                    ∑ 数式として保護 (protect_as_formula)
                  </option>
                  <option value="protect_as_figure">
                    ▣ 図として保護 (protect_as_figure)
                  </option>
                  <option value="translate_as_body">
                    ✎ 本文として翻訳 (translate_as_body)
                  </option>
                </select>
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="protection-note-input">
                  メモ（任意）
                </label>
                <input
                  id="protection-note-input"
                  data-testid="protection-note-input"
                  type="text"
                  className="form-input"
                  placeholder="例: 定理1の図式・可換図"
                  value={newRegionNote}
                  onChange={(ev) => setNewRegionNote(ev.target.value)}
                />
              </div>
            </>
          ) : (
            <div data-testid="split-tool-panel">
              <p className="card-subtitle" style={{ fontSize: "12px" }}>
                混在ブロック内の独立数式と式番号をドラッグで囲むと、「選択数式（原文保護）」と前後の「本文（翻訳対象）」に安全に分割するプレビューを作成します。
              </p>

              <label
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "6px",
                  fontSize: "12px",
                  marginBottom: "10px",
                }}
              >
                <input
                  type="checkbox"
                  data-testid="split-include-eqnum-checkbox"
                  checked={splitIncludeEqNum}
                  disabled={isPreviewLoading || isConfirmingSplit}
                  onChange={(ev) => void handleToggleSplitEqNum(ev.target.checked)}
                />
                式番号（例: (6)）を数式保護範囲へ含める
              </label>

              <div data-testid="split-preview-panel">
                {isPreviewLoading && (
                  <div className="alert alert-info" style={{ fontSize: "12px" }}>
                    分割候補を解析しています...
                  </div>
                )}

                {!isPreviewLoading && !splitPreview && (
                  <div style={{ fontSize: "12px", color: "#475569", marginBottom: "8px" }}>
                    PDFページ上で独立数式をドラッグして分割プレビューを生成してください。
                  </div>
                )}

                {splitPreview && splitPreview.valid && (
                  <div
                    data-testid="split-preview-valid-box"
                    style={{
                      border: "2px solid #7c3aed",
                      borderRadius: "8px",
                      padding: "10px",
                      backgroundColor: "#faf5ff",
                      marginBottom: "10px",
                      fontSize: "12px",
                    }}
                  >
                    <div style={{ fontWeight: 700, marginBottom: "6px", color: "#5b21b6" }}>
                      分割プレビュー（確定前）— 親ブロック: {splitPreview.parent_block_id}
                    </div>
                    {splitPreview.equation_number_text && (
                      <div
                        data-testid="split-preview-eqnum-info"
                        style={{ marginBottom: "6px", color: "#92400e" }}
                      >
                        検出された式番号: <code>{splitPreview.equation_number_text}</code>{" "}
                        （{splitIncludeEqNum ? "数式保護に含む" : "除外"}）
                      </div>
                    )}
                    {splitPreview.will_invalidate_manual_edits && (
                      <div
                        data-testid="split-manual-warning"
                        className="alert alert-warning"
                        style={{ padding: "6px 8px", marginBottom: "8px", fontSize: "11px" }}
                      >
                        ⚠ 対象ブロック（{splitPreview.affected_manual_block_ids.join(", ")}）の手動修正訳文は分割確定後に無効化され、再翻訳または再編集が必要になります。
                      </div>
                    )}
                    {splitPreview.has_active_job && (
                      <div
                        className="alert alert-warning"
                        style={{ padding: "6px 8px", marginBottom: "8px", fontSize: "11px" }}
                      >
                        実行中ジョブがあるため確定時に 409 Conflict で拒否される場合があります。
                      </div>
                    )}
                    {splitPreview.warnings.map((w, idx) => (
                      <div key={idx} style={{ color: "#b45309", marginBottom: "4px" }}>
                        ※ {w}
                      </div>
                    ))}
                    <ul
                      data-testid="split-preview-parts"
                      style={{
                        listStyle: "none",
                        padding: 0,
                        margin: "6px 0 8px 0",
                        display: "flex",
                        flexDirection: "column",
                        gap: "6px",
                      }}
                    >
                      {splitPreview.parts.map((p) => (
                        <li
                          key={p.role}
                          data-testid={`split-preview-part-${p.role}`}
                          style={{
                            border: p.will_protect
                              ? "1px solid #7c3aed"
                              : "1px solid #0d9488",
                            borderRadius: "6px",
                            padding: "6px",
                            backgroundColor: "#ffffff",
                          }}
                        >
                          <div
                            style={{
                              display: "flex",
                              justifyContent: "space-between",
                              marginBottom: "2px",
                            }}
                          >
                            <strong>{p.label_ja}</strong>
                            <span
                              className={`badge ${
                                p.will_protect ? "badge-warning" : "badge-success"
                              }`}
                            >
                              {p.will_protect ? "原文保護" : "翻訳対象"}
                            </span>
                          </div>
                          <div style={{ color: "#334155", wordBreak: "break-word" }}>
                            {p.source_text}
                          </div>
                        </li>
                      ))}
                    </ul>
                    <div className="form-group" style={{ marginBottom: "8px" }}>
                      <label className="form-label" htmlFor="split-note-input">
                        分割メモ（任意）
                      </label>
                      <input
                        id="split-note-input"
                        data-testid="split-note-input"
                        type="text"
                        className="form-input"
                        placeholder="例: 式(6)と直後本文の分離"
                        value={splitNote}
                        onChange={(ev) => setSplitNote(ev.target.value)}
                      />
                    </div>
                    {splitActionError && (
                      <div
                        data-testid="split-action-error"
                        className="alert alert-error"
                        style={{ padding: "6px 8px", marginBottom: "8px", fontSize: "11px" }}
                      >
                        {splitActionError}
                      </div>
                    )}
                    <div style={{ display: "flex", gap: "8px" }}>
                      <button
                        type="button"
                        className="btn btn-primary"
                        data-testid="confirm-split-btn"
                        disabled={isConfirmingSplit}
                        onClick={() => void handleConfirmSplit()}
                      >
                        {isConfirmingSplit ? "確定・保存中..." : "✂ 分割を確定して保存"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        data-testid="cancel-split-btn"
                        disabled={isConfirmingSplit}
                        onClick={() => {
                          setSplitPreview(null);
                          setSplitActionError(null);
                        }}
                      >
                        キャンセル
                      </button>
                    </div>
                  </div>
                )}

                {splitPreview && !splitPreview.valid && (
                  <div
                    data-testid="split-rejection-box"
                    className="alert alert-error"
                    style={{ fontSize: "12px", marginBottom: "10px" }}
                  >
                    <div style={{ fontWeight: 700, marginBottom: "4px" }}>
                      分割できません（{splitPreview.rejection_code || "REJECTED"}）
                    </div>
                    <div>{splitPreview.rejection_reason}</div>
                    {splitPreview.retry_guidance && (
                      <div style={{ marginTop: "4px", color: "#7f1d1d" }}>
                        <strong>対処方法:</strong> {splitPreview.retry_guidance}
                      </div>
                    )}
                    <button
                      type="button"
                      className="btn btn-secondary"
                      style={{ marginTop: "6px" }}
                      onClick={() => setSplitPreview(null)}
                    >
                      閉じる
                    </button>
                  </div>
                )}

                {!splitPreview && splitActionError && (
                  <div
                    data-testid="split-action-error"
                    className="alert alert-error"
                    style={{ fontSize: "12px", marginBottom: "10px" }}
                  >
                    {splitActionError}
                  </div>
                )}
              </div>
            </div>
          )}

          <hr style={{ border: "none", borderTop: "1px solid #e2e8f0", margin: "14px 0" }} />

          <h4 style={{ margin: "0 0 8px 0", fontSize: "14px" }}>
            このページの分割指定 ({pageSplits.length}件)
          </h4>
          {pageSplits.length === 0 ? (
            <div style={{ fontSize: "12px", color: "#64748b", marginBottom: "12px" }}>
              このページにはブロック分割指定はまだありません。
            </div>
          ) : (
            <ul
              data-testid="block-split-list"
              style={{
                listStyle: "none",
                padding: 0,
                margin: "0 0 14px 0",
                display: "flex",
                flexDirection: "column",
                gap: "8px",
              }}
            >
              {pageSplits.map((sp) => {
                const stInfo =
                  SPLIT_STATUS_LABELS[sp.applied_status] || SPLIT_STATUS_LABELS.applied;
                return (
                  <li
                    key={sp.id}
                    data-testid={`split-item-${sp.id}`}
                    style={{
                      border:
                        sp.applied_status === "applied"
                          ? "1px solid #7c3aed"
                          : "1px solid #d97706",
                      borderRadius: "6px",
                      padding: "8px",
                      fontSize: "12px",
                      backgroundColor:
                        sp.applied_status === "applied" ? "#faf5ff" : "#fffbeb",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        gap: "6px",
                      }}
                    >
                      <span
                        className={`badge ${stInfo.badgeClass}`}
                        data-testid={`split-status-${sp.id}`}
                      >
                        {stInfo.labelJa}
                      </span>
                      {onDeleteBlockSplit && (
                        <button
                          type="button"
                          className="btn btn-danger-sm"
                          data-testid={`delete-split-${sp.id}`}
                          onClick={() => void onDeleteBlockSplit(sp.id)}
                        >
                          削除
                        </button>
                      )}
                    </div>
                    <div style={{ fontFamily: "monospace", marginTop: "4px" }}>
                      親: {sp.parent_block_id || "-"} / snapped: [
                      {sp.snapped_bbox.map((v) => v.toFixed(1)).join(", ")}] pt
                    </div>
                    {sp.status_reason && (
                      <div
                        data-testid={`split-status-reason-${sp.id}`}
                        style={{ color: "#b45309", marginTop: "3px" }}
                      >
                        理由: {sp.status_reason}
                      </div>
                    )}
                    {sp.parent_source_preview && (
                      <div style={{ color: "#475569", marginTop: "3px" }}>
                        {sp.parent_source_preview}
                      </div>
                    )}
                    {sp.note && (
                      <div style={{ color: "#475569", marginTop: "2px" }}>備考: {sp.note}</div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}

          <hr style={{ border: "none", borderTop: "1px solid #e2e8f0", margin: "14px 0" }} />

          <h4 style={{ margin: "0 0 8px 0", fontSize: "14px" }}>
            このページの手動保護領域 ({pageRegions.length}件)
          </h4>
          {pageRegions.length === 0 ? (
            <div style={{ fontSize: "12px", color: "#64748b", marginBottom: "12px" }}>
              手動保護領域はまだ登録されていません。PDF上をドラッグして追加できます。
            </div>
          ) : (
            <ul
              data-testid="manual-region-list"
              style={{
                listStyle: "none",
                padding: 0,
                margin: "0 0 14px 0",
                display: "flex",
                flexDirection: "column",
                gap: "8px",
              }}
            >
              {pageRegions.map((reg) => {
                const modeInfo = PROTECTION_MODE_LABELS[reg.mode];
                return (
                  <li
                    key={reg.id}
                    data-testid={`region-item-${reg.id}`}
                    style={{
                      border:
                        selectedRegionId === reg.id
                          ? "2px solid #2563eb"
                          : "1px solid #cbd5e1",
                      borderRadius: "6px",
                      padding: "8px",
                      fontSize: "12px",
                      backgroundColor: "#f8fafc",
                    }}
                    onClick={() => setSelectedRegionId(reg.id)}
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                      }}
                    >
                      <strong style={{ color: modeInfo.color }}>{modeInfo.icon}</strong>
                      <button
                        type="button"
                        className="btn btn-danger-sm"
                        data-testid={`delete-region-${reg.id}`}
                        onClick={(ev) => {
                          ev.stopPropagation();
                          void onDeleteProtectionRegion(reg.id);
                        }}
                      >
                        削除
                      </button>
                    </div>
                    <div style={{ fontFamily: "monospace", marginTop: "4px" }}>
                      pt: [{reg.bbox.map((v) => v.toFixed(1)).join(", ")}]
                    </div>
                    {reg.note && <div style={{ color: "#475569" }}>備考: {reg.note}</div>}
                  </li>
                );
              })}
            </ul>
          )}

          <hr style={{ border: "none", borderTop: "1px solid #e2e8f0", margin: "14px 0" }} />

          <h4 style={{ margin: "0 0 8px 0", fontSize: "14px" }}>
            検出ブロック一覧 (Page {safeDisplayPage}: {pageBlocks.length}件)
          </h4>
          <div style={{ maxHeight: "260px", overflowY: "auto" }}>
            {pageBlocks.map((b) => {
              const st = BLOCK_KIND_STYLES[b.kind] || BLOCK_KIND_STYLES.unknown;
              return (
                <div
                  key={b.id}
                  data-testid={`block-list-item-${b.id}`}
                  onClick={() => setSelectedBlockId(b.id)}
                  style={{
                    padding: "6px 8px",
                    marginBottom: "6px",
                    borderRadius: "6px",
                    border:
                      selectedBlockId === b.id
                        ? "2px solid #2563eb"
                        : "1px solid #e2e8f0",
                    fontSize: "12px",
                    cursor: "pointer",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span>
                      <strong>
                        {st.shortIcon} {st.labelJa}
                      </strong>{" "}
                      (#{b.reading_order + 1})
                    </span>
                    <span
                      className={`badge ${
                        b.status === "fallback_original" ? "badge-error" : "badge-neutral"
                      }`}
                      data-testid={`block-status-badge-${b.id}`}
                    >
                      {b.status === "fallback_original"
                        ? `⚠ 原文保持フォールバック${
                            b.warnings && b.warnings.length > 0
                              ? ` (${b.warnings.join(", ")})`
                              : ""
                          }`
                        : b.split_role === "formula"
                        ? "✂ 分割数式・原文保護"
                        : b.status === "protected_skip"
                        ? b.protection_source === "manual"
                          ? "🔒手動保護"
                          : b.protection_reason || "自動保護"
                        : b.protection_source === "manual" ||
                          b.user_protection_override === "force_translate"
                        ? b.protection_reason || "翻訳対象（手動指定）"
                        : "翻訳対象"}
                    </span>
                  </div>
                  {(b.split_role || b.needs_retranslation) && (
                    <div
                      style={{
                        display: "flex",
                        gap: "4px",
                        flexWrap: "wrap",
                        marginTop: "3px",
                      }}
                    >
                      {b.split_role && (
                        <span
                          className="badge badge-info"
                          data-testid={`block-split-role-${b.id}`}
                        >
                          {SPLIT_ROLE_LABELS[b.split_role]?.labelJa || b.split_role}
                        </span>
                      )}
                      {b.needs_retranslation && (
                        <span
                          className="badge badge-warning"
                          data-testid={`block-needs-retranslation-${b.id}`}
                        >
                          ⚠ 要再翻訳
                        </span>
                      )}
                    </div>
                  )}
                  {(b.user_protection_override ||
                    b.protection_confidence === "review_candidate") && (
                    <div style={{ marginTop: "3px", color: "#b45309" }}>
                      {b.user_protection_override === "force_translate" && "利用者指定：翻訳対象"}
                      {b.user_protection_override === "force_protect" && "利用者指定：非翻訳"}
                      {!b.user_protection_override && "要確認候補"}
                    </div>
                  )}
                  {b.split_role === "formula" ? (
                    <div
                      data-testid={`split-formula-immutable-note-${b.id}`}
                      style={{ marginTop: "4px", fontSize: "11px", color: "#5b21b6" }}
                    >
                      ※分割指定で分離した数式のため原文保護に固定されています。翻訳対象に戻すには上の「分割指定」を削除してください。
                    </div>
                  ) : (
                    onSetBlockOverride && (
                      <div style={{ display: "flex", gap: "4px", marginTop: "4px" }}>
                        {b.status === "protected_skip" ? (
                          <button
                            type="button"
                            className="btn btn-secondary"
                            data-testid={`override-translate-${b.id}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              void onSetBlockOverride(b.id, "force_translate");
                            }}
                          >
                            翻訳対象にする
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="btn btn-secondary"
                            data-testid={`override-protect-${b.id}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              void onSetBlockOverride(b.id, "force_protect");
                            }}
                          >
                            非翻訳にする
                          </button>
                        )}
                        {b.user_protection_override && (
                          <button
                            type="button"
                            className="btn btn-secondary"
                            data-testid={`override-reset-${b.id}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              void onSetBlockOverride(b.id, null);
                            }}
                          >
                            自動判定に戻す
                          </button>
                        )}
                      </div>
                    )
                  )}
                  <div
                    style={{
                      color: "#334155",
                      marginTop: "3px",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {b.source_text}
                  </div>
                </div>
              );
            })}
          </div>
        </aside>
      </div>
    </div>
  );
};
