import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Screen1HomePreflight } from "../components/Screen1HomePreflight";
import { Screen4GlossaryDiagnostics } from "../components/Screen4GlossaryDiagnostics";
import { ModelLicenseNotice } from "../components/ModelLicenseNotice";
import type {
  CatalogModel,
  ModelDownloadJob,
  ModelsOverviewResponse,
  RegisteredModel,
  SystemDiagnostics,
  TranslationJobConfig,
} from "../types/api";

function makeCatalog(): CatalogModel[] {
  const baseLicense = {
    id: "plamo-community-license",
    name: "PLaMoコミュニティライセンス契約",
    tagline: "Built with PLaMo",
    url_ja: "https://plamo.preferredai.jp/info/plamo-community-license-ja",
    url_en: "https://plamo.preferredai.jp/info/plamo-community-license-en",
    model_card_url: "https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF",
    summary_ja: "テスト用ライセンス要約",
    disclosure_note_ja: "Built with PLaMo 注記",
    requires_agreement: true,
  };
  return [
    {
      model_id: "plamo-3-translate-0.8b-q4_k_m",
      display_name: "PLaMo 3 Translate 0.8B (Q4_K_M)",
      short_name: "PLaMo 3 Translate 0.8B",
      provider: "Preferred Networks, Inc.",
      family: "plamo-3-translate",
      parameter_size: "0.8B",
      quantization: "Q4_K_M",
      is_default: false,
      source_repo: "pfnet/plamo-3-translate-0.8b-GGUF",
      source_repo_type: "huggingface",
      pinned_revision: "1cf96ae02133958a20074bfdfab76517bb737d00",
      filename: "plamo-3-translate-0.8b-Q4_K_M.gguf",
      relative_subdir: "plamo-3-translate-0.8b-GGUF",
      download_url: "https://huggingface.co/pfnet/plamo-3-translate-0.8b-GGUF/resolve/main/0.8b.gguf",
      model_card_url: "https://huggingface.co/pfnet/plamo-3-translate-0.8b-GGUF",
      known_size_bytes: 573126848,
      known_sha256: "70891ec70dc7ab4205745a38307867c2881b99239f20327927633abfe4bc9ec6",
      accepted_artifacts: [],
      supported_backends: ["llama.cpp"],
      expected_gguf_architecture: "plamo3",
      expected_gguf_name_prefix: "plamo-3-translate-0.8b",
      prompt_format: "pfml3",
      prompt_version: "pfml3-v1",
      stop_tokens: [],
      context_length_default: 8192,
      context_length_max: 32768,
      launch_settings: {
        alias: "plamo-3-translate-0.8b-q4_k_m",
        context_length: 8192,
        parallel_slots: 1,
        gpu_layers_auto: 99,
        extra_args: [],
      },
      catalog_verification_status: "catalog_verified_runtime_unverified",
      hardware_note_ja: "軽量候補",
      license: { ...baseLicense, model_card_url: "https://huggingface.co/pfnet/plamo-3-translate-0.8b-GGUF" },
    },
    {
      model_id: "plamo-3-translate-2b-q4_k_m",
      display_name: "PLaMo 3 Translate 2B (Q4_K_M)",
      short_name: "PLaMo 3 Translate 2B",
      provider: "Preferred Networks, Inc.",
      family: "plamo-3-translate",
      parameter_size: "2B",
      quantization: "Q4_K_M",
      is_default: true,
      source_repo: "pfnet/plamo-3-translate-2b-GGUF",
      source_repo_type: "huggingface",
      pinned_revision: "4f672106b847200bfdc0f6af6c321cd94eba1aed",
      filename: "plamo-3-translate-2b-Q4_K_M.gguf",
      relative_subdir: "plamo-3-translate-2b-GGUF",
      download_url: "https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF/resolve/main/2b.gguf",
      model_card_url: "https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF",
      known_size_bytes: 1807203040,
      known_sha256: "1ffd9073afcc70e35ccc942428aa2b73d7aa2b9ea779bf504b775369e0276c13",
      accepted_artifacts: [],
      supported_backends: ["llama.cpp"],
      expected_gguf_architecture: "plamo3",
      expected_gguf_name_prefix: "plamo-3-translate-2b",
      prompt_format: "pfml3",
      prompt_version: "pfml3-v1",
      stop_tokens: [],
      context_length_default: 8192,
      context_length_max: 32768,
      launch_settings: {
        alias: "plamo-3-translate-2b-q4_k_m",
        context_length: 8192,
        parallel_slots: 1,
        gpu_layers_auto: 99,
        extra_args: [],
      },
      catalog_verification_status: "verified_runtime",
      hardware_note_ja: "標準推奨",
      license: baseLicense,
    },
    {
      model_id: "plamo-3-translate-8b-q4_k_m",
      display_name: "PLaMo 3 Translate 8B (Q4_K_M)",
      short_name: "PLaMo 3 Translate 8B",
      provider: "Preferred Networks, Inc.",
      family: "plamo-3-translate",
      parameter_size: "8B",
      quantization: "Q4_K_M",
      is_default: false,
      source_repo: "pfnet/plamo-3-translate-8b-GGUF",
      source_repo_type: "huggingface",
      pinned_revision: "c67efa4eefdc14d3c97bf8f4f35ee58e2ecd2f42",
      filename: "plamo-3-translate-8b-Q4_K_M.gguf",
      relative_subdir: "plamo-3-translate-8b-GGUF",
      download_url: "https://huggingface.co/pfnet/plamo-3-translate-8b-GGUF/resolve/main/8b.gguf",
      model_card_url: "https://huggingface.co/pfnet/plamo-3-translate-8b-GGUF",
      known_size_bytes: 5280445728,
      known_sha256: "94e5d3f91b421f7c8900ddaac27b11a19f7441426c954bbb25a2fd60a1640187",
      accepted_artifacts: [],
      supported_backends: ["llama.cpp"],
      expected_gguf_architecture: "plamo3",
      expected_gguf_name_prefix: "plamo-3-translate-8b",
      prompt_format: "pfml3",
      prompt_version: "pfml3-v1",
      stop_tokens: [],
      context_length_default: 8192,
      context_length_max: 32768,
      launch_settings: {
        alias: "plamo-3-translate-8b-q4_k_m",
        context_length: 8192,
        parallel_slots: 1,
        gpu_layers_auto: 99,
        extra_args: [],
      },
      catalog_verification_status: "catalog_verified_runtime_unverified",
      hardware_note_ja: "大容量VRAM推奨",
      license: { ...baseLicense, model_card_url: "https://huggingface.co/pfnet/plamo-3-translate-8b-GGUF" },
    },
  ];
}

