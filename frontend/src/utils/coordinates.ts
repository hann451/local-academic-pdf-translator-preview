/**
 * Coordinate Systems & Bidirectional Transformations (Section 3, 6, 11).
 *
 * Matches `backend/pdf/coordinates.py` to 0.01pt accuracy across:
 * - Zoom scales `0.5` .. `2.5` (and arbitrary positive scales)
 * - Page rotations `0`, `90`, `180`, `270` + user view rotation `0`, `90`, `180`, `270`
 * - `CropBox` offsets `(cropX0, cropY0)`
 * - High-DPI canvas backing store (`devicePixelRatio`) vs CSS/SVG viewport pixels
 * - Drag selection normalization (reverse drag, out-of-bounds clamping, minimum size threshold)
 */

export type BBox4 = [number, number, number, number];

export function normalizeRotation(deg: number): 0 | 90 | 180 | 270 {
  const norm = ((deg % 360) + 360) % 360;
  if (norm === 0 || norm === 90 || norm === 180 || norm === 270) {
    return norm;
  }
  throw new Error(`Rotation must be 0, 90, 180, or 270, got ${deg}`);
}

/**
 * Convert raw PDF bottom-left pt coordinates into canonical unrotated CropBox top-left pt coordinates.
 */
export function pdfBottomLeftToTopLeftBbox(
  pdfBbox: BBox4,
  cropBox: BBox4
): BBox4 {
  const [x0, y0, x1, y1] = pdfBbox;
  const [cx0, cy0, cx1, cy1] = cropBox;
  const widthPt = cx1 - cx0;
  const heightPt = cy1 - cy0;
  if (widthPt <= 0 || heightPt <= 0) {
    throw new Error(`Invalid CropBox dimensions: ${JSON.stringify(cropBox)}`);
  }
  const bx0 = x0 - cx0;
  const bx1 = x1 - cx0;
  const by0 = heightPt - (y1 - cy0);
  const by1 = heightPt - (y0 - cy0);
  return [
    Math.min(bx0, bx1),
    Math.min(by0, by1),
    Math.max(bx0, bx1),
    Math.max(by0, by1),
  ];
}

/**
 * Compute CSS viewport width/height (in CSS px) and physical canvas dimensions (with devicePixelRatio).
 */
export function getRotatedViewportDimensions(
  pageWidthPt: number,
  pageHeightPt: number,
  scale: number,
  pageRotationDeg: number = 0,
  userViewRotationDeg: number = 0,
  devicePixelRatio: number = 1
): {
  cssWidthPx: number;
  cssHeightPx: number;
  canvasWidthPx: number;
  canvasHeightPx: number;
  totalRotationDeg: 0 | 90 | 180 | 270;
} {
  if (scale <= 0) {
    throw new Error(`Scale must be > 0, got ${scale}`);
  }
  const totalRotationDeg = normalizeRotation(pageRotationDeg + userViewRotationDeg);
  const isSwapped = totalRotationDeg === 90 || totalRotationDeg === 270;
  const cssWidthPx = (isSwapped ? pageHeightPt : pageWidthPt) * scale;
  const cssHeightPx = (isSwapped ? pageWidthPt : pageHeightPt) * scale;
  const dpr = Math.max(1, devicePixelRatio || 1);
  return {
    cssWidthPx,
    cssHeightPx,
    canvasWidthPx: Math.round(cssWidthPx * dpr),
    canvasHeightPx: Math.round(cssHeightPx * dpr),
    totalRotationDeg,
  };
}

/**
 * Convert backend canonical bbox `[x0, y0, x1, y1]` (unrotated CropBox top-left pt)
 * to CSS/SVG viewport `[vx0, vy0, vx1, vy1]` (top-left px).
 */
export function backendToViewportBbox(
  bboxPt: BBox4,
  pageWidthPt: number,
  pageHeightPt: number,
  scale: number = 1.0,
  pageRotationDeg: number = 0,
  userViewRotationDeg: number = 0
): BBox4 {
  if (scale <= 0) {
    throw new Error(`Scale must be > 0, got ${scale}`);
  }
  const rot = normalizeRotation(pageRotationDeg + userViewRotationDeg);
  const [x0, y0, x1, y1] = bboxPt;
  const w = pageWidthPt;
  const h = pageHeightPt;

  let rx0: number, ry0: number, rx1: number, ry1: number;
  if (rot === 0) {
    [rx0, ry0, rx1, ry1] = [x0, y0, x1, y1];
  } else if (rot === 90) {
    // 90 deg clockwise: new_x = h - y, new_y = x
    [rx0, ry0, rx1, ry1] = [h - y1, x0, h - y0, x1];
  } else if (rot === 180) {
    [rx0, ry0, rx1, ry1] = [w - x1, h - y1, w - x0, h - y0];
  } else {
    // 270 deg clockwise: new_x = y, new_y = w - x
    [rx0, ry0, rx1, ry1] = [y0, w - x1, y1, w - x0];
  }

  return [
    Math.min(rx0, rx1) * scale,
    Math.min(ry0, ry1) * scale,
    Math.max(rx0, rx1) * scale,
    Math.max(ry0, ry1) * scale,
  ];
}

