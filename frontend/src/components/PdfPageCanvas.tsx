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
import { getRotatedViewportDimensions } from "../utils/coordinates";

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
  const [renderStatus, setRenderStatus] = useState<"idle" | "rendering" | "rendered" | "fallback">(
    "idle"
  );
  const [renderError, setRenderError] = useState<string | null>(null);

  const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
  const dims = getRotatedViewportDimensions(
    pageWidthPt,
    pageHeightPt,
    scale,
    pageRotationDeg,
    userViewRotationDeg,
    dpr
  );

  useEffect(() => {
    let cancelled = false;
    let renderTask: { cancel?: () => void; promise?: Promise<unknown> } | null = null;
    let loadingTask: { destroy?: () => Promise<void>; promise: Promise<pdfjsLib.PDFDocumentProxy> } | null =
      null;

    async function renderPdfPage() {
      const canvas = canvasRef.current;
      if (!canvas || !pdfBytes || pdfBytes.byteLength === 0) {
        setRenderStatus("idle");
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

      setRenderStatus("rendering");
      setRenderError(null);

      try {
        // Pass a copy of Uint8Array so PDF.js worker transfer doesn't detach the parent buffer
        const dataCopy = new Uint8Array(pdfBytes);
        loadingTask = pdfjsLib.getDocument({
          data: dataCopy,
          isEvalSupported: false,
          disableAutoFetch: true,
          disableFontFace: false,
        });
        const pdfDoc = await loadingTask.promise;
        if (cancelled) return;

        const safePageNum = Math.max(1, Math.min(pdfDoc.numPages, displayPageNumber));
        const pdfPage = await pdfDoc.getPage(safePageNum);
        if (cancelled) return;

        const viewport = pdfPage.getViewport({
          scale,
          rotation: dims.totalRotationDeg,
        });

        canvas.width = dims.canvasWidthPx;
        canvas.height = dims.canvasHeightPx;
        canvas.style.width = `${dims.cssWidthPx}px`;
        canvas.style.height = `${dims.cssHeightPx}px`;

        const transform =
          dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined;

        renderTask = pdfPage.render({
          canvasContext: ctx,
          viewport,
          transform,
        });
        await renderTask.promise;
        if (!cancelled) {
          setRenderStatus("rendered");
        }
      } catch (err) {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : String(err);
          setRenderError(msg);
          setRenderStatus("fallback");
        }
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
      try {
        void loadingTask?.destroy?.();
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
    dpr,
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
