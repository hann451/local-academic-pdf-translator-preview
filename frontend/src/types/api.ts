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
  split_spec_id?: string | null;
  split_role?: BlockSplitRole | null;
  parent_block_id?: string | null;
  parent_bbox?: [number, number, number, number] | null;
  parent_source_hash?: string | null;
  source_text_hash?: string | null;
  needs_retranslation?: boolean;
  translation_provenance: string;
  translation_is_mock?: boolean;
}

export type BlockSplitRole = "top" | "formula" | "bottom";
export type BlockSplitAppliedStatus =
  | "applied"
  | "pending_ambiguous"
  | "pending_missing"
  | "pending_changed";

export interface BlockSplitSpec {
  id: string;
  document_id: string;
  page_index: number;
  requested_bbox: [number, number, number, number];
  snapped_bbox: [number, number, number, number];
  operation_mode: "split_and_protect_formula";
  parent_block_id: string | null;
  parent_bbox: [number, number, number, number];
  parent_source_hash: string;
  parent_source_preview: string;
  include_equation_number: boolean;
  applied_status: BlockSplitAppliedStatus;
  status_reason: string | null;
  note: string | null;
  created_at: string;
}

export interface BlockSplitPreviewPart {
  role: BlockSplitRole;
  kind: BlockKind;
  will_protect: boolean;
  bbox: [number, number, number, number];
  source_text: string;
  label_ja: string;
}

export interface BlockSplitPreviewResult {
  valid: boolean;
  document_id: string;
  page_index: number;
  protection_revision: number;
  parse_revision: number;
  requested_bbox: [number, number, number, number];
  snapped_bbox: [number, number, number, number] | null;
  equation_number_candidate_bbox: [number, number, number, number] | null;
  equation_number_text: string | null;
  equation_number_auto_included: boolean;
  parent_block_id: string | null;
  parent_bbox: [number, number, number, number] | null;
  parent_source_hash: string | null;
  parent_signature: string | null;
  parts: BlockSplitPreviewPart[];
  rejection_code: string | null;
  rejection_reason: string | null;
  retry_guidance: string | null;
  warnings: string[];
  will_invalidate_manual_edits: boolean;
  affected_manual_block_ids: string[];
  has_active_job: boolean;
}

export interface BlockSplitMutationResponse {
  split: BlockSplitSpec | null;
  deleted: boolean;
  document: Document;
  blocks: Block[];
  splits: BlockSplitSpec[];
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
  parse_revision?: number;
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
  model_id?: string | null;
  model_sha256?: string | null;
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
  allow_pending_splits?: boolean;
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
  model_id?: string | null;
  model_display_name?: string | null;
  model_sha256?: string | null;
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
  outputs_stale?: boolean;
  outputs_stale_reason?: string | null;
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
  active_registration_id?: string | null;
  active_catalog_model_id?: string | null;
  active_model_display_name?: string | null;
  active_model_file_path?: string | null;
  active_model_status?: string | null;
  running_server_model_path?: string | null;
  model_server_match_status?: string | null;
  model_server_match_message?: string | null;
  model_server_mismatch?: boolean;
  app_version?: string | null;
}

export interface SystemUpdateCheckResponse {
  update_available: boolean;
  current_version: string;
  latest_version: string | null;
  message: string;
  status?: "up_to_date" | "update_available" | "offline_or_error" | "rate_limited";
  is_git_repo?: boolean;
  latest_commit_message?: string | null;
  latest_commit_date?: string | null;
  checked_at?: string;
}

export interface SystemUpdateApplyResponse {
  status: "updating";
  message: string;
  updater_pid?: number | null;
  log_path?: string | null;
}

export interface CatalogArtifact {
  revision: string;
  size_bytes: number;
  sha256: string;
  label: string;
}

export interface ModelLicenseInfo {
  id: string;
  name: string;
  tagline?: string | null;
  url_ja?: string | null;
  url_en?: string | null;
  model_card_url?: string | null;
  summary_ja: string;
  disclosure_note_ja?: string | null;
  requires_agreement: boolean;
}

export interface ModelLaunchSettings {
  alias: string;
  context_length: number;
  parallel_slots: number;
  gpu_layers_auto: number;
  extra_args: string[];
}

