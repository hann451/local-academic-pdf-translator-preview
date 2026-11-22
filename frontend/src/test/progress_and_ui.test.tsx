import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Block, Document, ExportArtifact, Job, TranslationJobConfig } from "../types/api";
import { Screen3ProgressReview } from "../components/Screen3ProgressReview";
import { App } from "../App";

const mockApi = vi.hoisted(() => ({
  ensureSessionToken: vi.fn(async () => "tok-test"),
  fetchGlossaries: vi.fn(async () => []),
  fetchSystemDiagnostics: vi.fn(async () => ({
    os_name: "Windows",
    os_version: "11",
    cpu_name: "Ryzen",
    cpu_cores: 8,
    cpu_threads: 16,
    ram_total_mb: 32768,
    gpu_name: "Radeon",
    gpu_vram_mb: 8192,
    gpu_driver_version: "1.0",
    python_version: "3.13",
    pdf_engine_primary: "PyMuPDF",
    babeldoc_installed: false,
    babeldoc_version: null,
    babeldoc_assets_cached: false,
    japanese_font_available: true,
    japanese_font_path: "NotoSerifJP",
    llama_server_url: "http://127.0.0.1:8089",
    llama_connection_status: "connected",
    loaded_model_name: "plamo",
    loopback_only_enforced: true,
    active_model_status: "ready",
    active_model_display_name: "PLaMo",
    model_server_match_status: "matched",
    model_server_mismatch: false,
  })),
  fetchModelsOverview: vi.fn(async () => ({
    catalog: [],
    registered: [],
    active_model_id: null,
    active_model: null,
    server_match: null,
    downloads: [],
    llama_server_url: "http://127.0.0.1:8089",
    server_status: "connected",
    has_active_translation_job: false,
    models_dir: "C:\\models",
  })),
  uploadPdfDocument: vi.fn(),
  fetchDocumentPdfBytes: vi.fn(async () => ({
    data: new Uint8Array([37, 80, 68, 70]),
    sha256: "docsha",
  })),
  parseDocumentBlocks: vi.fn(async () => []),
  fetchProtectionRegions: vi.fn(async () => []),
  fetchBlockSplits: vi.fn(async () => []),
  fetchDocument: vi.fn(),
  startTranslationJob: vi.fn(),
  fetchJob: vi.fn(),
  fetchDocumentBlocks: vi.fn(async () => []),
  fetchJobIssues: vi.fn(async () => []),
  fetchJobArtifacts: vi.fn(),
  fetchArtifactPdfBytes: vi.fn(async () => ({
    data: new Uint8Array([37, 80, 68, 70, 45]),
    sha256: "monosha",
  })),
  fetchModelDownload: vi.fn(),
  cancelTranslationJob: vi.fn(),
  resumeTranslationJob: vi.fn(),
  saveBlockManualEdit: vi.fn(),
  retranslateSpecificBlocks: vi.fn(),
  retypesetJob: vi.fn(),
  downloadArtifactFile: vi.fn(),
  createGlossary: vi.fn(),
  updateGlossary: vi.fn(),
  deleteGlossary: vi.fn(),
  createProtectionRegion: vi.fn(),
  deleteProtectionRegion: vi.fn(),
  createBlockSplit: vi.fn(),
  deleteBlockSplit: vi.fn(),
  previewBlockSplit: vi.fn(),
  updateBlockProtection: vi.fn(),
  reapplyAutoProtection: vi.fn(),
  selectActiveModel: vi.fn(),
  registerLocalModel: vi.fn(),
  browseLocalModelFile: vi.fn(),
  deleteRegisteredModel: vi.fn(),
  startModelDownload: vi.fn(),
  cancelModelDownload: vi.fn(),
  restartLlamaServerForActiveModel: vi.fn(),
  purgeRuntimeTempData: vi.fn(),
  rotateSessionToken: vi.fn(),
  checkSystemUpdate: vi.fn(),
  applySystemUpdate: vi.fn(),
}));

vi.mock("../api/client", () => ({
  ApiHttpError: class ApiHttpError extends Error {
    status: number;
    detail: string;
    constructor(status: number, detail: string) {
      super(detail);
      this.status = status;
      this.detail = detail;
    }
  },
  ...mockApi,
}));

vi.mock("../components/PdfPageCanvas", () => ({
  PdfPageCanvas: ({ children }: { children?: React.ReactNode }) => (
    <div data-testid="mock-pdf-canvas">{children}</div>
  ),
}));

