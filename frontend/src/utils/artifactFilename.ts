import type { ArtifactType } from "../types/api";

const INVALID_FILENAME_CHARS_RE = /[<>:"/\\|?*\x00-\x1f]/g;

export function sanitizeDownloadFilename(filename: string, fallback: string): string {
  const normalized = filename.replace(/\\/g, "/");
  const base = normalized.split("/").pop() ?? "";
  const cleaned = base
    .replace(INVALID_FILENAME_CHARS_RE, "_")
    .replace(/^[.\s]+|[.\s]+$/g, "");
  return cleaned || fallback;
}

export function buildSuggestedArtifactFilename(
  originalFilename: string | null | undefined,
  documentId: string,
  artifactType: ArtifactType
): string {
  const safeOrig = sanitizeDownloadFilename(originalFilename ?? "", documentId);
  const lastDot = safeOrig.lastIndexOf(".");
  const baseStem = lastDot > 0 ? safeOrig.slice(0, lastDot) : safeOrig;
  const stem = baseStem || documentId;
  switch (artifactType) {
    case "mono_ja_pdf":
      return sanitizeDownloadFilename(`${stem}.ja.pdf`, `${documentId}.ja.pdf`);
    case "dual_bilingual_pdf":
      return sanitizeDownloadFilename(`${stem}.dual.pdf`, `${documentId}.dual.pdf`);
    case "roundtrip_pdf":
      return sanitizeDownloadFilename(`${stem}.roundtrip.pdf`, `${documentId}.roundtrip.pdf`);
    case "validation_report_json":
      return sanitizeDownloadFilename(`${stem}.report.json`, `${documentId}.report.json`);
  }
}
