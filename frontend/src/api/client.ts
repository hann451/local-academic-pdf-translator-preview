/**
 * PaperBridge Local API Client (Section 3 & Section 4).
 *
 * Security & Boundary Guarantees:
 * - Strictly local loopback (`127.0.0.1`).
 * - Stores `X-PaperBridge-Token` in memory only (never in URL query string, localStorage, or bundle).
 * - Supplies `X-PaperBridge-Token` on all API calls, PDF.js binary fetches, and artifact downloads.
 * - Releases temporary Blob URLs via `URL.revokeObjectURL` after artifact downloads.
 */

import type {
  ArtifactType,
  AuthSessionResponse,
  Block,
  Document,
  ExportArtifact,
  Glossary,
  GlossaryEntry,
  Job,
  ProtectionMode,
  ProtectionRegion,
  RetypesetJobResponse,
  SystemDiagnostics,
  TranslationJobConfig,
  ValidationIssue,
} from "../types/api";

let inMemorySessionToken: string | null = null;
let apiBaseUrl = ""; // Relative to current origin (Vite proxy on :5173 or FastAPI static on :8080)

export function setApiBaseUrl(baseUrl: string): void {
  const trimmed = baseUrl.replace(/\/+$/, "");
  if (trimmed && !/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/i.test(trimmed)) {
    throw new Error(
      `Security policy violation: API base URL must be loopback (127.0.0.1/localhost), got "${baseUrl}"`
    );
  }
  apiBaseUrl = trimmed;
}

export function getInMemorySessionToken(): string | null {
  return inMemorySessionToken;
}

export function setInMemorySessionTokenForTest(token: string | null): void {
  inMemorySessionToken = token;
}

export class ApiHttpError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(`API Error (${status}): ${detail}`);
    this.name = "ApiHttpError";
    this.status = status;
    this.detail = detail;
  }
}

async function parseErrorDetail(resp: Response): Promise<string> {
  try {
    const data = (await resp.json()) as { detail?: unknown };
    if (typeof data?.detail === "string") {
      return data.detail;
    }
    if (data?.detail !== undefined) {
      return JSON.stringify(data.detail);
    }
  } catch {
    // ignore JSON parse failure
  }
  return resp.statusText || `HTTP ${resp.status}`;
}

export async function ensureSessionToken(reissue: boolean = false): Promise<string> {
  if (inMemorySessionToken && !reissue) {
    return inMemorySessionToken;
  }
  const url = `${apiBaseUrl}/api/auth/session${reissue ? "?reissue=true" : ""}`;
  const resp = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json" },
  });
  if (!resp.ok) {
    throw new ApiHttpError(resp.status, await parseErrorDetail(resp));
  }
  const data = (await resp.json()) as AuthSessionResponse;
  inMemorySessionToken = data.token;
  return data.token;
}

export async function rotateSessionToken(): Promise<AuthSessionResponse> {
  const token = await ensureSessionToken();
  const resp = await fetch(`${apiBaseUrl}/api/auth/rotate`, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "X-PaperBridge-Token": token,
    },
  });
  if (!resp.ok) {
    throw new ApiHttpError(resp.status, await parseErrorDetail(resp));
  }
  const data = (await resp.json()) as AuthSessionResponse;
  inMemorySessionToken = data.token;
  return data;
}

export async function revokeSessionToken(): Promise<{ revoked: boolean }> {
  const token = await ensureSessionToken();
  const resp = await fetch(`${apiBaseUrl}/api/auth/revoke`, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "X-PaperBridge-Token": token,
    },
  });
  if (!resp.ok) {
    throw new ApiHttpError(resp.status, await parseErrorDetail(resp));
  }
  inMemorySessionToken = null;
  return (await resp.json()) as { revoked: boolean };
}

async function apiRequest<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = await ensureSessionToken();
  const headers = new Headers(options.headers || {});
  headers.set("X-PaperBridge-Token", token);
  if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const resp = await fetch(`${apiBaseUrl}${path}`, {
    ...options,
    headers,
  });

  if (!resp.ok) {
    throw new ApiHttpError(resp.status, await parseErrorDetail(resp));
  }
  return (await resp.json()) as T;
}

// --- Diagnostics ---
export async function fetchSystemDiagnostics(
  probeLlama: boolean = false
): Promise<SystemDiagnostics> {
  return apiRequest<SystemDiagnostics>(
    `/api/diagnostics?probe_llama=${probeLlama ? "true" : "false"}`
  );
}

// --- Documents & PDFs ---
export async function uploadPdfDocument(file: File): Promise<Document> {
  const form = new FormData();
  form.append("file", file, file.name);
  return apiRequest<Document>("/api/documents/upload", {
    method: "POST",
    body: form,
  });
}

export async function fetchDocument(documentId: string): Promise<Document> {
  return apiRequest<Document>(`/api/documents/${encodeURIComponent(documentId)}`);
}

/**
 * Fetch PDF bytes with `X-PaperBridge-Token` authentication header for PDF.js rendering.
 * Never disables auth or appends token to URL.
 */
