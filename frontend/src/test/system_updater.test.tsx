import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "../App";
import { setInMemorySessionTokenForTest } from "../api/client";
import { Screen1HomePreflight } from "../components/Screen1HomePreflight";
import { Screen4GlossaryDiagnostics } from "../components/Screen4GlossaryDiagnostics";
import type {
  CatalogModel,
  ModelsOverviewResponse,
  SystemDiagnostics,
  SystemUpdateCheckResponse,
  TranslationJobConfig,
} from "../types/api";

function makeDiagnostics(overrides: Partial<SystemDiagnostics> = {}): SystemDiagnostics {
  return {
    os_name: "Windows 11",
    os_version: "10.0.26200",
    cpu_name: "AMD Ryzen 7",
    cpu_cores: 8,
    cpu_threads: 16,
    ram_total_mb: 32768,
    gpu_name: "AMD Radeon RX 7600",
    gpu_vram_mb: 8192,
    gpu_driver_version: "24.0",
    python_version: "3.13.13",
    pdf_engine_primary: "native_preserving_pymupdf",
    babeldoc_installed: false,
    babeldoc_version: null,
    babeldoc_assets_cached: false,
    japanese_font_available: true,
    japanese_font_path: "C:\\Windows\\Fonts\\msgothic.ttc",
    llama_server_url: "http://127.0.0.1:8000/v1",
    llama_connection_status: "not_probed",
    loaded_model_name: null,
    loopback_only_enforced: true,
    app_version: "c7f9296",
    ...overrides,
  };
}

function makeDefault08bCatalog(): CatalogModel[] {
  const lic = {
    id: "plamo-community-license",
    name: "PLaMoコミュニティライセンス契約",
    tagline: "Built with PLaMo",
    url_ja: "https://plamo.preferredai.jp/info/plamo-community-license-ja",
    url_en: "https://plamo.preferredai.jp/info/plamo-community-license-en",
    model_card_url: "https://huggingface.co/pfnet/plamo-3-translate-0.8b-GGUF",
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
      is_default: true,
      source_repo: "pfnet/plamo-3-translate-0.8b-GGUF",
      source_repo_type: "huggingface",
      pinned_revision: "1cf96ae02133958a20074bfdfab76517bb737d00",
      filename: "plamo-3-translate-0.8b-Q4_K_M.gguf",
      relative_subdir: "plamo-3-translate-0.8b-GGUF",
      download_url:
        "https://huggingface.co/pfnet/plamo-3-translate-0.8b-GGUF/resolve/1cf96ae02133958a20074bfdfab76517bb737d00/plamo-3-translate-0.8b-Q4_K_M.gguf",
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
      hardware_note_ja: "約 0.57 GB（573 MB）。初回既定・軽量モデル。",
      license: lic,
    },
    {
      model_id: "plamo-3-translate-2b-q4_k_m",
      display_name: "PLaMo 3 Translate 2B (Q4_K_M)",
      short_name: "PLaMo 3 Translate 2B",
      provider: "Preferred Networks, Inc.",
      family: "plamo-3-translate",
      parameter_size: "2B",
      quantization: "Q4_K_M",
      is_default: false,
      source_repo: "pfnet/plamo-3-translate-2b-GGUF",
      source_repo_type: "huggingface",
      pinned_revision: "4f672106b847200bfdc0f6af6c321cd94eba1aed",
      filename: "plamo-3-translate-2b-Q4_K_M.gguf",
      relative_subdir: "plamo-3-translate-2b-GGUF",
      download_url:
        "https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF/resolve/4f672106b847200bfdc0f6af6c321cd94eba1aed/plamo-3-translate-2b-Q4_K_M.gguf",
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
      hardware_note_ja: "約 1.81 GB。高精度・推奨モデル。",
      license: { ...lic, model_card_url: "https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF" },
    },
  ];
}