function makeRegistered(overrides: Partial<RegisteredModel> = {}): RegisteredModel {
  return {
    registration_id: "reg-2b-1",
    catalog_model_id: "plamo-3-translate-2b-q4_k_m",
    display_name: "PLaMo 3 Translate 2B (Q4_K_M)",
    short_name: "PLaMo 3 Translate 2B",
    provider: "Preferred Networks, Inc.",
    parameter_size: "2B",
    quantization: "Q4_K_M",
    source_type: "migrated_legacy",
    file_path: "C:\\models\\plamo-3-translate-2b-Q4_K_M.gguf",
    file_name: "plamo-3-translate-2b-Q4_K_M.gguf",
    file_size_bytes: 1807203040,
    file_mtime_ns: 100,
    measured_sha256: "1ffd9073afcc70e35ccc942428aa2b73d7aa2b9ea779bf504b775369e0276c13",
    gguf_architecture: "plamo3",
    gguf_model_name: "plamo-3-translate-2b",
    verification: {
      file_exists: true,
      format_valid: true,
      hash_status: "matched_known",
      backend_compatible: true,
      launch_verified: true,
      translation_verified: true,
      matched_revision: "4f672106",
      matched_label: "official-2026-11-09",
      validation_error: null,
      validated_at: "2026-11-15T12:00:00Z",
    },
    license_accepted: true,
    license_accepted_at: "2026-11-15T12:00:00Z",
    created_at: "2026-11-15T12:00:00Z",
    updated_at: "2026-11-15T12:00:00Z",
    ...overrides,
  };
}