export async function fetchDocumentPdfBytes(documentId: string): Promise<{
  data: Uint8Array;
  httpHeaders: Record<string, string>;
}> {
  const token = await ensureSessionToken();
  const resp = await fetch(
    `${apiBaseUrl}/api/documents/${encodeURIComponent(documentId)}/pdf`,
    {
      method: "GET",
      headers: {
        "X-PaperBridge-Token": token,
      },
    }
  );
  if (!resp.ok) {
    throw new ApiHttpError(resp.status, await parseErrorDetail(resp));
  }
  const buf = await resp.arrayBuffer();
  return {
    data: new Uint8Array(buf),
    httpHeaders: { "X-PaperBridge-Token": token },
  };
}

export async function fetchArtifactPdfBytes(
  jobId: string,
  artifactType: "mono_ja_pdf" | "dual_bilingual_pdf" | "roundtrip_pdf"
): Promise<{
  data: Uint8Array;
  httpHeaders: Record<string, string>;
}> {
  const token = await ensureSessionToken();
  const resp = await fetch(
    `${apiBaseUrl}/api/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(
      artifactType
    )}/download?inline=true`,
    {
      method: "GET",
      headers: {
        "X-PaperBridge-Token": token,
      },
    }
  );
  if (!resp.ok) {
    throw new ApiHttpError(resp.status, await parseErrorDetail(resp));
  }
  const buf = await resp.arrayBuffer();
  return {
    data: new Uint8Array(buf),
    httpHeaders: { "X-PaperBridge-Token": token },
  };
}

export async function parseDocumentBlocks(
  documentId: string,
  jobConfig?: Partial<TranslationJobConfig>
): Promise<Block[]> {
  return apiRequest<Block[]>(
    `/api/documents/${encodeURIComponent(documentId)}/parse`,
    {
      method: "POST",
      body: jobConfig ? JSON.stringify(jobConfig) : undefined,
    }
  );
}

export async function fetchDocumentBlocks(
  documentId: string,
  pageIndex?: number,
  autoParse: boolean = false
): Promise<Block[]> {
  const params = new URLSearchParams();
  if (pageIndex !== undefined) {
    params.set("page_index", String(pageIndex));
  }
  if (autoParse) {
    params.set("auto_parse", "true");
  }
  const qs = params.toString() ? `?${params.toString()}` : "";
  return apiRequest<Block[]>(
    `/api/documents/${encodeURIComponent(documentId)}/blocks${qs}`
  );
}

// --- Protection Regions ---
export async function fetchProtectionRegions(
  documentId: string,
  pageIndex?: number
): Promise<ProtectionRegion[]> {
  const qs = pageIndex !== undefined ? `?page_index=${pageIndex}` : "";
  return apiRequest<ProtectionRegion[]>(
    `/api/documents/${encodeURIComponent(documentId)}/protection-regions${qs}`
  );
}

export async function createProtectionRegion(
  documentId: string,
  payload: {
    page_index: number;
    bbox: [number, number, number, number];
    mode: ProtectionMode;
    note?: string | null;
  }
): Promise<ProtectionRegion> {
  return apiRequest<ProtectionRegion>(
    `/api/documents/${encodeURIComponent(documentId)}/protection-regions`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    }
  );
}

export async function deleteProtectionRegion(
  documentId: string,
  regionId: string
): Promise<{ deleted: boolean }> {
  return apiRequest<{ deleted: boolean }>(
    `/api/documents/${encodeURIComponent(documentId)}/protection-regions/${encodeURIComponent(
      regionId
    )}`,
    {
      method: "DELETE",
    }
  );
}

// --- Manual Block Edit ---
export async function saveBlockManualEdit(
  documentId: string,
  blockId: string,
  manualTranslationText: string | null,
  expectedEditRevision?: number
): Promise<Block> {
  return apiRequest<Block>(
    `/api/documents/${encodeURIComponent(documentId)}/blocks/${encodeURIComponent(
      blockId
    )}/manual-edit`,
    {
      method: "PUT",
      body: JSON.stringify({
        manual_translation_text: manualTranslationText,
        expected_edit_revision: expectedEditRevision,
      }),
    }
  );
}

// --- Auto Protection ---
export async function updateBlockProtection(
  documentId: string,
  blockId: string,
  override: "force_translate" | "force_protect" | null
): Promise<Block> {
  return apiRequest<Block>(
    `/api/documents/${encodeURIComponent(documentId)}/blocks/${encodeURIComponent(
      blockId
    )}/protection`,
    { method: "PUT", body: JSON.stringify({ user_protection_override: override }) }
  );
}

export async function reapplyAutoProtection(
  documentId: string,
  resetUserOverrides: boolean = true,
  clearManualRegions: boolean = false
): Promise<Record<string, unknown>> {
  return apiRequest<Record<string, unknown>>(
    `/api/documents/${encodeURIComponent(documentId)}/reapply-auto-protection`,
    {
      method: "POST",
      body: JSON.stringify({
        reset_user_overrides: resetUserOverrides,
        clear_manual_regions: clearManualRegions,
      }),
    }
  );
}

