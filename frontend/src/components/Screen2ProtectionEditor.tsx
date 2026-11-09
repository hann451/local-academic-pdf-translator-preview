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

import React, { useState } from "react";
import type {
  Block,
  BlockKind,
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
  onProceedToStartJob: () => Promise<void>;
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

export const Screen2ProtectionEditor: React.FC<Screen2Props> = ({
  document,
  pdfBytes,
  blocks,
  protectionRegions,
  isParsing,
  saveStatus,
  saveMessage,
  onReparseBlocks,
  onSetBlockOverride,
  onReapplyAutoProtection,
  onAddProtectionRegion,
  onDeleteProtectionRegion,
  onProceedToStartJob,
}) => {
  const [displayPage, setDisplayPage] = useState<number>(1);
  const [scale, setScale] = useState<number>(1.0);
  const [userRotation, setUserRotation] = useState<0 | 90 | 180 | 270>(0);
  const [newRegionMode, setNewRegionMode] = useState<ProtectionMode>("do_not_translate");
  const [newRegionNote, setNewRegionNote] = useState<string>("");
  const [selectedRegionId, setSelectedRegionId] = useState<string | null>(null);
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(null);
  const [dragNotice, setDragNotice] = useState<string | null>(null);

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

  const pageBlocks = blocks.filter((b) => b.page_index === currentPageIndex0);
  const pageRegions = protectionRegions.filter((r) => r.page_index === currentPageIndex0);

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
              onClick={() => setDisplayPage((p) => Math.max(1, p - 1))}
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
              onClick={() => setDisplayPage((p) => Math.min(totalPages, p + 1))}
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
              onClick={() => void onProceedToStartJob()}
            >
              この設定で翻訳を開始 ▶
            </button>
          </div>
        </div>

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

              const sourceBadge = isFallback
                ? "⚠Fallback"
                : isManualOverride
                ? "🔒Manual"
                : blk.protection_source === "auto"
                ? "Auto保護"
                : "翻訳対象";

              return (
                <g
                  key={blk.id}
                  data-testid={`svg-block-${blk.id}`}
                  data-block-kind={blk.kind}
                  data-protection-source={blk.protection_source}
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
                    fill={isFallback ? "rgba(234, 88, 12, 0.18)" : st.fill}
                    stroke={isFallback ? "#ea580c" : isManualOverride ? "#dc2626" : st.stroke}
                    strokeWidth={isSelected ? 2.5 : 1.4}
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

            {/* 3. Active drag preview rectangle */}
            {activeDragRect && (
              <rect
                data-testid="active-drag-rect"
                x={activeDragRect[0]}
                y={activeDragRect[1]}
                width={Math.max(1, activeDragRect[2] - activeDragRect[0])}
                height={Math.max(1, activeDragRect[3] - activeDragRect[1])}
                fill="rgba(37, 99, 235, 0.18)"
                stroke="#2563eb"
                strokeWidth={2}
                strokeDasharray="4 2"
              />
            )}
          </PdfPageCanvas>
        </div>

        {/* Right Sidebar: Drag Mode Configuration, Manual Regions List & Block Inspector */}
        <aside className="card" style={{ alignSelf: "start" }}>
          <h3 className="card-title" style={{ fontSize: "16px" }}>
            手動保護領域のドラッグ追加
          </h3>
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
                    <span className="badge badge-neutral">
                      {b.protection_source === "manual"
                        ? "🔒手動保護"
                        : b.status === "protected_skip"
                        ? b.protection_reason || "自動保護"
                        : "翻訳対象"}
                    </span>
                  </div>
                  {(b.user_protection_override ||
                    b.protection_confidence === "review_candidate") && (
                    <div style={{ marginTop: "3px", color: "#b45309" }}>
                      {b.user_protection_override === "force_translate" && "利用者指定：翻訳対象"}
                      {b.user_protection_override === "force_protect" && "利用者指定：非翻訳"}
                      {!b.user_protection_override && "要確認候補"}
                    </div>
                  )}
                  {onSetBlockOverride && (
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
