/**
 * Local-only PDF.js Canvas + SVG Overlay Renderer (Section 2, 4, 6, 7).
 *
 * Security & Fidelity Guarantees:
 * - Bundles PDF.js worker locally via Vite (`?url`), never connecting to any external CDN.
 * - Disables PDF JavaScript evaluation (`isEvalSupported: false`) and never renders clickable
 *   external PDF annotation links (`<a>`), preventing unintended outbound network requests.
 * - Uses authenticated PDF bytes (`Uint8Array` fetched with `X-PaperBridge-Token`).
 * - Aligns `<canvas>` (scaled by `devicePixelRatio`) and `<svg>` overlay (in CSS viewport px)
 *   using `getRotatedViewportDimensions` so `backendToViewportBbox` and `viewportToBackendBbox`
 *   match to 0.01pt across zoom (`0.5..2.5x`), rotation (`0/90/180/270°`), and `CropBox`.
 */

import React, { useEffect, useRef, useState } from "react";
import * as pdfjsLib from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import {
  FALLBACK_CANVAS_AREA_PX,
  FALLBACK_CANVAS_EDGE_PX,
  computeSafeCanvasRenderScale,
  getRotatedViewportDimensions,
} from "../utils/coordinates";

function isRenderCancelledError(err: unknown): boolean {
  if (!err) return false;
  const name =
    typeof err === "object" && "name" in err
      ? String((err as { name?: unknown }).name)
      : "";
  const msg = err instanceof Error ? err.message : String(err);
  return (
    name === "RenderingCancelledException" ||
    msg.includes("RenderingCancelledException")
  );
}

if (typeof window !== "undefined" && pdfjsLib.GlobalWorkerOptions) {
  pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
}

export interface PdfPageCanvasProps {
  pdfBytes: Uint8Array | null;
  displayPageNumber: number; // 1-indexed page number for PDF.js getPage(1..N)
  pageWidthPt: number;
  pageHeightPt: number;
  pageRotationDeg: 0 | 90 | 180 | 270;
  scale: number;
  userViewRotationDeg: 0 | 90 | 180 | 270;
  children?: React.ReactNode; // SVG overlay elements rendered in CSS viewport coordinates
  onViewportMouseDown?: (xPx: number, yPx: number, ev: React.MouseEvent<SVGSVGElement>) => void;
  onViewportMouseMove?: (xPx: number, yPx: number, ev: React.MouseEvent<SVGSVGElement>) => void;
  onViewportMouseUp?: (xPx: number, yPx: number, ev: React.MouseEvent<SVGSVGElement>) => void;
  ariaLabel?: string;
}

