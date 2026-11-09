import { describe, expect, it, vi } from "vitest";
import {
  displayPageToIndex0,
  index0ToDisplayPage,
  validateAndParsePageRange,
} from "../utils/pageRange";
import {
  backendToViewportBbox,
  getRotatedViewportDimensions,
  normalizeDragSelection,
  pdfBottomLeftToTopLeftBbox,
  viewportToBackendBbox,
  type BBox4,
} from "../utils/coordinates";
import { parseGlossaryInput } from "../utils/glossaryParser";
import { validateManualEditClientSide } from "../utils/manualEditValidator";
import {
  downloadArtifactFile,
  setApiBaseUrl,
  setInMemorySessionTokenForTest,
} from "../api/client";
import { PIPELINE_STAGES_13 } from "../types/api";

describe("Page numbering & Page Range Validator (Section 5 & 11)", () => {
  it("converts 1-indexed display page <-> 0-indexed backend page_index accurately", () => {
    expect(displayPageToIndex0(1, 5)).toBe(0);
    expect(displayPageToIndex0(5, 5)).toBe(4);
    expect(index0ToDisplayPage(0, 5)).toBe(1);
    expect(index0ToDisplayPage(4, 5)).toBe(5);

    expect(() => displayPageToIndex0(0, 5)).toThrow();
    expect(() => displayPageToIndex0(6, 5)).toThrow();
    expect(() => index0ToDisplayPage(-1, 5)).toThrow();
    expect(() => index0ToDisplayPage(5, 5)).toThrow();
  });

  it("accepts empty/all-pages and valid '1, 3-5' range specifications", () => {
    const allRes = validateAndParsePageRange("", 4);
    expect(allRes.valid).toBe(true);
    expect(allRes.displayPages).toEqual([1, 2, 3, 4]);
    expect(allRes.pageIndices0).toEqual([0, 1, 2, 3]);

    const specRes = validateAndParsePageRange("1, 3-5", 6);
    expect(specRes.valid).toBe(true);
    expect(specRes.displayPages).toEqual([1, 3, 4, 5]);
    expect(specRes.pageIndices0).toEqual([0, 2, 3, 4]);
  });

  it("rejects out-of-bounds, reverse ranges, invalid chars, empty tokens, and duplicates", () => {
    expect(validateAndParsePageRange("0", 5).valid).toBe(false);
    expect(validateAndParsePageRange("1-6", 5).valid).toBe(false);
    expect(validateAndParsePageRange("5-3", 5).valid).toBe(false);
    expect(validateAndParsePageRange("abc", 5).valid).toBe(false);
    expect(validateAndParsePageRange("1--3", 5).valid).toBe(false);
    expect(validateAndParsePageRange("1,,3", 5).valid).toBe(false);
    expect(validateAndParsePageRange("1, 1", 5).valid).toBe(false);
    expect(validateAndParsePageRange("1-3, 2", 5).valid).toBe(false);
  });
});

describe("Bidirectional Coordinate Transformation & Drag Normalization (Section 6 & 11)", () => {
  const scales = [0.5, 1.0, 1.25, 2.5];
  const rotations: Array<0 | 90 | 180 | 270> = [0, 90, 180, 270];

  it("round-trips backend pt <-> viewport px across scales 0.5..2.5 and rotations 0/90/180/270", () => {
    const origBbox: BBox4 = [72.25, 108.5, 295.75, 412.0];
    const pageW = 595.0;
    const pageH = 842.0;

    for (const scale of scales) {
      for (const pageRot of rotations) {
        for (const viewRot of rotations) {
          const vpBox = backendToViewportBbox(
            origBbox,
            pageW,
            pageH,
            scale,
            pageRot,
            viewRot
          );
          const restored = viewportToBackendBbox(
            vpBox,
            pageW,
            pageH,
            scale,
            pageRot,
            viewRot
          );
          expect(restored[0]).toBeCloseTo(origBbox[0], 2);
          expect(restored[1]).toBeCloseTo(origBbox[1], 2);
          expect(restored[2]).toBeCloseTo(origBbox[2], 2);
          expect(restored[3]).toBeCloseTo(origBbox[3], 2);
        }
      }
    }
  });

  it("handles CropBox offset and devicePixelRatio dimensions", () => {
    const topLeft = pdfBottomLeftToTopLeftBbox(
      [50, 100, 250, 300],
      [20, 40, 560, 760] // width=540, height=720
    );
    expect(topLeft).toEqual([30, 460, 230, 660]);

    const dims = getRotatedViewportDimensions(540, 720, 1.5, 90, 0, 2.0);
    // 90 deg swaps 540x720 -> 720x540; * 1.5 -> 1080x810 CSS px; * 2.0 DPR -> 2160x1620 canvas px
    expect(dims.cssWidthPx).toBe(1080);
    expect(dims.cssHeightPx).toBe(810);
    expect(dims.canvasWidthPx).toBe(2160);
    expect(dims.canvasHeightPx).toBe(1620);
  });

  it("normalizes reverse drag, clamps out-of-bounds drag, and rejects tiny accidental clicks", () => {
    // Reverse drag (bottom-right to top-left)
    const rev = normalizeDragSelection(300, 250, 100, 80, 600, 800, 8);
    expect(rev.valid).toBe(true);
    expect(rev.viewportBboxPx).toEqual([100, 80, 300, 250]);

    // Out-of-bounds drag clamped to page viewport
    const clamped = normalizeDragSelection(-25, 50, 640, 200, 600, 800, 8);
    expect(clamped.valid).toBe(true);
    expect(clamped.clampedOutOfBounds).toBe(true);
    expect(clamped.viewportBboxPx).toEqual([0, 50, 600, 200]);

    // Tiny accidental click rejected
    const tiny = normalizeDragSelection(100, 100, 104, 103, 600, 800, 8);
    expect(tiny.valid).toBe(false);
    expect(tiny.rejectedReason).toBe("too_small");
  });
});

