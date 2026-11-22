/**
 * Screen 3: 13-Stage Job Progress, Side-by-Side Review, Separated Manual Edit & Zero-LLM Retypeset (Section 7).
 */

import React, { useEffect, useMemo, useState } from "react";
import {
  PIPELINE_STAGES_13,
  type ArtifactSaveResult,
  type ArtifactType,
  type Block,
  type Document,
  type ExportArtifact,
  type Job,
  type JobState,
  type ValidationIssue,
} from "../types/api";
import { buildSuggestedArtifactFilename } from "../utils/artifactFilename";
import { backendToViewportBbox } from "../utils/coordinates";
import { validateManualEditClientSide } from "../utils/manualEditValidator";
import { displayPageToIndex0 } from "../utils/pageRange";
import { PdfPageCanvas } from "./PdfPageCanvas";

export interface Screen3Props {
  document: Document;
  originalPdfBytes: Uint8Array | null;
  translatedPdfBytes: Uint8Array | null;
  job: Job;
  blocks: Block[];
  issues: ValidationIssue[];
  artifacts: ExportArtifact[];
  isCanceling: boolean;
  isResuming: boolean;
  isRetypesetting: boolean;
  isRetranslatingBlock: boolean;
  retypesetMessage: string | null;
  retypesetError: string | null;
  onCancelJob: () => Promise<void>;
  onResumeJob: () => Promise<void>;
  onSaveManualEdit: (
    blockId: string,
    manualText: string | null,
    expectedEditRevision?: number
  ) => Promise<Block>;
  onRetranslateBlock: (blockId: string) => Promise<void>;
  onRetypesetJob: (allowValidationWarnings: boolean) => Promise<void>;
  onDownloadArtifact: (
    artifactType: ArtifactType,
    fallbackName: string,
    options?: { chooseLocation?: boolean }
  ) => Promise<ArtifactSaveResult | void>;
  onDirtyStateChange?: (isDirty: boolean) => void;
}

const STAGE_ORDER_INDEX: Record<JobState, number> = {
  created: 0,
  preflight: 1,
  parsing: 2,
  awaiting_review: 2,
  translating: 3,
  validating: 4,
  typesetting: 5,
  completed: 6,
  completed_with_warnings: 6,
  failed: -1,
  canceled: -1,
  interrupted: -1,
};

function getStageStatus(
  stagePhase: JobState,
  currentJobState: JobState
): "done" | "active" | "pending" {
  if (
    currentJobState === "completed" ||
    currentJobState === "completed_with_warnings"
  ) {
    return "done";
  }
  const curIdx = STAGE_ORDER_INDEX[currentJobState] ?? 0;
  const stIdx = STAGE_ORDER_INDEX[stagePhase] ?? 0;
  if (curIdx > stIdx) return "done";
  if (curIdx === stIdx && curIdx > 0) return "active";
  return "pending";
}

function formatDurationJa(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) {
    return "-";
  }
  const rounded = Math.max(0, Math.round(seconds));
  const h = Math.floor(rounded / 3600);
  const m = Math.floor((rounded % 3600) / 60);
  const s = rounded % 60;
  if (h > 0) {
    return `${h}時間 ${String(m).padStart(2, "0")}分`;
  }
  if (m > 0) {
    return `${m}分 ${String(s).padStart(2, "0")}秒`;
  }
  return `${s}秒`;
}

function computeDisplayPercent(job: Job): number {
  if (job.state === "completed" || job.state === "completed_with_warnings") {
    return 100;
  }
  if (
    typeof job.progress_percent === "number" &&
    Number.isFinite(job.progress_percent) &&
    job.progress_percent > 0
  ) {
    return Math.max(0, Math.min(100, Math.round(job.progress_percent)));
  }
  if (job.total_blocks > 0 && job.processed_blocks > 0 && job.state === "translating") {
    return Math.min(75, Math.max(8, Math.round(8 + (67 * job.processed_blocks) / job.total_blocks)));
  }
  switch (job.state) {
    case "preflight":
      return 2;
    case "parsing":
      return 5;
    case "awaiting_review":
      return 8;
    case "translating":
      return 8;
    case "validating":
      return 76;
    case "typesetting":
      return 82;
    default:
      return 0;
  }
}

function getMilestoneStepStatus(
  stepIdx: number,
  jobState: JobState
): "done" | "active" | "pending" {
  if (jobState === "completed" || jobState === "completed_with_warnings") {
    return "done";
  }
  const map: Record<JobState, number> = {
    created: 0,
    preflight: 0,
    parsing: 0,
    awaiting_review: 0,
    translating: 1,
    validating: 1,
    typesetting: 2,
    completed: 3,
    completed_with_warnings: 3,
    failed: -1,
    canceled: -1,
    interrupted: -1,
  };
  const activeIdx = map[jobState] ?? 0;
  if (activeIdx < 0) return "pending";
  if (stepIdx < activeIdx) return "done";
  if (stepIdx === activeIdx) return "active";
  return "pending";
}

function isInferenceConnectionError(job: Job): boolean {
  const combined = `${job.error_message ?? ""} ${job.current_stage ?? ""}`;
  return (
    combined.includes("推論サーバー接続中断") ||
    combined.includes("推論サーバ接続中断") ||
    combined.includes("推論サーバー切断") ||
    combined.includes("推論サーバ切断") ||
    combined.includes("TranslationConnectionError") ||
    /Failed to communicate with llama-server/i.test(combined) ||
    /llama-server returned empty choices/i.test(combined) ||
    /Connection refused|ConnectError|TimeoutException|NetworkError/i.test(combined)
  );
}

