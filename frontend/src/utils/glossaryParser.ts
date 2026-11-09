/**
 * Glossary TSV / JSON Parser & Validator (Section 8 & Section 11).
 *
 * Inspects each row/entry for:
 * - Insufficient columns in TSV (`< 2` columns)
 * - Empty source or target term
 * - Duplicate source terms (reporting exact line/row number of the first occurrence)
 * - Invalid JSON syntax or structure
 * Returns structured preview + row-level error list so the user can review before saving.
 */

import type { GlossaryEntry } from "../types/api";

export interface GlossaryRowError {
  rowNumber: number; // 1-indexed line or array element index
  rawText: string;
  reason: string;
}

export interface GlossaryParseReport {
  detectedFormat: "tsv" | "json";
  entries: GlossaryEntry[];
  rowErrors: GlossaryRowError[];
  duplicateCount: number;
  validCount: number;
}

export function parseGlossaryInput(
  rawInput: string,
  format: "tsv" | "json" | "auto" = "auto"
): GlossaryParseReport {
  const trimmed = rawInput.trim();
  const detectedFormat: "tsv" | "json" =
    format !== "auto"
      ? format
      : trimmed.startsWith("[") || trimmed.startsWith("{")
      ? "json"
      : "tsv";

  if (!trimmed) {
    return {
      detectedFormat,
      entries: [],
      rowErrors: [
        {
          rowNumber: 1,
          rawText: "",
          reason: "入力が空です。TSV（原文<TAB>訳語）またはJSON配列を入力してください。",
        },
      ],
      duplicateCount: 0,
      validCount: 0,
    };
  }

  if (detectedFormat === "json") {
    return parseJsonGlossary(trimmed);
  }
  return parseTsvGlossary(rawInput);
}

function parseJsonGlossary(jsonText: string): GlossaryParseReport {
  let parsed: unknown;
  try {
    parsed = JSON.parse(jsonText);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return {
      detectedFormat: "json",
      entries: [],
      rowErrors: [
        {
          rowNumber: 1,
          rawText: jsonText.slice(0, 80),
          reason: `JSON構文エラー: ${msg}`,
        },
      ],
      duplicateCount: 0,
      validCount: 0,
    };
  }

  let rawArray: unknown[] | null = null;
  if (Array.isArray(parsed)) {
    rawArray = parsed;
  } else if (
    parsed !== null &&
    typeof parsed === "object" &&
    Array.isArray((parsed as Record<string, unknown>).entries)
  ) {
    rawArray = (parsed as { entries: unknown[] }).entries;
  }

  if (!rawArray) {
    return {
      detectedFormat: "json",
      entries: [],
      rowErrors: [
        {
          rowNumber: 1,
          rawText: jsonText.slice(0, 80),
          reason: "JSONは配列 `[{source, target, notes}]` または `{entries: [...]}` 形式である必要があります。",
        },
      ],
      duplicateCount: 0,
      validCount: 0,
    };
  }

  const entries: GlossaryEntry[] = [];
  const rowErrors: GlossaryRowError[] = [];
  const seenSourceRow = new Map<string, number>();
  let duplicateCount = 0;

  rawArray.forEach((item, idx) => {
    const rowNum = idx + 1;
    if (item === null || typeof item !== "object" || Array.isArray(item)) {
      rowErrors.push({
        rowNumber: rowNum,
        rawText: JSON.stringify(item),
        reason: `要素 #${rowNum} がオブジェクトではありません。`,
      });
      return;
    }
    const obj = item as Record<string, unknown>;
    const source = typeof obj.source === "string" ? obj.source.trim() : "";
    const target = typeof obj.target === "string" ? obj.target.trim() : "";
    const notes =
      typeof obj.notes === "string" && obj.notes.trim() ? obj.notes.trim() : null;

    if (!source || !target) {
      rowErrors.push({
        rowNumber: rowNum,
        rawText: JSON.stringify(item),
        reason: `要素 #${rowNum}: source（原語）または target（訳語）が空または欠落しています。`,
      });
      return;
    }

    const firstRow = seenSourceRow.get(source);
    if (firstRow !== undefined) {
      duplicateCount += 1;
      rowErrors.push({
        rowNumber: rowNum,
        rawText: `${source} -> ${target}`,
        reason: `原語 "${source}" が要素 #${firstRow} と重複しています。`,
      });
      return;
    }

    seenSourceRow.set(source, rowNum);
    entries.push({ source, target, notes });
  });

  return {
    detectedFormat: "json",
    entries,
    rowErrors,
    duplicateCount,
    validCount: entries.length,
  };
}

function parseTsvGlossary(tsvText: string): GlossaryParseReport {
  const lines = tsvText.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  const entries: GlossaryEntry[] = [];
  const rowErrors: GlossaryRowError[] = [];
  const seenSourceRow = new Map<string, number>();
  let duplicateCount = 0;

  lines.forEach((line, idx) => {
    const rowNum = idx + 1;
    if (!line.trim()) {
      // Skip blank lines unless it's a line containing only tabs/spaces with missing content
      if (line.includes("\t")) {
        rowErrors.push({
          rowNumber: rowNum,
          rawText: line,
          reason: `${rowNum}行目: 原語と訳語の両方が空です。`,
        });
      }
      return;
    }

    // Skip optional TSV header on row 1 (`source\ttarget...`)
    if (
      rowNum === 1 &&
      /^source\t+target(?:\t+notes?)?$/i.test(line.trim())
    ) {
      return;
    }

    const cols = line.split("\t");
    if (cols.length < 2) {
      rowErrors.push({
        rowNumber: rowNum,
        rawText: line,
        reason: `${rowNum}行目: 列数が不足しています（タブ区切りで「原語<TAB>訳語」の2列以上が必要です）。`,
      });
      return;
    }

    const source = cols[0].trim();
    const target = cols[1].trim();
    const notes = cols.length >= 3 && cols[2].trim() ? cols[2].trim() : null;

    if (!source || !target) {
      rowErrors.push({
        rowNumber: rowNum,
        rawText: line,
        reason: `${rowNum}行目: 原語または訳語が空欄です。`,
      });
      return;
    }

    const firstRow = seenSourceRow.get(source);
    if (firstRow !== undefined) {
      duplicateCount += 1;
      rowErrors.push({
        rowNumber: rowNum,
        rawText: line,
        reason: `${rowNum}行目: 原語 "${source}" が ${firstRow}行目 と重複しています。`,
      });
      return;
    }

    seenSourceRow.set(source, rowNum);
    entries.push({ source, target, notes });
  });

  return {
    detectedFormat: "tsv",
    entries,
    rowErrors,
    duplicateCount,
    validCount: entries.length,
  };
}
