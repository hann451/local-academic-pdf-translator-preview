import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Screen1HomePreflight } from "../components/Screen1HomePreflight";
import { Screen2ProtectionEditor } from "../components/Screen2ProtectionEditor";
import { Screen3ProgressReview } from "../components/Screen3ProgressReview";
import { Screen4GlossaryDiagnostics } from "../components/Screen4GlossaryDiagnostics";
import type {
  Block,
  Document,
  ExportArtifact,
  Glossary,
  Job,
  ProtectionRegion,
  SystemDiagnostics,
  TranslationJobConfig,
  ValidationIssue,
} from "../types/api";

const SAMPLE_DOC: Document = {
  id: "doc-abc123456789",
  original_filename: "attention_math.pdf",
  safe_storage_path: "C:/runtime/uploads/doc-abc123456789.pdf",
  sha256_hash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  file_size_bytes: 128000,
  page_count: 2,
  is_encrypted: false,
  is_corrupted: false,
  is_scanned_pdf: false,
  extractable_Sync_status: "ok",
  unsupported_reason: null,
  protection_revision: 1,
  edit_revision: 1,
  pages: [
    {
      document_id: "doc-abc123456789",
      page_index: 0,
      width_pt: 595,
      height_pt: 842,
      rotation_deg: 0,
      media_box: [0, 0, 595, 842],
      crop_box: [0, 0, 595, 842],
      has_extractable_text: true,
      is_scanned_like: false,
      cid_ratio: 0.0,
      column_count: 2,
    },
    {
      document_id: "doc-abc123456789",
      page_index: 1,
      width_pt: 595,
      height_pt: 842,
      rotation_deg: 90,
      media_box: [0, 0, 595, 842],
      crop_box: [0, 0, 595, 842],
      has_extractable_text: true,
      is_scanned_like: false,
      cid_ratio: 0.01,
      column_count: 2,
    },
  ],
  created_at: "2026-11-06T12:00:00Z",
};

const DEFAULT_CFG: TranslationJobConfig = {
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
  use_mock_translator: false,
};

const SAMPLE_BLOCKS: Block[] = [
  {
    id: "doc-abc123456789:page-1:block-1",
    page_index: 0,
    bbox: [60, 80, 530, 140],
    kind: "paragraph",
    reading_order: 0,
    column_index: 1,
    source_text: "Given E(x) = α in 100 steps [1, 2].",
    masked_source_text: "Given {v1} in 100 steps [1, 2].",
    protected_items: [
      {
        placeholder: "{v1}",
        kind: "inline_formula",
        original_text: "E(x) = α",
        bbox: null,
        font_name: null,
      },
    ],
    translation_text: "100ステップにおいて {v1} が与えられたとする [1, 2]。",
    manual_translation_text: null,
    status: "translated",
    warnings: [],
    font_size_pt: 10.0,
    fitted_font_size_pt: 9.5,
    is_uncertain_region: false,
    protection_source: "none",
    edit_revision: 0,
    manual_validation_errors: [],
    manual_validation_warnings: [],
    semantic_category: "body",
    protection_reason: "",
    protection_rule_id: "",
    protection_confidence: "high",
    user_protection_override: null,
    translation_provenance: "none",
  },
  {
    id: "doc-abc123456789:page-2:block-1",
    page_index: 1,
    bbox: [70, 100, 500, 160],
    kind: "isolated_formula",
    reading_order: 0,
    column_index: 0,
    source_text: "L(θ) = ∑_i (y_i - f(x_i; θ))^2 (1)",
    masked_source_text: "L(θ) = ∑_i (y_i - f(x_i; θ))^2 (1)",
    protected_items: [],
    translation_text: null,
    manual_translation_text: null,
    status: "protected_skip",
    warnings: [],
    font_size_pt: 10.0,
    fitted_font_size_pt: null,
    is_uncertain_region: false,
    protection_source: "auto",
    edit_revision: 0,
    manual_validation_errors: [],
    manual_validation_warnings: [],
    semantic_category: "body",
    protection_reason: "",
    protection_rule_id: "",
    protection_confidence: "high",
    user_protection_override: null,
    translation_provenance: "none",
  },
];

