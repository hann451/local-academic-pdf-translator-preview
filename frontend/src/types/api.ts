/**
 * Canonical TypeScript API Contract for PaperBridge Local (Phase 3).
 * Synchronized with `backend/domain/models.py` and `docs/API_CONTRACT.md`.
 */

export type BlockKind =
  | "title"
  | "heading"
  | "paragraph"
  | "caption"
  | "isolated_formula"
  | "figure"
  | "table"
  | "reference"
  | "header_footer"
  | "unknown";

export type BlockStatus =
  | "pending"
  | "protected_skip"
  | "translating"
  | "translated"
  | "manually_edited"
  | "fallback_original"
  | "failed";

export type ProtectionMode =
  | "do_not_translate"
  | "translate_as_body"
  | "protect_as_formula"
  | "protect_as_figure";

export type JobState =
  | "created"
  | "preflight"
  | "parsing"
  | "awaiting_review"
  | "translating"
  | "validating"
  | "typesetting"
  | "completed"
  | "completed_with_warnings"
  | "failed"
  | "canceled"
  | "interrupted";

export type IssueSeverity = "info" | "warning" | "error";

export type ProtectedItemKind =
  | "inline_formula"
  | "greek_or_math_symbol"
  | "sub_superscript"
  | "equation_number"
  | "citation_ref"
  | "url_or_doi"
  | "user_protected";

export interface ProtectedItem {
  placeholder: string;
  kind: ProtectedItemKind;
  original_text: string;
  bbox: [number, number, number, number] | null;
  font_name: string | null;
}

export interface ContinuationCandidate {
  counterpart_block_id: string;
  direction: "previous" | "next";
  relation:
    | "same_page_cross_column"
    | "adjacent_pages_two_column"
    | "adjacent_pages_one_column";
  confidence: "high" | "review_candidate";
  reasons: string[];
  use_as_context: boolean;
}

export interface Block {
  id: string;
  page_index: number; // 0-indexed backend internal
  bbox: [number, number, number, number]; // [x0, y0, x1, y1] unrotated CropBox top-left pt
  kind: BlockKind;
  reading_order: number;
  column_index: number;
  source_text: string;
  masked_source_text: string | null;
  protected_items: ProtectedItem[];
  continuation_candidates?: ContinuationCandidate[];
  translation_text: string | null;
  manual_translation_text: string | null;
  status: BlockStatus;
  warnings: string[];
  font_size_pt: number;
  fitted_font_size_pt: number | null;
  is_uncertain_region: boolean;
  protection_source: "auto" | "manual" | "none";
  edit_revision: number;
  manual_validation_errors: string[];
  manual_validation_warnings: string[];
  semantic_category: string;
  protection_reason: string;
  protection_rule_id: string;
  protection_confidence: string;
  user_protection_override: "force_translate" | "force_protect" | null;
  translation_provenance: string;
}

export interface ProtectionRegion {
  id: string;
  document_id: string;
  page_index: number; // 0-indexed backend internal
  bbox: [number, number, number, number];
  mode: ProtectionMode;
  source: "manual" | "auto";
  note: string | null;
  created_at: string;
}

export interface Page {
  document_id: string;
  page_index: number; // 0-indexed backend internal
  width_pt: number;
  height_pt: number;
  rotation_deg: 0 | 90 | 180 | 270;
  media_box: [number, number, number, number];
  crop_box: [number, number, number, number];
  has_extractable_text: boolean;
  is_scanned_like: boolean;
  cid_ratio: number;
  column_count: number;
}

export interface Document {
  id: string;
  original_filename: string;
  safe_storage_path: string;
  sha256_hash: string;
  file_size_bytes: number;
  page_count: number;
  is_encrypted: boolean;
  is_corrupted: boolean;
  is_scanned_pdf: boolean;
  extractable_Sync_status: string;
  unsupported_reason: string | null;
  protection_revision: number;
  edit_revision: number;
  pages: Page[];
  created_at: string;
}

export interface GlossaryEntry {
  source: string;
  target: string;
  notes?: string | null;
}

export interface Glossary {
  id: string;
  name: string;
  source_lang: string;
  target_lang: string;
  version: number;
  entries: GlossaryEntry[];
  updated_at: string;
}

export interface ValidationIssue {
  id: string;
  job_id: string;
  document_id: string;
  page_index: number; // 0-indexed backend internal
  block_id: string | null;
  bbox: [number, number, number, number] | null;
  code: string;
  severity: IssueSeverity;
  message: string;
  details: Record<string, unknown>;
  fallback_applied: boolean;
  created_at: string;
}

export type ArtifactType =
  | "mono_ja_pdf"
  | "dual_bilingual_pdf"
  | "roundtrip_pdf"
  | "validation_report_json";

export interface ExportArtifact {
  id: string;
  job_id: string;
  document_id: string;
  artifact_type: ArtifactType;
  file_path: string;
  sha256_hash: string;
  file_size_bytes: number;
  is_finalized: boolean;
  is_mock_translation: boolean;
  created_at: string;
}