describe("Software Update Card on Screen 4 & Default 0.8B Selection", () => {
  it("shows current version, checks update, guards with confirmation modal, and shows loading overlay", async () => {
    const onCheckUpdate = vi.fn().mockResolvedValue(undefined);
    const onApplyUpdate = vi.fn().mockResolvedValue(undefined);

    const availableResult: SystemUpdateCheckResponse = {
      update_available: true,
      current_version: "c7f9296",
      latest_version: "fe517c2",
      message: "最新のアップデートが利用可能です",
      status: "update_available",
      latest_commit_message: "feat: add self updater",
    };

    const { rerender } = render(
      <Screen4GlossaryDiagnostics
        glossaries={[]}
        diagnostics={makeDiagnostics({ app_version: "c7f9296" })}
        hasProbedLlama={false}
        isLoadingDiagnostics={false}
        diagnosticsError={null}
        onRefreshDiagnostics={vi.fn()}
        onCreateGlossary={vi.fn()}
        onUpdateGlossary={vi.fn()}
        onDeleteGlossary={vi.fn()}
        onPurgeTempData={vi.fn()}
        onRotateToken={vi.fn()}
        updateCheckResult={null}
        onCheckUpdate={onCheckUpdate}
        onApplyUpdate={onApplyUpdate}
      />
    );

    expect(screen.getByTestId("software-update-card")).toBeInTheDocument();
    expect(screen.getByTestId("update-current-version")).toHaveTextContent("c7f9296");
    expect(screen.getByTestId("check-update-btn")).toHaveTextContent("🔄 更新を確認");

    fireEvent.click(screen.getByTestId("check-update-btn"));
    expect(onCheckUpdate).toHaveBeenCalledTimes(1);

    // Rerender with update available
    rerender(
      <Screen4GlossaryDiagnostics
        glossaries={[]}
        diagnostics={makeDiagnostics({ app_version: "c7f9296" })}
        hasProbedLlama={false}
        isLoadingDiagnostics={false}
        diagnosticsError={null}
        onRefreshDiagnostics={vi.fn()}
        onCreateGlossary={vi.fn()}
        onUpdateGlossary={vi.fn()}
        onDeleteGlossary={vi.fn()}
        onPurgeTempData={vi.fn()}
        onRotateToken={vi.fn()}
        updateCheckResult={availableResult}
        onCheckUpdate={onCheckUpdate}
        onApplyUpdate={onApplyUpdate}
      />
    );

    expect(screen.getByTestId("update-available-banner")).toHaveTextContent(
      "新しいバージョンが利用可能です"
    );
    expect(screen.getByTestId("update-latest-version")).toHaveTextContent("fe517c2");
    expect(screen.getByTestId("apply-update-btn")).toHaveTextContent(
      "今すぐアップデートして再起動"
    );

    // Clicking apply opens confirmation modal first (does NOT immediately call onApplyUpdate)
    fireEvent.click(screen.getByTestId("apply-update-btn"));
    expect(screen.getByTestId("update-confirm-modal")).toHaveTextContent(
      "アプリを更新して再起動します。よろしいですか？"
    );
    expect(onApplyUpdate).not.toHaveBeenCalled();

    // Cancel closes modal without applying
    fireEvent.click(screen.getByTestId("update-confirm-cancel-btn"));
    expect(screen.queryByTestId("update-confirm-modal")).not.toBeInTheDocument();
    expect(onApplyUpdate).not.toHaveBeenCalled();

    // Open modal again and confirm -> calls onApplyUpdate
    fireEvent.click(screen.getByTestId("apply-update-btn"));
    fireEvent.click(screen.getByTestId("update-confirm-ok-btn"));
    await waitFor(() => {
      expect(onApplyUpdate).toHaveBeenCalledTimes(1);
    });
    expect(screen.queryByTestId("update-confirm-modal")).not.toBeInTheDocument();

    // Loading overlay appears while applying with elapsed time, progress bar, and separate window hint
    const retryFn = vi.fn();
    const dismissFn = vi.fn();
    rerender(
      <Screen4GlossaryDiagnostics
        glossaries={[]}
        diagnostics={makeDiagnostics({ app_version: "c7f9296" })}
        hasProbedLlama={false}
        isLoadingDiagnostics={false}
        diagnosticsError={null}
        onRefreshDiagnostics={vi.fn()}
        onCreateGlossary={vi.fn()}
        onUpdateGlossary={vi.fn()}
        onDeleteGlossary={vi.fn()}
        onPurgeTempData={vi.fn()}
        onRotateToken={vi.fn()}
        updateCheckResult={availableResult}
        isApplyingUpdate={true}
        updateApplyingMessage="最新版を適用して再起動しています..."
        updateLogPath={"C:\\runtime\\logs\\updater.log"}
        updateElapsedSeconds={25}
        updateReconnectTimedOut={false}
        onRetryUpdateReconnect={retryFn}
        onDismissUpdateOverlay={dismissFn}
        onCheckUpdate={onCheckUpdate}
        onApplyUpdate={onApplyUpdate}
      />
    );
    expect(screen.getByTestId("system-update-loading-overlay")).toBeInTheDocument();
    expect(screen.getByTestId("system-update-loading-overlay")).toHaveTextContent(
      "最新版を適用して再起動しています..."
    );
    expect(screen.getByTestId("system-update-elapsed")).toHaveTextContent("経過時間: 00:25");
    expect(screen.getByTestId("system-update-progress-bar")).toBeInTheDocument();
    expect(screen.getByTestId("system-update-log-path")).toHaveTextContent("updater.log");

    // When reconnect times out, show recovery UI with retry and dismiss buttons
    rerender(
      <Screen4GlossaryDiagnostics
        glossaries={[]}
        diagnostics={makeDiagnostics({ app_version: "c7f9296" })}
        hasProbedLlama={false}
        isLoadingDiagnostics={false}
        diagnosticsError={null}
        onRefreshDiagnostics={vi.fn()}
        onCreateGlossary={vi.fn()}
        onUpdateGlossary={vi.fn()}
        onDeleteGlossary={vi.fn()}
        onPurgeTempData={vi.fn()}
        onRotateToken={vi.fn()}
        updateCheckResult={availableResult}
        isApplyingUpdate={true}
        updateApplyingMessage="最新版を適用して再起動しています..."
        updateLogPath={"C:\\runtime\\logs\\updater.log"}
        updateElapsedSeconds={180}
        updateReconnectTimedOut={true}
        onRetryUpdateReconnect={retryFn}
        onDismissUpdateOverlay={dismissFn}
        onCheckUpdate={onCheckUpdate}
        onApplyUpdate={onApplyUpdate}
      />
    );
    expect(screen.getByTestId("system-update-timeout-panel")).toHaveTextContent(
      "再接続がタイムアウトしました（180秒経過）"
    );
    expect(screen.getByTestId("system-update-log-path")).toHaveTextContent(
      "C:\\runtime\\logs\\updater.log"
    );
    fireEvent.click(screen.getByTestId("update-retry-reconnect-btn"));
    expect(retryFn).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("update-dismiss-overlay-btn"));
    expect(dismissFn).toHaveBeenCalledTimes(1);
  });

  it("App end-to-end update flow checks GitHub API, confirms modal, handles 409 conflict and applies update", async () => {
    setInMemorySessionTokenForTest(null);
    let applyMode: "conflict" | "ok" = "conflict";

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method || "GET").toUpperCase();

      if (url.includes("/api/auth/session")) {
        return new Response(
          JSON.stringify({
            token: "upd-tok",
            header_name: "X-PaperBridge-Token",
            loopback_only: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.endsWith("/api/glossaries")) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/diagnostics")) {
        return new Response(JSON.stringify(makeDiagnostics({ app_version: "c7f9296" })), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/models")) {
        return new Response(
          JSON.stringify({
            catalog: makeDefault08bCatalog(),
            unsupported_policies: {},
            registered: [],
            active_model_id: null,
            active_model: null,
            has_active_translation_job: false,
            managed_models_dir: "C:\\runtime\\models",
            downloads: [],
            server_match: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.endsWith("/api/system/update/check") && method === "GET") {
        return new Response(
          JSON.stringify({
            update_available: true,
            current_version: "c7f9296",
            latest_version: "fe517c2",
            message: "最新のアップデートが利用可能です",
            status: "update_available",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.endsWith("/api/system/update/apply") && method === "POST") {
        if (applyMode === "conflict") {
          return new Response(
            JSON.stringify({
              detail: "UpdateConflict: 翻訳ジョブ実行中はアップデートを実行できません。",
            }),
            { status: 409, headers: { "Content-Type": "application/json" } }
          );
        }
        return new Response(
          JSON.stringify({
            status: "updating",
            message: "アップデートを開始しました。再起動します。",
            updater_pid: 9876,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`);
    });

    try {
      render(<App />);
      await waitFor(() => {
        expect(screen.getByTestId("nav-screen4-btn")).toBeInTheDocument();
      });

      // Navigate to Screen 4
      fireEvent.click(screen.getByTestId("nav-screen4-btn"));
      await waitFor(() => {
        expect(screen.getByTestId("software-update-card")).toBeInTheDocument();
      });
      expect(screen.getByTestId("update-current-version")).toHaveTextContent("c7f9296");

      // Check for updates
      fireEvent.click(screen.getByTestId("check-update-btn"));
      await waitFor(() => {
        expect(screen.getByTestId("update-available-banner")).toHaveTextContent(
          "新しいバージョンが利用可能です"
        );
      });

      // 409 Conflict shows error and clears loading overlay
      fireEvent.click(screen.getByTestId("apply-update-btn"));
      fireEvent.click(screen.getByTestId("update-confirm-ok-btn"));
      await waitFor(() => {
        expect(screen.getByTestId("update-error-banner")).toHaveTextContent("409 Conflict");
      });
      expect(screen.queryByTestId("system-update-loading-overlay")).not.toBeInTheDocument();

      // Successful apply shows loading overlay
      applyMode = "ok";
      fireEvent.click(screen.getByTestId("apply-update-btn"));
      fireEvent.click(screen.getByTestId("update-confirm-ok-btn"));
      await waitFor(() => {
        expect(screen.getByTestId("system-update-loading-overlay")).toBeInTheDocument();
      });
    } finally {
      fetchSpy.mockRestore();
      setInMemorySessionTokenForTest(null);
    }
  });

  it("defaults to 0.8B (0.57 GB) on Home Screen and ModelSelectionPanel when unconfigured", () => {
    const overview: ModelsOverviewResponse = {
      catalog: makeDefault08bCatalog(),
      unsupported_policies: {},
      registered: [],
      active_model_id: null,
      active_model: null,
      has_active_translation_job: false,
      managed_models_dir: "C:\\runtime\\models",
      downloads: [],
      server_match: null,
    };
    const defaultCfg: TranslationJobConfig = {
      page_range_spec: null,
      source_lang: "en",
      target_lang: "ja",
      form: "plain",
      model_name: "plamo-3-translate-0.8b-Q4_K_M",
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

    render(
      <Screen1HomePreflight
        currentDocument={null}
        jobConfig={defaultCfg}
        glossaries={[]}
        modelsOverview={overview}
        isUploading={false}
        uploadError={null}
        isStartingJob={false}
        jobStartError={null}
        onUploadFile={vi.fn()}
        onUpdateJobConfig={vi.fn()}
        onOpenProtectionScreen={vi.fn()}
        onRefreshModels={vi.fn().mockResolvedValue(undefined)}
        onBrowseLocalModelFile={vi.fn().mockResolvedValue(null)}
        onRegisterLocalModel={vi.fn()}
        onSelectModel={vi.fn().mockResolvedValue(undefined)}
        onDeleteModel={vi.fn().mockResolvedValue(undefined)}
        onStartModelDownload={vi.fn()}
        onCancelModelDownload={vi.fn().mockResolvedValue(undefined)}
        onStartTranslationJob={vi.fn()}
      />
    );

    expect(screen.getByTestId("inference-mode-select")).toHaveTextContent(
      "PLaMo 3 Translate 0.8B (Q4_K_M)"
    );
    expect(screen.getByTestId("screen1-link-plamo-model")).toHaveAttribute(
      "href",
      "https://huggingface.co/pfnet/plamo-3-translate-0.8b-GGUF"
    );

    // Open quick model drawer -> 0.8B is selected by default with 0.57 GB size text
    fireEvent.click(screen.getByTestId("toggle-screen1-model-drawer-btn"));
    expect(screen.getByTestId("catalog-model-select")).toHaveValue(
      "plamo-3-translate-0.8b-q4_k_m"
    );
    expect(screen.getByTestId("start-model-download-btn")).toHaveTextContent("0.57 GB");
  });
});