describe("Screen 1: Home, Preflight & Job Configuration (Section 5)", () => {
  it("displays preflight details, enforces real-by-default inference, and validates page range", () => {
    let currentCfg = { ...DEFAULT_CFG };
    const onUpdate = vi.fn((next: TranslationJobConfig) => {
      currentCfg = next;
    });

    const { rerender } = render(
      <Screen1HomePreflight
        currentDocument={SAMPLE_DOC}
        jobConfig={currentCfg}
        glossaries={[]}
        isUploading={false}
        uploadError={null}
        isStartingJob={false}
        jobStartError={null}
        onUploadFile={vi.fn()}
        onUpdateJobConfig={onUpdate}
        onOpenProtectionScreen={vi.fn()}
        onStartTranslationJob={vi.fn()}
      />
    );

    expect(screen.getByTestId("doc-page-count")).toHaveTextContent("2 ページ");
    expect(screen.getByTestId("doc-sha256")).toHaveTextContent(SAMPLE_DOC.sha256_hash);
    expect(screen.getByTestId("style-form-select")).toHaveValue("plain");
    expect(screen.getByTestId("inference-mode-select")).toHaveValue("real");
    expect(screen.getByTestId("real-inference-prerequisite-note")).toHaveTextContent(
      "別ターミナルで推論サーバ"
    );

    const notice = screen.getByTestId("plamo-model-notice-screen1");
    expect(notice).toHaveTextContent("Built with PLaMo");
    expect(notice).toHaveTextContent("PLaMo 3 Translate");
    expect(notice).toHaveTextContent("Preferred Networks（PFN）の公式製品ではありません");
    expect(notice).toHaveTextContent("AGPL-3.0-only");
    expect(notice).toHaveTextContent("同梱せず別途取得");
    expect(notice).toHaveTextContent("契約条件を確認・遵守");
    expect(notice).toHaveTextContent("2026年11月9日改定ライセンスに関してPFNから受領した回答に基づく");
    expect(notice).toHaveTextContent("書籍・論文などの独立した長文公表物に関する場合を除き");
    expect(notice).toHaveTextContent(
      "ただし書籍・論文などの独立した長文公表物の全部または実質的部分として公表する場合は、PLaMoを翻訳に利用した旨をPDF内に含めて明示する必要があります"
    );
    expect(notice).toHaveTextContent("PDF末尾の説明ページでも対応可能");
    expect(notice).toHaveTextContent("説明ページを自動追加しません");
    expect(notice).toHaveTextContent("原著作物の翻訳・配布・公表の許諾まで与えるものではありません");
    expect(screen.getByTestId("screen1-link-plamo-model")).toHaveAttribute(
      "href",
      "https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF"
    );
    expect(screen.getByTestId("screen1-link-plamo-license-ja")).toHaveAttribute(
      "href",
      "https://plamo.preferredai.jp/info/plamo-community-license-ja"
    );
    expect(screen.getByTestId("screen1-link-plamo-license-en")).toHaveAttribute(
      "href",
      "https://plamo.preferredai.jp/info/plamo-community-license-en"
    );

    // Switch to custom range and enter reverse range "2-1" -> error shown & start button disabled
    fireEvent.click(screen.getByTestId("range-mode-custom"));
    const rangeInput = screen.getByTestId("page-range-input");
    fireEvent.change(rangeInput, { target: { value: "2-1" } });
    expect(screen.getByTestId("page-range-error")).toHaveTextContent("逆順のページ範囲");
    expect(screen.getByTestId("start-job-btn")).toBeDisabled();

    // Reject scanned PDF without silent OCR
    const scannedDoc: Document = {
      ...SAMPLE_DOC,
      is_scanned_pdf: true,
      extractable_Sync_status: "unsupported_scanned",
      unsupported_reason: "スキャンPDFは非対応です（無断OCRは実行しません）。",
    };
    rerender(
      <Screen1HomePreflight
        currentDocument={scannedDoc}
        jobConfig={DEFAULT_CFG}
        glossaries={[]}
        isUploading={false}
        uploadError={null}
        isStartingJob={false}
        jobStartError={null}
        onUploadFile={vi.fn()}
        onUpdateJobConfig={onUpdate}
        onOpenProtectionScreen={vi.fn()}
        onStartTranslationJob={vi.fn()}
      />
    );
    expect(screen.getByTestId("unsupported-pdf-alert")).toHaveTextContent("無断OCR");
    expect(screen.getByTestId("start-job-btn")).toBeDisabled();
  });
});

