import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Document as ApiDocument } from "../types/api";
import {
  FALLBACK_CANVAS_AREA_PX,
  FALLBACK_CANVAS_EDGE_PX,
  MAX_CANVAS_AREA_PX,
  MAX_CANVAS_EDGE_PX,
  backendToViewportBbox,
  computeSafeCanvasRenderScale,
  getRotatedViewportDimensions,
  viewportToBackendBbox,
} from "../utils/coordinates";

type MockRenderCall = {
  viewport: { scale: number; rotation: number };
  transform: number[] | undefined;
  canvasWidth: number;
  canvasHeight: number;
};

const pdfMockState = vi.hoisted(() => {
  const renderCalls: MockRenderCall[] = [];
  let getDocumentCount = 0;
  let destroyCount = 0;
  let customRenderImpl: ((call: MockRenderCall) => {
    promise: Promise<void>;
    cancel: () => void;
  }) | null = null;

  const mockGetDocument = vi.fn(() => {
    getDocumentCount += 1;
    const mockPage = {
      getViewport: vi.fn(({ scale, rotation }: { scale: number; rotation: number }) => ({
        scale,
        rotation,
      })),
      render: vi.fn(
        ({
          canvasContext,
          viewport,
          transform,
        }: {
          canvasContext: { canvas?: { width: number; height: number } };
          viewport: { scale: number; rotation: number };
          transform?: number[];
        }) => {
          const call: MockRenderCall = {
            viewport,
            transform,
            canvasWidth: canvasContext?.canvas?.width ?? 0,
            canvasHeight: canvasContext?.canvas?.height ?? 0,
          };
          renderCalls.push(call);
          if (customRenderImpl) {
            return customRenderImpl(call);
          }
          return {
            promise: Promise.resolve(),
            cancel: vi.fn(),
          };
        }
      ),
    };
    const mockDoc = {
      numPages: 5,
      getPage: vi.fn(async () => mockPage),
      destroy: vi.fn(async () => {
        destroyCount += 1;
      }),
    };
    return {
      promise: Promise.resolve(mockDoc),
      destroy: vi.fn(async () => {
        destroyCount += 1;
      }),
    };
  });

  return {
    mockGetDocument,
    renderCalls,
    reset() {
      renderCalls.length = 0;
      getDocumentCount = 0;
      destroyCount = 0;
      customRenderImpl = null;
      mockGetDocument.mockClear();
    },
    setCustomRenderImpl(
      fn:
        | ((call: MockRenderCall) => { promise: Promise<void>; cancel: () => void })
        | null
    ) {
      customRenderImpl = fn;
    },
    getCounts() {
      return { getDocumentCount, destroyCount };
    },
  };
});

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: { workerSrc: "" },
  getDocument: pdfMockState.mockGetDocument,
}));

import { PdfPageCanvas } from "../components/PdfPageCanvas";
import { Screen2ProtectionEditor } from "../components/Screen2ProtectionEditor";

const SAMPLE_DOC: ApiDocument = {
  id: "doc-highzoom-test",
  original_filename: "wifi_csi.pdf",
  safe_storage_path: "C:/runtime/uploads/doc-highzoom-test.pdf",
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
      document_id: "doc-highzoom-test",
      page_index: 0,
      width_pt: 595.28,
      height_pt: 841.89,
      rotation_deg: 0,
      media_box: [0, 0, 595.28, 841.89],
      crop_box: [0, 0, 595.28, 841.89],
      has_extractable_text: true,
      is_scanned_like: false,
      cid_ratio: 0.0,
      column_count: 2,
    },
    {
      document_id: "doc-highzoom-test",
      page_index: 1,
      width_pt: 595.28,
      height_pt: 841.89,
      rotation_deg: 90,
      media_box: [0, 0, 595.28, 841.89],
      crop_box: [0, 0, 595.28, 841.89],
      has_extractable_text: true,
      is_scanned_like: false,
      cid_ratio: 0.0,
      column_count: 2,
    },
  ],
  created_at: "2026-11-15T12:00:00Z",
};