export interface TranslationJobConfig {
  page_range_spec: string | null;
  source_lang: string;
  target_lang: string;
  form: "plain" | "polite";
  model_name: string;
  quantization: string;
  context_length: number;
  translate_captions: boolean;
  translate_tables: boolean;
  translate_references: boolean;
  translate_figures: boolean;
  min_font_size_pt: number;
  glossary_id: string | null;
  output_mono_pdf: boolean;
  output_dual_pdf: boolean;
  only_roundtrip_preservation: boolean;
  pause_for_review: boolean;
  use_mock_translator: boolean;
}

export interface AppliedBlockRevisionInfo {
  source: "manual" | "model" | "original";
  edit_revision: number;
  status: BlockStatus;
}

export interface ExecutionProvenance {
  adapter_name?: string;
  engine_process?: string;
  endpoint_url?: string;
  model_name?: string;
  model_file?: string | null;
  quantization?: string;
  compute_backend?: string;
  is_mock?: boolean;
  total_blocks?: number;
  newly_inferenced_blocks?: number;
  llm_api_calls_made?: number;
  cache_hit_blocks?: number;
  reused_existing_blocks?: number;
  manual_translation_blocks?: number;
  protected_skip_blocks?: number;
  fallback_original_blocks?: number;
  last_retypeset_revision?: number;
  last_retypeset_llm_calls_made?: number;
}

export interface Job {
  id: string;
  document_id: string;
  state: JobState;
  current_stage: string;
  config: TranslationJobConfig;
  total_pages: number;
  processed_pages: number;
  total_blocks: number;
  processed_blocks: number;
  retry_count: number;
  warning_count: number;
  error_message: string | null;
  original_sha256: string;
  original_hash_verified: boolean;
  is_mock_run: boolean;
  cancel_requested: boolean;
  glossary_version_used: number | null;
  retypeset_revision: number;
  based_on_edit_revision: number;
  based_on_protection_revision: number;
  applied_block_revisions: Record<string, AppliedBlockRevisionInfo>;
  execution_provenance: ExecutionProvenance;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface RetypesetJobResponse {
  job: Job;
  artifacts: ExportArtifact[];
  issues: ValidationIssue[];
  requires_reparse_or_retranslate: boolean;
  llm_calls_made: number;
}

export interface SystemDiagnostics {
  os_name: string;
  os_version: string;
  cpu_name: string;
  cpu_cores: number;
  cpu_threads: number;
  ram_total_mb: number;
  gpu_name: string;
  gpu_vram_mb: number;
  gpu_driver_version: string;
  python_version: string;
  pdf_engine_primary: string;
  babeldoc_installed: boolean;
  babeldoc_version: string | null;
  babeldoc_assets_cached: boolean;
  japanese_font_available: boolean;
  japanese_font_path: string | null;
  llama_server_url: string;
  llama_connection_status: string;
  loaded_model_name: string | null;
  loopback_only_enforced: boolean;
  llama_model_path?: string | null;
  llama_build_info?: string | null;
  llama_compute_backend?: string | null;
  llama_server_pid?: number | null;
}

export interface AuthSessionResponse {
  token: string;
  header_name: string;
  loopback_only: boolean;
  rotated?: boolean;
}

export interface PipelineStageInfo {
  stageNumber: number;
  title: string;
  phaseKey: JobState;
}

export const PIPELINE_STAGES_13: PipelineStageInfo[] = [
  { stageNumber: 1, title: "ファイル・原本SHA-256検証", phaseKey: "preflight" },
  { stageNumber: 2, title: "ページ寸法・回転・CropBox検査", phaseKey: "preflight" },
  { stageNumber: 3, title: "テキスト抽出可否・スキャン判定", phaseKey: "preflight" },
  { stageNumber: 4, title: "レイアウト・ブロック種別解析", phaseKey: "parsing" },
  { stageNumber: 5, title: "段組み・読み順決定", phaseKey: "parsing" },
  { stageNumber: 6, title: "保護領域（自動＋手動）確定", phaseKey: "parsing" },
  { stageNumber: 7, title: "インライン数式・記号マスク化 ({v1})", phaseKey: "parsing" },
  { stageNumber: 8, title: "PLaMo PFML3 ローカル段落翻訳", phaseKey: "translating" },
  { stageNumber: 9, title: "訳文・保護プレースホルダ10項目検証", phaseKey: "validating" },
  { stageNumber: 10, title: "日本語禁則処理・枠内フォント組版", phaseKey: "typesetting" },
  { stageNumber: 11, title: "数式・図表の視覚保持検証", phaseKey: "typesetting" },
  { stageNumber: 12, title: "日本語PDF・見開き対訳PDF出力", phaseKey: "typesetting" },
  { stageNumber: 13, title: "検証レポート生成・原本不変性確認", phaseKey: "completed" },
];