describe("Screen 2: PDF Viewer & SVG Protection Editor (Section 6)", () => {
  it("renders legend with icons/patterns, supports drag creation, and rejects tiny accidental clicks", async () => {
    const onAddRegion = vi.fn().mockResolvedValue(undefined);
    const onDeleteRegion = vi.fn().mockResolvedValue(undefined);
    const regions: ProtectionRegion[] = [
      {
        id: "reg-001",
        document_id: SAMPLE_DOC.id,
        page_index: 0,
        bbox: [50, 50, 200, 120],
        mode: "do_not_translate",
        source: "manual",
        note: "定理の可換図",
        created_at: "2026-11-06T12:00:00Z",
      },
    ];

    render(
      <Screen2ProtectionEditor
        document={SAMPLE_DOC}
        pdfBytes={null}
        blocks={SAMPLE_BLOCKS}
        protectionRegions={regions}
        isParsing={false}
        saveStatus="saved"
        saveMessage={null}
        onReparseBlocks={vi.fn()}
        onAddProtectionRegion={onAddRegion}
        onDeleteProtectionRegion={onDeleteRegion}
        onProceedToStartJob={vi.fn()}
      />
    );

    expect(screen.getByTestId("page-indicator")).toHaveTextContent("ページ 1 / 2");
    expect(screen.getByTestId("block-legend")).toHaveTextContent("[∑]");
    expect(screen.getByTestId("block-legend")).toHaveTextContent("[🔒手動]");

    const svg = screen.getByTestId("pdf-svg-overlay");
    vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 595,
      bottom: 842,
      width: 595,
      height: 842,
      toJSON: () => ({}),
    });

    // 1. Tiny click (< 8px) -> skipped with notice
    fireEvent.mouseDown(svg, { clientX: 100, clientY: 100 });
    fireEvent.mouseUp(svg, { clientX: 103, clientY: 102 });
    expect(onAddRegion).not.toHaveBeenCalled();
    expect(screen.getByTestId("screen2-notice-banner")).toHaveTextContent("極小");

    // 2. Valid reverse drag (bottom-right 300,260 -> top-left 100,120) -> normalized & added
    fireEvent.mouseDown(svg, { clientX: 300, clientY: 260 });
    fireEvent.mouseMove(svg, { clientX: 100, clientY: 120 });
    fireEvent.mouseUp(svg, { clientX: 100, clientY: 120 });

    await waitFor(() => {
      expect(onAddRegion).toHaveBeenCalledWith({
        page_index: 0,
        bbox: [100, 120, 300, 260],
        mode: "do_not_translate",
        note: null,
      });
    });

    // 3. Delete existing region
    fireEvent.click(screen.getByTestId("delete-region-reg-001"));
    expect(onDeleteRegion).toHaveBeenCalledWith("reg-001");
  });
});

