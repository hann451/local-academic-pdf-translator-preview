/**
 * PaperBridge Local Main Application (Phase 3: Screens 1–4 Integration).
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiHttpError,
  browseLocalModelFile,
  cancelModelDownload,
  cancelTranslationJob,
  createBlockSplit,
  createGlossary,
  createProtectionRegion,
  deleteBlockSplit,
  deleteGlossary,
  deleteProtectionRegion,
  deleteRegisteredModel,
  downloadArtifactFile,
  ensureSessionToken,
  fetchArtifactPdfBytes,
  fetchBlockSplits,
  fetchDocument,
  fetchDocumentBlocks,
  fetchDocumentPdfBytes,
  fetchGlossaries,
  fetchJob,
  fetchJobArtifacts,
  fetchJobIssues,
  fetchModelDownload,
  fetchModelsOverview,
  fetchProtectionRegions,
  fetchSystemDiagnostics,
  previewBlockSplit,
  registerLocalModel,
  selectActiveModel,
  startModelDownload,
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
  BlockSplitPreviewResult,
  BlockSplitSpec,
  Document,
  ExportArtifact,
  Glossary,
  GlossaryEntry,
  Job,
  ModelDownloadJob,
  ModelsOverviewResponse,
  ProtectionMode,
  ProtectionRegion,
  RegisterLocalModelRequest,
  RegisteredModel,
  StartModelDownloadRequest,
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
  const [pendingUnsavedSource, setPendingUnsavedSource] = useState<"screen2" | "screen3" | null>(
    null
  );
  const [isScreen2Dirty, setIsScreen2Dirty] = useState<boolean>(false);
  const isScreen2DirtyRef = useRef<boolean>(false);
  isScreen2DirtyRef.current = isScreen2Dirty;
  const [isScreen3Dirty, setIsScreen3Dirty] = useState<boolean>(false);

  // Shared Domain State
  const [currentDocument, setCurrentDocument] = useState<Document | null>(null);
  const [originalPdfBytes, setOriginalPdfBytes] = useState<Uint8Array | null>(null);
  const [translatedPdfBytes, setTranslatedPdfBytes] = useState<Uint8Array | null>(null);
  const [jobConfig, setJobConfig] = useState<TranslationJobConfig>(DEFAULT_JOB_CONFIG);
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [protectionRegions, setProtectionRegions] = useState<ProtectionRegion[]>([]);
  const [blockSplits, setBlockSplits] = useState<BlockSplitSpec[]>([]);
  const [currentJob, setCurrentJob] = useState<Job | null>(null);
  const [issues, setIssues] = useState<ValidationIssue[]>([]);
  const [artifacts, setArtifacts] = useState<ExportArtifact[]>([]);
  const [glossaries, setGlossaries] = useState<Glossary[]>([]);
  const [diagnostics, setDiagnostics] = useState<SystemDiagnostics | null>(null);
  const [hasProbedLlama, setHasProbedLlama] = useState<boolean>(false);
  const [modelsOverview, setModelsOverview] = useState<ModelsOverviewResponse | null>(null);
  const [activeModelDownload, setActiveModelDownload] = useState<ModelDownloadJob | null>(null);
  const [isLoadingModels, setIsLoadingModels] = useState<boolean>(false);
  const [modelsError, setModelsError] = useState<string | null>(null);

  // Loading & Error Flags
  const [isUploading, setIsUploading] = useState<boolean>(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [isStartingJob, setIsStartingJob] = useState<boolean>(false);
  const [jobStartError, setJobStartError] = useState<string | null>(null);
  const [isParsingBlocks, setIsParsingBlocks] = useState<boolean>(false);
  const [screen2SyncStatus, setScreen2SyncStatus] = useState<"loading" | "synced" | "error">(
    "synced"
  );
  const [screen2SyncError, setScreen2SyncError] = useState<string | null>(null);
  const [screen2UnsavedSyncBlocked, setScreen2UnsavedSyncBlocked] = useState<boolean>(false);
  const screen2SyncSeqRef = useRef<number>(0);
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

  const syncJobConfigWithOverview = useCallback((overview: ModelsOverviewResponse) => {
    const active = overview.active_model;
    if (!active) return;
    const cat = overview.catalog.find((c) => c.model_id === active.catalog_model_id);
    setJobConfig((prev) => ({
      ...prev,
      model_id: active.catalog_model_id,
      model_name: active.file_name || active.display_name || prev.model_name,
      model_sha256: active.measured_sha256 || null,
      quantization: active.quantization || prev.quantization,
      context_length: cat?.context_length_default ?? prev.context_length,
    }));
  }, []);

  const refreshModelsOverview = useCallback(
    async (probeLlama: boolean = false) => {
      setIsLoadingModels(true);
      setModelsError(null);
      try {
        const overview = await fetchModelsOverview(probeLlama);
        setModelsOverview(overview);
        syncJobConfigWithOverview(overview);
        if (probeLlama) {
          setHasProbedLlama(true);
        }
        const runningDl = overview.downloads.find(
          (dl) => dl.status === "downloading" || dl.status === "verifying"
        );
        if (runningDl) {
          setActiveModelDownload(runningDl);
        }
      } catch (err) {
        setModelsError(err instanceof Error ? err.message : String(err));
      } finally {
        setIsLoadingModels(false);
      }
    },
    [syncJobConfigWithOverview]
  );

  const loadInitialSessionAndData = useCallback(async () => {
    try {
      await ensureSessionToken();
      const [gls, diag] = await Promise.all([
        fetchGlossaries(),
        fetchSystemDiagnostics(false),
      ]);
      setGlossaries(gls);
      setDiagnostics(diag);
      void refreshModelsOverview(false);
    } catch (err) {
      setDiagnosticsError(err instanceof Error ? err.message : String(err));
    }
  }, [refreshModelsOverview]);

  useEffect(() => {
    void loadInitialSessionAndData();
  }, [loadInitialSessionAndData]);

  // Poll model download status while downloading or verifying
  useEffect(() => {
    if (
      !activeModelDownload ||
      (activeModelDownload.status !== "downloading" &&
        activeModelDownload.status !== "verifying")
    ) {
      return;
    }

    const timer = window.setInterval(async () => {
      try {
        const updated = await fetchModelDownload(activeModelDownload.download_id);
        setActiveModelDownload(updated);
        if (
          updated.status === "completed" ||
          updated.status === "canceled" ||
          updated.status === "failed"
        ) {
          await refreshModelsOverview(false);
          try {
            const diag = await fetchSystemDiagnostics(false);
            setDiagnostics(diag);
          } catch {
            // ignore diagnostics refresh failure on background download finish
          }
        }
      } catch {
        // ignore transient poll error
      }
    }, 600);

    return () => window.clearInterval(timer);
  }, [activeModelDownload?.download_id, activeModelDownload?.status, refreshModelsOverview]);

  const maybeRefreshModelsAfterRealJob = useCallback(
    (job: Job) => {
      if (
        (job.state === "completed" || job.state === "completed_with_warnings") &&
        !job.is_mock_run
      ) {
        void refreshModelsOverview(false);
      }
    },
    [refreshModelsOverview]
  );

  const refreshJobDetails = useCallback(
    async (jobId: string, docId: string) => {
      const [job, blks, iss, arts, doc, splits] = await Promise.all([
        fetchJob(jobId),
        fetchDocumentBlocks(docId),
        fetchJobIssues(jobId),
        fetchJobArtifacts(jobId),
        fetchDocument(docId),
        fetchBlockSplits(docId),
      ]);
      setCurrentJob(job);
      setBlocks(blks);
      setIssues(iss);
      setArtifacts(arts);
      setCurrentDocument(doc);
      setBlockSplits(splits);

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
        const updatedJob = await refreshJobDetails(currentJob.id, currentDocument.id);
        maybeRefreshModelsAfterRealJob(updatedJob);
      } catch {
        // ignore transient poll error
      }
    }, 600);

    return () => window.clearInterval(timer);
  }, [currentJob?.id, currentJob?.state, currentDocument?.id, refreshJobDetails, maybeRefreshModelsAfterRealJob]);

  const syncScreen2Data = useCallback(
    async (docId: string, options?: { forceDiscardUnsaved?: boolean }) => {
      if (isScreen2DirtyRef.current && !options?.forceDiscardUnsaved) {
        setScreen2UnsavedSyncBlocked(true);
        return;
      }
      if (options?.forceDiscardUnsaved) {
        isScreen2DirtyRef.current = false;
        setIsScreen2Dirty(false);
        setScreen2UnsavedSyncBlocked(false);
      }
      const reqSeq = ++screen2SyncSeqRef.current;
      setScreen2SyncStatus("loading");
      setScreen2SyncError(null);
      setScreen2UnsavedSyncBlocked(false);
      try {
        const [latestDoc, latestBlocks, latestSplits, latestRegs] = await Promise.all([
          fetchDocument(docId),
          fetchDocumentBlocks(docId),
          fetchBlockSplits(docId),
          fetchProtectionRegions(docId),
        ]);
        if (reqSeq !== screen2SyncSeqRef.current) return;
        setCurrentDocument(latestDoc);
        setBlocks(latestBlocks);
        setBlockSplits(latestSplits);
        setProtectionRegions(latestRegs);
        setScreen2SyncStatus("synced");
        setScreen2SyncError(null);
      } catch (err) {
        if (reqSeq !== screen2SyncSeqRef.current) return;
        setScreen2SyncStatus("error");
        setScreen2SyncError(err instanceof Error ? err.message : String(err));
      }
    },
    []
  );

  const handleNavigate = (targetScreen: ScreenId) => {
    if (activeScreen === "screen3" && isScreen3Dirty && targetScreen !== "screen3") {
      setPendingUnsavedSource("screen3");
      setPendingNavScreen(targetScreen);
      return;
    }
    if (activeScreen === "screen2" && isScreen2Dirty && targetScreen !== "screen2") {
      setPendingUnsavedSource("screen2");
      setPendingNavScreen(targetScreen);
      return;
    }
    if (targetScreen === "screen2") {
      setActiveScreen("screen2");
      if (currentDocument) {
        void syncScreen2Data(currentDocument.id);
      }
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
      setIsScreen2Dirty(false);
      isScreen2DirtyRef.current = false;
      setScreen2UnsavedSyncBlocked(false);
      setScreen2SyncStatus("synced");
      setScreen2SyncError(null);

      // Fetch authenticated PDF bytes & pre-parse blocks if extractable
      const pdfRes = await fetchDocumentPdfBytes(doc.id);
      setOriginalPdfBytes(pdfRes.data);

      if (
        !doc.is_corrupted &&
        !doc.is_encrypted &&
        !doc.is_scanned_pdf &&
        doc.extractable_Sync_status === "ok"
      ) {
        const [parsed, regs, splits, refreshedDoc] = await Promise.all([
          parseDocumentBlocks(doc.id, jobConfig),
          fetchProtectionRegions(doc.id),
          fetchBlockSplits(doc.id),
          fetchDocument(doc.id),
        ]);
        setBlocks(parsed);
        setProtectionRegions(regs);
        setBlockSplits(splits);
        setCurrentDocument(refreshedDoc);
      } else {
        setBlocks([]);
        setProtectionRegions([]);
        setBlockSplits([]);
      }
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsUploading(false);
    }
  };

  const handleStartTranslationJob = async (options?: {
    allowPendingSplits?: boolean;
  }) => {
    if (!currentDocument) return;
    setIsStartingJob(true);
    setJobStartError(null);
    setRetypesetMessage(null);
    setRetypesetError(null);
    try {
      const activeModel = modelsOverview?.active_model;
      const baseConfig: TranslationJobConfig = activeModel
        ? {
            ...jobConfig,
            model_id: activeModel.catalog_model_id,
            model_name: activeModel.file_name || activeModel.display_name || jobConfig.model_name,
            model_sha256: activeModel.measured_sha256 || jobConfig.model_sha256 || null,
          }
        : jobConfig;
      const effectiveConfig: TranslationJobConfig = options?.allowPendingSplits
        ? { ...baseConfig, allow_pending_splits: true }
        : baseConfig;
      const job = await startTranslationJob(
        currentDocument.id,
        effectiveConfig,
        runJobsInBackground
      );
      setCurrentJob(job);
      const refreshedJob = await refreshJobDetails(job.id, currentDocument.id);
      maybeRefreshModelsAfterRealJob(refreshedJob);
      setActiveScreen("screen3");
    } catch (err) {
      setJobStartError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsStartingJob(false);
    }
  };

  const formatProtectionError = (err: unknown): string => {
    if (err instanceof ApiHttpError && err.status === 409) {
      return `他処理との競合または状態更新により実行できませんでした（409 Conflict: ${err.detail}）。最新状態を再取得してからお試しください。`;
    }
    return err instanceof Error ? err.message : String(err);
  };

  // --- Screen 2 Handlers ---
  const handleReparseBlocks = async () => {
    if (!currentDocument) return;
    setIsParsingBlocks(true);
    setProtectionSaveStatus("saving");
    try {
      const [parsed, updatedDoc, splits] = await Promise.all([
        parseDocumentBlocks(currentDocument.id, jobConfig),
        fetchDocument(currentDocument.id),
        fetchBlockSplits(currentDocument.id),
      ]);
      setBlocks(parsed);
      setCurrentDocument(updatedDoc);
      setBlockSplits(splits);
      setProtectionSaveStatus("saved");
      setProtectionSaveMessage(
        `保護領域と分割指定を反映して全 ${parsed.length} ブロックを再解析しました。`
      );
    } catch (err) {
      setProtectionSaveStatus("error");
      setProtectionSaveMessage(formatProtectionError(err));
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
      setProtectionSaveMessage(formatProtectionError(err));
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
      setProtectionSaveMessage(formatProtectionError(err));
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
      const [regs, parsed, updatedDoc, splits] = await Promise.all([
        fetchProtectionRegions(currentDocument.id),
        parseDocumentBlocks(currentDocument.id, jobConfig),
        fetchDocument(currentDocument.id),
        fetchBlockSplits(currentDocument.id),
      ]);
      setProtectionRegions(regs);
      setBlocks(parsed);
      setCurrentDocument(updatedDoc);
      setBlockSplits(splits);
      setProtectionSaveStatus("saved");
    } catch (err) {
      setProtectionSaveStatus("error");
      setProtectionSaveMessage(formatProtectionError(err));
    }
  };

  const handleDeleteProtectionRegion = async (regionId: string) => {
    if (!currentDocument) return;
    setProtectionSaveStatus("saving");
    try {
      await deleteProtectionRegion(currentDocument.id, regionId);
      const [regs, parsed, updatedDoc, splits] = await Promise.all([
        fetchProtectionRegions(currentDocument.id),
        parseDocumentBlocks(currentDocument.id, jobConfig),
        fetchDocument(currentDocument.id),
        fetchBlockSplits(currentDocument.id),
      ]);
      setProtectionRegions(regs);
      setBlocks(parsed);
      setCurrentDocument(updatedDoc);
      setBlockSplits(splits);
      setProtectionSaveStatus("saved");
      setProtectionSaveMessage("手動保護領域を削除し、ブロック保護判定を更新しました。");
    } catch (err) {
      setProtectionSaveStatus("error");
      setProtectionSaveMessage(formatProtectionError(err));
    }
  };

  const handlePreviewBlockSplit = async (payload: {
    page_index: number;
    requested_bbox: BBox4;
    include_equation_number: boolean;
  }): Promise<BlockSplitPreviewResult> => {
    if (!currentDocument) {
      throw new Error("Document not loaded");
    }
    return previewBlockSplit(currentDocument.id, payload);
  };

  const handleCreateBlockSplit = async (payload: {
    page_index: number;
    requested_bbox: BBox4;
    include_equation_number: boolean;
    note?: string | null;
    expected_protection_revision?: number | null;
    expected_parse_revision?: number | null;
    expected_parent_signature?: string | null;
  }) => {
    if (!currentDocument) return;
    setProtectionSaveStatus("saving");
    try {
      const res = await createBlockSplit(currentDocument.id, payload);
      setBlocks(res.blocks);
      setBlockSplits(res.splits);
      setCurrentDocument(res.document);
      setProtectionSaveStatus("saved");
      setProtectionSaveMessage(
        "選択範囲を数式（原文保護）と本文へ分割・保存しました。変更があった本文ブロックには再翻訳が必要です。"
      );
      return res;
    } catch (err) {
      setProtectionSaveStatus("error");
      setProtectionSaveMessage(formatProtectionError(err));
      throw err;
    }
  };

  const handleDeleteBlockSplit = async (splitId: string) => {
    if (!currentDocument) return;
    setProtectionSaveStatus("saving");
    try {
      const res = await deleteBlockSplit(currentDocument.id, splitId);
      setBlocks(res.blocks);
      setBlockSplits(res.splits);
      setCurrentDocument(res.document);
      setProtectionSaveStatus("saved");
      setProtectionSaveMessage(
        "分割指定を削除し、元の親ブロック構成へ復元しました（他の手動保護領域は維持されています）。"
      );
    } catch (err) {
      setProtectionSaveStatus("error");
      setProtectionSaveMessage(formatProtectionError(err));
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
      const refreshedJob = await refreshJobDetails(currentJob.id, currentDocument.id);
      maybeRefreshModelsAfterRealJob(refreshedJob);
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
      const refreshedJob = await refreshJobDetails(job.id, currentDocument.id);
      maybeRefreshModelsAfterRealJob(refreshedJob);
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

  // --- Screen 4 & Model Management Handlers ---
  const handleRefreshDiagnostics = async (probeLlama: boolean) => {
    setIsLoadingDiagnostics(true);
    setDiagnosticsError(null);
    try {
      const diag = await fetchSystemDiagnostics(probeLlama);
      setDiagnostics(diag);
      if (probeLlama) {
        setHasProbedLlama(true);
      }
      void refreshModelsOverview(probeLlama);
    } catch (err) {
      setDiagnosticsError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsLoadingDiagnostics(false);
    }
  };

  const handleRefreshModels = async (probeLlama: boolean): Promise<void> => {
    await refreshModelsOverview(probeLlama);
    try {
      const diag = await fetchSystemDiagnostics(probeLlama);
      setDiagnostics(diag);
      if (probeLlama) {
        setHasProbedLlama(true);
      }
    } catch (err) {
      setDiagnosticsError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleBrowseLocalModelFile = async (): Promise<string | null> => {
    const res = await browseLocalModelFile();
    return res.canceled ? null : res.file_path;
  };

  const handleRegisterLocalModel = async (
    payload: RegisterLocalModelRequest
  ): Promise<RegisteredModel> => {
    const reg = await registerLocalModel(payload);
    await handleRefreshModels(false);
    return reg;
  };

  const handleSelectModel = async (registrationId: string): Promise<void> => {
    await selectActiveModel(registrationId, true);
    await handleRefreshModels(true);
  };

  const handleDeleteModel = async (registrationId: string): Promise<void> => {
    await deleteRegisteredModel(registrationId);
    await handleRefreshModels(false);
  };

  const handleStartModelDownload = async (
    payload: StartModelDownloadRequest
  ): Promise<ModelDownloadJob> => {
    const dlJob = await startModelDownload(payload);
    setActiveModelDownload(dlJob);
    if (
      dlJob.status === "completed" ||
      dlJob.status === "canceled" ||
      dlJob.status === "failed"
    ) {
      await handleRefreshModels(false);
    }
    return dlJob;
  };

  const handleCancelModelDownload = async (downloadId: string): Promise<void> => {
    const canceled = await cancelModelDownload(downloadId);
    setActiveModelDownload(canceled);
    await handleRefreshModels(false);
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
            {pendingUnsavedSource === "screen2"
              ? "画面2（保護領域・分割エディタ）に未確定の分割プレビューまたは入力中のメモがあります。破棄して画面を切り替えますか？"
              : "手動修正エディタに未保存の変更があります。破棄して画面を切り替えますか？"}
          </div>
          <div style={{ display: "flex", gap: "8px" }}>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                setPendingNavScreen(null);
                setPendingUnsavedSource(null);
              }}
            >
              現在の画面に留まる
            </button>
            <button
              type="button"
              className="btn btn-danger-sm"
              data-testid="confirm-nav-discard-btn"
              onClick={() => {
                const target = pendingNavScreen;
                const source = pendingUnsavedSource;
                setPendingNavScreen(null);
                setPendingUnsavedSource(null);
                if (source === "screen2") {
                  isScreen2DirtyRef.current = false;
                  setIsScreen2Dirty(false);
                  setScreen2UnsavedSyncBlocked(false);
                } else {
                  setIsScreen3Dirty(false);
                }
                setActiveScreen(target);
                if (target === "screen2" && currentDocument) {
                  void syncScreen2Data(currentDocument.id, { forceDiscardUnsaved: true });
                }
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
            modelsOverview={modelsOverview}
            activeModelDownload={activeModelDownload}
            isLoadingModels={isLoadingModels}
            modelsError={modelsError}
            isUploading={isUploading}
            uploadError={uploadError}
            isStartingJob={isStartingJob}
            jobStartError={jobStartError}
            onUploadFile={handleUploadFile}
            onUpdateJobConfig={setJobConfig}
            onOpenProtectionScreen={() => handleNavigate("screen2")}
            onOpenModelsScreen={() => handleNavigate("screen4")}
            onRefreshModels={handleRefreshModels}
            onBrowseLocalModelFile={handleBrowseLocalModelFile}
            onRegisterLocalModel={handleRegisterLocalModel}
            onSelectModel={handleSelectModel}
            onDeleteModel={handleDeleteModel}
            onStartModelDownload={handleStartModelDownload}
            onCancelModelDownload={handleCancelModelDownload}
            onStartTranslationJob={handleStartTranslationJob}
          />
        )}

        {activeScreen === "screen2" && currentDocument && (
          <Screen2ProtectionEditor
            document={currentDocument}
            pdfBytes={originalPdfBytes}
            blocks={blocks}
            protectionRegions={protectionRegions}
            blockSplits={blockSplits}
            isParsing={isParsingBlocks}
            saveStatus={protectionSaveStatus}
            saveMessage={protectionSaveMessage}
            syncStatus={screen2SyncStatus}
            syncError={screen2SyncError}
            unsavedSyncBlocked={screen2UnsavedSyncBlocked}
            onSyncDocumentData={(opts) => syncScreen2Data(currentDocument.id, opts)}
            onDismissUnsavedSyncWarning={() => setScreen2UnsavedSyncBlocked(false)}
            onDirtyStateChange={setIsScreen2Dirty}
            onReparseBlocks={handleReparseBlocks}
            onAddProtectionRegion={handleAddProtectionRegion}
            onDeleteProtectionRegion={handleDeleteProtectionRegion}
            onPreviewBlockSplit={handlePreviewBlockSplit}
            onCreateBlockSplit={handleCreateBlockSplit}
            onDeleteBlockSplit={handleDeleteBlockSplit}
            onProceedToStartJob={handleStartTranslationJob}
            jobStartError={jobStartError}
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
            modelsOverview={modelsOverview}
            activeModelDownload={activeModelDownload}
            isLoadingModels={isLoadingModels}
            modelsError={modelsError}
            onRefreshDiagnostics={handleRefreshDiagnostics}
            onRefreshModels={handleRefreshModels}
            onBrowseLocalModelFile={handleBrowseLocalModelFile}
            onRegisterLocalModel={handleRegisterLocalModel}
            onSelectModel={handleSelectModel}
            onDeleteModel={handleDeleteModel}
            onStartModelDownload={handleStartModelDownload}
            onCancelModelDownload={handleCancelModelDownload}
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