const BASE_DOC: Document = {
  id: "doc-progress-1",
  original_filename: "sample.pdf",
  safe_storage_path: "/tmp/sample.pdf",
  sha256_hash: "abc123",
  file_size_bytes: 1024,
  page_count: 10,
  pages: [
    {
      document_id: "doc-progress-1",
      page_index: 0,
      width_pt: 595,
      height_pt: 842,
      rotation_deg: 0,
      media_box: [0, 0, 595, 842],
      crop_box: [0, 0, 595, 842],
      has_extractable_text: true,
      is_scanned_like: false,
      cid_ratio: 0,
      column_count: 1,
    },
  ],
  is_scanned_pdf: false,
  is_corrupted: false,
  is_encrypted: false,
  extractable_Sync_status: "ok",
  unsupported_reason: null,
  protection_revision: 0,
  edit_revision: 0,
  created_at: "2026-11-22T07:00:00Z",
};

const BASE_CFG: TranslationJobConfig = {
  page_range_spec: null,
  source_lang: "en",
  target_lang: "ja",
  form: "plain",
  model_id: null,
  model_name: "plamo-3-translate-0.8b-Q4_K_M",
  model_sha256: null,
  quantization: "Q4_K_M",
  context_length: 8192,
  translate_captions: true,
  translate_tables: false,
  translate_references: false,
  translate_figures: false,
  min_font_size_pt: 8,
  glossary_id: null,
  output_mono_pdf: true,
  output_dual_pdf: true,
  only_roundtrip_preservation: false,
  pause_for_review: false,
  use_mock_translator: true,
};

const PENDING_BLK: Block = {
  id: "blk-1",
  page_index: 0,
  bbox: [50, 100, 300, 140],
  kind: "paragraph",
  reading_order: 0,
  column_index: 0,
  source_text: "Sample sentence.",
  masked_source_text: "Sample sentence.",
  protected_items: [],
  translation_text: null,
  manual_translation_text: null,
  translation_provenance: "none",
  translation_is_mock: false,
  status: "pending",
  font_size_pt: 10,
  fitted_font_size_pt: null,
  protection_source: "auto",
  semantic_category: "body",
  protection_reason: "",
  protection_rule_id: "",
  protection_confidence: "high",
  user_protection_override: null,
  is_uncertain_region: false,
  edit_revision: 0,
  warnings: [],
  needs_retranslation: false,
  manual_validation_errors: [],
  manual_validation_warnings: [],
};

