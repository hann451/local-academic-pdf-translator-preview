/**
 * PaperBridge Local Main Application (Phase 3: Screens 1–4 Integration).
 */

import React, { useCallback, useEffect, useState } from "react";
import {
  cancelTranslationJob,
  createGlossary,
  createProtectionRegion,
  deleteGlossary,
  deleteProtectionRegion,
  downloadArtifactFile,
  ensureSessionToken,
  fetchArtifactPdfBytes,
  fetchDocument,
  fetchDocumentBlocks,
  fetchDocumentPdfBytes,
  fetchGlossaries,
  fetchJob,
  fetchJobArtifacts,
  fetchJobIssues,
  fetchProtectionRegions,
  fetchSystemDiagnostics,
  updateBlockProtection,
  reapplyAutoProtection,
  parseDocumentBlocks,
  purgeRuntimeTempData,
  retranslateSpecificBlocks,
  retypesetJob,
  resumeTranslationJob,
  rotateSessionToken,
  saveBlockManualEdit,
  startTranslationJob,
  updateGlossary,
  uploadPdfDocument,
} from "./api/client";
import { Screen1HomePreflight } from "./components/Screen1HomePreflight";
import { Screen2ProtectionEditor } from "./components/Screen2ProtectionEditor";
import { Screen3ProgressReview } from "./components/Screen3ProgressReview";
import { Screen4GlossaryDiagnostics } from "./components/Screen4GlossaryDiagnostics";
import type {
  ArtifactType,
  Block,
  Document,
  ExportArtifact,
  Glossary,
  GlossaryEntry,
  Job,
  ProtectionMode,
  ProtectionRegion,
  SystemDiagnostics,
  TranslationJobConfig,
  ValidationIssue,
} from "./types/api";
import type { BBox4 } from "./utils/coordinates";

export type ScreenId = "screen1" | "screen2" | "screen3" | "screen4";

const DEFAULT_JOB_CONFIG: TranslationJobConfig = {
  page_range_spec: null,
  source_lang: "en",
  target_lang: "ja",
  form: "plain",
  model_name: "plamo-3-translate-2b-Q4_K_M",
  quantization: "Q4_K_M",
  context_length: 8192,
  translate_captions: true,
  translate_tables: false,
  translate_references: false,
  translate_figures: false,
  min_font_size_pt: 8.0,
  glossary_id: null,
  output_mono_pdf: true,
  output_dual_pdf: true,
  only_roundtrip_preservation: false,
  pause_for_review: false,
  use_mock_translator: false, // Real hardware is the strict default (Section 5)
};

export interface AppProps {
  runJobsInBackground?: boolean;
}