// --- Jobs ---
export async function startTranslationJob(
  documentId: string,
  config: TranslationJobConfig,
  background: boolean = true
): Promise<Job> {
  return apiRequest<Job>(
    `/api/documents/${encodeURIComponent(documentId)}/jobs?background=${
      background ? "true" : "false"
    }`,
    {
      method: "POST",
      body: JSON.stringify(config),
    }
  );
}

export async function fetchJob(jobId: string): Promise<Job> {
  return apiRequest<Job>(`/api/jobs/${encodeURIComponent(jobId)}`);
}

export async function cancelTranslationJob(jobId: string): Promise<Job> {
  return apiRequest<Job>(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
  });
}

export async function resumeTranslationJob(
  jobId: string,
  background: boolean = true
): Promise<Job> {
  return apiRequest<Job>(
    `/api/jobs/${encodeURIComponent(jobId)}/resume?background=${
      background ? "true" : "false"
    }`,
    {
      method: "POST",
    }
  );
}

export async function retranslateSpecificBlocks(
  jobId: string,
  blockIds: string[]
): Promise<Job> {
  return apiRequest<Job>(
    `/api/jobs/${encodeURIComponent(jobId)}/retranslate-blocks`,
    {
      method: "POST",
      body: JSON.stringify({ block_ids: blockIds }),
    }
  );
}

export async function retypesetJob(
  jobId: string,
  options: {
    expected_edit_revision?: number;
    allow_validation_warnings?: boolean;
  } = {}
): Promise<RetypesetJobResponse> {
  return apiRequest<RetypesetJobResponse>(
    `/api/jobs/${encodeURIComponent(jobId)}/retypeset`,
    {
      method: "POST",
      body: JSON.stringify({
        expected_edit_revision: options.expected_edit_revision,
        allow_validation_warnings: options.allow_validation_warnings ?? true,
      }),
    }
  );
}

export async function fetchJobIssues(jobId: string): Promise<ValidationIssue[]> {
  return apiRequest<ValidationIssue[]>(
    `/api/jobs/${encodeURIComponent(jobId)}/issues`
  );
}

export async function fetchJobArtifacts(jobId: string): Promise<ExportArtifact[]> {
  return apiRequest<ExportArtifact[]>(
    `/api/jobs/${encodeURIComponent(jobId)}/artifacts`
  );
}

/**
 * Download an artifact using authenticated fetch + temporary Blob URL,
 * revoking the Blob URL immediately after triggering the download (Section 4 & 7).
 */
export async function downloadArtifactFile(
  jobId: string,
  artifactType: ArtifactType,
  fallbackFilename: string
): Promise<{ filename: string; byteSize: number; revokedUrl: boolean }> {
  const token = await ensureSessionToken();
  const resp = await fetch(
    `${apiBaseUrl}/api/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(
      artifactType
    )}/download`,
    {
      method: "GET",
      headers: {
        "X-PaperBridge-Token": token,
      },
    }
  );
  if (!resp.ok) {
    throw new ApiHttpError(resp.status, await parseErrorDetail(resp));
  }

  let filename = fallbackFilename;
  const disp = resp.headers.get("Content-Disposition");
  if (disp) {
    const utf8Match = /filename\*=UTF-8''([^;]+)/i.exec(disp);
    if (utf8Match) {
      filename = decodeURIComponent(utf8Match[1]);
    }
  }

  const blob = await resp.blob();
  const blobUrl = URL.createObjectURL(blob);
  let revokedUrl = false;
  try {
    const anchor = document.createElement("a");
    anchor.href = blobUrl;
    anchor.download = filename;
    anchor.rel = "noopener noreferrer";
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
  } finally {
    URL.revokeObjectURL(blobUrl);
    revokedUrl = true;
  }

  return { filename, byteSize: blob.size, revokedUrl };
}

// --- Glossaries ---
export async function fetchGlossaries(): Promise<Glossary[]> {
  return apiRequest<Glossary[]>("/api/glossaries");
}

export async function createGlossary(
  name: string,
  entries: GlossaryEntry[]
): Promise<Glossary> {
  return apiRequest<Glossary>("/api/glossaries", {
    method: "POST",
    body: JSON.stringify({ name, entries }),
  });
}

export async function updateGlossary(
  glossaryId: string,
  name: string,
  entries: GlossaryEntry[]
): Promise<Glossary> {
  return apiRequest<Glossary>(
    `/api/glossaries/${encodeURIComponent(glossaryId)}`,
    {
      method: "PUT",
      body: JSON.stringify({ name, entries }),
    }
  );
}

export async function deleteGlossary(
  glossaryId: string
): Promise<{ deleted: boolean; id: string }> {
  return apiRequest<{ deleted: boolean; id: string }>(
    `/api/glossaries/${encodeURIComponent(glossaryId)}`,
    {
      method: "DELETE",
    }
  );
}

// --- Temp Data Purge ---
export async function purgeRuntimeTempData(): Promise<{
  status: string;
  directory: string;
}> {
  return apiRequest<{ status: string; directory: string }>(
    "/api/runtime/temp-data",
    {
      method: "DELETE",
    }
  );
}
