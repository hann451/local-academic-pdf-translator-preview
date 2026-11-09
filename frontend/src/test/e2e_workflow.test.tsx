/**
 * Frontend Full-Workflow E2E Test across Screens 1 -> 2 -> 3 -> 4 (Section 10 & 11).
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "../App";
import { setInMemorySessionTokenForTest } from "../api/client";

describe("Full Application E2E Workflow (Screen 1 -> Screen 2 -> Screen 3 -> Screen 4)", () => {
  it("completes PDF upload, protection region check, job execution, manual edit, zero-LLM retypeset, and glossary/diagnostics inspection", async () => {
    setInMemorySessionTokenForTest(null);

    let editRev = 0;
    let retypesetRev = 0;
    let savedManualText: string | null = null;
    let retypesetCalledCount = 0;

    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method || "GET").toUpperCase();

      if (url.includes("/api/auth/session")) {
        return new Response(
          JSON.stringify({
            token: "e2e-session-token-1234567890",
            header_name: "X-PaperBridge-Token",
            loopback_only: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }

      // Verify X-PaperBridge-Token is sent on every protected request
      const reqHeaders = new Headers(init?.headers || {});
      expect(reqHeaders.get("X-PaperBridge-Token")).toBe("e2e-session-token-1234567890");

      if (url.includes("/api/diagnostics")) {
        const isProbed = url.includes("probe_llama=true");
        return new Response(
          JSON.stringify({
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
            llama_connection_status: isProbed ? "connected" : "not_connected",
            loaded_model_name: isProbed ? "plamo-3-translate" : null,
            loopback_only_enforced: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }

      if (url.endsWith("/api/glossaries") && method === "GET") {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.includes("/api/documents/upload") && method === "POST") {
        return new Response(
          JSON.stringify({
            id: "doc-e2e001",
            original_filename: "paper_e2e.pdf",
            safe_storage_path: "C:/runtime/uploads/doc-e2e001.pdf",
            sha256_hash: "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899",
            file_size_bytes: 65536,
            page_count: 1,
            is_encrypted: false,
            is_corrupted: false,
            is_scanned_pdf: false,
            extractable_Sync_status: "ok",
            unsupported_reason: null,
            protection_revision: 0,
            edit_revision: editRev,
            pages: [
              {
                document_id: "doc-e2e001",
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
            created_at: "2026-11-06T12:00:00Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }

      if (url.endsWith("/api/documents/doc-e2e001") && method === "GET") {
        return new Response(
          JSON.stringify({
            id: "doc-e2e001",
            original_filename: "paper_e2e.pdf",
            safe_storage_path: "C:/runtime/uploads/doc-e2e001.pdf",
            sha256_hash: "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899",
            file_size_bytes: 65536,
            page_count: 1,
            is_encrypted: false,
            is_corrupted: false,
            is_scanned_pdf: false,
            extractable_Sync_status: "ok",
            unsupported_reason: null,
            protection_revision: 0,
            edit_revision: editRev,
            pages: [
              {
                document_id: "doc-e2e001",
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
            created_at: "2026-11-06T12:00:00Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }

      if (url.includes("/pdf") || url.includes("/download")) {
        return new Response(new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d]), {
          status: 200,
          headers: { "Content-Type": "application/pdf" },
        });
      }

      if (url.includes("/parse") || url.includes("/blocks")) {
        if (url.includes("/manual-edit") && method === "PUT") {
          const body = JSON.parse(String(init?.body || "{}"));
          savedManualText = body.manual_translation_text;
          editRev += 1;
          return new Response(
            JSON.stringify({
              id: "doc-e2e001:page-1:block-1",
              page_index: 0,
              bbox: [60, 100, 520, 160],
              kind: "paragraph",
              reading_order: 0,
              column_index: 0,
              source_text: "We propose a novel convergence bound for E(x) = α [1].",
              masked_source_text: "We propose a novel convergence bound for {v1} [1].",
              protected_items: [
                {
                  placeholder: "{v1}",
                  kind: "inline_formula",
                  original_text: "E(x) = α",
                  bbox: null,
                  font_name: null,
                },
              ],
              translation_text: "我々は {v1} に対する新しい収束限界を提案する [1]。",
              manual_translation_text: savedManualText,
              status: savedManualText ? "manually_edited" : "translated",
              warnings: [],
              font_size_pt: 10.0,
              fitted_font_size_pt: 9.5,
              is_uncertain_region: false,
              protection_source: "none",
              edit_revision: editRev,
              manual_validation_errors: [],
              manual_validation_warnings: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }

        return new Response(
          JSON.stringify([
            {
              id: "doc-e2e001:page-1:block-1",
              page_index: 0,
              bbox: [60, 100, 520, 160],
              kind: "paragraph",
              reading_order: 0,
              column_index: 0,
              source_text: "We propose a novel convergence bound for E(x) = α [1].",
              masked_source_text: "We propose a novel convergence bound for {v1} [1].",
              protected_items: [
                {
                  placeholder: "{v1}",
                  kind: "inline_formula",
                  original_text: "E(x) = α",
                  bbox: null,
                  font_name: null,
                },
              ],
              translation_text: "我々は {v1} に対する新しい収束限界を提案する [1]。",
              manual_translation_text: savedManualText,
              status: savedManualText ? "manually_edited" : "translated",
              warnings: [],
              font_size_pt: 10.0,
              fitted_font_size_pt: 9.5,
              is_uncertain_region: false,
              protection_source: "none",
              edit_revision: editRev,
              manual_validation_errors: [],
              manual_validation_warnings: [],
            },
          ]),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }

      if (url.includes("/protection-regions")) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.includes("/jobs") && method === "POST" && !url.includes("/retypeset")) {
        return new Response(
          JSON.stringify({
            id: "job-e2e001",
            document_id: "doc-e2e001",
            state: "completed",
            current_stage: "全工程正常完了",
            config: {
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
            },
            total_pages: 1,
            processed_pages: 1,
            total_blocks: 1,
            processed_blocks: 1,
            retry_count: 0,
            warning_count: 0,
            error_message: null,
            original_sha256: "aabbcc",
            original_hash_verified: true,
            is_mock_run: false,
            cancel_requested: false,
            glossary_version_used: null,
            retypeset_revision: retypesetRev,
            based_on_edit_revision: editRev,
            based_on_protection_revision: 0,
            applied_block_revisions: {},
            created_at: "2026-11-06T12:00:00Z",
            updated_at: "2026-11-06T12:00:10Z",
            completed_at: "2026-11-06T12:00:10Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }

      if (url.endsWith("/api/jobs/job-e2e001") && method === "GET") {
        return new Response(
          JSON.stringify({
            id: "job-e2e001",
            document_id: "doc-e2e001",
            state: "completed",
            current_stage: "全工程正常完了",
            config: {
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
            },
            total_pages: 1,
            processed_pages: 1,
            total_blocks: 1,
            processed_blocks: 1,
            retry_count: 0,
            warning_count: 0,
            error_message: null,
            original_sha256: "aabbcc",
            original_hash_verified: true,
            is_mock_run: false,
            cancel_requested: false,
            glossary_version_used: null,
            retypeset_revision: retypesetRev,
            based_on_edit_revision: editRev,
            based_on_protection_revision: 0,
            applied_block_revisions: {},
            created_at: "2026-11-06T12:00:00Z",
            updated_at: "2026-11-06T12:00:10Z",
            completed_at: "2026-11-06T12:00:10Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }

      if (url.includes("/issues")) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.includes("/artifacts") && method === "GET") {
        return new Response(
          JSON.stringify([
            {
              id: "art-mono-e2e",
              job_id: "job-e2e001",
              document_id: "doc-e2e001",
              artifact_type: "mono_ja_pdf",
              file_path: "C:/runtime/outputs/doc-e2e001.ja.pdf",
              sha256_hash: retypesetRev === 0 ? "hash_initial_000" : "hash_retypeset_111",
              file_size_bytes: 70000,
              is_finalized: true,
              is_mock_translation: false,
              created_at: "2026-11-06T12:00:10Z",
            },
          ]),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }

      if (url.includes("/retypeset") && method === "POST") {
        retypesetCalledCount += 1;
        retypesetRev += 1;
        return new Response(
          JSON.stringify({
            job: {
              id: "job-e2e001",
              document_id: "doc-e2e001",
              state: "completed",
              current_stage: "再組版完了（rev 1・LLM呼び出し0回）",
              config: {
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
              },
              total_pages: 1,
              processed_pages: 1,
              total_blocks: 1,
              processed_blocks: 1,
              retry_count: 0,
              warning_count: 0,
              error_message: null,
              original_sha256: "aabbcc",
              original_hash_verified: true,
              is_mock_run: false,
              cancel_requested: false,
              glossary_version_used: null,
              retypeset_revision: retypesetRev,
              based_on_edit_revision: editRev,
              based_on_protection_revision: 0,
              applied_block_revisions: {
                "doc-e2e001:page-1:block-1": {
                  source: "manual",
                  edit_revision: editRev,
                  status: "manually_edited",
                },
              },
              created_at: "2026-11-06T12:00:00Z",
              updated_at: "2026-11-06T12:00:20Z",
              completed_at: "2026-11-06T12:00:20Z",
            },
            artifacts: [
              {
                id: "art-mono-e2e",
                job_id: "job-e2e001",
                document_id: "doc-e2e001",
                artifact_type: "mono_ja_pdf",
                file_path: "C:/runtime/outputs/doc-e2e001.ja.pdf",
                sha256_hash: "hash_retypeset_111",
                file_size_bytes: 71000,
                is_finalized: true,
                is_mock_translation: false,
                created_at: "2026-11-06T12:00:20Z",
              },
            ],
            issues: [],
            requires_reparse_or_retranslate: false,
            llm_calls_made: 0,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }

      throw new Error(`Unhandled fetch in E2E test: ${method} ${url}`);
    });

    render(<App runJobsInBackground={false} />);

    // 1. Screen 1: Upload PDF
    const fileInput = screen.getByTestId("pdf-file-input");
    const testPdf = new File(["%PDF-1.4 mock"], "paper_e2e.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(fileInput, { target: { files: [testPdf] } });

    await waitFor(() => {
      expect(screen.getByTestId("preflight-result-card")).toBeInTheDocument();
    });

    // 2. Navigate to Screen 2 (Protection Editor)
    fireEvent.click(screen.getByTestId("open-screen2-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("screen2-protection-editor")).toBeInTheDocument();
    });

    // 3. Start Job from Screen 2 -> automatically transitions to Screen 3
    fireEvent.click(screen.getByTestId("screen2-start-job-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("screen3-progress-review")).toBeInTheDocument();
    });

    // 4. In Screen 3: Edit manual translation (keeping {v1} and [1]) and save
    const manualBox = screen.getByTestId("manual-translation-textarea");
    fireEvent.change(manualBox, {
      target: { value: "本研究では {v1} に対して改良された収束限界を導出する [1]。" },
    });
    fireEvent.click(screen.getByTestId("save-manual-edit-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("manual-save-feedback")).toHaveTextContent(
        "edit_revision=1"
      );
    });
    // Verify AI model translation is still preserved untouched
    expect(screen.getByTestId("inspector-model-translation")).toHaveTextContent(
      "我々は {v1} に対する新しい収束限界を提案する [1]。"
    );

    // 5. Run zero-LLM retypeset
    fireEvent.click(screen.getByTestId("retypeset-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("retypeset-success-banner")).toHaveTextContent(
        "LLM呼び出し 0 回"
      );
    });
    expect(retypesetCalledCount).toBe(1);

    // 6. Navigate to Screen 4 and probe llama-server
    fireEvent.click(screen.getByTestId("nav-screen4-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("screen4-glossary-diagnostics")).toBeInTheDocument();
    });
    expect(screen.getByTestId("diag-llama-status")).toHaveTextContent("未確認");
    fireEvent.click(screen.getByTestId("probe-llama-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("diag-llama-status")).toHaveTextContent(
        "実機接続正常 (connected — モデル: plamo-3-translate)"
      );
    });

    fetchMock.mockRestore();
  });
});