describe("Progress Hero, ETA & Polling Storm Prevention", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders %, live stage detail, ETA, elapsed time and hides unprotected-pending-notice while running", () => {
    const runningJob: Job = {
      id: "job-running",
      document_id: BASE_DOC.id,
      state: "translating",
      current_stage: "Stage 8: 翻訳中",
      stage_detail: "Stage 8: 翻訳中 120/200 ブロック (P.6)",
      progress_percent: 64.2,
      estimated_remaining_seconds: 85,
      config: BASE_CFG,
      total_pages: 10,
      processed_pages: 5,
      total_blocks: 200,
      processed_blocks: 120,
      retry_count: 0,
      warning_count: 0,
      error_message: null,
      original_sha256: "abc123",
      original_hash_verified: true,
      is_mock_run: true,
      cancel_requested: false,
      glossary_version_used: null,
      retypeset_revision: 0,
      based_on_edit_revision: 0,
      based_on_protection_revision: 0,
      applied_block_revisions: {},
      execution_provenance: {},
      created_at: "2026-11-22T07:00:00Z",
      started_at: "2026-11-22T07:01:00Z",
      updated_at: "2026-11-22T07:02:00Z",
      completed_at: null,
    };

    const { rerender } = render(
      <Screen3ProgressReview
        document={BASE_DOC}
        originalPdfBytes={null}
        translatedPdfBytes={null}
        job={runningJob}
        blocks={[PENDING_BLK]}
        issues={[]}
        artifacts={[]}
        isCanceling={false}
        isResuming={false}
        isRetypesetting={false}
        isRetranslatingBlock={false}
        retypesetMessage={null}
        retypesetError={null}
        onCancelJob={vi.fn(async () => {})}
        onResumeJob={vi.fn(async () => {})}
        onSaveManualEdit={vi.fn(async () => PENDING_BLK)}
        onRetranslateBlock={vi.fn(async () => {})}
        onRetypesetJob={vi.fn(async () => {})}
        onDownloadArtifact={vi.fn(async () => {})}
      />
    );

    expect(screen.getByTestId("job-progress-percent")).toHaveTextContent("64%");
    expect(screen.getByTestId("job-stage-detail")).toHaveTextContent(
      "Stage 8: 翻訳中 120/200 ブロック (P.6)"
    );
    expect(screen.getByTestId("job-eta")).toHaveTextContent("残り約 1分 25秒");
    expect(screen.getByTestId("job-elapsed-time")).toHaveTextContent("経過時間:");
    expect(screen.queryByTestId("unprotected-pending-notice")).toBeNull();

    rerender(
      <Screen3ProgressReview
        document={BASE_DOC}
        originalPdfBytes={null}
        translatedPdfBytes={null}
        job={{
          ...runningJob,
          state: "completed_with_warnings",
          progress_percent: 100,
          estimated_remaining_seconds: 0,
          completed_at: "2026-11-22T07:05:00Z",
        }}
        blocks={[PENDING_BLK]}
        issues={[]}
        artifacts={[]}
        isCanceling={false}
        isResuming={false}
        isRetypesetting={false}
        isRetranslatingBlock={false}
        retypesetMessage={null}
        retypesetError={null}
        onCancelJob={vi.fn(async () => {})}
        onResumeJob={vi.fn(async () => {})}
        onSaveManualEdit={vi.fn(async () => PENDING_BLK)}
        onRetranslateBlock={vi.fn(async () => {})}
        onRetypesetJob={vi.fn(async () => {})}
        onDownloadArtifact={vi.fn(async () => {})}
      />
    );

    expect(screen.getByTestId("job-progress-percent")).toHaveTextContent("100%");
    expect(screen.getByTestId("job-eta")).toHaveTextContent("完了");
    expect(screen.getByTestId("job-elapsed-time")).toHaveTextContent("4分 00秒");
    expect(screen.getByTestId("unprotected-pending-notice")).toBeInTheDocument();
  });

  it("avoids re-downloading mono_ja_pdf bytes on repeated poll/refresh cycles with unchanged SHA-256", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const monoArt: ExportArtifact = {
      id: "art-mono-1",
      job_id: "job-poll",
      document_id: BASE_DOC.id,
      artifact_type: "mono_ja_pdf",
      file_path: "/tmp/out.ja.pdf",
      sha256_hash: "fixed-sha-111",
      file_size_bytes: 512,
      is_finalized: true,
      is_mock_translation: true,
      created_at: "2026-11-22T07:02:00Z",
    };
    const activeJob: Job = {
      id: "job-poll",
      document_id: BASE_DOC.id,
      state: "translating",
      current_stage: "translating",
      stage_detail: "翻訳中 1/2",
      progress_percent: 40,
      estimated_remaining_seconds: 10,
      config: BASE_CFG,
      total_pages: 10,
      processed_pages: 4,
      total_blocks: 2,
      processed_blocks: 1,
      retry_count: 0,
      warning_count: 0,
      error_message: null,
      original_sha256: "abc123",
      original_hash_verified: true,
      is_mock_run: true,
      cancel_requested: false,
      glossary_version_used: null,
      retypeset_revision: 0,
      based_on_edit_revision: 0,
      based_on_protection_revision: 0,
      applied_block_revisions: {},
      execution_provenance: {},
      created_at: "2026-11-22T07:00:00Z",
      updated_at: "2026-11-22T07:01:00Z",
      completed_at: null,
    };

    mockApi.uploadPdfDocument.mockResolvedValue(BASE_DOC);
    mockApi.fetchDocument.mockResolvedValue(BASE_DOC);
    mockApi.startTranslationJob.mockResolvedValue(activeJob);
    mockApi.fetchJob.mockResolvedValue(activeJob);
    mockApi.fetchJobArtifacts.mockResolvedValue([monoArt]);

    render(<App />);

    const fileInput = screen.getByTestId("pdf-file-input") as HTMLInputElement;
    const file = new File(["%PDF"], "sample.pdf", { type: "application/pdf" });
    await act(async () => {
      fireEvent.change(fileInput, { target: { files: [file] } });
    });

    await waitFor(() => expect(screen.getByTestId("start-job-btn")).toBeEnabled());
    await act(async () => {
      fireEvent.click(screen.getByTestId("start-job-btn"));
    });

    await waitFor(() => expect(screen.getByTestId("screen3-progress-review")).toBeInTheDocument());
    expect(mockApi.fetchArtifactPdfBytes).toHaveBeenCalledTimes(1);

    // Advance 7 ticks (5600ms -> crosses 6-tick detail refresh boundary once)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5600);
    });

    // fetchJob polled frequently (~7 calls), while mono_ja_pdf bytes stayed cached (still 1 call)
    expect(mockApi.fetchJob.mock.calls.length).toBeGreaterThanOrEqual(5);
    expect(mockApi.fetchArtifactPdfBytes).toHaveBeenCalledTimes(1);
  });
});