export interface CatalogModel {
  model_id: string;
  display_name: string;
  short_name: string;
  provider: string;
  family: string;
  parameter_size: string;
  quantization: string;
  is_default: boolean;
  source_repo: string;
  source_repo_type: string;
  pinned_revision: string;
  filename: string;
  relative_subdir: string;
  download_url: string;
  model_card_url: string;
  known_size_bytes: number;
  known_sha256: string;
  accepted_artifacts: CatalogArtifact[];
  supported_backends: string[];
  expected_gguf_architecture: string;
  expected_gguf_name_prefix: string;
  prompt_format: string;
  prompt_version: string;
  stop_tokens: string[];
  context_length_default: number;
  context_length_max: number;
  launch_settings: ModelLaunchSettings;
  catalog_verification_status:
    | "verified_runtime"
    | "catalog_verified_runtime_unverified"
    | "experimental";
  hardware_note_ja: string;
  license: ModelLicenseInfo;
}

export type VerificationStage =
  | "unverified"
  | "file_verified"
  | "launch_verified"
  | "translation_verified";

export interface ModelVerificationState {
  file_exists: boolean;
  format_valid: boolean;
  hash_status: "matched_known" | "mismatch" | "unverified" | "missing_file";
  backend_compatible: boolean;
  launch_verified: boolean;
  translation_verified: boolean;
  verification_stage?: VerificationStage;
  matched_revision?: string | null;
  matched_label?: string | null;
  validation_error?: string | null;
  validated_at: string;
}

export interface RegisteredModel {
  registration_id: string;
  catalog_model_id: string;
  display_name: string;
  short_name: string;
  provider: string;
  parameter_size: string;
  quantization: string;
  source_type: "downloaded" | "reference" | "managed_copy" | "migrated_legacy";
  file_path: string;
  file_name: string;
  file_size_bytes: number;
  file_mtime_ns: number;
  measured_sha256: string;
  gguf_architecture?: string | null;
  gguf_model_name?: string | null;
  verification: ModelVerificationState;
  license_accepted: boolean;
  license_accepted_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelServerMatchResult {
  match_status:
    | "matched"
    | "matched_same_model_different_path"
    | "mismatch"
    | "server_unreachable"
    | "no_active_model"
    | "server_path_unverified"
    | "selected_model_invalid";
  is_matched: boolean;
  active_registration_id?: string | null;
  active_catalog_model_id?: string | null;
  active_display_name?: string | null;
  active_file_path?: string | null;
  active_sha256_prefix?: string | null;
  running_loaded_alias?: string | null;
  running_model_path?: string | null;
  running_model_file?: string | null;
  running_identified_model_id?: string | null;
  running_identified_display_name?: string | null;
  message_ja: string;
}

export interface ModelDownloadJob {
  download_id: string;
  catalog_model_id: string;
  display_name: string;
  destination_path: string;
  status: "downloading" | "verifying" | "completed" | "canceled" | "failed";
  bytes_downloaded: number;
  total_bytes: number;
  percent: number;
  error?: string | null;
  registration_id?: string | null;
  started_at: string;
  finished_at?: string | null;
}

export interface ModelsOverviewResponse {
  catalog: CatalogModel[];
  unsupported_policies: Record<string, string>;
  registered: RegisteredModel[];
  active_model_id?: string | null;
  active_model?: RegisteredModel | null;
  has_active_translation_job: boolean;
  managed_models_dir: string;
  downloads: ModelDownloadJob[];
  server_match?: ModelServerMatchResult | null;
}

export interface ModelSelectResponse {
  active_model_id: string;
  selected_model: RegisteredModel;
  server_match?: ModelServerMatchResult | null;
}

export interface ModelServerRestartRequest {
  registration_id?: string | null;
  use_cpu?: boolean | null;
}

export interface ModelServerRestartResponse {
  status: "restarted";
  pid: number;
  model_path: string;
  model_alias: string;
  registration_id: string;
  catalog_model_id: string;
  display_name: string;
  endpoint_url: string;
  compute_mode: "vulkan" | "cpu";
  executable_path: string;
  connection_status: string;
  server_match: ModelServerMatchResult;
  stopped_pids: number[];
  message_ja: string;
}

export interface BrowseLocalModelResponse {
  canceled: boolean;
  file_path: string | null;
}

export interface RegisterLocalModelRequest {
  file_path: string;
  expected_catalog_model_id?: string | null;
  register_mode?: "reference" | "copy";
  license_accepted: boolean;
  set_active?: boolean;
  replaces_registration_id?: string | null;
}

export interface StartModelDownloadRequest {
  catalog_model_id: string;
  license_accepted: boolean;
  set_active?: boolean;
  background?: boolean;
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