describe("Screen 3: 13-Stage Progress, Cancel Distinction, Manual Edit & Retypeset (Section 7)", () => {
  const sampleJob: Job = {
    id: "job-xyz987654321",
    document_id: SAMPLE_DOC.id,
    state: "translating",
    current_stage: "Stage 8: PLaMo PFML3 段落翻訳中",
    config: DEFAULT_CFG,
    total_pages: 2,
    processed_pages: 1,
    total_blocks: 2,
    processed_blocks: 1,
    retry_count: 0,
    warning_count: 1,
    error_message: null,
    original_sha256: SAMPLE_DOC.sha256_hash,
    original_hash_verified: true,
    is_mock_run: false,
    cancel_requested: true,
    glossary_version_used: null,
    retypeset_revision: 0,
    based_on_edit_revision: 0,
    based_on_protection_revision: 1,
    applied_block_revisions: {},
    execution_provenance: {},
    created_at: "2026-11-06T12:00:00Z",
    updated_at: "2026-11-06T12:01:00Z",
    completed_at: null,
  };

  const sampleIssues: ValidationIssue[] = [
    {
      id: "iss-page2",
      job_id: sampleJob.id,
      document_id: SAMPLE_DOC.id,
      page_index: 1,
      block_id: "doc-abc123456789:page-2:block-1",
      bbox: [70, 100, 500, 160],
      code: "TYPESETTING_OVERFLOW_FALLBACK",
      severity: "warning",
      message: "最小フォントサイズ超過のため原文保持",
      details: {},
      fallback_applied: true,
      created_at: "2026-11-06T12:01:00Z",
    },
  ];

  const sampleArtifacts: ExportArtifact[] = [
    {
      id: "art-mono",
      job_id: sampleJob.id,
      document_id: SAMPLE_DOC.id,
      artifact_type: "mono_ja_pdf",
      file_path: "C:/runtime/outputs/doc.ja.pdf",
      sha256_hash: "11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff",
      file_size_bytes: 135000,
      is_finalized: true,
      is_mock_translation: false,
      created_at: "2026-11-06T12:01:00Z",
    },
  ];

  it("distinguishes cancel_requested vs canceled, validates manual edits live, warns on unsaved switch, and triggers zero-LLM retypeset", async () => {
    const onSaveManual = vi.fn().mockImplementation(async (_id, txt) => ({
      ...SAMPLE_BLOCKS[0],
      manual_translation_text: txt,
      status: "manually_edited",
      edit_revision: 1,
    }));
    const onRetypeset = vi.fn().mockResolvedValue(undefined);

    const { rerender } = render(
      <Screen3ProgressReview
        document={SAMPLE_DOC}
        originalPdfBytes={null}
        translatedPdfBytes={null}
        job={sampleJob}
        blocks={SAMPLE_BLOCKS}
        issues={sampleIssues}
        artifacts={sampleArtifacts}
        isCanceling={false}
        isResuming={false}
        isRetypesetting={false}
        isRetranslatingBlock={false}
        retypesetMessage={null}
        retypesetError={null}
        onCancelJob={vi.fn()}
        onResumeJob={vi.fn()}
        onSaveManualEdit={onSaveManual}
        onRetranslateBlock={vi.fn()}
        onRetypesetJob={onRetypeset}
        onDownloadArtifact={vi.fn()}
      />
    );

    // 1. Verify "停止要求中" badge is shown while translating with cancel_requested=true
    expect(screen.getByTestId("cancel-requested-badge")).toHaveTextContent("停止要求中");

    // 2. Transition job to "canceled" -> verify "停止完了" badge and Resume button appear
    rerender(
      <Screen3ProgressReview
        document={SAMPLE_DOC}
        originalPdfBytes={null}
        translatedPdfBytes={null}
        job={{ ...sampleJob, state: "canceled" }}
        blocks={SAMPLE_BLOCKS}
        issues={sampleIssues}
        artifacts={sampleArtifacts}
        isCanceling={false}
        isResuming={false}
        isRetypesetting={false}
        isRetranslatingBlock={false}
        retypesetMessage={null}
        retypesetError={null}
        onCancelJob={vi.fn()}
        onResumeJob={vi.fn()}
        onSaveManualEdit={onSaveManual}
        onRetranslateBlock={vi.fn()}
        onRetypesetJob={onRetypeset}
        onDownloadArtifact={vi.fn()}
      />
    );
    expect(screen.getByTestId("cancel-completed-badge")).toHaveTextContent("停止完了");
    expect(screen.getByTestId("resume-job-btn")).toBeInTheDocument();

    // 3. Edit manual translation removing {v1} -> live pre-save error shown, while model translation remains untouched
    const textarea = screen.getByTestId("manual-translation-textarea");
    fireEvent.change(textarea, {
      target: { value: "プレースホルダを消した未保存の手動訳文" },
    });
    expect(screen.getByTestId("inspector-model-translation")).toHaveTextContent(
      "100ステップにおいて {v1} が与えられたとする [1, 2]。"
    );
    expect(screen.getByTestId("manual-validation-errors")).toHaveTextContent(
      "PLACEHOLDER_MISSING"
    );
    expect(screen.getByTestId("manual-validation-warnings")).toHaveTextContent(
      "CITATION_REF_MISSING"
    );

    // 4. Attempt to jump to issue on Page 2 while dirty -> unsaved warning appears
    fireEvent.click(screen.getByTestId("jump-to-issue-iss-page2"));
    expect(screen.getByTestId("unsaved-leave-warning")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("cancel-switch-btn"));

    // 5. Fix manual translation and save, then trigger zero-LLM retypeset
    fireEvent.change(textarea, {
      target: { value: "100ステップにおいて {v1} が与えられた場合の修正訳 [1, 2]。" },
    });
    expect(screen.queryByTestId("manual-validation-errors")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("save-manual-edit-btn"));
    await waitFor(() => {
      expect(onSaveManual).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByTestId("retypeset-btn"));
    expect(onRetypeset).toHaveBeenCalledWith(true);

    // 6. Transition job to "interrupted" with connection error -> verify job-error-banner and guidance
    rerender(
      <Screen3ProgressReview
        document={SAMPLE_DOC}
        originalPdfBytes={null}
        translatedPdfBytes={null}
        job={{
          ...sampleJob,
          state: "interrupted",
          current_stage: "推論サーバー切断により一時停止",
          error_message: "推論サーバー接続中断（再開可能）: Connection refused",
        }}
        blocks={SAMPLE_BLOCKS}
        issues={sampleIssues}
        artifacts={sampleArtifacts}
        isCanceling={false}
        isResuming={false}
        isRetypesetting={false}
        isRetranslatingBlock={false}
        retypesetMessage={null}
        retypesetError={null}
        onCancelJob={vi.fn()}
        onResumeJob={vi.fn()}
        onSaveManualEdit={onSaveManual}
        onRetranslateBlock={vi.fn()}
        onRetypesetJob={onRetypeset}
        onDownloadArtifact={vi.fn()}
      />
    );
    const errBanner = screen.getByTestId("job-error-banner");
    expect(errBanner).toHaveTextContent("推論サーバ接続中断（再開可能）:");
    expect(errBanner).toHaveTextContent("推論サーバー接続中断（再開可能）: Connection refused");
    expect(errBanner).toHaveTextContent("llama-server");
    expect(errBanner).toHaveTextContent("翻訳リクエストは送信しません");
    expect(errBanner).toHaveTextContent("中断箇所からジョブを再開 (Resume)");

    // 7. Non-connection failure (e.g. failed state with sensitive token / prompt / path) ->
    // does not treat as server connection error, does not recommend server start as cure-all,
    // does not recommend Resume on non-resumable state, and sanitizes sensitive strings.
    rerender(
      <Screen3ProgressReview
        document={SAMPLE_DOC}
        originalPdfBytes={null}
        translatedPdfBytes={null}
        job={{
          ...sampleJob,
          state: "failed",
          current_stage: "エラー発生: RuntimeError",
          error_message:
            'RuntimeError at C:\\Users\\secret_alice\\doc.pdf X-PaperBridge-Token: super_secret_token_999 {"source_text":"Confidential input paragraph"} <|plamo:op|>dataset translation text secret',
        }}
        blocks={SAMPLE_BLOCKS}
        issues={sampleIssues}
        artifacts={sampleArtifacts}
        isCanceling={false}
        isResuming={false}
        isRetypesetting={false}
        isRetranslatingBlock={false}
        retypesetMessage={null}
        retypesetError={null}
        onCancelJob={vi.fn()}
        onResumeJob={vi.fn()}
        onSaveManualEdit={onSaveManual}
        onRetranslateBlock={vi.fn()}
        onRetypesetJob={onRetypeset}
        onDownloadArtifact={vi.fn()}
      />
    );
    const failedBanner = screen.getByTestId("job-error-banner");
    expect(failedBanner).toHaveTextContent("ジョブエラー:");
    expect(failedBanner).toHaveTextContent("推論サーバ未接続によるものではありません");
    expect(failedBanner).not.toHaveTextContent("別ターミナルで PLaMo モデルを指定して");
    expect(failedBanner).not.toHaveTextContent("super_secret_token_999");
    expect(failedBanner).not.toHaveTextContent("secret_alice");
    expect(failedBanner).not.toHaveTextContent("Confidential input paragraph");
    expect(failedBanner).not.toHaveTextContent("dataset translation text secret");
    expect(failedBanner).toHaveTextContent("X-PaperBridge-Token=[REDACTED]");
    expect(failedBanner).toHaveTextContent("C:\\Users\\[USER]");

    // 8. Non-resumable state with error_message -> does not recommend Resume
    rerender(
      <Screen3ProgressReview
        document={SAMPLE_DOC}
        originalPdfBytes={null}
        translatedPdfBytes={null}
        job={{
          ...sampleJob,
          state: "translating",
          cancel_requested: false,
          current_stage: "translating",
          error_message: "推論サーバー接続中断（再開可能）: Connection refused",
        }}
        blocks={SAMPLE_BLOCKS}
        issues={sampleIssues}
        artifacts={sampleArtifacts}
        isCanceling={false}
        isResuming={false}
        isRetypesetting={false}
        isRetranslatingBlock={false}
        retypesetMessage={null}
        retypesetError={null}
        onCancelJob={vi.fn()}
        onResumeJob={vi.fn()}
        onSaveManualEdit={onSaveManual}
        onRetranslateBlock={vi.fn()}
        onRetypesetJob={onRetypeset}
        onDownloadArtifact={vi.fn()}
      />
    );
    expect(screen.queryByTestId("resume-job-btn")).not.toBeInTheDocument();
    expect(screen.getByTestId("job-error-recovery-guide")).not.toHaveTextContent(
      "中断箇所からジョブを再開 (Resume)"
    );
  });
});

describe("Screen 4: Glossary CRUD, TSV Import Preview & Dynamic Diagnostics (Section 8 & 9)", () => {
  const sampleDiag: SystemDiagnostics = {
    os_name: "Windows",
    os_version: "10.0.26200",
    cpu_name: "AMD Ryzen 7",
    cpu_cores: 8,
    cpu_threads: 16,
    ram_total_mb: 32768,
    gpu_name: "AMD Radeon RX 7600",
    gpu_vram_mb: 8192,
    gpu_driver_version: "32.0.12033",
    python_version: "3.13.13",
    pdf_engine_primary: "native_preserving_pymupdf",
    babeldoc_installed: false,
    babeldoc_version: null,
    babeldoc_assets_cached: false,
    japanese_font_available: true,
    japanese_font_path: "assets/fonts/NotoSerifJP-VF.ttf",
    llama_server_url: "http://127.0.0.1:8089/v1",
    llama_connection_status: "connected",
    loaded_model_name: "plamo-3-translate",
    loopback_only_enforced: true,
  };

  it("previews TSV import with row errors and displays dynamic diagnostics accurately", () => {
    const sampleGlossary: Glossary = {
      id: "gls-001",
      name: "ML Glossary",
      source_lang: "en",
      target_lang: "ja",
      version: 1,
      entries: [{ source: "gradient", target: "勾配", notes: null }],
      updated_at: "2026-11-06T12:00:00Z",
    };

    render(
      <Screen4GlossaryDiagnostics
        glossaries={[sampleGlossary]}
        diagnostics={sampleDiag}
        hasProbedLlama={true}
        isLoadingDiagnostics={false}
        diagnosticsError={null}
        onRefreshDiagnostics={vi.fn()}
        onCreateGlossary={vi.fn()}
        onUpdateGlossary={vi.fn()}
        onDeleteGlossary={vi.fn()}
        onPurgeTempData={vi.fn().mockResolvedValue("C:/runtime/working")}
        onRotateToken={vi.fn().mockResolvedValue(undefined)}
      />
    );

    // Verify dynamic diagnostics come strictly from props
    expect(screen.getByTestId("diag-gpu")).toHaveTextContent("AMD Radeon RX 7600");
    expect(screen.getByTestId("diag-llama-status")).toHaveTextContent(
      "実機接続正常 (connected — モデル: plamo-3-translate)"
    );
    expect(screen.queryByTestId("diag-llama-disconnected-guide")).not.toBeInTheDocument();

    // Verify PLaMo model & license guidance panel and static links
    const licensePanel = screen.getByTestId("plamo-model-license-panel");
    expect(licensePanel).toHaveTextContent("PLaMo 3 Translate");
    expect(licensePanel).toHaveTextContent("Built with PLaMo");
    expect(licensePanel).toHaveTextContent("Preferred Networks（PFN）の公式製品ではありません");
    expect(licensePanel).toHaveTextContent("AGPL-3.0-only");
    expect(licensePanel).toHaveTextContent("同梱しておらず、利用者が別途取得");
    expect(licensePanel).toHaveTextContent("PLaMo 3 Translate の適用条件（2026年11月9日改定に関するPFN回答に基づく案内）");
    expect(licensePanel).toHaveTextContent("契約条件を確認・遵守");
    expect(licensePanel).toHaveTextContent("書籍・論文などの独立した長文公表物に関する場合を除き");
    expect(licensePanel).toHaveTextContent(
      "ただし書籍・論文などの独立した長文公表物の全部または実質的部分として公表する場合は、PLaMoを翻訳に利用した旨をPDF内に含めて明示する必要があります"
    );
    expect(licensePanel).toHaveTextContent("PDF末尾の説明ページでも対応可能");
    expect(licensePanel).toHaveTextContent("説明ページを自動追加しません");
    expect(licensePanel).toHaveTextContent("原著作物の翻訳・配布・公表の許諾まで与えるものではありません");
    expect(screen.getByTestId("link-plamo-model-repo")).toHaveAttribute(
      "href",
      "https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF"
    );
    expect(screen.getByTestId("link-plamo-license-ja")).toHaveAttribute(
      "href",
      "https://plamo.preferredai.jp/info/plamo-community-license-ja"
    );
    expect(screen.getByTestId("link-plamo-license-en")).toHaveAttribute(
      "href",
      "https://plamo.preferredai.jp/info/plamo-community-license-en"
    );

    // Enter TSV with 1 valid row and 1 invalid row -> preview shows both
    fireEvent.change(screen.getByTestId("glossary-import-textarea"), {
      target: {
        value: "variational inference\t変分推論\nmalformed_single_column_row",
      },
    });
    fireEvent.click(screen.getByTestId("preview-import-btn"));

    expect(screen.getByTestId("import-valid-count")).toHaveTextContent("1件");
    expect(screen.getByTestId("import-error-count")).toHaveTextContent("1件");
    expect(screen.getByTestId("import-row-errors")).toHaveTextContent("2行目: 列数が不足");
  });

  it("shows model-disconnected guidance when llama_connection_status is not connected", () => {
    render(
      <Screen4GlossaryDiagnostics
        glossaries={[]}
        diagnostics={{
          ...sampleDiag,
          llama_connection_status: "server_unreachable",
          loaded_model_name: null,
        }}
        hasProbedLlama={true}
        isLoadingDiagnostics={false}
        diagnosticsError={null}
        onRefreshDiagnostics={vi.fn()}
        onCreateGlossary={vi.fn()}
        onUpdateGlossary={vi.fn()}
        onDeleteGlossary={vi.fn()}
        onPurgeTempData={vi.fn().mockResolvedValue("C:/runtime/working")}
        onRotateToken={vi.fn().mockResolvedValue(undefined)}
      />
    );

    expect(screen.getByTestId("diag-llama-status")).toHaveTextContent(
      "推論サーバ未接続 (server_unreachable)"
    );
    const guide = screen.getByTestId("diag-llama-disconnected-guide");
    expect(guide).toHaveTextContent("llama-server");
    expect(guide).toHaveTextContent("翻訳リクエストは送信しません");
    expect(guide).toHaveTextContent("モックは決定論的な疑似翻訳であり実翻訳ではありません");
  });
});