describe("High-zoom PDF canvas safety caps & lifecycle", () => {
  const originalGetContext = HTMLCanvasElement.prototype.getContext;
  const originalDpr = Object.getOwnPropertyDescriptor(window, "devicePixelRatio");

  beforeEach(() => {
    pdfMockState.reset();
    HTMLCanvasElement.prototype.getContext = vi.fn(function (
      this: HTMLCanvasElement,
      kind: string
    ) {
      if (kind !== "2d") return null;
      return {
        canvas: this,
        fillStyle: "#ffffff",
        setTransform: vi.fn(),
        fillRect: vi.fn(),
        clearRect: vi.fn(),
      } as unknown as CanvasRenderingContext2D;
    }) as unknown as typeof HTMLCanvasElement.prototype.getContext;
  });

  afterEach(() => {
    HTMLCanvasElement.prototype.getContext = originalGetContext;
    if (originalDpr) {
      Object.defineProperty(window, "devicePixelRatio", originalDpr);
    } else {
      Object.defineProperty(window, "devicePixelRatio", {
        value: 1,
        configurable: true,
        writable: true,
      });
    }
  });

  it("caps canvas backing-store dimensions across high zoom and high DPR while keeping CSS/SVG coords 1:1", () => {
    for (const scale of [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5]) {
      for (const dpr of [1, 2, 3]) {
        const dims = getRotatedViewportDimensions(595.28, 841.89, scale, 0, 0, dpr);
        expect(dims.cssWidthPx).toBeCloseTo(595.28 * scale, 6);
        expect(dims.cssHeightPx).toBeCloseTo(841.89 * scale, 6);
        expect(dims.canvasWidthPx).toBeLessThanOrEqual(MAX_CANVAS_EDGE_PX);
        expect(dims.canvasHeightPx).toBeLessThanOrEqual(MAX_CANVAS_EDGE_PX);
        expect(dims.canvasWidthPx * dims.canvasHeightPx).toBeLessThanOrEqual(
          MAX_CANVAS_AREA_PX
        );
      }
    }

    // Extreme poster page at 250% zoom must also respect MAX_CANVAS_EDGE_PX without clamping CSS px.
    const poster = getRotatedViewportDimensions(2400, 3200, 2.5, 0, 0, 2.0);
    expect(poster.cssWidthPx).toBe(6000);
    expect(poster.cssHeightPx).toBe(8000);
    expect(poster.canvasWidthPx).toBeLessThanOrEqual(MAX_CANVAS_EDGE_PX);
    expect(poster.canvasHeightPx).toBeLessThanOrEqual(MAX_CANVAS_EDGE_PX);
    expect(poster.canvasWidthPx * poster.canvasHeightPx).toBeLessThanOrEqual(
      MAX_CANVAS_AREA_PX
    );

    // Coordinate transforms stay invariant across all rotations at 2.0x and 2.5x zoom.
    const sampleBbox: [number, number, number, number] = [48, 310, 290, 395];
    for (const scale of [2.0, 2.5]) {
      for (const rot of [0, 90, 180, 270]) {
        const vp = backendToViewportBbox(sampleBbox, 595.28, 841.89, scale, rot, 0);
        const back = viewportToBackendBbox(vp, 595.28, 841.89, scale, rot, 0);
        expect(back[0]).toBeCloseTo(sampleBbox[0], 5);
        expect(back[1]).toBeCloseTo(sampleBbox[1], 5);
        expect(back[2]).toBeCloseTo(sampleBbox[2], 5);
        expect(back[3]).toBeCloseTo(sampleBbox[3], 5);
      }
    }
  });

  it("renders high-DPR 250% zoom within caps, passes effective transform, and reuses PDFDocumentProxy across zoom changes", async () => {
    Object.defineProperty(window, "devicePixelRatio", {
      value: 3,
      configurable: true,
      writable: true,
    });
    const pdfBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x34]);

    const { rerender } = render(
      <PdfPageCanvas
        pdfBytes={pdfBytes}
        displayPageNumber={3}
        pageWidthPt={595.28}
        pageHeightPt={841.89}
        pageRotationDeg={0}
        scale={2.5}
        userViewRotationDeg={0}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("pdf-page-container")).toHaveAttribute(
        "data-render-status",
        "rendered"
      );
    });

    const container = screen.getByTestId("pdf-page-container");
    const canvas = screen.getByLabelText("PDF Page Canvas") as HTMLCanvasElement;
    const svg = screen.getByTestId("pdf-svg-overlay");

    expect(canvas.width).toBeLessThanOrEqual(MAX_CANVAS_EDGE_PX);
    expect(canvas.height).toBeLessThanOrEqual(MAX_CANVAS_EDGE_PX);
    expect(canvas.width * canvas.height).toBeLessThanOrEqual(MAX_CANVAS_AREA_PX);
    expect(canvas.style.width).toBe(`${595.28 * 2.5}px`);
    expect(canvas.style.height).toBe(`${841.89 * 2.5}px`);
    expect(svg.getAttribute("viewBox")).toBe(`0 0 ${595.28 * 2.5} ${841.89 * 2.5}`);

    const firstRender = pdfMockState.renderCalls[0];
    expect(firstRender.transform).toBeDefined();
    expect(firstRender.transform![0]).toBeCloseTo(canvas.width / (595.28 * 2.5), 3);
    expect(firstRender.transform![3]).toBeCloseTo(canvas.height / (841.89 * 2.5), 3);

    // Zoom changes with identical pdfBytes reuse the loaded PDFDocumentProxy cleanly.
    rerender(
      <PdfPageCanvas
        pdfBytes={pdfBytes}
        displayPageNumber={3}
        pageWidthPt={595.28}
        pageHeightPt={841.89}
        pageRotationDeg={0}
        scale={2.0}
        userViewRotationDeg={0}
      />
    );
    await waitFor(() => {
      expect(pdfMockState.renderCalls.length).toBeGreaterThanOrEqual(2);
      expect(screen.getByTestId("pdf-page-container")).toHaveAttribute("data-scale", "2");
    });
    expect(pdfMockState.getCounts().getDocumentCount).toBe(1);

    // Replacing pdfBytes destroys the old document once and loads the new bytes.
    const nextBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x37]);
    rerender(
      <PdfPageCanvas
        pdfBytes={nextBytes}
        displayPageNumber={1}
        pageWidthPt={595.28}
        pageHeightPt={841.89}
        pageRotationDeg={0}
        scale={2.0}
        userViewRotationDeg={0}
      />
    );
    await waitFor(() => {
      expect(pdfMockState.getCounts().getDocumentCount).toBe(2);
    });
    expect(pdfMockState.getCounts().destroyCount).toBeGreaterThanOrEqual(1);
    expect(container.textContent).not.toContain("描画フォールバック");
  });

  it("ignores RenderingCancelledException during rapid zoom changes and retries at low-res budget when high-res render rejects", async () => {
    Object.defineProperty(window, "devicePixelRatio", {
      value: 2,
      configurable: true,
      writable: true,
    });
    const pdfBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46]);

    // Case 1: Slow first render is superseded by zoom to 2.5x and rejects with RenderingCancelledException.
    let rejectCancelled: ((err: Error) => void) | null = null;
    let callIdx = 0;
    pdfMockState.setCustomRenderImpl(() => {
      callIdx += 1;
      if (callIdx === 1) {
        return {
          promise: new Promise<void>((_, reject) => {
            rejectCancelled = reject;
          }),
          cancel: () => {
            const err = Object.assign(new Error("Rendering cancelled"), {
              name: "RenderingCancelledException",
            });
            rejectCancelled?.(err);
          },
        };
      }
      return { promise: Promise.resolve(), cancel: vi.fn() };
    });

    const { rerender } = render(
      <PdfPageCanvas
        pdfBytes={pdfBytes}
        displayPageNumber={1}
        pageWidthPt={595.28}
        pageHeightPt={841.89}
        pageRotationDeg={0}
        scale={1.5}
        userViewRotationDeg={0}
      />
    );

    await waitFor(() => {
      expect(pdfMockState.renderCalls.length).toBe(1);
    });

    rerender(
      <PdfPageCanvas
        pdfBytes={pdfBytes}
        displayPageNumber={1}
        pageWidthPt={595.28}
        pageHeightPt={841.89}
        pageRotationDeg={0}
        scale={2.5}
        userViewRotationDeg={0}
      />
    );

    await waitFor(() => {
      expect(pdfMockState.renderCalls.length).toBe(2);
      expect(screen.getByTestId("pdf-page-container")).toHaveAttribute(
        "data-render-status",
        "rendered"
      );
    });
    expect(screen.queryByText(/描画フォールバック/)).not.toBeInTheDocument();

    // Case 2: High-res render rejects with an unexpected GPU/canvas error -> retries at conservative fallback budget.
    pdfMockState.reset();
    let attempt = 0;
    pdfMockState.setCustomRenderImpl(() => {
      attempt += 1;
      if (attempt === 1) {
        return {
          promise: Promise.reject(new Error("Canvas backing store allocation failed")),
          cancel: vi.fn(),
        };
      }
      return { promise: Promise.resolve(), cancel: vi.fn() };
    });

    const freshBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x32]);
    rerender(
      <PdfPageCanvas
        pdfBytes={freshBytes}
        displayPageNumber={1}
        pageWidthPt={595.28}
        pageHeightPt={841.89}
        pageRotationDeg={0}
        scale={2.5}
        userViewRotationDeg={0}
      />
    );

    await waitFor(() => {
      expect(pdfMockState.renderCalls.length).toBe(2);
      expect(screen.getByTestId("pdf-page-container")).toHaveAttribute(
        "data-render-status",
        "rendered"
      );
    });

    const fallbackDpr = computeSafeCanvasRenderScale(
      595.28 * 2.5,
      841.89 * 2.5,
      1,
      FALLBACK_CANVAS_EDGE_PX,
      FALLBACK_CANVAS_AREA_PX
    );
    const retryCall = pdfMockState.renderCalls[1];
    expect(retryCall.canvasWidth).toBeLessThanOrEqual(FALLBACK_CANVAS_EDGE_PX);
    expect(retryCall.canvasHeight).toBeLessThanOrEqual(FALLBACK_CANVAS_EDGE_PX);
    expect(retryCall.canvasWidth).toBe(Math.floor(595.28 * 2.5 * fallbackDpr));
    expect(screen.queryByText(/描画フォールバック/)).not.toBeInTheDocument();
  });

  it("renders Screen2 at 250% zoom inside an unclipped scrollable grid column", async () => {
    Object.defineProperty(window, "devicePixelRatio", {
      value: 3,
      configurable: true,
      writable: true,
    });
    const pdfBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46]);

    render(
      <Screen2ProtectionEditor
        document={SAMPLE_DOC}
        pdfBytes={pdfBytes}
        blocks={[]}
        protectionRegions={[]}
        blockSplits={[]}
        isParsing={false}
        saveStatus="saved"
        saveMessage={null}
        onReparseBlocks={vi.fn()}
        onAddProtectionRegion={vi.fn()}
        onDeleteProtectionRegion={vi.fn()}
        onProceedToStartJob={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("pdf-page-container")).toHaveAttribute(
        "data-render-status",
        "rendered"
      );
    });

    fireEvent.change(screen.getByTestId("zoom-select"), { target: { value: "2.5" } });

    await waitFor(() => {
      expect(screen.getByTestId("pdf-page-container")).toHaveAttribute(
        "data-scale",
        "2.5"
      );
      expect(screen.getByTestId("pdf-page-container")).toHaveAttribute(
        "data-render-status",
        "rendered"
      );
    });

    const scrollBox = screen.getByTestId("pdf-viewer-scroll-container");
    expect(scrollBox.style.overflow).toBe("auto");
    expect(scrollBox.style.minWidth).toBe("0");
    expect(scrollBox.style.display).not.toBe("flex");

    const canvas = screen.getByLabelText("PDF Page Canvas") as HTMLCanvasElement;
    expect(canvas.width).toBeLessThanOrEqual(MAX_CANVAS_EDGE_PX);
    expect(canvas.height).toBeLessThanOrEqual(MAX_CANVAS_EDGE_PX);
    expect(canvas.width * canvas.height).toBeLessThanOrEqual(MAX_CANVAS_AREA_PX);
  });
});