describe("Glossary TSV / JSON Parser & 1,200 Entries Scaling (Section 8 & 11)", () => {
  it("parses valid TSV and JSON glossaries and reports row-level errors", () => {
    const tsv = [
      "source\ttarget\tnotes",
      "stochastic gradient descent\t確率的勾配降下法\t最適化",
      "missing_second_column_only",
      "empirical risk\t\t訳語空エラー",
      "stochastic gradient descent\t重複用語\t重複エラー",
    ].join("\n");

    const rep = parseGlossaryInput(tsv, "tsv");
    expect(rep.validCount).toBe(1);
    expect(rep.duplicateCount).toBe(1);
    expect(rep.rowErrors.length).toBe(3);
    expect(rep.rowErrors[0].rowNumber).toBe(3); // missing column
    expect(rep.rowErrors[1].rowNumber).toBe(4); // empty target
    expect(rep.rowErrors[2].rowNumber).toBe(5); // duplicate of row 2

    const badJson = parseGlossaryInput("{broken json", "json");
    expect(badJson.validCount).toBe(0);
    expect(badJson.rowErrors[0].reason).toContain("JSON構文エラー");
  });

  it("handles 1,200 glossary entries cleanly", () => {
    const lines = Array.from(
      { length: 1200 },
      (_, i) => `domain_term_${i}\t専門訳語_${i}\tnote_${i}`
    ).join("\n");
    const rep = parseGlossaryInput(lines, "tsv");
    expect(rep.validCount).toBe(1200);
    expect(rep.rowErrors).toHaveLength(0);
  });
});

describe("Manual Edit Pre-save Validator & Security Helpers (Section 4 & 7)", () => {
  it("separates placeholder/control-token Errors from citation/number Warnings", () => {
    const mockBlock = {
      source_text: "Given E(x) = α in 100 steps [1, 2].",
      masked_source_text: "Given {v1} in 100 steps [1, 2].",
      protected_items: [
        {
          placeholder: "{v1}",
          kind: "inline_formula" as const,
          original_text: "E(x) = α",
          bbox: null,
          font_name: null,
        },
      ],
    };

    // Missing {v1}, undefined {v99}, missing [1, 2] and 100
    const bad = validateManualEditClientSide(
      mockBlock,
      "未定義 {v99} と制御 <|plamo:tag|> を含む不正な手動訳文。"
    );
    const errorCodes = bad.errors.map((e) => e.code);
    const warnCodes = bad.warnings.map((w) => w.code);
    expect(errorCodes).toContain("CONTROL_TOKEN_RESIDUE");
    expect(errorCodes).toContain("PLACEHOLDER_MISSING");
    expect(errorCodes).toContain("PLACEHOLDER_UNDEFINED");
    expect(warnCodes).toContain("CITATION_REF_MISSING");
    expect(warnCodes).toContain("NUMERIC_VALUE_MISMATCH");

    // Valid edit
    const good = validateManualEditClientSide(
      mockBlock,
      "100ステップにおいて {v1} が与えられたとする [1, 2]。"
    );
    expect(good.errors).toHaveLength(0);
    expect(good.warnings).toHaveLength(0);
  });

  it("enforces loopback API base URL and revokes Blob URLs after artifact download", async () => {
    expect(() => setApiBaseUrl("https://evil.example.com")).toThrow();
    expect(() => setApiBaseUrl("http://127.0.0.1:8080")).not.toThrow();
    setApiBaseUrl("");

    setInMemorySessionTokenForTest("unit-test-token");
    const createObjSpy = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:http://127.0.0.1:5173/mock-blob-id");
    const revokeObjSpy = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new Uint8Array([0x25, 0x50, 0x44, 0x46]), {
        status: 200,
        headers: {
          "Content-Type": "application/pdf",
          "Content-Disposition": "attachment; filename*=UTF-8''paper.ja.pdf",
        },
      })
    );

    const res = await downloadArtifactFile("job-123", "mono_ja_pdf", "fallback.pdf");
    expect(res.filename).toBe("paper.ja.pdf");
    expect(res.revokedUrl).toBe(true);
    expect(createObjSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjSpy).toHaveBeenCalledWith("blob:http://127.0.0.1:5173/mock-blob-id");

    fetchSpy.mockRestore();
    createObjSpy.mockRestore();
    revokeObjSpy.mockRestore();
    expect(PIPELINE_STAGES_13).toHaveLength(13);
  });
});