/**
 * Convert CSS/SVG viewport bbox `[vx0, vy0, vx1, vy1]` (top-left px)
 * back to backend canonical `[x0, y0, x1, y1]` (unrotated CropBox top-left pt).
 */
export function viewportToBackendBbox(
  viewportBboxPx: BBox4,
  pageWidthPt: number,
  pageHeightPt: number,
  scale: number = 1.0,
  pageRotationDeg: number = 0,
  userViewRotationDeg: number = 0
): BBox4 {
  if (scale <= 0) {
    throw new Error(`Scale must be > 0, got ${scale}`);
  }
  const rot = normalizeRotation(pageRotationDeg + userViewRotationDeg);
  const rx0 = viewportBboxPx[0] / scale;
  const ry0 = viewportBboxPx[1] / scale;
  const rx1 = viewportBboxPx[2] / scale;
  const ry1 = viewportBboxPx[3] / scale;
  const w = pageWidthPt;
  const h = pageHeightPt;

  let x0: number, y0: number, x1: number, y1: number;
  if (rot === 0) {
    [x0, y0, x1, y1] = [rx0, ry0, rx1, ry1];
  } else if (rot === 90) {
    // Inverse of (h - y1, x0, h - y0, x1)
    [x0, y0, x1, y1] = [ry0, h - rx1, ry1, h - rx0];
  } else if (rot === 180) {
    [x0, y0, x1, y1] = [w - rx1, h - ry1, w - rx0, h - ry0];
  } else {
    // Inverse of (y0, w - x1, y1, w - x0)
    [x0, y0, x1, y1] = [w - ry1, rx0, w - ry0, rx1];
  }

  return [
    Number(Math.min(x0, x1).toFixed(2)),
    Number(Math.min(y0, y1).toFixed(2)),
    Number(Math.max(x0, x1).toFixed(2)),
    Number(Math.max(y0, y1).toFixed(2)),
  ];
}

export interface NormalizedDragResult {
  valid: boolean;
  viewportBboxPx: BBox4 | null;
  clampedOutOfBounds: boolean;
  rejectedReason: "too_small" | "outside_page" | null;
}

/**
 * Normalize a user drag rectangle in CSS viewport coordinates:
 * - Supports reverse drag (`endX < startX` or `endY < startY`)
 * - Clamps coordinates to `[0, viewportWidthPx]` x `[0, viewportHeightPx]`
 * - Rejects tiny accidental clicks (`width < minSizePx` or `height < minSizePx`)
 */
export function normalizeDragSelection(
  startX: number,
  startY: number,
  endX: number,
  endY: number,
  viewportWidthPx: number,
  viewportHeightPx: number,
  minSizePx: number = 8
): NormalizedDragResult {
  const rawX0 = Math.min(startX, endX);
  const rawY0 = Math.min(startY, endY);
  const rawX1 = Math.max(startX, endX);
  const rawY1 = Math.max(startY, endY);

  if (rawX1 <= 0 || rawY1 <= 0 || rawX0 >= viewportWidthPx || rawY0 >= viewportHeightPx) {
    return {
      valid: false,
      viewportBboxPx: null,
      clampedOutOfBounds: true,
      rejectedReason: "outside_page",
    };
  }

  const clampedX0 = Math.max(0, Math.min(viewportWidthPx, rawX0));
  const clampedY0 = Math.max(0, Math.min(viewportHeightPx, rawY0));
  const clampedX1 = Math.max(0, Math.min(viewportWidthPx, rawX1));
  const clampedY1 = Math.max(0, Math.min(viewportHeightPx, rawY1));

  const clampedOutOfBounds =
    clampedX0 !== rawX0 ||
    clampedY0 !== rawY0 ||
    clampedX1 !== rawX1 ||
    clampedY1 !== rawY1;

  if (clampedX1 - clampedX0 < minSizePx || clampedY1 - clampedY0 < minSizePx) {
    return {
      valid: false,
      viewportBboxPx: null,
      clampedOutOfBounds,
      rejectedReason: "too_small",
    };
  }

  return {
    valid: true,
    viewportBboxPx: [clampedX0, clampedY0, clampedX1, clampedY1],
    clampedOutOfBounds,
    rejectedReason: null,
  };
}
