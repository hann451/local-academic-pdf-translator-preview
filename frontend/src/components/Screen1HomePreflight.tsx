/**
 * Screen 1: Home, PDF Registration, Preflight Inspection & Job Configuration (Section 5).
 */

import React, { useRef, useState } from "react";
import type {
  Document,
  Glossary,
  ModelDownloadJob,
  ModelsOverviewResponse,
  RegisteredModel,
  RegisterLocalModelRequest,
  StartModelDownloadRequest,
  TranslationJobConfig,
} from "../types/api";
import { validateAndParsePageRange } from "../utils/pageRange";
import { ModelLicenseNotice, resolveActiveCatalogModel } from "./ModelLicenseNotice";
import { ModelSelectionPanel } from "./ModelSelectionPanel";

export interface Screen1Props {
  currentDocument: Document | null;
  jobConfig: TranslationJobConfig;
  glossaries: Glossary[];
  modelsOverview?: ModelsOverviewResponse | null;
  activeModelDownload?: ModelDownloadJob | null;
  isLoadingModels?: boolean;
  modelsError?: string | null;
  isUploading: boolean;
  uploadError: string | null;
  isStartingJob: boolean;
  jobStartError: string | null;
  onUploadFile: (file: File) => Promise<void>;
  onUpdateJobConfig: (next: TranslationJobConfig) => void;
  onOpenProtectionScreen: () => void;
  onOpenModelsScreen?: () => void;
  onRefreshModels?: (probeLlama: boolean) => Promise<void>;
  onBrowseLocalModelFile?: () => Promise<string | null>;
  onRegisterLocalModel?: (payload: RegisterLocalModelRequest) => Promise<RegisteredModel>;
  onSelectModel?: (registrationId: string) => Promise<void>;
  onDeleteModel?: (registrationId: string) => Promise<void>;
  onStartModelDownload?: (payload: StartModelDownloadRequest) => Promise<ModelDownloadJob>;
  onCancelModelDownload?: (downloadId: string) => Promise<void>;
  onStartTranslationJob: () => Promise<void>;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export const Screen1HomePreflight: React.FC<Screen1Props> = ({
  currentDocument,
  jobConfig,
  glossaries,
  modelsOverview = null,
  activeModelDownload = null,
  isLoadingModels = false,
  modelsError = null,
  isUploading,
  uploadError,
  isStartingJob,
  jobStartError,
  onUploadFile,
  onUpdateJobConfig,
  onOpenProtectionScreen,
  onOpenModelsScreen,
  onRefreshModels,
  onBrowseLocalModelFile,
  onRegisterLocalModel,
  onSelectModel,
  onDeleteModel,
  onStartModelDownload,
  onCancelModelDownload,
  onStartTranslationJob,
}) => {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [isDraggingOver, setIsDraggingOver] = useState(false);
  const [lastSelectedFile, setLastSelectedFile] = useState<File | null>(null);
  const [showModelDrawer, setShowModelDrawer] = useState<boolean>(false);
  const [quickSwitchError, setQuickSwitchError] = useState<string | null>(null);
  const [isQuickSwitching, setIsQuickSwitching] = useState<boolean>(false);
  const [rangeMode, setRangeMode] = useState<"all" | "custom">(
    jobConfig.page_range_spec ? "custom" : "all"
  );
  const [customRangeInput, setCustomRangeInput] = useState<string>(
    jobConfig.page_range_spec || "1"
  );

  const totalPages = currentDocument?.page_count || 1;
  const pageRangeValidation =
    rangeMode === "all"
      ? validateAndParsePageRange(null, totalPages)
      : validateAndParsePageRange(customRangeInput, totalPages);

  const isDocumentUnsupported =
    !!currentDocument &&
    (currentDocument.is_corrupted ||
      currentDocument.is_encrypted ||
      currentDocument.is_scanned_pdf ||
      currentDocument.extractable_Sync_status !== "ok");

  const hasAtLeastOneOutput =
    jobConfig.output_mono_pdf ||
    jobConfig.output_dual_pdf ||
    jobConfig.only_roundtrip_preservation;

  const canStartJob =
    !!currentDocument &&
    !isDocumentUnsupported &&
    !isUploading &&
    !isStartingJob &&
    pageRangeValidation.valid &&
    hasAtLeastOneOutput;

  const handleFileChosen = async (file: File) => {
    if (isUploading) return;
    setLastSelectedFile(file);
    await onUploadFile(file);
  };

  return (
    <div className="screen-container" data-testid="screen1-home">
      <div className="grid-2col">
        {/* Left Column: PDF Upload & Preflight Inspection */}
        <section className="card" aria-labelledby="upload-section-title">
          <h2 id="upload-section-title" className="card-title">
            1. 論文PDFの登録・プリフライト検証
          </h2>
          <p className="card-subtitle">
            原本PDFはローカルの隔離領域へ安全にコピーされ、SHA-256ハッシュにより処理前後の不変性が保証されます（外部送信・Word中間変換・無断OCRは一切行いません）。
          </p>

          <div
            data-testid="pdf-dropzone"
            onDragOver={(ev) => {
              ev.preventDefault();
              setIsDraggingOver(true);
            }}
            onDragLeave={() => setIsDraggingOver(false)}
            onDrop={(ev) => {
              ev.preventDefault();
              setIsDraggingOver(false);
              const file = ev.dataTransfer.files?.[0];
              if (file) {
                void handleFileChosen(file);
              }
            }}
            style={{
              border: isDraggingOver ? "2px solid #2563eb" : "2px dashed #94a3b8",
              backgroundColor: isDraggingOver ? "#eff6ff" : "#f8fafc",
              borderRadius: "8px",
              padding: "24px",
              textAlign: "center",
              marginBottom: "16px",
            }}
          >
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,application/pdf"
              data-testid="pdf-file-input"
              disabled={isUploading}
              style={{ display: "none" }}
              onChange={(ev) => {
                const file = ev.target.files?.[0];
                if (file) {
                  void handleFileChosen(file);
                }
              }}
            />
            <div style={{ fontWeight: 600, marginBottom: "8px", color: "#1e293b" }}>
              {isUploading
                ? "PDFを登録・プリフライト検査中..."
                : "ここに論文PDFをドラッグ＆ドロップ、またはファイルを選択"}
            </div>
            <button
              type="button"
              className="btn btn-primary"
              data-testid="select-pdf-btn"
              disabled={isUploading}
              onClick={() => fileInputRef.current?.click()}
            >
              {isUploading ? "処理中..." : "PDFファイルを選択"}
            </button>
          </div>

          {uploadError && (
            <div
              role="alert"
              data-testid="upload-error-banner"
              className="alert alert-error"
              style={{ marginBottom: "16px" }}
            >
              <div>
                <strong>アップロード失敗:</strong> {uploadError}
              </div>
              {lastSelectedFile && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  data-testid="retry-upload-btn"
                  disabled={isUploading}
                  style={{ marginTop: "8px" }}
                  onClick={() => void handleFileChosen(lastSelectedFile)}
                >
                  同じファイルで再試行 ({lastSelectedFile.name})
                </button>
              )}
            </div>
          )}

          {currentDocument && (
            <div
              data-testid="preflight-result-card"
              style={{
                border: "1px solid #cbd5e1",
                borderRadius: "8px",
                padding: "16px",
                backgroundColor: "#ffffff",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: "12px",
                }}
              >
                <h3 style={{ margin: 0, fontSize: "16px" }}>
                  プリフライト検査結果: {currentDocument.original_filename}
                </h3>
                <span
                  data-testid="preflight-status-badge"
                  className={`badge ${
                    isDocumentUnsupported ? "badge-error" : "badge-success"
                  }`}
                >
                  {isDocumentUnsupported
                    ? `非対応 (${currentDocument.extractable_Sync_status})`
                    : "テキスト抽出可能 (OK)"}
                </span>
              </div>

              <dl className="meta-grid">
                <div>
                  <dt>ドキュメントID</dt>
                  <dd data-testid="doc-id-value">{currentDocument.id}</dd>
                </div>
                <div>
                  <dt>総ページ数</dt>
                  <dd data-testid="doc-page-count">{currentDocument.page_count} ページ</dd>
                </div>
                <div>
                  <dt>ファイルサイズ</dt>
                  <dd data-testid="doc-file-size">
                    {formatBytes(currentDocument.file_size_bytes)} (
                    {currentDocument.file_size_bytes.toLocaleString()} bytes)
                  </dd>
                </div>
                <div>
                  <dt>抽出ステータス</dt>
                  <dd data-testid="doc-extract-status">
                    {currentDocument.extractable_Sync_status}
                  </dd>
                </div>
                <div style={{ gridColumn: "1 / -1" }}>
                  <dt>原本 SHA-256 ハッシュ</dt>
                  <dd
                    data-testid="doc-sha256"
                    style={{
                      fontFamily: "monospace",
                      fontSize: "12px",
                      wordBreak: "break-all",
                    }}
                  >
                    {currentDocument.sha256_hash}
                  </dd>
                </div>
              </dl>

              <div
                style={{
                  display: "flex",
                  gap: "8px",
                  flexWrap: "wrap",
                  marginTop: "12px",
                }}
              >
                <span
                  className={`badge ${
                    currentDocument.is_scanned_pdf ? "badge-error" : "badge-neutral"
                  }`}
                >
                  スキャン判定: {currentDocument.is_scanned_pdf ? "スキャンPDF (非対応)" : "デジタルテキストPDF"}
                </span>
                <span
                  className={`badge ${
                    currentDocument.is_corrupted ? "badge-error" : "badge-neutral"
                  }`}
                >
                  破損判定: {currentDocument.is_corrupted ? "破損あり" : "正常"}
                </span>
                <span
                  className={`badge ${
                    currentDocument.is_encrypted ? "badge-error" : "badge-neutral"
                  }`}
                >
                  暗号化判定: {currentDocument.is_encrypted ? "暗号化あり" : "なし"}
                </span>
              </div>

              {isDocumentUnsupported && (
                <div
                  role="alert"
                  data-testid="unsupported-pdf-alert"
                  className="alert alert-error"
                  style={{ marginTop: "12px" }}
                >
                  <strong>処理対象外PDF（無断OCR・暗黙変換は行いません）:</strong>{" "}
                  {currentDocument.unsupported_reason ||
                    "テキスト抽出不可、スキャン画像、暗号化、または破損が検出されたため翻訳ジョブを開始できません。"}
                </div>
              )}

              {currentDocument.pages.length > 0 && (
                <div style={{ marginTop: "14px" }}>
                  <div style={{ fontSize: "13px", fontWeight: 600, marginBottom: "6px" }}>
                    ページ別ジオメトリ・CropBox情報（1始まり表示）:
                  </div>
                  <div style={{ maxHeight: "140px", overflowY: "auto" }}>
                    <table className="compact-table">
                      <thead>
                        <tr>
                          <th>ページ</th>
                          <th>寸法 (pt)</th>
                          <th>回転</th>
                          <th>段組推定</th>
                          <th>CID率</th>
                        </tr>
                      </thead>
                      <tbody>
                        {currentDocument.pages.map((p) => (
                          <tr key={p.page_index}>
                            <td>Page {p.page_index + 1}</td>
                            <td>
                              {p.width_pt.toFixed(1)} × {p.height_pt.toFixed(1)}
                            </td>
                            <td>{p.rotation_deg}°</td>
                            <td>{p.column_count}段</td>
                            <td>{(p.cid_ratio * 100).toFixed(1)}%</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {!isDocumentUnsupported && (
                <div style={{ marginTop: "16px", display: "flex", gap: "10px" }}>
                  <button
                    type="button"
                    className="btn btn-secondary"
                    data-testid="open-screen2-btn"
                    onClick={onOpenProtectionScreen}
                  >
                    画面2へ：PDFプレビューと保護領域を確認・編集
                  </button>
                </div>
              )}
            </div>
          )}
        </section>

        {/* Right Column: Job Configuration */}
        <section className="card" aria-labelledby="job-config-title">
          <h2 id="job-config-title" className="card-title">
            2. 翻訳ジョブ設定
          </h2>

          {/* Page Range */}
          <div className="form-group">
            <label className="form-label">対象ページ範囲（1始まり）</label>
            <div style={{ display: "flex", gap: "16px", marginBottom: "8px" }}>
              <label style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                <input
                  type="radio"
                  name="pageRangeMode"
                  checked={rangeMode === "all"}
                  onChange={() => {
                    setRangeMode("all");
                    onUpdateJobConfig({ ...jobConfig, page_range_spec: null });
                  }}
                />
                全ページ ({totalPages}ページ)
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                <input
                  type="radio"
                  name="pageRangeMode"
                  data-testid="range-mode-custom"
                  checked={rangeMode === "custom"}
                  onChange={() => {
                    setRangeMode("custom");
                    onUpdateJobConfig({
                      ...jobConfig,
                      page_range_spec: customRangeInput,
                    });
                  }}
                />
                範囲指定（「1, 3-5」形式）
              </label>
            </div>

            {rangeMode === "custom" && (
              <div>
                <input
                  type="text"
                  className="form-input"
                  data-testid="page-range-input"
                  placeholder="例: 1, 3-5"
                  value={customRangeInput}
                  onChange={(ev) => {
                    const val = ev.target.value;
                    setCustomRangeInput(val);
                    onUpdateJobConfig({ ...jobConfig, page_range_spec: val });
                  }}
                />
                {!pageRangeValidation.valid ? (
                  <div
                    role="alert"
                    data-testid="page-range-error"
                    className="field-error"
                  >
                    {pageRangeValidation.error}
                  </div>
                ) : (
                  <div className="field-hint" data-testid="page-range-preview">
                    対象ページ (1始まり): {pageRangeValidation.displayPages.join(", ")} (
                    計 {pageRangeValidation.displayPages.length} ページ)
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Style Form */}
          <div className="form-group">
            <label className="form-label" htmlFor="style-form-select">
              日本語文体（PFML3 form タグ）
            </label>
            <select
              id="style-form-select"
              data-testid="style-form-select"
              className="form-input"
              value={jobConfig.form}
              onChange={(ev) =>
                onUpdateJobConfig({
                  ...jobConfig,
                  form: ev.target.value as "plain" | "polite",
                })
              }
            >
              <option value="plain">常体（plain: だ・である調 — 学術論文推奨・初期値）</option>
              <option value="polite">敬体（polite: です・ます調）</option>
            </select>
          </div>

          {/* Output Formats */}
          <div className="form-group">
            <label className="form-label">出力成果物モード</label>
            <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
              <label style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <input
                  type="checkbox"
                  data-testid="output-mono-checkbox"
                  checked={jobConfig.output_mono_pdf}
                  onChange={(ev) =>
                    onUpdateJobConfig({
                      ...jobConfig,
                      output_mono_pdf: ev.target.checked,
                    })
                  }
                />
                日本語単一PDF（数式・図表を原本ベクター保持した日本語置換PDF）
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <input
                  type="checkbox"
                  data-testid="output-dual-checkbox"
                  checked={jobConfig.output_dual_pdf}
                  onChange={(ev) =>
                    onUpdateJobConfig({
                      ...jobConfig,
                      output_dual_pdf: ev.target.checked,
                    })
                  }
                />
                見開き対訳PDF（左：英語原本ページ ／ 右：日本語組版ページ）
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <input
                  type="checkbox"
                  data-testid="roundtrip-only-checkbox"
                  checked={jobConfig.only_roundtrip_preservation}
                  onChange={(ev) =>
                    onUpdateJobConfig({
                      ...jobConfig,
                      only_roundtrip_preservation: ev.target.checked,
                    })
                  }
                />
                非翻訳ラウンドトリップ検証（翻訳を行わずPDF再構成の完全性のみ検証）
              </label>
            </div>
            {!hasAtLeastOneOutput && (
              <div className="field-error" role="alert">
                少なくとも1つの出力形式を選択してください。
              </div>
            )}
          </div>

          {/* Active Model Quick Card & Selector */}
          {(() => {
            const activeModel = modelsOverview?.active_model ?? null;
            const activeCatalogModel = resolveActiveCatalogModel(modelsOverview);
            const serverMatch = modelsOverview?.server_match ?? null;
            const usableRegistered = (modelsOverview?.registered ?? []).filter(
              (r) => r.verification.file_exists && r.verification.format_valid
            );
            const hasModelActions = Boolean(
              onRefreshModels &&
                onBrowseLocalModelFile &&
                onRegisterLocalModel &&
                onSelectModel &&
                onDeleteModel &&
                onStartModelDownload &&
                onCancelModelDownload
            );

            return (
              <>
                {modelsOverview && (
                  <div
                    data-testid="screen1-model-card"
                    style={{
                      marginBottom: "14px",
                      padding: "10px 12px",
                      border: "1px solid #cbd5e1",
                      borderRadius: "6px",
                      backgroundColor: "#f8fafc",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        flexWrap: "wrap",
                        gap: "8px",
                        marginBottom: "6px",
                      }}
                    >
                      <div style={{ fontSize: "13px" }}>
                        <strong>使用中の翻訳モデル:</strong>{" "}
                        <span data-testid="screen1-active-model-display">
                          {activeModel
                            ? `${activeModel.display_name} (${activeModel.quantization})`
                            : "未登録（画面4または下ボタンからダウンロード・参照登録してください）"}
                        </span>
                      </div>
                      <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
                        {hasModelActions && (
                          <button
                            type="button"
                            className="btn btn-secondary"
                            data-testid="toggle-screen1-model-drawer-btn"
                            onClick={() => setShowModelDrawer((prev) => !prev)}
                          >
                            {showModelDrawer
                              ? "▲ モデル選択・導入を閉じる"
                              : "📦 モデル選択・導入 / 参照登録"}
                          </button>
                        )}
                        {onOpenModelsScreen && (
                          <button
                            type="button"
                            className="btn btn-secondary"
                            data-testid="open-screen4-models-btn"
                            onClick={() => onOpenModelsScreen()}
                          >
                            画面4で詳細管理
                          </button>
                        )}
                      </div>
                    </div>

                    {activeModel && (
                      <div style={{ fontSize: "12px", color: "#334155", marginBottom: "6px" }}>
                        ファイル: <code>{activeModel.file_path}</code>
                      </div>
                    )}

                    {usableRegistered.length > 0 && onSelectModel && (
                      <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                        <label
                          htmlFor="screen1-quick-model-select"
                          style={{ fontSize: "12px", fontWeight: 600 }}
                        >
                          登録済みモデル切替:
                        </label>
                        <select
                          id="screen1-quick-model-select"
                          data-testid="screen1-quick-model-select"
                          className="form-input"
                          style={{ width: "auto", minWidth: "240px", padding: "4px 8px" }}
                          value={modelsOverview.active_model_id || ""}
                          disabled={
                            isQuickSwitching || Boolean(modelsOverview.has_active_translation_job)
                          }
                          onChange={async (ev) => {
                            const nextId = ev.target.value;
                            if (!nextId || nextId === modelsOverview.active_model_id) return;
                            setQuickSwitchError(null);
                            setIsQuickSwitching(true);
                            try {
                              await onSelectModel(nextId);
                            } catch (err) {
                              setQuickSwitchError(err instanceof Error ? err.message : String(err));
                            } finally {
                              setIsQuickSwitching(false);
                            }
                          }}
                        >
                          {usableRegistered.map((reg) => (
                            <option key={reg.registration_id} value={reg.registration_id}>
                              {reg.display_name} ({reg.quantization}) - {reg.file_name}
                            </option>
                          ))}
                        </select>
                        {modelsOverview.has_active_translation_job && (
                          <span style={{ fontSize: "11px", color: "#b45309" }}>
                            翻訳ジョブ実行中のため切替不可
                          </span>
                        )}
                      </div>
                    )}

                    {quickSwitchError && (
                      <div
                        role="alert"
                        data-testid="screen1-quick-switch-error"
                        className="alert alert-error"
                        style={{ marginTop: "8px", fontSize: "12px" }}
                      >
                        {quickSwitchError}
                      </div>
                    )}

                    {serverMatch &&
                      (serverMatch.match_status === "mismatch" ||
                        serverMatch.match_status === "selected_model_invalid") && (
                        <div
                          role="alert"
                          data-testid="screen1-model-server-mismatch"
                          className="alert alert-error"
                          style={{ marginTop: "8px", fontSize: "12px" }}
                        >
                          <strong>モデル不整合・起動切替が必要:</strong> {serverMatch.message_ja}
                        </div>
                      )}

                    {showModelDrawer &&
                      hasModelActions &&
                      onRefreshModels &&
                      onBrowseLocalModelFile &&
                      onRegisterLocalModel &&
                      onSelectModel &&
                      onDeleteModel &&
                      onStartModelDownload &&
                      onCancelModelDownload && (
                        <div
                          data-testid="screen1-model-drawer"
                          style={{
                            marginTop: "12px",
                            paddingTop: "12px",
                            borderTop: "1px solid #cbd5e1",
                          }}
                        >
                          <ModelSelectionPanel
                            compact
                            overview={modelsOverview}
                            activeDownload={activeModelDownload}
                            isLoading={isLoadingModels}
                            error={modelsError}
                            onRefreshModels={onRefreshModels}
                            onBrowseLocalFile={onBrowseLocalModelFile}
                            onRegisterLocalModel={onRegisterLocalModel}
                            onSelectModel={onSelectModel}
                            onDeleteModel={onDeleteModel}
                            onStartDownload={onStartModelDownload}
                            onCancelDownload={onCancelModelDownload}
                          />
                        </div>
                      )}
                  </div>
                )}

                {/* Inference Mode (Real Default vs Explicit Mock) */}
                <div className="form-group">
                  <label className="form-label" htmlFor="inference-mode-select">
                    推論エンジン（選択中：
                    {activeModel ? activeModel.display_name : "PLaMo 3 Translate 2B"}）
                  </label>
                  <select
                    id="inference-mode-select"
                    data-testid="inference-mode-select"
                    className="form-input"
                    value={jobConfig.use_mock_translator ? "mock" : "real"}
                    onChange={(ev) =>
                      onUpdateJobConfig({
                        ...jobConfig,
                        use_mock_translator: ev.target.value === "mock",
                      })
                    }
                  >
                    <option value="real">
                      実機推論（llama.cpp +{" "}
                      {activeModel
                        ? `${activeModel.display_name} ${activeModel.quantization}`
                        : "PLaMo 3 Translate 2B Q4_K_M"}{" "}
                      @ 127.0.0.1:8000）
                    </option>
                    <option value="mock">
                      モック推論（UI・パイプライン検証用の決定論的疑似翻訳）
                    </option>
                  </select>
                  {jobConfig.use_mock_translator ? (
                    <div
                      data-testid="mock-selected-warning"
                      className="alert alert-warning"
                      style={{ marginTop: "8px", fontSize: "13px" }}
                    >
                      <strong>[MOCK 選択中]</strong>{" "}
                      現在はモック推論が明示選択されています。モックはUI・パイプライン確認用の決定論的疑似翻訳であり、実翻訳ではありません。生成される成果物には{" "}
                      <code>is_mock_translation: true</code> が記録され、実機翻訳とは区別されます。
                    </div>
                  ) : (
                    <div
                      data-testid="real-inference-prerequisite-note"
                      className="field-hint"
                      style={{ marginTop: "8px", fontSize: "12px", color: "#334155" }}
                    >
                      実機翻訳には、本アプリ（<code>127.0.0.1:8080</code>）とは別ターミナルで推論サーバ（
                      選択中の翻訳モデルを指定した <code>llama-server</code>、既定{" "}
                      <code>127.0.0.1:8000</code>
                      ）の起動が必要です。推論サーバ未起動または別モデルが起動中のまま開始すると中断またはエラーとなります。事前確認は画面4の「推論サーバ疎通確認」（翻訳リクエストは送信しません）で行えます。モデル未導入で画面動作のみ確認する場合は「モック推論」（実翻訳ではありません）を選択してください。
                    </div>
                  )}

                  <ModelLicenseNotice
                    variant="screen1"
                    catalogModel={activeCatalogModel}
                    activeModel={activeModel}
                  />
                </div>
              </>
            );
          })()}

          {/* Glossary Selection */}
          <div className="form-group">
            <label className="form-label" htmlFor="glossary-select">
              適用する用語集（任意）
            </label>
            <select
              id="glossary-select"
              data-testid="glossary-select"
              className="form-input"
              value={jobConfig.glossary_id || ""}
              onChange={(ev) =>
                onUpdateJobConfig({
                  ...jobConfig,
                  glossary_id: ev.target.value || null,
                })
              }
            >
              <option value="">用語集を使用しない</option>
              {glossaries.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name} ({g.entries.length}語 / v{g.version})
                </option>
              ))}
            </select>
          </div>

          {jobStartError && (
            <div
              role="alert"
              data-testid="job-start-error"
              className="alert alert-error"
              style={{ marginBottom: "12px" }}
            >
              <strong>ジョブ開始エラー:</strong> {jobStartError}
            </div>
          )}

          <div style={{ display: "flex", gap: "12px", marginTop: "16px" }}>
            <button
              type="button"
              className="btn btn-primary"
              data-testid="start-job-btn"
              disabled={!canStartJob}
              onClick={() => void onStartTranslationJob()}
            >
              {isStartingJob
                ? "翻訳ジョブを起動中..."
                : jobConfig.only_roundtrip_preservation
                ? "非翻訳ラウンドトリップ検証を実行"
                : jobConfig.use_mock_translator
                ? "モック翻訳ジョブを開始"
                : modelsOverview?.active_model
                ? `実機翻訳ジョブを開始 (${modelsOverview.active_model.short_name})`
                : "実機 PLaMo 翻訳ジョブを開始"}
            </button>
          </div>
        </section>
      </div>
    </div>
  );
};