function sanitizeJobErrorMessage(rawMessage: string | null | undefined, fallbackStage: string): string {
  const base = (rawMessage && rawMessage.trim().length > 0 ? rawMessage : fallbackStage).trim();
  if (!base) {
    return "エラーが発生しました（詳細は診断情報を確認してください）。";
  }
  let safe = base.replace(/\r?\n+/g, " ");
  // Mask header / bearer / query tokens or API keys if present
  safe = safe.replace(
    /(X-PaperBridge-Token|Authorization|Bearer|api[_-]?key|token)\s*[:=]\s*[^\s,;'"&]+/gi,
    "$1=[REDACTED]"
  );
  safe = safe.replace(/([?&](?:token|key|auth|secret)=)[^&\s'"]+/gi, "$1[REDACTED]");
  // Mask PFML3 prompt tags or JSON prompt/text fields if accidentally echoed in an exception
  safe = safe.replace(/<\|plamo:[\s\S]*$/i, "[プロンプト・本文情報を省略]");
  safe = safe.replace(
    /("(?:prompt|source_text|translated_text|manual_translation_text|raw_output|text)"\s*:\s*")[^"]*(")/gi,
    "$1[REDACTED]$2"
  );
  // Mask local Windows user directory paths
  safe = safe.replace(/[A-Za-z]:\\Users\\[^\\\s"']+/g, "C:\\Users\\[USER]");
  safe = safe.replace(/\/Users\/[^/\s"']+/g, "/Users/[USER]");
  if (safe.length > 240) {
    safe = `${safe.slice(0, 240)}...（省略）`;
  }
  return safe;
}

export const Screen3ProgressReview: React.FC<Screen3Props> = ({
  document,
  originalPdfBytes,
  translatedPdfBytes,
  job,
  blocks,
  issues,
  artifacts,
  isCanceling,
  isResuming,
  isRetypesetting,
  isRetranslatingBlock,
  retypesetMessage,
  retypesetError,
  onCancelJob,
  onResumeJob,
  onSaveManualEdit,
  onRetranslateBlock,
  onRetypesetJob,
  onDownloadArtifact,
  onDirtyStateChange,
}) => {
  const [displayPage, setDisplayPage] = useState<number>(1);
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(
    blocks[0]?.id || null
  );
  const [draftManualText, setDraftManualText] = useState<string>("");
  const [isDirty, setIsDirty] = useState<boolean>(false);
  const [pendingSwitchTarget, setPendingSwitchTarget] = useState<{
    page: number;
    blockId: string | null;
  } | null>(null);
  const [saveFeedback, setSaveFeedback] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [isSavingBlock, setIsSavingBlock] = useState<boolean>(false);
  const [strictRetypesetGate, setStrictRetypesetGate] = useState<boolean>(false);
  const [downloadFeedback, setDownloadFeedback] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloadingKey, setDownloadingKey] = useState<string | null>(null);
  const isJobRunning = [
    "created",
    "preflight",
    "parsing",
    "translating",
    "validating",
    "typesetting",
  ].includes(job.state);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());

  useEffect(() => {
    if (!isJobRunning) return;
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [isJobRunning]);

  const totalPages = Math.max(1, document.page_count);
  const safeDisplayPage = Math.max(1, Math.min(totalPages, displayPage));
  const currentPageIndex0 = displayPageToIndex0(safeDisplayPage, totalPages);

  const pageBlocks = useMemo(
    () => blocks.filter((b) => b.page_index === currentPageIndex0),
    [blocks, currentPageIndex0]
  );

  const selectedBlock = useMemo(
    () => blocks.find((b) => b.id === selectedBlockId) || pageBlocks[0] || null,
    [blocks, selectedBlockId, pageBlocks]
  );

  // Sync draft editor text when selectedBlock changes (and not dirty)
  useEffect(() => {
    if (!selectedBlock) {
      setDraftManualText("");
      setIsDirty(false);
      onDirtyStateChange?.(false);
      return;
    }
    const initialDraft =
      selectedBlock.manual_translation_text ??
      selectedBlock.translation_text ??
      "";
    setDraftManualText(initialDraft);
    setIsDirty(false);
    onDirtyStateChange?.(false);
    setSaveFeedback(null);
    setSaveError(null);
  }, [selectedBlock?.id, selectedBlock?.edit_revision]);

  // Warn on browser unload when manual edit has unsaved changes
  useEffect(() => {
    if (!isDirty) return;
    const handler = (ev: BeforeUnloadEvent) => {
      ev.preventDefault();
      ev.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isDirty]);

  const requestSelectPageAndBlock = (targetDisplayPage: number, targetBlockId: string | null) => {
    if (isDirty && targetBlockId !== selectedBlock?.id) {
      setPendingSwitchTarget({ page: targetDisplayPage, blockId: targetBlockId });
      return;
    }
    setDisplayPage(targetDisplayPage);
    if (targetBlockId) {
      setSelectedBlockId(targetBlockId);
    }
  };

  const confirmDiscardAndSwitch = () => {
    if (!pendingSwitchTarget) return;
    setIsDirty(false);
    onDirtyStateChange?.(false);
    setDisplayPage(pendingSwitchTarget.page);
    if (pendingSwitchTarget.blockId) {
      setSelectedBlockId(pendingSwitchTarget.blockId);
    }
    setPendingSwitchTarget(null);
  };

  const liveValidation = useMemo(() => {
    if (!selectedBlock) return { errors: [], warnings: [] };
    return validateManualEditClientSide(selectedBlock, draftManualText);
  }, [selectedBlock, draftManualText]);

  const handleSaveManual = async () => {
    if (!selectedBlock) return;
    setIsSavingBlock(true);
    setSaveFeedback(null);
    setSaveError(null);
    try {
      const updated = await onSaveManualEdit(
        selectedBlock.id,
        draftManualText,
        selectedBlock.edit_revision
      );
      setIsDirty(false);
      onDirtyStateChange?.(false);
      setSaveFeedback(
        `ブロック #${updated.reading_order + 1} の手動修正を保存しました (edit_revision=${updated.edit_revision})。AI訳 (translation_text) は上書きせずそのまま保持されています。`
      );
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSavingBlock(false);
    }
  };

  const handleRevertToAiTranslation = async () => {
    if (!selectedBlock) return;
    setIsSavingBlock(true);
    setSaveFeedback(null);
    setSaveError(null);
    try {
      const updated = await onSaveManualEdit(
        selectedBlock.id,
        null,
        selectedBlock.edit_revision
      );
      setDraftManualText(updated.translation_text || "");
      setIsDirty(false);
      onDirtyStateChange?.(false);
      setSaveFeedback("手動修正を解除し、モデル出力訳文 (translation_text) に戻しました。");
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSavingBlock(false);
    }
  };

  const isCancelRequestedWaiting = job.cancel_requested && job.state !== "canceled";
  const isCancelCompleted = job.state === "canceled";
  const canResume =
    job.state === "interrupted" ||
    job.state === "canceled" ||
    job.state === "awaiting_review" ||
    job.state === "failed";

  const displayPercent = computeDisplayPercent(job);
  const startIso = job.started_at || job.created_at;
  const startMs = Date.parse(startIso);
  const endMs =
    job.completed_at && !isJobRunning ? Date.parse(job.completed_at) : nowMs;
  const elapsedSeconds =
    Number.isFinite(startMs) && Number.isFinite(endMs)
      ? Math.max(0, (endMs - startMs) / 1000)
      : null;
  const etaText =
    job.state === "completed" || job.state === "completed_with_warnings"
      ? "完了"
      : isJobRunning
      ? job.estimated_remaining_seconds !== null &&
        job.estimated_remaining_seconds !== undefined &&
        Number.isFinite(job.estimated_remaining_seconds)
        ? `残り約 ${formatDurationJa(job.estimated_remaining_seconds)}`
        : "計算中..."
      : job.state === "canceled"
      ? "停止済み"
      : "中断中";

  const pageMeta = document.pages.find((p) => p.page_index === currentPageIndex0) || {
    width_pt: 595,
    height_pt: 842,
    rotation_deg: 0 as const,
  };

  return (
    <div className="screen-container" data-testid="screen3-progress-review">
      {/* Unsaved Changes Confirmation Banner */}
      {pendingSwitchTarget && (
        <div
          role="alert"
          data-testid="unsaved-leave-warning"
          className="alert alert-warning"
          style={{
            marginBottom: "16px",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <div>
            <strong>未保存の手動修正があります:</strong>{" "}
            保存せずに別のブロックまたはページへ移動すると編集中の内容が破棄されます。移動しますか？
          </div>
          <div style={{ display: "flex", gap: "8px" }}>
            <button
              type="button"
              className="btn btn-secondary"
              data-testid="cancel-switch-btn"
              onClick={() => setPendingSwitchTarget(null)}
            >
              編集を続ける
            </button>
            <button
              type="button"
              className="btn btn-danger-sm"
              data-testid="confirm-discard-switch-btn"
              onClick={confirmDiscardAndSwitch}
            >
              未保存の変更を破棄して移動
            </button>
          </div>
        </div>
      )}

      {/* Top Panel: Hero Progress Card, 4-Step Stepper & Job Control */}
      <section className="hero-progress-card">
        <div className="hero-progress-header">
          <div>
            <h2 className="card-title" style={{ marginBottom: "6px" }}>
              翻訳の進行状況 <span style={{ fontSize: "13px", fontWeight: 500, color: "#64748b" }}>({job.id})</span>
            </h2>
            <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
              <span
                data-testid="job-state-badge"
                className={`badge ${
                  job.state === "completed"
                    ? "badge-success"
                    : job.state === "completed_with_warnings"
                    ? "badge-warning"
                    : job.state === "failed"
                    ? "badge-error"
                    : "badge-info"
                }`}
              >
                状態: {job.state} ({job.current_stage})
              </span>

              <span
                data-testid="job-engine-mode-badge"
                className={`badge ${job.is_mock_run ? "badge-warning" : "badge-success"}`}
              >
                {job.is_mock_run
                  ? "[MOCK 疑似翻訳]"
                  : `[実機 ${
                      job.execution_provenance?.model_display_name ||
                      (job.config.model_name === "plamo-3-translate-0.8b-Q4_K_M"
                        ? "PLaMo 3 Translate 0.8B"
                        : job.config.model_name === "plamo-3-translate-2b-Q4_K_M"
                        ? "PLaMo 3 Translate 2B"
                        : job.config.model_name)
                    }]`}
              </span>

              {job.outputs_stale && (
                <span data-testid="job-stale-badge" className="badge badge-warning">
                  ⚠ 成果物未同期（要再翻訳・再組版）
                </span>
              )}

              {isCancelRequestedWaiting && (
                <span
                  data-testid="cancel-requested-badge"
                  className="badge badge-warning"
                >
                  ⏳ 停止要求中（安全区切り待機中）
                </span>
              )}
              {isCancelCompleted && (
                <span
                  data-testid="cancel-completed-badge"
                  className="badge badge-error"
                >
                  ⏹ 停止完了（キャンセル済み）
                </span>
              )}
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" }}>
            <div className="hero-percent" data-testid="job-progress-percent">
              {displayPercent}%
            </div>
            {isJobRunning && (
              <button
                type="button"
                className="btn btn-danger-sm"
                data-testid="cancel-job-btn"
                disabled={isCanceling || job.cancel_requested}
                onClick={() => void onCancelJob()}
              >
                {job.cancel_requested ? "停止要求送信済み..." : "⏹ 安全停止"}
              </button>
            )}

            {canResume && (
              <button
                type="button"
                className="btn btn-primary"
                data-testid="resume-job-btn"
                disabled={isResuming}
                onClick={() => void onResumeJob()}
              >
                {isResuming ? "再開処理中..." : "▶ 中断箇所からジョブを再開 (Resume)"}
              </button>
            )}
          </div>
        </div>

        {/* Visual Progress Bar & Live ETA */}
        <div
          className="progress-bar-track"
          role="progressbar"
          data-testid="job-progress-bar"
          aria-valuenow={displayPercent}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className="progress-bar-fill"
            style={{ width: `${displayPercent}%` }}
          />
        </div>

        <div className="hero-eta-row">
          <div data-testid="job-stage-detail" style={{ fontWeight: 600, color: "#0f172a" }}>
            {job.stage_detail || job.current_stage}
          </div>
          <div style={{ display: "flex", gap: "16px", flexWrap: "wrap" }}>
            <span data-testid="job-elapsed-time">
              経過時間: <strong>{formatDurationJa(elapsedSeconds)}</strong>
            </span>
            <span data-testid="job-eta">
              予測残り時間: <strong>{etaText}</strong>
            </span>
          </div>
        </div>

        {/* 4-Step High-Level Milestone Stepper */}
        <div className="milestone-stepper">
          {[
            "1. 準備・解析",
            "2. 翻訳",
            "3. 組版・検証",
            "4. 完了",
          ].map((label, stepIdx) => {
            const st = getMilestoneStepStatus(stepIdx, job.state);
            const icon = st === "done" ? "✓" : st === "active" ? "●" : "○";
            return (
              <div key={label} className={`milestone-step ${st}`}>
                <span>{icon}</span>
                <span>{label}</span>
              </div>
            );
          })}
        </div>

        <div className="stat-strip">
          <span className="stat-chip" data-testid="job-progress-counts">
            ページ: {job.processed_pages}/{job.total_pages} | ブロック:{" "}
            {job.processed_blocks}/{job.total_blocks} | 再試行: {job.retry_count}回 | 警告:{" "}
            {job.warning_count}件 | 再組版rev: {job.retypeset_revision}
          </span>
        </div>

        {(() => {
          if (isJobRunning) return null;
          const untranslatedCount = blocks.filter(
            (b) =>
              b.status === "pending" ||
              (b.needs_retranslation &&
                b.status !== "protected_skip" &&
                b.manual_translation_text == null)
          ).length;
          if (untranslatedCount === 0) return null;
          return (
            <div
              data-testid="unprotected-pending-notice"
              className="badge badge-warning"
              style={{ marginTop: "10px" }}
            >
              未翻訳・再翻訳待ちのブロックがあります（
              {untranslatedCount}件）。ブロックを選択して「再翻訳」を実行し、再組版してください。
            </div>
          );
        })()}

        {(job.error_message || job.state === "interrupted" || job.state === "failed") && (() => {
          const isConnectionError = isInferenceConnectionError(job);
          const safeErrorSummary = sanitizeJobErrorMessage(job.error_message, job.current_stage);
          return (
            <div
              role="alert"
              data-testid="job-error-banner"
              className={`alert ${job.state === "interrupted" ? "alert-warning" : "alert-error"}`}
              style={{ marginTop: "12px", fontSize: "13px" }}
            >
              <div>
                <strong>
                  {job.state === "interrupted" && isConnectionError
                    ? "推論サーバ接続中断（再開可能）:"
                    : job.state === "interrupted"
                    ? "ジョブ一時中断（再開可能）:"
                    : "ジョブエラー:"}
                </strong>{" "}
                <span data-testid="job-error-summary">{safeErrorSummary}</span>
              </div>
              {isConnectionError ? (
                <div data-testid="job-error-recovery-guide" style={{ marginTop: "4px", fontSize: "12px" }}>
                  対処方法：別ターミナルで選択中の翻訳モデルを指定して <code>llama-server</code>{" "}
                  を起動し、画面4の「推論サーバ疎通確認」（翻訳リクエストは送信しません）で接続を確認してください。
                  {canResume && (
                    <>
                      接続確認後、「▶ 中断箇所からジョブを再開 (Resume)」で続きから再開できます。
                    </>
                  )}
                  モデル未導入で画面動作のみ確認する場合は、画面1で「モック推論」（疑似翻訳であり実翻訳ではありません）を選択して新規ジョブを開始してください。
                </div>
              ) : (
                <div data-testid="job-error-recovery-guide" style={{ marginTop: "4px", fontSize: "12px" }}>
                  対処方法：この停止は推論サーバ未接続によるものではありません（サーバの再起動だけでは解消しない場合があります）。上記のエラー要約や画面1のPreflight検査結果・設定内容を確認してください。
                  {job.state === "interrupted" && canResume && (
                    <>
                      一時中断からの再開を行う場合は「▶ 中断箇所からジョブを再開 (Resume)」を使用できます。
                    </>
                  )}
                </div>
              )}
            </div>
          );
        })()}

        {/* Collapsible Technical Pipeline Details Drawer */}
        <details className="details-drawer">
          <summary>詳細な処理ステップ（13ステージ・実行詳細）を表示</summary>
          {job.execution_provenance && job.execution_provenance.total_blocks !== undefined && (
            <div
              data-testid="job-execution-provenance"
              style={{ marginBottom: "10px", fontSize: "12px", color: "#334155" }}
            >
              <div>
                実行経路: {job.execution_provenance.adapter_name ?? "不明"}（
                {job.execution_provenance.engine_process ?? "-"}） / モデル:{" "}
                {job.execution_provenance.model_display_name
                  ? `${job.execution_provenance.model_display_name} (${
                      job.execution_provenance.model_file ??
                      job.execution_provenance.model_name ??
                      "不明"
                    })`
                  : job.execution_provenance.model_file ??
                    job.execution_provenance.model_name ??
                    "不明"}
                {job.execution_provenance.model_sha256
                  ? ` [SHA:${job.execution_provenance.model_sha256.slice(0, 12)}]`
                  : ""}{" "}
                / 計算: {job.execution_provenance.compute_backend ?? "不明"}
                {job.execution_provenance.is_mock ? "（疑似翻訳）" : ""}
              </div>
              <div>
                新規LLM推論 {job.execution_provenance.newly_inferenced_blocks ?? 0} / キャッシュ利用{" "}
                {job.execution_provenance.cache_hit_blocks ?? 0} / 再利用{" "}
                {job.execution_provenance.reused_existing_blocks ?? 0} / 手動修正{" "}
                {job.execution_provenance.manual_translation_blocks ?? 0} / 非翻訳保護{" "}
                {job.execution_provenance.protected_skip_blocks ?? 0} / 原文維持{" "}
                {job.execution_provenance.fallback_original_blocks ?? 0}（LLM呼出し{" "}
                {job.execution_provenance.llm_api_calls_made ?? 0}回）
              </div>
            </div>
          )}

          <div
            data-testid="pipeline-13-stages"
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(175px, 1fr))",
              gap: "6px",
            }}
          >
            {PIPELINE_STAGES_13.map((st) => {
              const status = getStageStatus(st.phaseKey, job.state);
              return (
                <div
                  key={st.stageNumber}
                  data-testid={`stage-item-${st.stageNumber}`}
                  data-stage-status={status}
                  style={{
                    padding: "6px 8px",
                    borderRadius: "6px",
                    fontSize: "11px",
                    border:
                      status === "active"
                        ? "2px solid #2563eb"
                        : status === "done"
                        ? "1px solid #16a34a"
                        : "1px solid #cbd5e1",
                    backgroundColor:
                      status === "active"
                        ? "#eff6ff"
                        : status === "done"
                        ? "#f0fdf4"
                        : "#ffffff",
                  }}
                >
                  <strong>Stage {st.stageNumber}:</strong> {st.title}
                </div>
              );
            })}
          </div>
        </details>
      </section>

      {/* Artifacts & Zero-LLM Retypeset Bar */}
      <section className="card" style={{ marginBottom: "16px" }}>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            justifyContent: "space-between",
            alignItems: "center",
            gap: "12px",
          }}
        >
          <div>
            <h3 className="card-title" style={{ fontSize: "16px", marginBottom: "2px" }}>
              成果物ダウンロード ＆ PDF再組版
            </h3>
            <div style={{ fontSize: "12px", color: "#475569" }}>
              手動修正後、LLM呼び出し0回でPDFのみを高速に更新します。「保存先を選んで保存」から任意のフォルダ・ファイル名を指定して保存できます。
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
            <label style={{ fontSize: "12px", display: "flex", alignItems: "center", gap: "4px" }}>
              <input
                type="checkbox"
                data-testid="strict-retypeset-checkbox"
                checked={strictRetypesetGate}
                onChange={(ev) => setStrictRetypesetGate(ev.target.checked)}
              />
              警告・エラーがある場合は再組版を拒否 (422検査)
            </label>
            <button
              type="button"
              className="btn btn-primary"
              data-testid="retypeset-btn"
              disabled={isRetypesetting || isJobRunning}
              onClick={() => void onRetypesetJob(!strictRetypesetGate)}
            >
              {isRetypesetting
                ? "再組版中..."
                : "🔄 LLM呼び出しなしでPDF再組版 (Retypeset)"}
            </button>
          </div>
        </div>

        {job.outputs_stale && (
          <div
            role="alert"
            data-testid="outputs-stale-banner"
            className="alert alert-warning"
            style={{ marginTop: "10px", fontSize: "13px" }}
          >
            <strong>⚠ 成果物は古い解析状態に基づいています:</strong>{" "}
            {job.outputs_stale_reason ||
              "ブロック分割または保護設定が変更されたため、最新のブロック構成へ反映するには再翻訳または再組版を実行してください。"}
          </div>
        )}
        {retypesetMessage && (
          <div
            data-testid="retypeset-success-banner"
            className="alert alert-info"
            style={{ marginTop: "10px", fontSize: "13px" }}
          >
            {retypesetMessage}
          </div>
        )}
        {retypesetError && (
          <div
            role="alert"
            data-testid="retypeset-error-banner"
            className="alert alert-error"
            style={{ marginTop: "10px", fontSize: "13px" }}
          >
            <strong>再組版エラー（直前の成功済み成果物は保持されています）:</strong>{" "}
            {retypesetError}
          </div>
        )}
        {downloadFeedback && (
          <div
            data-testid="download-feedback-banner"
            className="alert alert-info"
            style={{ marginTop: "10px", fontSize: "12px" }}
          >
            {downloadFeedback}
          </div>
        )}
        {downloadError && (
          <div
            role="alert"
            data-testid="download-error-banner"
            className="alert alert-error"
            style={{ marginTop: "10px", fontSize: "12px" }}
          >
            <strong>成果物保存エラー:</strong> {downloadError}
          </div>
        )}

        {/* Artifact Download Buttons */}
        <div
          data-testid="artifact-list"
          className="artifact-card-grid"
        >
          {artifacts.length === 0 ? (
            <span style={{ fontSize: "13px", color: "#64748b" }}>
              完了済みの出力成果物はまだありません。
            </span>
          ) : (
            artifacts.map((art) => {
              const labelMap: Record<ArtifactType, string> = {
                mono_ja_pdf: "📄 日本語単一PDF (mono_ja_pdf)",
                dual_bilingual_pdf: "📖 見開き対訳PDF (dual_bilingual_pdf)",
                roundtrip_pdf: "🔁 非翻訳ラウンドトリップPDF",
                validation_report_json: "📊 検証レポートJSON",
              };
              const suggestedName = buildSuggestedArtifactFilename(
                document.original_filename,
                document.id,
                art.artifact_type
              );
              const chooseKey = `${art.artifact_type}:choose`;
              const quickKey = `${art.artifact_type}:quick`;
              const isBusyAny = downloadingKey !== null;

              const handleArtifactSave = async (chooseLocation: boolean) => {
                setDownloadingKey(chooseLocation ? chooseKey : quickKey);
                setDownloadFeedback(null);
                setDownloadError(null);
                try {
                  const res = await onDownloadArtifact(
                    art.artifact_type,
                    suggestedName,
                    { chooseLocation }
                  );
                  if (!res) {
                    setDownloadFeedback(
                      chooseLocation
                        ? `${suggestedName} の保存処理を実行しました。`
                        : `${suggestedName} をブラウザの既定ダウンロードフォルダへ保存しました（一時Blob URLを解放済み）。`
                    );
                    return;
                  }
                  if (res.canceled) {
                    setDownloadFeedback(
                      `${res.filename || suggestedName} の保存をキャンセルしました。`
                    );
                    return;
                  }
                  if (res.method === "native_dialog") {
                    setDownloadFeedback(
                      `${res.savedPath ?? res.filename} に保存しました（${res.byteSize.toLocaleString()} bytes）。`
                    );
                  } else if (res.method === "file_picker") {
                    setDownloadFeedback(
                      `選択した保存先に「${res.filename}」を保存しました（${res.byteSize.toLocaleString()} bytes）。`
                    );
                  } else {
                    setDownloadFeedback(
                      `${res.filename} をブラウザの既定ダウンロードフォルダへ保存しました（一時Blob URLを解放済み）。`
                    );
                  }
                } catch (err) {
                  setDownloadError(err instanceof Error ? err.message : String(err));
                } finally {
                  setDownloadingKey(null);
                }
              };

              return (
                <div
                  key={art.id}
                  data-testid={`artifact-card-${art.artifact_type}`}
                  style={{
                    border: "1px solid #cbd5e1",
                    borderRadius: "6px",
                    padding: "8px 12px",
                    backgroundColor: "#f8fafc",
                    fontSize: "12px",
                    display: "flex",
                    flexDirection: "column",
                    gap: "4px",
                  }}
                >
                  <div style={{ fontWeight: 600 }}>
                    {labelMap[art.artifact_type] || art.artifact_type}{" "}
                    {art.is_mock_translation && (
                      <span className="badge badge-warning">[MOCK]</span>
                    )}
                  </div>
                  <div style={{ fontFamily: "monospace", fontSize: "11px", color: "#475569" }}>
                    SHA-256: {art.sha256_hash.slice(0, 16)}... ({art.file_size_bytes.toLocaleString()}{" "}
                    bytes)
                  </div>
                  <div className="artifact-card-actions">
                    <button
                      type="button"
                      className="btn btn-primary"
                      data-testid={`download-artifact-${art.artifact_type}`}
                      disabled={isBusyAny}
                      onClick={() => void handleArtifactSave(true)}
                    >
                      {downloadingKey === chooseKey
                        ? "保存先を選択中..."
                        : "💾 保存先を選んで保存"}
                    </button>
                    <button
                      type="button"
                      className="btn btn-secondary btn-compact"
                      data-testid={`quick-download-artifact-${art.artifact_type}`}
                      disabled={isBusyAny}
                      onClick={() => void handleArtifactSave(false)}
                    >
                      {downloadingKey === quickKey
                        ? "ダウンロード中..."
                        : "⬇ 既定フォルダへダウンロード"}
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </section>

      {/* Validation Issues List (Jump to Page & Block) */}
      {issues.length > 0 && (
        <section
          className="card"
          data-testid="validation-issues-panel"
          style={{ marginBottom: "16px" }}
        >
          <h3 className="card-title" style={{ fontSize: "15px", marginBottom: "8px" }}>
            要確認箇所・検証イシュー一覧 ({issues.length}件 — クリックで該当ページ・ブロックへジャンプ)
          </h3>
          <div style={{ maxHeight: "160px", overflowY: "auto" }}>
            <table className="compact-table">
              <thead>
                <tr>
                  <th>重要度</th>
                  <th>ページ (1始まり)</th>
                  <th>コード</th>
                  <th>内容</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {issues.map((iss) => (
                  <tr key={iss.id} data-testid={`issue-row-${iss.id}`}>
                    <td>
                      <span
                        className={`badge ${
                          iss.severity === "error"
                            ? "badge-error"
                            : iss.severity === "warning"
                            ? "badge-warning"
                            : "badge-info"
                        }`}
                      >
                        {iss.severity.toUpperCase()}
                      </span>
                    </td>
                    <td>Page {iss.page_index + 1}</td>
                    <td>
                      <code>{iss.code}</code>
                    </td>
                    <td>{iss.message}</td>
                    <td>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        data-testid={`jump-to-issue-${iss.id}`}
                        onClick={() =>
                          requestSelectPageAndBlock(
                            iss.page_index + 1,
                            iss.block_id
                          )
                        }
                      >
                        該当箇所へ移動
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* Side-by-Side PDF & Block Review + Separated Manual Editor */}
      <div className="grid-2col">
        {/* Left: Original PDF Preview + Page Block Selector */}
        <section className="card">
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: "10px",
            }}
          >
            <h3 className="card-title" style={{ fontSize: "16px", margin: 0 }}>
              左：原本ページ ＆ ブロック一覧 (Page {safeDisplayPage}/{totalPages})
            </h3>
            <div style={{ display: "flex", gap: "6px" }}>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={safeDisplayPage <= 1}
                onClick={() => requestSelectPageAndBlock(safeDisplayPage - 1, null)}
              >
                ◀ 前頁
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={safeDisplayPage >= totalPages}
                onClick={() => requestSelectPageAndBlock(safeDisplayPage + 1, null)}
              >
                次頁 ▶
              </button>
            </div>
          </div>

          <div
            style={{
              display: "flex",
              gap: "12px",
              overflowX: "auto",
              padding: "8px",
              backgroundColor: "#f1f5f9",
              borderRadius: "6px",
              marginBottom: "12px",
            }}
          >
            <div>
              <div style={{ fontSize: "11px", fontWeight: 600, marginBottom: "4px" }}>
                英語原本 PDF (Page {safeDisplayPage})
              </div>
              <PdfPageCanvas
                pdfBytes={originalPdfBytes}
                displayPageNumber={safeDisplayPage}
                pageWidthPt={pageMeta.width_pt}
                pageHeightPt={pageMeta.height_pt}
                pageRotationDeg={pageMeta.rotation_deg}
                scale={0.65}
                userViewRotationDeg={0}
              >
                {pageBlocks.map((b) => {
                  const vp = backendToViewportBbox(
                    b.bbox,
                    pageMeta.width_pt,
                    pageMeta.height_pt,
                    0.65,
                    pageMeta.rotation_deg,
                    0
                  );
                  const isSel = selectedBlock?.id === b.id;
                  return (
                    <rect
                      key={b.id}
                      x={vp[0]}
                      y={vp[1]}
                      width={Math.max(2, vp[2] - vp[0])}
                      height={Math.max(2, vp[3] - vp[1])}
                      fill={
                        isSel
                          ? "rgba(37, 99, 235, 0.24)"
                          : "rgba(15, 118, 110, 0.06)"
                      }
                      stroke={isSel ? "#2563eb" : "#0f766e"}
                      strokeWidth={isSel ? 2.5 : 1}
                      onClick={() => requestSelectPageAndBlock(safeDisplayPage, b.id)}
                    />
                  );
                })}
              </PdfPageCanvas>
            </div>

            {translatedPdfBytes && (
              <div>
                <div style={{ fontSize: "11px", fontWeight: 600, marginBottom: "4px" }}>
                  日本語組版 PDF (Page {safeDisplayPage})
                </div>
                <PdfPageCanvas
                  pdfBytes={translatedPdfBytes}
                  displayPageNumber={safeDisplayPage}
                  pageWidthPt={pageMeta.width_pt}
                  pageHeightPt={pageMeta.height_pt}
                  pageRotationDeg={pageMeta.rotation_deg}
                  scale={0.65}
                  userViewRotationDeg={0}
                />
              </div>
            )}
          </div>

          {/* Block list on current page */}
          <div
            data-testid="review-block-list"
            style={{ maxHeight: "280px", overflowY: "auto" }}
          >
            {pageBlocks.map((b) => {
              const isSel = selectedBlock?.id === b.id;
              return (
                <div
                  key={b.id}
                  data-testid={`review-block-item-${b.id}`}
                  onClick={() => requestSelectPageAndBlock(safeDisplayPage, b.id)}
                  style={{
                    padding: "8px 10px",
                    marginBottom: "6px",
                    borderRadius: "6px",
                    border: isSel ? "2px solid #2563eb" : "1px solid #cbd5e1",
                    backgroundColor: isSel ? "#eff6ff" : "#ffffff",
                    cursor: "pointer",
                    fontSize: "12px",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", gap: "6px" }}>
                    <strong>
                      #{b.reading_order + 1} [{b.kind}] ({b.id})
                    </strong>
                    <div style={{ display: "flex", gap: "4px", flexWrap: "wrap" }}>
                      {b.split_role === "formula" && (
                        <span className="badge badge-info">✂ 数式保護</span>
                      )}
                      {b.needs_retranslation && (
                        <span
                          className="badge badge-warning"
                          data-testid={`review-needs-retranslation-${b.id}`}
                        >
                          要再翻訳
                        </span>
                      )}
                      <span
                        className={`badge ${
                          b.status === "manually_edited"
                            ? "badge-info"
                            : b.status === "translated"
                            ? "badge-success"
                            : b.status === "fallback_original"
                            ? "badge-error"
                            : "badge-neutral"
                        }`}
                      >
                        {b.status === "manually_edited"
                          ? "✎ 手動修正済み"
                          : b.status === "translated"
                          ? "AI翻訳済み"
                          : b.status === "fallback_original"
                          ? "⚠ 原文保持フォールバック"
                          : "🔒 保護スキップ"}
                      </span>
                    </div>
                  </div>
                  <div style={{ color: "#334155", marginTop: "4px" }}>
                    {b.source_text.slice(0, 120)}
                    {b.source_text.length > 120 ? "..." : ""}
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        {/* Right: Selected Block Inspector & Separated Manual Translation Editor */}
        <section className="card" data-testid="manual-editor-panel">
          {!selectedBlock ? (
            <div>ブロックを選択してください。</div>
          ) : (
            <>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: "10px",
                }}
              >
                <h3 className="card-title" style={{ fontSize: "16px", margin: 0 }}>
                  右：対訳検査・手動修正 ({selectedBlock.id})
                </h3>
                <span className="badge badge-neutral" data-testid="selected-block-rev">
                  edit_revision: {selectedBlock.edit_revision}
                </span>
              </div>

              {/* 1. Original Source & Masked Source */}
              <div className="form-group">
                <label className="form-label">英語原文 (source_text)</label>
                <div
                  data-testid="inspector-source-text"
                  style={{
                    padding: "8px 10px",
                    backgroundColor: "#f8fafc",
                    border: "1px solid #cbd5e1",
                    borderRadius: "6px",
                    fontSize: "13px",
                  }}
                >
                  {selectedBlock.source_text}
                </div>
              </div>

              {selectedBlock.protected_items.length > 0 && (
                <div className="form-group">
                  <label className="form-label">
                    インライン保護プレースホルダ（訳文内で必ず維持）
                  </label>
                  <div
                    data-testid="inspector-protected-items"
                    style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}
                  >
                    {selectedBlock.protected_items.map((pi) => (
                      <span
                        key={pi.placeholder}
                        className="badge badge-info"
                        style={{ fontFamily: "monospace" }}
                      >
                        {pi.placeholder} = {pi.original_text} ({pi.kind})
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* 2. Read-only Model Translation (translation_text) — NEVER overwritten by manual edits */}
              <div className="form-group">
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                  }}
                >
                  <label className="form-label">
                    AIモデル訳文 (<code>translation_text</code> — 読み取り専用・分離保持)
                  </label>
                  <button
                    type="button"
                    className="btn btn-secondary"
                    data-testid="retranslate-block-btn"
                    disabled={
                      isRetranslatingBlock ||
                      isJobRunning ||
                      selectedBlock.split_role === "formula"
                    }
                    title={
                      selectedBlock.split_role === "formula"
                        ? "分割指定で原文保護された数式のため部分再翻訳できません（解除は画面2で分割を削除）"
                        : undefined
                    }
                    onClick={() => void onRetranslateBlock(selectedBlock.id)}
                  >
                    {isRetranslatingBlock
                      ? "部分再翻訳中..."
                      : selectedBlock.split_role === "formula"
                      ? "分割数式（原文保護）"
                      : "このブロックのみ部分再翻訳"}
                  </button>
                </div>
                <div
                  data-testid="inspector-model-translation"
                  style={{
                    padding: "8px 10px",
                    backgroundColor: "#f1f5f9",
                    border: "1px solid #cbd5e1",
                    borderRadius: "6px",
                    fontSize: "13px",
                    minHeight: "42px",
                  }}
                >
                  {selectedBlock.translation_text ?? "（AI訳なし / 保護またはフォールバック）"}
                </div>
              </div>

              {/* 3. Editable Manual Translation (manual_translation_text) */}
              <div className="form-group">
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                  }}
                >
                  <label className="form-label" htmlFor="manual-translation-textarea">
                    手動修正訳文 (<code>manual_translation_text</code>)
                    {isDirty && (
                      <span
                        data-testid="dirty-indicator"
                        className="badge badge-warning"
                        style={{ marginLeft: "8px" }}
                      >
                        未保存の変更あり
                      </span>
                    )}
                  </label>
                  {selectedBlock.manual_translation_text !== null && (
                    <button
                      type="button"
                      className="btn btn-secondary"
                      data-testid="revert-manual-edit-btn"
                      disabled={isSavingBlock}
                      onClick={() => void handleRevertToAiTranslation()}
                    >
                      手動修正を解除（AI訳に戻す）
                    </button>
                  )}
                </div>
                <textarea
                  id="manual-translation-textarea"
                  data-testid="manual-translation-textarea"
                  className="form-input"
                  rows={4}
                  value={draftManualText}
                  onChange={(ev) => {
                    setDraftManualText(ev.target.value);
                    setIsDirty(true);
                    onDirtyStateChange?.(true);
                  }}
                />
              </div>

              {/* Pre-save Live Validation: Separated Errors vs Warnings */}
              {liveValidation.errors.length > 0 && (
                <div
                  role="alert"
                  data-testid="manual-validation-errors"
                  className="alert alert-error"
                  style={{ marginBottom: "10px", fontSize: "12px" }}
                >
                  <strong>保存前検査エラー ({liveValidation.errors.length}件):</strong>
                  <ul style={{ margin: "4px 0 0 18px", padding: 0 }}>
                    {liveValidation.errors.map((e, idx) => (
                      <li key={idx}>
                        <code>{e.code}</code>: {e.message}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {liveValidation.warnings.length > 0 && (
                <div
                  data-testid="manual-validation-warnings"
                  className="alert alert-warning"
                  style={{ marginBottom: "10px", fontSize: "12px" }}
                >
                  <strong>保存前検査警告 ({liveValidation.warnings.length}件):</strong>
                  <ul style={{ margin: "4px 0 0 18px", padding: 0 }}>
                    {liveValidation.warnings.map((w, idx) => (
                      <li key={idx}>
                        <code>{w.code}</code>: {w.message}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {saveFeedback && (
                <div
                  data-testid="manual-save-feedback"
                  className="alert alert-info"
                  style={{ marginBottom: "10px", fontSize: "12px" }}
                >
                  {saveFeedback}
                </div>
              )}
              {saveError && (
                <div
                  role="alert"
                  data-testid="manual-save-error"
                  className="alert alert-error"
                  style={{ marginBottom: "10px", fontSize: "12px" }}
                >
                  {saveError}
                </div>
              )}

              <div style={{ display: "flex", gap: "10px" }}>
                <button
                  type="button"
                  className="btn btn-primary"
                  data-testid="save-manual-edit-btn"
                  disabled={isSavingBlock || draftManualText.trim().length === 0}
                  onClick={() => void handleSaveManual()}
                >
                  {isSavingBlock ? "保存中..." : "💾 手動修正を保存"}
                </button>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
};
