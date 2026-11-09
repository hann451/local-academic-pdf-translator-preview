/**
 * Client-side Pre-save Manual Translation Validator (Section 7).
 *
 * Separates hard Errors from Warnings before saving manual edits:
 * - Errors:
 *   - `EMPTY_TRANSLATION`: Empty manual translation string
 *   - `CONTROL_TOKEN_RESIDUE`: `<|plamo:...` control token mixed in
 *   - `PLACEHOLDER_MISSING`: Required `{v1}`..`{vN}` missing
 *   - `PLACEHOLDER_COUNT_MISMATCH`: Duplicate `{v1}`
 *   - `PLACEHOLDER_UNDEFINED`: Undefined `{v99}` introduced
 * - Warnings:
 *   - `CITATION_REF_MISSING`: Bracket citation like `[1]` or `[1, 2]` missing
 *   - `NUMERIC_VALUE_MISMATCH`: Numeric value from masked source missing
 */

import type { Block } from "../types/api";

export interface ManualValidationItem {
  code: string;
  message: string;
}

export interface ManualEditValidationReport {
  errors: ManualValidationItem[];
  warnings: ManualValidationItem[];
}

const PLACEHOLDER_RE = /\{v(\d+)\}/g;
const CITATION_BRACKET_RE = /\[\d+(?:\s*[,–-]\s*\d+)*\]/g;
const NUMBER_RE = /(?<![A-Za-z0-9_])-?\d+(?:\.\d+)?%?(?![A-Za-z0-9_])/g;

export function validateManualEditClientSide(
  block: Pick<Block, "source_text" | "masked_source_text" | "protected_items">,
  candidateText: string
): ManualEditValidationReport {
  const errors: ManualValidationItem[] = [];
  const warnings: ManualValidationItem[] = [];

  if (!candidateText.trim()) {
    errors.push({
      code: "EMPTY_TRANSLATION",
      message: "訳文が空です。原文に戻す場合は「手動修正を解除（AI訳に戻す）」を使用してください。",
    });
    return { errors, warnings };
  }

  if (candidateText.includes("<|plamo:")) {
    errors.push({
      code: "CONTROL_TOKEN_RESIDUE",
      message: "PLaMo制御トークン（<|plamo:...|）が混入しています。削除してください。",
    });
  }

  const expectedPlaceholders = new Set<string>(
    block.protected_items.map((item) => item.placeholder)
  );
  const maskedSrc = block.masked_source_text || block.source_text;
  for (const m of maskedSrc.matchAll(PLACEHOLDER_RE)) {
    expectedPlaceholders.add(`{v${m[1]}}`);
  }

  const foundCounts = new Map<string, number>();
  for (const m of candidateText.matchAll(PLACEHOLDER_RE)) {
    const token = `{v${m[1]}}`;
    foundCounts.set(token, (foundCounts.get(token) || 0) + 1);
  }

  for (const exp of expectedPlaceholders) {
    const cnt = foundCounts.get(exp) || 0;
    if (cnt === 0) {
      errors.push({
        code: "PLACEHOLDER_MISSING",
        message: `数式・保護プレースホルダ ${exp} が欠落しています。`,
      });
    } else if (cnt > 1) {
      errors.push({
        code: "PLACEHOLDER_COUNT_MISMATCH",
        message: `プレースホルダ ${exp} が ${cnt} 回重複しています（1回のみ配置してください）。`,
      });
    }
  }

  for (const foundTok of foundCounts.keys()) {
    if (!expectedPlaceholders.has(foundTok)) {
      errors.push({
        code: "PLACEHOLDER_UNDEFINED",
        message: `原文に存在しない未定義のプレースホルダ ${foundTok} が含まれています。`,
      });
    }
  }

  // Warnings: citations & numbers
  const strippedSrc = maskedSrc.replace(PLACEHOLDER_RE, " ");
  const strippedTgt = candidateText.replace(PLACEHOLDER_RE, " ");

  const srcCitations = new Set(strippedSrc.match(CITATION_BRACKET_RE) || []);
  const tgtCitations = new Set(strippedTgt.match(CITATION_BRACKET_RE) || []);
  for (const cit of srcCitations) {
    if (!tgtCitations.has(cit)) {
      warnings.push({
        code: "CITATION_REF_MISSING",
        message: `引用番号 ${cit} が訳文に見当たりません。`,
      });
    }
  }

  const srcNums = new Set(
    (strippedSrc.replace(CITATION_BRACKET_RE, " ").match(NUMBER_RE) || []).map((n) =>
      n.replace(/,/g, "")
    )
  );
  const tgtNums = new Set(
    (strippedTgt.replace(CITATION_BRACKET_RE, " ").match(NUMBER_RE) || []).map((n) =>
      n.replace(/,/g, "")
    )
  );
  const missingNums = [...srcNums].filter((n) => !tgtNums.has(n));
  if (missingNums.length > 0) {
    warnings.push({
      code: "NUMERIC_VALUE_MISMATCH",
      message: `原文の数値（${missingNums.join(", ")}）が訳文で変更または省略されている可能性があります。`,
    });
  }

  return { errors, warnings };
}