function makeDiagnostics(overrides: Partial<SystemDiagnostics> = {}): SystemDiagnostics {
  return {
    os_name: "Windows",
    os_version: "11",
    cpu_name: "Ryzen",
    cpu_cores: 8,
    cpu_threads: 16,
    ram_total_mb: 32768,
    gpu_name: "Radeon RX",
    gpu_vram_mb: 8192,
    gpu_driver_version: "24.0",
    python_version: "3.13.13",
    pdf_engine_primary: "pymupdf",
    babeldoc_installed: false,
    babeldoc_version: null,
    babeldoc_assets_cached: false,
    japanese_font_available: true,
    japanese_font_path: "C:\\Windows\\Fonts\\msgothic.ttc",
    llama_server_url: "http://127.0.0.1:8000",
    llama_connection_status: "connected",
    loaded_model_name: "plamo-3-translate-2b-q4_k_m",
    loopback_only_enforced: true,
    llama_model_path: "C:\\models\\plamo-3-translate-2b-Q4_K_M.gguf",
    llama_build_info: "b1234",
    llama_compute_backend: "Vulkan",
    llama_server_pid: 1234,
    active_registration_id: "reg-08b-1",
    active_catalog_model_id: "plamo-3-translate-0.8b-q4_k_m",
    active_model_display_name: "PLaMo 3 Translate 0.8B (Q4_K_M)",
    active_model_file_path: "C:\\ext\\plamo-3-translate-0.8b-Q4_K_M.gguf",
    active_model_status: "registered",
    running_server_model_path: "C:\\models\\plamo-3-translate-2b-Q4_K_M.gguf",
    model_server_match_status: "mismatch",
    model_server_match_message: "UI選択は0.8Bですがサーバは2Bを実行中です。",
    model_server_mismatch: true,
    ...overrides,
  };
}

