/**
 * Page Range Parser & Validator (Section 5 & Section 11).
 *
 * UI uses 1-indexed page numbers (`1..totalPages`).
 * Backend internal representations use 0-indexed page indices (`0..totalPages-1`).
 * Explicitly rejects out-of-bounds, reverse ranges (e.g. "5-3"), invalid characters,
 * empty tokens (e.g. "1,,3"), and duplicate page specifications (e.g. "1, 1" or "1-3, 2").
 */

export interface PageRangeParseResult {
  valid: boolean;
  normalizedSpec: string | null; // null means all pages
  displayPages: number[]; // 1-indexed sorted pages
  pageIndices0: number[]; // 0-indexed sorted pages
  error: string | null;
}

const VALID_RANGE_TOKEN_RE = /^(\d+)(?:\s*-\s*(\d+))?$/;

export function displayPageToIndex0(displayPage: number, totalPages?: number): number {
  if (!Number.isInteger(displayPage) || displayPage < 1) {
    throw new Error(`Display page must be an integer >= 1, got ${displayPage}`);
  }
  if (totalPages !== undefined && displayPage > totalPages) {
    throw new Error(`Display page ${displayPage} exceeds total pages ${totalPages}`);
  }
  return displayPage - 1;
}

export function index0ToDisplayPage(pageIndex0: number, totalPages?: number): number {
  if (!Number.isInteger(pageIndex0) || pageIndex0 < 0) {
    throw new Error(`Backend page_index must be an integer >= 0, got ${pageIndex0}`);
  }
  if (totalPages !== undefined && pageIndex0 >= totalPages) {
    throw new Error(`Backend page_index ${pageIndex0} out of range for ${totalPages} pages`);
  }
  return pageIndex0 + 1;
}

export function validateAndParsePageRange(
  spec: string | null | undefined,
  totalPages: number
): PageRangeParseResult {
  if (totalPages <= 0) {
    return {
      valid: false,
      normalizedSpec: null,
      displayPages: [],
      pageIndices0: [],
      error: "総ページ数が0以下のためページ範囲を指定できません。",
    };
  }

  if (spec === null || spec === undefined || spec.trim() === "") {
    const displayPages = Array.from({ length: totalPages }, (_, i) => i + 1);
    const pageIndices0 = Array.from({ length: totalPages }, (_, i) => i);
    return {
      valid: true,
      normalizedSpec: null,
      displayPages,
      pageIndices0,
      error: null,
    };
  }

  const trimmed = spec.trim();
  const rawParts = trimmed.split(",");
  const seenPages = new Set<number>();
  const displayPages: number[] = [];

  for (const rawPart of rawParts) {
    const token = rawPart.trim();
    if (!token) {
      return {
        valid: false,
        normalizedSpec: trimmed,
        displayPages: [],
        pageIndices0: [],
        error: `空のページ指定が含まれています（"${trimmed}"）。カンマ区切りを確認してください。`,
      };
    }

    const match = VALID_RANGE_TOKEN_RE.exec(token);
    if (!match) {
      return {
        valid: false,
        normalizedSpec: trimmed,
        displayPages: [],
        pageIndices0: [],
        error: `不正なページ範囲指定です: "${token}"（「1, 3-5」のような1始まりの数値で入力してください）。`,
      };
    }

    const startPage = Number(match[1]);
    const endPage = match[2] !== undefined ? Number(match[2]) : startPage;

    if (startPage < 1 || endPage < 1) {
      return {
        valid: false,
        normalizedSpec: trimmed,
        displayPages: [],
        pageIndices0: [],
        error: `ページ番号は1以上を指定してください（指定値: "${token}"）。`,
      };
    }

    if (endPage < startPage) {
      return {
        valid: false,
        normalizedSpec: trimmed,
        displayPages: [],
        pageIndices0: [],
        error: `逆順のページ範囲 "${token}" は指定できません（開始ページ <= 終了ページとしてください）。`,
      };
    }

    if (startPage > totalPages || endPage > totalPages) {
      return {
        valid: false,
        normalizedSpec: trimmed,
        displayPages: [],
        pageIndices0: [],
        error: `ページ範囲 "${token}" が総ページ数（1〜${totalPages}ページ）を超えています。`,
      };
    }

    for (let p = startPage; p <= endPage; p++) {
      if (seenPages.has(p)) {
        return {
          valid: false,
          normalizedSpec: trimmed,
          displayPages: [],
          pageIndices0: [],
          error: `ページ ${p} が重複して指定されています（"${trimmed}"）。重複を解消してください。`,
        };
      }
      seenPages.add(p);
      displayPages.push(p);
    }
  }

  displayPages.sort((a, b) => a - b);
  return {
    valid: true,
    normalizedSpec: trimmed,
    displayPages,
    pageIndices0: displayPages.map((p) => p - 1),
    error: null,
  };
}