export const App: React.FC<AppProps> = ({ runJobsInBackground = true }) => {
  const [activeScreen, setActiveScreen] = useState<ScreenId>("screen1");
  const [pendingNavScreen, setPendingNavScreen] = useState<ScreenId | null>(null);
  const [isScreen3Dirty, setIsScreen3Dirty] = useState<boolean>(false);

  // Shared Domain State
  const [currentDocument, setCurrentDocument] = useState<Document | null>(null);
  const [originalPdfBytes, setOriginalPdfBytes] = useState<Uint8Array | null>(null);
  const [translatedPdfBytes, setTranslatedPdfBytes] = useState<Uint8Array | null>(null);
  const [jobConfig, setJobConfig] = useState<TranslationJobConfig>(DEFAULT_JOB_CONFIG);
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [protectionRegions, setProtectionRegions] = useState<ProtectionRegion[]>([]);
  const [currentJob, setCurrentJob] = useState<Job | null>(null);
  const [issues, setIssues] = useState<ValidationIssue[]>([]);
  const [artifacts, setArtifacts] = useState<ExportArtifact[]>([]);
  const [glossaries, setGlossaries] = useState<Glossary[]>([]);
  const [diagnostics, setDiagnostics] = useState<SystemDiagnostics | null>(null);
  const [hasProbedLlama, setHasProbedLlama] = useState<boolean>(false);

  // Loading & Error Flags
  const [isUploading, setIsUploading] = useState<boolean>(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [isStartingJob, setIsStartingJob] = useState<boolean>(false);
  const [jobStartError, setJobStartError] = useState<string | null>(null);
  const [isParsingBlocks, setIsParsingBlocks] = useState<boolean>(false);
  const [protectionSaveStatus, setProtectionSaveStatus] = useState<
    "saved" | "saving" | "error"
  >("saved");
  const [protectionSaveMessage, setProtectionSaveMessage] = useState<string | null>(null);

  const [isCancelingJob, setIsCancelingJob] = useState<boolean>(false);
  const [isResumingJob, setIsResumingJob] = useState<boolean>(false);
  const [isRetypesetting, setIsRetypesetting] = useState<boolean>(false);
  const [isRetranslatingBlock, setIsRetranslatingBlock] = useState<boolean>(false);
  const [retypesetMessage, setRetypesetMessage] = useState<string | null>(null);
  const [retypesetError, setRetypesetError] = useState<string | null>(null);

  const [isLoadingDiagnostics, setIsLoadingDiagnostics] = useState<boolean>(false);
  const [diagnosticsError, setDiagnosticsError] = useState<string | null>(null);

  const loadInitialSessionAndData = useCallback(async () => {
    try {
      await ensureSessionToken();
      const [gls, diag] = await Promise.all([
        fetchGlossaries(),
        fetchSystemDiagnostics(false),
      ]);
      setGlossaries(gls);
      setDiagnostics(diag);
    } catch (err) {
      setDiagnosticsError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void loadInitialSessionAndData();
  }, [loadInitialSessionAndData]);

  const refreshJobDetails = useCallback(
    async (jobId: string, docId: string) => {
      const [job, blks, iss, arts, doc] = await Promise.all([
        fetchJob(jobId),
        fetchDocumentBlocks(docId),
        fetchJobIssues(jobId),
        fetchJobArtifacts(jobId),
        fetchDocument(docId),
      ]);
      setCurrentJob(job);
      setBlocks(blks);
      setIssues(iss);
      setArtifacts(arts);
      setCurrentDocument(doc);

      const hasMono = arts.some(
        (a) => a.artifact_type === "mono_ja_pdf" && a.is_finalized
      );
      if (hasMono) {
        try {
          const pdfRes = await fetchArtifactPdfBytes(jobId, "mono_ja_pdf");
          setTranslatedPdfBytes(pdfRes.data);
        } catch {
          // ignore if preview bytes unavailable
        }
      }
      return job;
    },
    []
  );

  // Poll while currentJob is in an active running state
  useEffect(() => {
    if (!currentJob || !currentDocument) return;
    const isRunning = [
      "created",
      "preflight",
      "parsing",
      "translating",
      "validating",
      "typesetting",
    ].includes(currentJob.state);
    if (!isRunning) return;

    const timer = window.setInterval(async () => {
      try {
        await refreshJobDetails(currentJob.id, currentDocument.id);
      } catch {
        // ignore transient poll error
      }
    }, 600);

    return () => window.clearInterval(timer);
  }, [currentJob?.id, currentJob?.state, currentDocument?.id, refreshJobDetails]);

  const handleNavigate = (targetScreen: ScreenId) => {
    if (activeScreen === "screen3" && isScreen3Dirty && targetScreen !== "screen3") {
      setPendingNavScreen(targetScreen);
      return;
    }
    setActiveScreen(targetScreen);
  };

  // --- Screen 1 Handlers ---
  const handleUploadFile = async (file: File) => {
    setIsUploading(true);
    setUploadError(null);
    setJobStartError(null);
    try {
      const doc = await uploadPdfDocument(file);
      setCurrentDocument(doc);
      setCurrentJob(null);
      setTranslatedPdfBytes(null);
      setIssues([]);
      setArtifacts([]);

      // Fetch authenticated PDF bytes & pre-parse blocks if extractable
      const pdfRes = await fetchDocumentPdfBytes(doc.id);
      setOriginalPdfBytes(pdfRes.data);

      if (
        !doc.is_corrupted &&
        !doc.is_encrypted &&
        !doc.is_scanned_pdf &&
        doc.extractable_Sync_status === "ok"
      ) {
        const [parsed, regs] = await Promise.all([
          parseDocumentBlocks(doc.id, jobConfig),
          fetchProtectionRegions(doc.id),
        ]);
        setBlocks(parsed);
        setProtectionRegions(regs);
      } else {
        setBlocks([]);
        setProtectionRegions([]);
      }
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsUploading(false);
    }
  };

  const handleStartTranslationJob = async () => {
    if (!currentDocument) return;
    setIsStartingJob(true);
    setJobStartError(null);
    setRetypesetMessage(null);
    setRetypesetError(null);
    try {
      const job = await startTranslationJob(
        currentDocument.id,
        jobConfig,
        runJobsInBackground
      );
      setCurrentJob(job);
      await refreshJobDetails(job.id, currentDocument.id);
      setActiveScreen("screen3");
    } catch (err) {
      setJobStartError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsStartingJob(false);
    }
  };

  // --- Screen 2 Handlers ---
  const handleReparseBlocks = async () => {
    if (!currentDocument) return;
    setIsParsingBlocks(true);
    setProtectionSaveStatus("saving");
    try {
      const [parsed, updatedDoc] = await Promise.all([
        parseDocumentBlocks(currentDocument.id, jobConfig),
        fetchDocument(currentDocument.id),
      ]);
      setBlocks(parsed);
      setCurrentDocument(updatedDoc);
      setProtectionSaveStatus("saved");
      setProtectionSaveMessage(
        `保護領域を反映して全 ${parsed.length} ブロックを再解析しました。`
      );
    } catch (err) {
      setProtectionSaveStatus("error");
      setProtectionSaveMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setIsParsingBlocks(false);
    }
  };

  const handleSetBlockOverride = async (
    blockId: string,
    override: "force_translate" | "force_protect" | null
  ) => {
    if (!currentDocument) return;
    setProtectionSaveStatus("saving");
    try {
      await updateBlockProtection(currentDocument.id, blockId, override);
      const [blks, updatedDoc] = await Promise.all([
        fetchDocumentBlocks(currentDocument.id),
        fetchDocument(currentDocument.id),
      ]);
      setBlocks(blks);
      setCurrentDocument(updatedDoc);
      setProtectionSaveStatus("saved");
      setProtectionSaveMessage("ブロックの翻訳／非翻訳指定を更新しました。");
    } catch (err) {
      setProtectionSaveStatus("error");
      setProtectionSaveMessage(err instanceof Error ? err.message : String(err));
    }
  };

  const handleReapplyAutoProtection = async () => {
    if (!currentDocument) return;
    setProtectionSaveStatus("saving");
    try {
      await reapplyAutoProtection(currentDocument.id, true, false);
      const [blks, updatedDoc] = await Promise.all([
        fetchDocumentBlocks(currentDocument.id),
        fetchDocument(currentDocument.id),
      ]);
      setBlocks(blks);
      setCurrentDocument(updatedDoc);
      setProtectionSaveStatus("saved");
      setProtectionSaveMessage(
        "初期非翻訳判定を再適用しました（完了済みジョブの成果物は変更していません）。"
      );
    } catch (err) {
      setProtectionSaveStatus("error");
      setProtectionSaveMessage(err instanceof Error ? err.message : String(err));
    }
  };

  const handleAddProtectionRegion = async (payload: {
    page_index: number;
    bbox: BBox4;
    mode: ProtectionMode;
    note?: string | null;
  }) => {
    if (!currentDocument) return;
    setProtectionSaveStatus("saving");
    try {
      await createProtectionRegion(currentDocument.id, payload);
      const [regs, parsed, updatedDoc] = await Promise.all([
        fetchProtectionRegions(currentDocument.id),
        parseDocumentBlocks(currentDocument.id, jobConfig),
        fetchDocument(currentDocument.id),
      ]);
      setProtectionRegions(regs);
      setBlocks(parsed);
      setCurrentDocument(updatedDoc);
      setProtectionSaveStatus("saved");
    } catch (err) {
      setProtectionSaveStatus("error");
      setProtectionSaveMessage(err instanceof Error ? err.message : String(err));
    }
  };

  const handleDeleteProtectionRegion = async (regionId: string) => {
    if (!currentDocument) return;
    setProtectionSaveStatus("saving");
    try {
      await deleteProtectionRegion(currentDocument.id, regionId);
      const [regs, parsed, updatedDoc] = await Promise.all([
        fetchProtectionRegions(currentDocument.id),
        parseDocumentBlocks(currentDocument.id, jobConfig),
        fetchDocument(currentDocument.id),
      ]);
      setProtectionRegions(regs);
      setBlocks(parsed);
      setCurrentDocument(updatedDoc);
      setProtectionSaveStatus("saved");
      setProtectionSaveMessage("手動保護領域を削除し、ブロック保護判定を更新しました。");
    } catch (err) {
      setProtectionSaveStatus("error");
      setProtectionSaveMessage(err instanceof Error ? err.message : String(err));
    }
  };

  // --- Screen 3 Handlers ---
  const handleCancelJob = async () => {
    if (!currentJob || !currentDocument) return;
    setIsCancelingJob(true);
    try {
      const updated = await cancelTranslationJob(currentJob.id);
      setCurrentJob(updated);
      await refreshJobDetails(currentJob.id, currentDocument.id);
    } finally {
      setIsCancelingJob(false);
    }
  };

  const handleResumeJob = async () => {
    if (!currentJob || !currentDocument) return;
    setIsResumingJob(true);
    try {
      const resumed = await resumeTranslationJob(
        currentJob.id,
        runJobsInBackground
      );
      setCurrentJob(resumed);
      await refreshJobDetails(currentJob.id, currentDocument.id);
    } finally {
      setIsResumingJob(false);
    }
  };

  const handleSaveManualEdit = async (
    blockId: string,
    manualText: string | null,
    expectedEditRevision?: number
  ): Promise<Block> => {
    if (!currentDocument) {
      throw new Error("Document not loaded");
    }
    const updatedBlock = await saveBlockManualEdit(
      currentDocument.id,
      blockId,
      manualText,
      expectedEditRevision
    );
    const updatedDoc = await fetchDocument(currentDocument.id);
    setCurrentDocument(updatedDoc);
    setBlocks((prev) =>
      prev.map((b) => (b.id === updatedBlock.id ? updatedBlock : b))
    );
    return updatedBlock;
  };

  const handleRetranslateBlock = async (blockId: string) => {
    if (!currentJob || !currentDocument) return;
    setIsRetranslatingBlock(true);
    try {
      const job = await retranslateSpecificBlocks(currentJob.id, [blockId]);
      setCurrentJob(job);
      await refreshJobDetails(job.id, currentDocument.id);
    } finally {
      setIsRetranslatingBlock(false);
    }
  };

  const handleRetypesetJob = async (allowValidationWarnings: boolean) => {
    if (!currentJob || !currentDocument) return;
    setIsRetypesetting(true);
    setRetypesetMessage(null);
    setRetypesetError(null);
    try {
      const res = await retypesetJob(currentJob.id, {
        expected_edit_revision: currentDocument.edit_revision,
        allow_validation_warnings: allowValidationWarnings,
      });
      setCurrentJob(res.job);
      setArtifacts(res.artifacts);
      setIssues(res.issues);
      await refreshJobDetails(currentJob.id, currentDocument.id);
      setRetypesetMessage(
        `LLM再呼び出しなし（LLM呼び出し ${res.llm_calls_made} 回）でPDF再組版が完了しました (retypeset_revision=${res.job.retypeset_revision}, based_on_edit_revision=${res.job.based_on_edit_revision})${
          res.requires_reparse_or_retranslate
            ? " ※保護領域解除により未翻訳となったブロックは原文保持されています。"
            : "。"
        }`
      );
    } catch (err) {
      setRetypesetError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsRetypesetting(false);
    }
  };

  const handleDownloadArtifact = async (
    artifactType: ArtifactType,
    fallbackName: string
  ) => {
    if (!currentJob) return;
    await downloadArtifactFile(currentJob.id, artifactType, fallbackName);
  };

  // --- Screen 4 Handlers ---
  const handleRefreshDiagnostics = async (probeLlama: boolean) => {
    setIsLoadingDiagnostics(true);
    setDiagnosticsError(null);
    try {
      const diag = await fetchSystemDiagnostics(probeLlama);
      setDiagnostics(diag);
      if (probeLlama) {
        setHasProbedLlama(true);
      }
    } catch (err) {
      setDiagnosticsError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsLoadingDiagnostics(false);
    }
  };

  const handleCreateGlossary = async (
    name: string,
    entries: GlossaryEntry[]
  ): Promise<Glossary> => {
    const created = await createGlossary(name, entries);
    const gls = await fetchGlossaries();
    setGlossaries(gls);
    return created;
  };

  const handleUpdateGlossary = async (
    glossaryId: string,
    name: string,
    entries: GlossaryEntry[]
  ): Promise<Glossary> => {
    const updated = await updateGlossary(glossaryId, name, entries);
    const gls = await fetchGlossaries();
    setGlossaries(gls);
    return updated;
  };

  const handleDeleteGlossary = async (glossaryId: string): Promise<void> => {
    await deleteGlossary(glossaryId);
    const gls = await fetchGlossaries();
    setGlossaries(gls);
    if (jobConfig.glossary_id === glossaryId) {
      setJobConfig((prev) => ({ ...prev, glossary_id: null }));
    }
  };

  const handlePurgeTempData = async (): Promise<string> => {
    const res = await purgeRuntimeTempData();
    return res.directory;
  };

  const handleRotateToken = async (): Promise<void> => {
    await rotateSessionToken();
  };

  return (
    <div data-testid="paperbridge-app">
      {/* Global Navigation Header */}
      <header className="app-header">
        <div className="app-brand">
          <h1>PaperBridge Local</h1>
          <span className="badge badge-info">
            127.0.0.1 ローカル完結・数式保護優先
          </span>
        </div>

        <nav className="nav-tabs" aria-label="画面切り替えナビゲーション">
          <button
            type="button"
            data-testid="nav-screen1-btn"
            className={`nav-tab-btn ${activeScreen === "screen1" ? "active" : ""}`}
            onClick={() => handleNavigate("screen1")}
          >
            1. ホーム・プリフライト・設定
          </button>
          <button
            type="button"
            data-testid="nav-screen2-btn"
            disabled={!currentDocument}
            className={`nav-tab-btn ${activeScreen === "screen2" ? "active" : ""}`}
            onClick={() => handleNavigate("screen2")}
          >
            2. PDFビューア・保護領域編集
          </button>
          <button
            type="button"
            data-testid="nav-screen3-btn"
            disabled={!currentJob || !currentDocument}
            className={`nav-tab-btn ${activeScreen === "screen3" ? "active" : ""}`}
            onClick={() => handleNavigate("screen3")}
          >
            3. 進行表示・対訳レビュー・再組版
          </button>
          <button
            type="button"
            data-testid="nav-screen4-btn"
            className={`nav-tab-btn ${activeScreen === "screen4" ? "active" : ""}`}
            onClick={() => handleNavigate("screen4")}
          >
            4. 用語集管理・システム診断
          </button>
        </nav>
      </header>

      {/* Unsaved Screen Switch Confirmation Modal Banner */}
      {pendingNavScreen && (
        <div
          role="alert"
          data-testid="nav-unsaved-warning"
          className="alert alert-warning"
          style={{
            margin: "12px 20px 0 20px",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <div>
            <strong>画面移動の確認:</strong>{" "}
            手動修正エディタに未保存の変更があります。破棄して画面を切り替えますか？
          </div>
          <div style={{ display: "flex", gap: "8px" }}>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setPendingNavScreen(null)}
            >
              現在の画面に留まる
            </button>
            <button
              type="button"
              className="btn btn-danger-sm"
              data-testid="confirm-nav-discard-btn"
              onClick={() => {
                const target = pendingNavScreen;
                setPendingNavScreen(null);
                setIsScreen3Dirty(false);
                setActiveScreen(target);
              }}
            >
              未保存の変更を破棄して移動
            </button>
          </div>
        </div>
      )}

      {/* Active Screen Content */}
      <main>
        {activeScreen === "screen1" && (
          <Screen1HomePreflight
            currentDocument={currentDocument}
            jobConfig={jobConfig}
            glossaries={glossaries}
            isUploading={isUploading}
            uploadError={uploadError}
            isStartingJob={isStartingJob}
            jobStartError={jobStartError}
            onUploadFile={handleUploadFile}
            onUpdateJobConfig={setJobConfig}
            onOpenProtectionScreen={() => setActiveScreen("screen2")}
            onStartTranslationJob={handleStartTranslationJob}
          />
        )}

        {activeScreen === "screen2" && currentDocument && (
          <Screen2ProtectionEditor
            document={currentDocument}
            pdfBytes={originalPdfBytes}
            blocks={blocks}
            protectionRegions={protectionRegions}
            isParsing={isParsingBlocks}
            saveStatus={protectionSaveStatus}
            saveMessage={protectionSaveMessage}
            onReparseBlocks={handleReparseBlocks}
            onAddProtectionRegion={handleAddProtectionRegion}
            onDeleteProtectionRegion={handleDeleteProtectionRegion}
            onProceedToStartJob={handleStartTranslationJob}
            onSetBlockOverride={handleSetBlockOverride}
            onReapplyAutoProtection={handleReapplyAutoProtection}
          />
        )}

        {activeScreen === "screen3" && currentDocument && currentJob && (
          <Screen3ProgressReview
            document={currentDocument}
            originalPdfBytes={originalPdfBytes}
            translatedPdfBytes={translatedPdfBytes}
            job={currentJob}
            blocks={blocks}
            issues={issues}
            artifacts={artifacts}
            isCanceling={isCancelingJob}
            isResuming={isResumingJob}
            isRetypesetting={isRetypesetting}
            isRetranslatingBlock={isRetranslatingBlock}
            retypesetMessage={retypesetMessage}
            retypesetError={retypesetError}
            onCancelJob={handleCancelJob}
            onResumeJob={handleResumeJob}
            onSaveManualEdit={handleSaveManualEdit}
            onRetranslateBlock={handleRetranslateBlock}
            onRetypesetJob={handleRetypesetJob}
            onDownloadArtifact={handleDownloadArtifact}
            onDirtyStateChange={setIsScreen3Dirty}
          />
        )}

        {activeScreen === "screen4" && (
          <Screen4GlossaryDiagnostics
            glossaries={glossaries}
            diagnostics={diagnostics}
            hasProbedLlama={hasProbedLlama}
            isLoadingDiagnostics={isLoadingDiagnostics}
            diagnosticsError={diagnosticsError}
            onRefreshDiagnostics={handleRefreshDiagnostics}
            onCreateGlossary={handleCreateGlossary}
            onUpdateGlossary={handleUpdateGlossary}
            onDeleteGlossary={handleDeleteGlossary}
            onPurgeTempData={handlePurgeTempData}
            onRotateToken={handleRotateToken}
          />
        )}
      </main>
    </div>
  );
};