describe("Model Selection, Download, Local Registry & Consistency UI", () => {
  it("supports catalog selection, download+cancel, local browse/reference/copy, missing relocate, active-job lock, and mismatch alerts on Screen 4", async () => {
    const reg2 = makeRegistered();
    const regMissing = makeRegistered({
      registration_id: "reg-8b-missing",
      catalog_model_id: "plamo-3-translate-8b-q4_k_m",
      display_name: "PLaMo 3 Translate 8B (Q4_K_M)",
      short_name: "PLaMo 3 Translate 8B",
      parameter_size: "8B",
      source_type: "reference",
      file_path: "D:\\external\\plamo-3-translate-8b-Q4_K_M.gguf",
      file_name: "plamo-3-translate-8b-Q4_K_M.gguf",
      verification: {
        file_exists: false,
        format_valid: false,
        hash_status: "missing_file",
        backend_compatible: false,
        launch_verified: false,
        translation_verified: false,
        validation_error: "登録済みモデルファイルが見つかりません。",
        validated_at: "2026-11-15T12:00:00Z",
      },
    });
    const overview: ModelsOverviewResponse = {
      catalog: makeCatalog(),
      unsupported_policies: {},
      registered: [reg2, regMissing],
      active_model_id: reg2.registration_id,
      active_model: reg2,
      has_active_translation_job: false,
      managed_models_dir: "C:\\runtime\\models",
      downloads: [],
      server_match: {
        match_status: "mismatch",
        is_matched: false,
        active_registration_id: reg2.registration_id,
        active_catalog_model_id: reg2.catalog_model_id,
        active_display_name: reg2.display_name,
        active_file_path: reg2.file_path,
        running_identified_display_name: "PLaMo 3 Translate 0.8B (Q4_K_M)",
        running_model_path: "C:\\other\\plamo-3-translate-0.8b-Q4_K_M.gguf",
        message_ja: "モデル不一致: 選択中は2Bですが0.8Bが稼働中です。",
      },
    };
    const activeDownload: ModelDownloadJob = {
      download_id: "dl-abc",
      catalog_model_id: "plamo-3-translate-0.8b-q4_k_m",
      display_name: "PLaMo 3 Translate 0.8B (Q4_K_M)",
      destination_path: "C:\\runtime\\models\\plamo-3-translate-0.8b-GGUF\\plamo-3-translate-0.8b-Q4_K_M.gguf",
      status: "downloading",
      bytes_downloaded: 286563424,
      total_bytes: 573126848,
      percent: 50.0,
      started_at: "2026-11-15T12:00:00Z",
    };

    const onRefreshModels = vi.fn().mockResolvedValue(undefined);
    const onBrowseLocalFile = vi.fn().mockResolvedValue("D:\\my_models\\plamo-3-translate-0.8b-Q4_K_M.gguf");
    const onRegisterLocalModel = vi.fn().mockResolvedValue(makeRegistered({ registration_id: "reg-08-new", catalog_model_id: "plamo-3-translate-0.8b-q4_k_m", display_name: "PLaMo 3 Translate 0.8B (Q4_K_M)" }));
    const onSelectModel = vi.fn().mockResolvedValue(undefined);
    const onDeleteModel = vi.fn().mockResolvedValue(undefined);
    const onStartDownload = vi.fn().mockResolvedValue(activeDownload);
    const onCancelDownload = vi.fn().mockResolvedValue(undefined);

    const { rerender } = render(
      <Screen4GlossaryDiagnostics
        glossaries={[]}
        diagnostics={makeDiagnostics()}
        hasProbedLlama={true}
        isLoadingDiagnostics={false}
        diagnosticsError={null}
        modelsOverview={overview}
        activeModelDownload={activeDownload}
        onRefreshDiagnostics={vi.fn().mockResolvedValue(undefined)}
        onRefreshModels={onRefreshModels}
        onBrowseLocalModelFile={onBrowseLocalFile}
        onRegisterLocalModel={onRegisterLocalModel}
        onSelectModel={onSelectModel}
        onDeleteModel={onDeleteModel}
        onStartModelDownload={onStartDownload}
        onCancelModelDownload={onCancelDownload}
        onCreateGlossary={vi.fn()}
        onUpdateGlossary={vi.fn()}
        onDeleteGlossary={vi.fn()}
        onPurgeTempData={vi.fn()}
        onRotateToken={vi.fn()}
      />
    );

    // Mismatch alerts shown both in model panel & diagnostics
    expect(screen.getByTestId("model-server-mismatch-alert")).toBeInTheDocument();
    expect(screen.getByTestId("diag-model-server-mismatch-alert")).toBeInTheDocument();

    // Missing file actions & active download progress/cancel
    expect(screen.getByTestId("registered-model-item-reg-8b-missing")).toHaveTextContent("ファイル欠損");
    expect(screen.getByTestId("relocate-model-btn-reg-8b-missing")).toBeInTheDocument();
    expect(screen.getByTestId("redownload-model-btn-reg-8b-missing")).toBeInTheDocument();
    expect(screen.getByTestId("model-download-progress")).toHaveTextContent("50.0%");

    fireEvent.click(screen.getByTestId("cancel-model-download-btn"));
    await waitFor(() => {
      expect(onCancelDownload).toHaveBeenCalledWith("dl-abc");
    });

    // Clear active download to interact with catalog download + local browse
    rerender(
      <Screen4GlossaryDiagnostics
        glossaries={[]}
        diagnostics={makeDiagnostics()}
        hasProbedLlama={true}
        isLoadingDiagnostics={false}
        diagnosticsError={null}
        modelsOverview={overview}
        activeModelDownload={null}
        onRefreshDiagnostics={vi.fn().mockResolvedValue(undefined)}
        onRefreshModels={onRefreshModels}
        onBrowseLocalModelFile={onBrowseLocalFile}
        onRegisterLocalModel={onRegisterLocalModel}
        onSelectModel={onSelectModel}
        onDeleteModel={onDeleteModel}
        onStartModelDownload={onStartDownload}
        onCancelModelDownload={onCancelDownload}
        onCreateGlossary={vi.fn()}
        onUpdateGlossary={vi.fn()}
        onDeleteGlossary={vi.fn()}
        onPurgeTempData={vi.fn()}
        onRotateToken={vi.fn()}
      />
    );

    fireEvent.change(screen.getByTestId("catalog-model-select"), {
      target: { value: "plamo-3-translate-0.8b-q4_k_m" },
    });
    expect(screen.getByTestId("catalog-model-select")).toHaveTextContent("実機未検証");
    expect(screen.getByTestId("selected-catalog-details")).toHaveTextContent("軽量候補");
    expect(screen.getByTestId("selected-catalog-details")).toHaveTextContent(
      "plamo-3-translate-0.8b-Q4_K_M.gguf"
    );
    fireEvent.click(screen.getByTestId("model-license-agree-checkbox"));
    fireEvent.click(screen.getByTestId("start-model-download-btn"));
    await waitFor(() => {
      expect(onStartDownload).toHaveBeenCalledWith(
        expect.objectContaining({
          catalog_model_id: "plamo-3-translate-0.8b-q4_k_m",
          license_accepted: true,
        })
      );
    });

    // Browse local GGUF & register in Reference mode by default
    fireEvent.click(screen.getByTestId("browse-local-model-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("local-model-path-input")).toHaveValue(
        "D:\\my_models\\plamo-3-translate-0.8b-Q4_K_M.gguf"
      );
    });
    fireEvent.click(screen.getByTestId("register-local-model-btn"));
    await waitFor(() => {
      expect(onRegisterLocalModel).toHaveBeenCalledWith(
        expect.objectContaining({
          file_path: "D:\\my_models\\plamo-3-translate-0.8b-Q4_K_M.gguf",
          register_mode: "reference",
          license_accepted: true,
        })
      );
    });

    // Active job lock disables switching
    rerender(
      <Screen4GlossaryDiagnostics
        glossaries={[]}
        diagnostics={makeDiagnostics()}
        hasProbedLlama={true}
        isLoadingDiagnostics={false}
        diagnosticsError={null}
        modelsOverview={{ ...overview, has_active_translation_job: true }}
        activeModelDownload={null}
        onRefreshDiagnostics={vi.fn().mockResolvedValue(undefined)}
        onRefreshModels={onRefreshModels}
        onBrowseLocalModelFile={onBrowseLocalFile}
        onRegisterLocalModel={onRegisterLocalModel}
        onSelectModel={onSelectModel}
        onDeleteModel={onDeleteModel}
        onStartModelDownload={onStartDownload}
        onCancelModelDownload={onCancelDownload}
        onCreateGlossary={vi.fn()}
        onUpdateGlossary={vi.fn()}
        onDeleteGlossary={vi.fn()}
        onPurgeTempData={vi.fn()}
        onRotateToken={vi.fn()}
      />
    );
    expect(screen.getByTestId("model-active-job-lock")).toBeInTheDocument();
  });

  it("displays dynamic active model, quick switcher, drawer toggle & mismatch alert on Screen 1", async () => {
    const reg08 = makeRegistered({
      registration_id: "reg-08b-1",
      catalog_model_id: "plamo-3-translate-0.8b-q4_k_m",
      display_name: "PLaMo 3 Translate 0.8B (Q4_K_M)",
      short_name: "PLaMo 3 Translate 0.8B",
      parameter_size: "0.8B",
      file_path: "C:\\ext\\plamo-3-translate-0.8b-Q4_K_M.gguf",
      file_name: "plamo-3-translate-0.8b-Q4_K_M.gguf",
    });
    const reg2 = makeRegistered();
    const overview: ModelsOverviewResponse = {
      catalog: makeCatalog(),
      unsupported_policies: {},
      registered: [reg08, reg2],
      active_model_id: reg08.registration_id,
      active_model: reg08,
      has_active_translation_job: false,
      managed_models_dir: "C:\\runtime\\models",
      downloads: [],
      server_match: {
        match_status: "mismatch",
        is_matched: false,
        active_display_name: reg08.display_name,
        message_ja: "0.8B選択中ですが2Bが稼働中です。",
      },
    };
    const cfg: TranslationJobConfig = {
      page_range_spec: null,
      source_lang: "en",
      target_lang: "ja",
      form: "plain",
      model_name: "plamo-3-translate-0.8b-Q4_K_M",
      model_id: "plamo-3-translate-0.8b-q4_k_m",
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
      use_mock_translator: false,
    };
    const onSelectModel = vi.fn().mockResolvedValue(undefined);

    render(
      <Screen1HomePreflight
        currentDocument={null}
        jobConfig={cfg}
        glossaries={[]}
        modelsOverview={overview}
        isUploading={false}
        uploadError={null}
        isStartingJob={false}
        jobStartError={null}
        onUploadFile={vi.fn()}
        onUpdateJobConfig={vi.fn()}
        onOpenProtectionScreen={vi.fn()}
        onOpenModelsScreen={vi.fn()}
        onRefreshModels={vi.fn().mockResolvedValue(undefined)}
        onBrowseLocalModelFile={vi.fn().mockResolvedValue(null)}
        onRegisterLocalModel={vi.fn()}
        onSelectModel={onSelectModel}
        onDeleteModel={vi.fn()}
        onStartModelDownload={vi.fn()}
        onCancelModelDownload={vi.fn()}
        onStartTranslationJob={vi.fn()}
      />
    );

    expect(screen.getByTestId("screen1-active-model-display")).toHaveTextContent(
      "PLaMo 3 Translate 0.8B (Q4_K_M)"
    );
    expect(screen.getByTestId("start-job-btn")).toHaveTextContent(
      "実機翻訳ジョブを開始 (PLaMo 3 Translate 0.8B)"
    );
    expect(screen.getByTestId("screen1-model-server-mismatch")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("screen1-quick-model-select"), {
      target: { value: reg2.registration_id },
    });
    await waitFor(() => {
      expect(onSelectModel).toHaveBeenCalledWith(reg2.registration_id);
    });

    fireEvent.click(screen.getByTestId("toggle-screen1-model-drawer-btn"));
    expect(screen.getByTestId("screen1-model-drawer")).toBeInTheDocument();
  });

  it("renders non-PLaMo model license & provenance without leaking any PLaMo wording", () => {
    const nonPlamoCatalog: CatalogModel = {
      ...makeCatalog()[0],
      model_id: "opentranslate-7b-q4",
      display_name: "OpenTranslate 7B",
      short_name: "OpenTranslate 7B",
      provider: "OpenTranslate Community",
      family: "opentranslate",
      source_repo: "opentranslate/opentranslate-7b-GGUF",
      model_card_url: "https://example.org/opentranslate-7b",
      license: {
        id: "apache-2.0",
        name: "Apache License 2.0",
        tagline: "Open Weight Translation",
        url_ja: "https://example.org/apache-2.0-ja",
        url_en: "https://example.org/apache-2.0-en",
        model_card_url: "https://example.org/opentranslate-7b",
        summary_ja: "Apache License 2.0 に従って利用できます。",
        disclosure_note_ja: "変更点がある場合は NOTICE を保持してください。",
        requires_agreement: true,
      },
    };

    const { container } = render(
      <ModelLicenseNotice variant="screen4" catalogModel={nonPlamoCatalog} activeModel={null} />
    );
    expect(container.textContent).toContain("OpenTranslate 7B");
    expect(container.textContent).toContain("Apache License 2.0");
    expect(container.textContent).not.toContain("PLaMo");
    expect(container.textContent).not.toContain("Preferred Networks");
    expect(container.textContent).not.toContain("pfnet");
  });
});