export const PdfPageCanvas: React.FC<PdfPageCanvasProps> = ({
  pdfBytes,
  displayPageNumber,
  pageWidthPt,
  pageHeightPt,
  pageRotationDeg,
  scale,
  userViewRotationDeg,
  children,
  onViewportMouseDown,
  onViewportMouseMove,
  onViewportMouseUp,
  ariaLabel = "PDF Page Canvas",
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const docCacheRef = useRef<{
    pdfBytes: Uint8Array;
    loadingTask: {
      destroy?: () => Promise<void>;
      promise: Promise<pdfjsLib.PDFDocumentProxy>;
    };
    doc: pdfjsLib.PDFDocumentProxy | null;
  } | null>(null);
  const activeRenderRef = useRef<{
    task: { cancel?: () => void; promise?: Promise<unknown> } | null;
    promise: Promise<unknown> | null;
  }>({ task: null, promise: null });
  const [renderStatus, setRenderStatus] = useState<
    "idle" | "rendering" | "rendered" | "fallback"
  >("idle");
  const [renderError, setRenderError] = useState<string | null>(null);
  const [renderedCanvasSize, setRenderedCanvasSize] = useState<{
    width: number;
    height: number;
  } | null>(null);

  const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
  const dims = getRotatedViewportDimensions(
    pageWidthPt,
    pageHeightPt,
    scale,
    pageRotationDeg,
    userViewRotationDeg,
    dpr
  );

  // Destroy the cached PDFDocumentProxy only when pdfBytes changes or on unmount so zoom/page changes reuse it.
  useEffect(() => {
    return () => {
      const cached = docCacheRef.current;
      docCacheRef.current = null;
      if (!cached) return;
      try {
        if (cached.loadingTask?.destroy) {
          void cached.loadingTask.destroy();
        } else {
          void cached.doc?.destroy?.();
        }
      } catch {
        // ignore cleanup errors
      }
    };
  }, [pdfBytes]);

  useEffect(() => {
    let cancelled = false;
    let renderTask: { cancel?: () => void; promise?: Promise<unknown> } | null =
      null;

    async function getOrLoadDocument(
      bytes: Uint8Array
    ): Promise<pdfjsLib.PDFDocumentProxy> {
      const existing = docCacheRef.current;
      if (existing && existing.pdfBytes === bytes) {
        if (existing.doc) return existing.doc;
        const resolved = await existing.loadingTask.promise;
        if (docCacheRef.current === existing) {
          existing.doc = resolved;
        }
        return resolved;
      }

      // Pass a copy of Uint8Array so PDF.js worker transfer doesn't detach the parent buffer.
      const dataCopy = new Uint8Array(bytes);
      const loadingTask = pdfjsLib.getDocument({
        data: dataCopy,
        isEvalSupported: false,
        disableAutoFetch: true,
        disableFontFace: false,
      });
      const entry: NonNullable<typeof docCacheRef.current> = {
        pdfBytes: bytes,
        loadingTask,
        doc: null,
      };
      docCacheRef.current = entry;
      try {
        const loaded = await loadingTask.promise;
        if (docCacheRef.current === entry) {
          entry.doc = loaded;
        }
        return loaded;
      } catch (loadErr) {
        if (docCacheRef.current === entry) {
          docCacheRef.current = null;
        }
        throw loadErr;
      }
    }

    async function renderPdfPage() {
      const canvas = canvasRef.current;
      if (!canvas || !pdfBytes || pdfBytes.byteLength === 0) {
        setRenderStatus("idle");
        setRenderedCanvasSize(null);
        return;
      }

      let ctx: CanvasRenderingContext2D | null = null;
      try {
        ctx = canvas.getContext("2d");
      } catch {
        ctx = null;
      }
      if (!ctx) {
        // Test environment (e.g. jsdom without canvas native bindings)
        setRenderStatus("fallback");
        return;
      }
      const canvasCtx = ctx;

      setRenderStatus("rendering");
      setRenderError(null);

      // Wait for any previous in-flight PDF.js render() on this canvas to settle before resizing/re-rendering.
      const prevRender = activeRenderRef.current;
      if (prevRender.task) {
        try {
          prevRender.task.cancel?.();
        } catch {
          // ignore
        }
        if (prevRender.promise) {
          try {
            await prevRender.promise;
          } catch {
            // expected RenderingCancelledException from superseded zoom/page render
          }
        }
        if (cancelled) return;
      }

      try {
        const pdfDoc = await getOrLoadDocument(pdfBytes);
        if (cancelled) return;

        const safePageNum = Math.max(
          1,
          Math.min(pdfDoc.numPages, displayPageNumber)
        );
        const pdfPage = await pdfDoc.getPage(safePageNum);
        if (cancelled) return;

        const attemptRender = async (targetW: number, targetH: number) => {
          const safeCssW = Math.max(1, dims.cssWidthPx);
          const safeCssH = Math.max(1, dims.cssHeightPx);
          const scaleX = targetW / safeCssW;
          const scaleY = targetH / safeCssH;
          const transform =
            Math.abs(scaleX - 1) > 1e-4 || Math.abs(scaleY - 1) > 1e-4
              ? [scaleX, 0, 0, scaleY, 0, 0]
              : undefined;

          canvas.width = targetW;
          canvas.height = targetH;
          canvas.style.width = `${dims.cssWidthPx}px`;
          canvas.style.height = `${dims.cssHeightPx}px`;
          try {
            canvasCtx.setTransform(1, 0, 0, 1, 0, 0);
            canvasCtx.fillStyle = "#ffffff";
            canvasCtx.fillRect(0, 0, targetW, targetH);
          } catch {
            // ignore on minimal mock 2d contexts
          }

          const viewport = pdfPage.getViewport({
            scale,
            rotation: dims.totalRotationDeg,
          });

          renderTask = pdfPage.render({
            canvasContext: canvasCtx,
            viewport,
            transform,
          });
          activeRenderRef.current = {
            task: renderTask,
            promise: renderTask.promise ?? null,
          };
          try {
            await renderTask.promise;
          } finally {
            if (activeRenderRef.current.task === renderTask) {
              activeRenderRef.current = { task: null, promise: null };
            }
          }
        };

        try {
          await attemptRender(dims.canvasWidthPx, dims.canvasHeightPx);
          if (!cancelled) {
            setRenderedCanvasSize({
              width: dims.canvasWidthPx,
              height: dims.canvasHeightPx,
            });
            setRenderStatus("rendered");
          }
        } catch (primaryErr) {
          if (cancelled || isRenderCancelledError(primaryErr)) {
            return;
          }
          // Retry once at a conservative backing-store budget if the browser/GPU/RDP rejected the larger buffer.
          const fallbackDpr = computeSafeCanvasRenderScale(
            dims.cssWidthPx,
            dims.cssHeightPx,
            1,
            FALLBACK_CANVAS_EDGE_PX,
            FALLBACK_CANVAS_AREA_PX
          );
          const fallbackW = Math.max(
            1,
            Math.floor(dims.cssWidthPx * fallbackDpr)
          );
          const fallbackH = Math.max(
            1,
            Math.floor(dims.cssHeightPx * fallbackDpr)
          );
          if (
            fallbackW < dims.canvasWidthPx ||
            fallbackH < dims.canvasHeightPx
          ) {
            await attemptRender(fallbackW, fallbackH);
            if (!cancelled) {
              setRenderedCanvasSize({ width: fallbackW, height: fallbackH });
              setRenderStatus("rendered");
            }
            return;
          }
          throw primaryErr;
        }
      } catch (err) {
        if (cancelled || isRenderCancelledError(err)) {
          return;
        }
        const msg = err instanceof Error ? err.message : String(err);
        setRenderError(msg);
        setRenderStatus("fallback");
      }
    }

    void renderPdfPage();

    return () => {
      cancelled = true;
      try {
        renderTask?.cancel?.();
      } catch {
        // ignore
      }
    };
  }, [
    pdfBytes,
    displayPageNumber,
    scale,
    dims.totalRotationDeg,
    dims.canvasWidthPx,
    dims.canvasHeightPx,
    dims.cssWidthPx,
    dims.cssHeightPx,
  ]);

  const toLocalCoords = (ev: React.MouseEvent<SVGSVGElement>): [number, number] => {
    const svg = svgRef.current;
    if (!svg) return [0, 0];
    const rect = svg.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) {
      return [ev.nativeEvent.offsetX || 0, ev.nativeEvent.offsetY || 0];
    }
    return [ev.clientX - rect.left, ev.clientY - rect.top];
  };

  return (
    <div
      className="pdf-canvas-wrapper"
      style={{
        position: "relative",
        flexShrink: 0,
        width: `${dims.cssWidthPx}px`,
        height: `${dims.cssHeightPx}px`,
        backgroundColor: "#ffffff",
        boxShadow: "0 2px 10px rgba(15, 23, 42, 0.14)",
        border: "1px solid #cbd5e1",
        userSelect: "none",
        overflow: "hidden",
      }}
      data-testid="pdf-page-container"
      data-display-page={displayPageNumber}
      data-scale={scale}
      data-total-rotation={dims.totalRotationDeg}
      data-render-status={renderStatus}
      data-canvas-width={renderedCanvasSize?.width ?? dims.canvasWidthPx}
      data-canvas-height={renderedCanvasSize?.height ?? dims.canvasHeightPx}
    >
      <canvas
        ref={canvasRef}
        aria-label={ariaLabel}
        style={{
          display: "block",
          width: `${dims.cssWidthPx}px`,
          height: `${dims.cssHeightPx}px`,
        }}
      />
      {renderError && (
        <div
          style={{
            position: "absolute",
            bottom: 4,
            right: 4,
            fontSize: "11px",
            background: "rgba(254, 242, 242, 0.92)",
            color: "#991b1b",
            padding: "2px 6px",
            borderRadius: "4px",
            pointerEvents: "none",
          }}
        >
          PDF Canvas 描画フォールバック ({ dims.cssWidthPx }×{ dims.cssHeightPx }px)
        </div>
      )}
      <svg
        ref={svgRef}
        data-testid="pdf-svg-overlay"
        width={dims.cssWidthPx}
        height={dims.cssHeightPx}
        viewBox={`0 0 ${dims.cssWidthPx} ${dims.cssHeightPx}`}
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          width: `${dims.cssWidthPx}px`,
          height: `${dims.cssHeightPx}px`,
          cursor: onViewportMouseDown ? "crosshair" : "default",
        }}
        onMouseDown={(ev) => {
          if (onViewportMouseDown) {
            const [x, y] = toLocalCoords(ev);
            onViewportMouseDown(x, y, ev);
          }
        }}
        onMouseMove={(ev) => {
          if (onViewportMouseMove) {
            const [x, y] = toLocalCoords(ev);
            onViewportMouseMove(x, y, ev);
          }
        }}
        onMouseUp={(ev) => {
          if (onViewportMouseUp) {
            const [x, y] = toLocalCoords(ev);
            onViewportMouseUp(x, y, ev);
          }
        }}
      >
        {children}
      </svg>
    </div>
  );
};
