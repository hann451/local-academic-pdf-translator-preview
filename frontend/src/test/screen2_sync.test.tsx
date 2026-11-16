/**
 * Proposal C tests: Screen 2 re-entry sync, loading/error stale-state safety,
 * unsaved-edit non-overwrite protection, and both same-screen normal edits & external API updates.
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "../App";
import { setInMemorySessionTokenForTest } from "../api/client";
import type { Block, BlockSplitSpec, Document } from "../types/api";

function makeDoc(protectionRevision: number): Document {
  return {
    id: "doc-sync001",
    original_filename: "sync_paper.pdf",
    safe_storage_path: "C:/runtime/uploads/doc-sync001.pdf",
    sha256_hash: "c".repeat(64),
    file_size_bytes: 65536,
    page_count: 2,
    is_encrypted: false,
    is_corrupted: false,
    is_scanned_pdf: false,
    extractable_Sync_status: "ok",
    unsupported_reason: null,
    protection_revision: protectionRevision,
    edit_revision: 0,
    pages: [0, 1].map((idx) => ({
      document_id: "doc-sync001",
      page_index: idx,
      width_pt: 595,
      height_pt: 842,
      rotation_deg: 0,
      media_box: [0, 0, 595, 842],
      crop_box: [0, 0, 595, 842],
      has_extractable_text: true,
      is_scanned_like: false,
      cid_ratio: 0,
      column_count: 1,
    })),
    created_at: "2026-11-16T00:00:00Z",
  };
}

function makeBlockBase(overrides: Partial<Block>): Block {
  return {
    id: "doc-sync001:page-1:block-28",
    page_index: 0,
    bbox: [311.5, 316.3, 543.9, 363.1],
    kind: "paragraph",
    reading_order: 0,
    column_index: 1,
    source_text: "Rising width = a + b (4) Here, a represents data.",
    masked_source_text: "Rising width = a + b (4) Here, a represents data.",
    protected_items: [],
    translation_text: null,
    manual_translation_text: null,
    status: "pending",
    warnings: [],
    font_size_pt: 10,
    fitted_font_size_pt: null,
    is_uncertain_region: false,
    protection_source: "none",
    edit_revision: 0,
    manual_validation_errors: [],
    manual_validation_warnings: [],
    semantic_category: "body",
    protection_reason: "本文",
    protection_rule_id: "default_translate",
    protection_confidence: "high",
    user_protection_override: null,
    translation_provenance: "none",
    ...overrides,
  };
}

function makeInitialBlocks(): Block[] {
  return [makeBlockBase({})];
}

function makeExternalSplitBlocks(): { blocks: Block[]; splits: BlockSplitSpec[] } {
  const splitSpec: BlockSplitSpec = {
    id: "split-ext-eq4",
    document_id: "doc-sync001",
    page_index: 0,
    requested_bbox: [305, 314, 550, 339],
    snapped_bbox: [317.1, 316.3, 543.9, 338.5],
    operation_mode: "split_and_protect_formula",
    parent_block_id: "doc-sync001:page-1:block-28",
    parent_bbox: [311.5, 316.3, 543.9, 363.1],
    parent_source_hash: "h".repeat(64),
    parent_source_preview: "Rising width = a + b (4) Here, a represents data.",
    include_equation_number: true,
    applied_status: "applied",
    status_reason: null,
    note: "External API split",
    created_at: "2026-11-16T00:10:00Z",
  };
  const blocks: Block[] = [
    makeBlockBase({
      id: "doc-sync001:page-1:block-28:split-split-ext-eq4:formula",
      bbox: [317.1, 316.3, 543.9, 338.5],
      kind: "isolated_formula",
      reading_order: 0,
      source_text: "Rising width = a + b (4)",
      masked_source_text: "Rising width = a + b (4)",
      status: "protected_skip",
      protection_source: "auto",
      semantic_category: "formula",
      protection_reason: "分割された独立数式",
      protection_rule_id: "split_formula",
      split_spec_id: "split-ext-eq4",
      split_role: "formula",
      parent_block_id: "doc-sync001:page-1:block-28",
      needs_retranslation: false,
      translation_provenance: "protected_skip",
    }),
    makeBlockBase({
      id: "doc-sync001:page-1:block-28:split-split-ext-eq4:bottom",
      bbox: [311.5, 340.4, 543.9, 363.1],
      kind: "paragraph",
      reading_order: 1,
      source_text: "Here, a represents data.",
      masked_source_text: "Here, a represents data.",
      status: "pending",
      split_spec_id: "split-ext-eq4",
      split_role: "bottom",
      parent_block_id: "doc-sync001:page-1:block-28",
      needs_retranslation: true,
    }),
  ];
  return { blocks, splits: [splitSpec] };
}

function makeCommonFetchResponse(url: string): Response | null {
  if (url.includes("/api/auth/session")) {
    return new Response(
      JSON.stringify({ token: "sync-test-tok", header_name: "X-PaperBridge-Token", loopback_only: true }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  }
  if (url.includes("/api/glossaries")) {
    return new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.includes("/api/diagnostics")) {
    return new Response(
      JSON.stringify({
        os_name: "Windows",
        os_version: "10",
        cpu_name: "CPU",
        cpu_cores: 4,
        cpu_threads: 8,
        ram_total_mb: 16384,
        python_version: "3.13",
        pdf_engine_primary: "native_preserving_pymupdf",
        babeldoc_installed: false,
        babeldoc_assets_cached: false,
        japanese_font_available: true,
        llama_server_url: "http://127.0.0.1:8000/v1",
        llama_connection_status: "not_probed",
        loopback_only_enforced: true,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  }
  if (url.includes("/api/models")) {
    return new Response(
      JSON.stringify({
        catalog: [],
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
  if (url.includes("/pdf")) {
    return new Response(new Uint8Array([0x25, 0x50, 0x44, 0x46]), {
      status: 200,
      headers: { "Content-Type": "application/pdf" },
    });
  }
  if (url.includes("/protection-regions")) {
    return new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  return null;
}

describe("Proposal C: Screen 2 Re-entry Sync, Stale-Failure Guard & Unsaved Protection", () => {
  it("syncs external API split updates on Screen 2 re-entry and supports same-screen normal preview/confirm/delete", async () => {
    setInMemorySessionTokenForTest(null);

    let serverDoc = makeDoc(0);
    let serverBlocks = makeInitialBlocks();
    let serverSplits: BlockSplitSpec[] = [];
    let resolver: (() => void) | null = null;
    let delayNextSplitsFetch = false;

    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method || "GET").toUpperCase();
      const common = makeCommonFetchResponse(url);
      if (common && !url.includes("/protection-regions")) return common;
      if (url.includes("/protection-regions") && method === "GET") {
        return new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.includes("/api/documents/upload") && method === "POST") {
        return new Response(JSON.stringify(serverDoc), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.endsWith("/api/documents/doc-sync001") && method === "GET") {
        return new Response(JSON.stringify(serverDoc), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.includes("/block-splits/preview") && method === "POST") {
        return new Response(
          JSON.stringify({
            valid: true,
            document_id: "doc-sync001",
            page_index: 0,
            protection_revision: serverDoc.protection_revision,
            parse_revision: 0,
            requested_bbox: [305, 314, 550, 339],
            snapped_bbox: [317.1, 316.3, 543.9, 338.5],
            parent_block_id: "doc-sync001:page-1:block-28",
            parent_bbox: [311.5, 316.3, 543.9, 363.1],
            parent_source_hash: "h".repeat(64),
            parent_signature: "sig-parent",
            parts: [
              {
                role: "formula",
                kind: "isolated_formula",
                will_protect: true,
                bbox: [317.1, 316.3, 543.9, 338.5],
                source_text: "Rising width = a + b (4)",
                label_ja: "選択数式（原文保護）",
              },
              {
                role: "bottom",
                kind: "paragraph",
                will_protect: false,
                bbox: [311.5, 340.4, 543.9, 363.1],
                source_text: "Here, a represents data.",
                label_ja: "下の本文（翻訳対象）",
              },
            ],
            warnings: [],
            will_invalidate_manual_edits: false,
            affected_manual_block_ids: [],
            has_active_job: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/block-splits/") && method === "DELETE") {
        serverDoc = makeDoc(serverDoc.protection_revision + 1);
        serverBlocks = makeInitialBlocks();
        serverSplits = [];
        return new Response(
          JSON.stringify({ split: null, deleted: true, document: serverDoc, blocks: serverBlocks, splits: serverSplits }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/block-splits") && method === "POST") {
        const ext = makeExternalSplitBlocks();
        const localSplit: BlockSplitSpec = { ...ext.splits[0], id: "split-local-eq4", note: "Same-screen split" };
        serverDoc = makeDoc(serverDoc.protection_revision + 1);
        serverBlocks = ext.blocks.map((b) => ({
          ...b,
          id: b.id.replace("split-ext-eq4", "split-local-eq4"),
          split_spec_id: "split-local-eq4",
        }));
        serverSplits = [localSplit];
        return new Response(
          JSON.stringify({ split: localSplit, deleted: false, document: serverDoc, blocks: serverBlocks, splits: serverSplits }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/block-splits") && method === "GET") {
        if (delayNextSplitsFetch) {
          delayNextSplitsFetch = false;
          await new Promise<void>((resolve) => {
            resolver = resolve;
          });
        }
        return new Response(JSON.stringify(serverSplits), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.includes("/parse") || url.includes("/blocks")) {
        return new Response(JSON.stringify(serverBlocks), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      throw new Error(`Unhandled fetch: ${method} ${url}`);
    });

    render(<App runJobsInBackground={false} />);

    fireEvent.change(screen.getByTestId("pdf-file-input"), {
      target: { files: [new File(["%PDF-1.4"], "sync_paper.pdf", { type: "application/pdf" })] },
    });
    await waitFor(() => expect(screen.getByTestId("preflight-result-card")).toBeInTheDocument());

    // Enter Screen 2 -> initial sync
    fireEvent.click(screen.getByTestId("open-screen2-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("screen2-sync-status")).toHaveTextContent("最新同期済み");
      expect(screen.getByTestId("block-list-item-doc-sync001:page-1:block-28")).toBeInTheDocument();
    });

    // Leave to Screen 1
    fireEvent.click(screen.getByTestId("nav-screen1-btn"));
    await waitFor(() => expect(screen.getByTestId("screen1-home")).toBeInTheDocument());

    // External API creates split & updates blocks on server while user is away
    const ext = makeExternalSplitBlocks();
    serverDoc = makeDoc(1);
    serverBlocks = ext.blocks;
    serverSplits = ext.splits;
    delayNextSplitsFetch = true;

    // Re-enter Screen 2 -> shows loading indicator first, then renders latest splits & child blocks
    fireEvent.click(screen.getByTestId("nav-screen2-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("screen2-sync-status")).toHaveTextContent("最新状態を取得中");
      expect(screen.getByTestId("screen2-sync-loading-banner")).toBeInTheDocument();
    });
    if (resolver) (resolver as () => void)();
    await waitFor(() => {
      expect(screen.getByTestId("screen2-sync-status")).toHaveTextContent("最新同期済み");
      expect(screen.getByTestId("protection-save-status")).toHaveTextContent("保存済み (rev 1)");
      expect(screen.getByTestId("split-item-split-ext-eq4")).toBeInTheDocument();
      expect(
        screen.getByTestId("block-list-item-doc-sync001:page-1:block-28:split-split-ext-eq4:formula")
      ).toBeInTheDocument();
    });

    // Same-screen normal operation: delete external split -> rev 2, then create split via UI drag -> rev 3
    fireEvent.click(screen.getByTestId("delete-split-split-ext-eq4"));
    await waitFor(() => {
      expect(screen.getByTestId("protection-save-status")).toHaveTextContent("保存済み (rev 2)");
      expect(screen.getByTestId("block-list-item-doc-sync001:page-1:block-28")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("drag-tool-split-btn"));
    const svg = screen.getByTestId("pdf-svg-overlay");
    vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, left: 0, top: 0, width: 595, height: 842, right: 595, bottom: 842, toJSON: () => ({}),
    });
    fireEvent.mouseDown(svg, { clientX: 305, clientY: 314 });
    fireEvent.mouseMove(svg, { clientX: 550, clientY: 339 });
    fireEvent.mouseUp(svg, { clientX: 550, clientY: 339 });

    await waitFor(() => expect(screen.getByTestId("confirm-split-btn")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("confirm-split-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("split-item-split-local-eq4")).toBeInTheDocument();
      expect(screen.getByTestId("protection-save-status")).toHaveTextContent("保存済み (rev 3)");
    });

    fetchMock.mockRestore();
  });

  it("never presents stale state as latest when sync fails and allows recovery via retry", async () => {
    setInMemorySessionTokenForTest(null);

    const serverDoc0 = makeDoc(0);
    const serverBlocks0 = makeInitialBlocks();
    let failSplitsGet = false;

    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method || "GET").toUpperCase();
      const common = makeCommonFetchResponse(url);
      if (common && !url.includes("/protection-regions")) return common;
      if (url.includes("/protection-regions") && method === "GET") {
        return new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.includes("/api/documents/upload") && method === "POST") {
        return new Response(JSON.stringify(serverDoc0), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.endsWith("/api/documents/doc-sync001") && method === "GET") {
        return new Response(JSON.stringify(serverDoc0), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.includes("/block-splits") && method === "GET") {
        if (failSplitsGet) {
          return new Response(JSON.stringify({ detail: "DB temporarily unavailable" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.includes("/parse") || url.includes("/blocks")) {
        return new Response(JSON.stringify(serverBlocks0), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      throw new Error(`Unhandled fetch: ${method} ${url}`);
    });

    render(<App runJobsInBackground={false} />);

    fireEvent.change(screen.getByTestId("pdf-file-input"), {
      target: { files: [new File(["%PDF-1.4"], "sync_paper.pdf", { type: "application/pdf" })] },
    });
    await waitFor(() => expect(screen.getByTestId("preflight-result-card")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("open-screen2-btn"));
    await waitFor(() => expect(screen.getByTestId("screen2-sync-status")).toHaveTextContent("最新同期済み"));

    fireEvent.click(screen.getByTestId("nav-screen1-btn"));
    await waitFor(() => expect(screen.getByTestId("screen1-home")).toBeInTheDocument());

    // Fail /block-splits fetch when returning to Screen 2 via Screen 1 button
    failSplitsGet = true;
    fireEvent.click(screen.getByTestId("open-screen2-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("screen2-sync-error-banner")).toBeInTheDocument();
      expect(screen.getByTestId("screen2-sync-error-banner")).toHaveTextContent("最新状態ではありません");
      expect(screen.getByTestId("screen2-sync-status")).toHaveTextContent("同期失敗");
      expect(screen.getByTestId("protection-save-status")).not.toHaveTextContent("保存済み");
      expect(screen.getByTestId("protection-save-status")).toHaveTextContent("要再同期");
      expect(screen.getByTestId("screen2-start-job-btn")).toBeDisabled();
    });

    // Recover & retry
    failSplitsGet = false;
    fireEvent.click(screen.getByTestId("screen2-retry-sync-btn"));
    await waitFor(() => {
      expect(screen.queryByTestId("screen2-sync-error-banner")).toBeNull();
      expect(screen.getByTestId("screen2-sync-status")).toHaveTextContent("最新同期済み");
      expect(screen.getByTestId("protection-save-status")).toHaveTextContent("保存済み (rev 0)");
      expect(screen.getByTestId("screen2-start-job-btn")).not.toBeDisabled();
    });

    fetchMock.mockRestore();
  });

  it("does not silently overwrite unsaved edits on sync or navigation and applies update only after explicit discard", async () => {
    setInMemorySessionTokenForTest(null);

    let serverDoc = makeDoc(0);
    let serverBlocks = makeInitialBlocks();
    let serverSplits: BlockSplitSpec[] = [];

    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method || "GET").toUpperCase();
      const common = makeCommonFetchResponse(url);
      if (common && !url.includes("/protection-regions")) return common;
      if (url.includes("/protection-regions") && method === "GET") {
        return new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.includes("/api/documents/upload") && method === "POST") {
        return new Response(JSON.stringify(serverDoc), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.endsWith("/api/documents/doc-sync001") && method === "GET") {
        return new Response(JSON.stringify(serverDoc), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.includes("/block-splits/preview") && method === "POST") {
        return new Response(
          JSON.stringify({
            valid: true,
            document_id: "doc-sync001",
            page_index: 0,
            protection_revision: 0,
            parse_revision: 0,
            requested_bbox: [305, 314, 550, 339],
            snapped_bbox: [317.1, 316.3, 543.9, 338.5],
            parent_block_id: "doc-sync001:page-1:block-28",
            parent_bbox: [311.5, 316.3, 543.9, 363.1],
            parent_source_hash: "h".repeat(64),
            parent_signature: "sig-parent",
            parts: [
              {
                role: "formula",
                kind: "isolated_formula",
                will_protect: true,
                bbox: [317.1, 316.3, 543.9, 338.5],
                source_text: "Rising width = a + b (4)",
                label_ja: "選択数式（原文保護）",
              },
            ],
            warnings: [],
            will_invalidate_manual_edits: false,
            affected_manual_block_ids: [],
            has_active_job: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/block-splits") && method === "GET") {
        return new Response(JSON.stringify(serverSplits), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.includes("/parse") || url.includes("/blocks")) {
        return new Response(JSON.stringify(serverBlocks), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      throw new Error(`Unhandled fetch: ${method} ${url}`);
    });

    render(<App runJobsInBackground={false} />);

    fireEvent.change(screen.getByTestId("pdf-file-input"), {
      target: { files: [new File(["%PDF-1.4"], "sync_paper.pdf", { type: "application/pdf" })] },
    });
    await waitFor(() => expect(screen.getByTestId("preflight-result-card")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("open-screen2-btn"));
    await waitFor(() => expect(screen.getByTestId("screen2-sync-status")).toHaveTextContent("最新同期済み"));

    // User enters unsaved split preview & note
    fireEvent.click(screen.getByTestId("drag-tool-split-btn"));
    const svg = screen.getByTestId("pdf-svg-overlay");
    vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, left: 0, top: 0, width: 595, height: 842, right: 595, bottom: 842, toJSON: () => ({}),
    });
    fireEvent.mouseDown(svg, { clientX: 305, clientY: 314 });
    fireEvent.mouseMove(svg, { clientX: 550, clientY: 339 });
    fireEvent.mouseUp(svg, { clientX: 550, clientY: 339 });
    await waitFor(() => expect(screen.getByTestId("split-note-input")).toBeInTheDocument());
    fireEvent.change(screen.getByTestId("split-note-input"), { target: { value: "作業中の下書き" } });

    // External API updates server meanwhile
    const ext = makeExternalSplitBlocks();
    serverDoc = makeDoc(1);
    serverBlocks = ext.blocks;
    serverSplits = ext.splits;

    // 1. Navigation away while unsaved is blocked with confirmation warning
    fireEvent.click(screen.getByTestId("nav-screen1-btn"));
    expect(screen.getByTestId("nav-unsaved-warning")).toHaveTextContent("画面2");
    fireEvent.click(screen.getByText("現在の画面に留まる"));
    expect(screen.queryByTestId("nav-unsaved-warning")).toBeNull();
    expect(screen.getByTestId("split-note-input")).toHaveValue("作業中の下書き");

    // 2. Manual/re-sync while unsaved refuses silent overwrite and shows warning
    fireEvent.click(screen.getByTestId("screen2-refresh-sync-btn"));
    await waitFor(() => expect(screen.getByTestId("screen2-unsaved-sync-warning")).toBeInTheDocument());
    expect(screen.queryByTestId("split-item-split-ext-eq4")).toBeNull();
    expect(screen.getByTestId("split-note-input")).toHaveValue("作業中の下書き");

    fireEvent.click(screen.getByTestId("screen2-keep-unsaved-btn"));
    expect(screen.queryByTestId("screen2-unsaved-sync-warning")).toBeNull();
    expect(screen.getByTestId("split-note-input")).toHaveValue("作業中の下書き");

    // 3. Explicit discard-and-sync fetches latest external state cleanly
    fireEvent.click(screen.getByTestId("screen2-refresh-sync-btn"));
    await waitFor(() => expect(screen.getByTestId("screen2-discard-and-sync-btn")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("screen2-discard-and-sync-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("split-item-split-ext-eq4")).toBeInTheDocument();
      expect(screen.getByTestId("protection-save-status")).toHaveTextContent("保存済み (rev 1)");
      expect(screen.queryByTestId("confirm-split-btn")).toBeNull();
    });

    fetchMock.mockRestore();
  });
});
