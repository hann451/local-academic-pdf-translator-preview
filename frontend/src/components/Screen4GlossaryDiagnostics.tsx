/**
 * Screen 4: Glossary Management (CRUD + TSV/JSON Import Preview) & Dynamic System Diagnostics (Section 8 & 9).
 */

import React, { useMemo, useState } from "react";
import type { Glossary, GlossaryEntry, SystemDiagnostics } from "../types/api";
import { parseGlossaryInput, type GlossaryParseReport } from "../utils/glossaryParser";

export interface Screen4Props {
  glossaries: Glossary[];
  diagnostics: SystemDiagnostics | null;
  hasProbedLlama: boolean;
  isLoadingDiagnostics: boolean;
  diagnosticsError: string | null;
  onRefreshDiagnostics: (probeLlama: boolean) => Promise<void>;
  onCreateGlossary: (name: string, entries: GlossaryEntry[]) => Promise<Glossary>;
  onUpdateGlossary: (
    glossaryId: string,
    name: string,
    entries: GlossaryEntry[]
  ) => Promise<Glossary>;
  onDeleteGlossary: (glossaryId: string) => Promise<void>;
  onPurgeTempData: () => Promise<string>;
  onRotateToken: () => Promise<void>;
}

export const Screen4GlossaryDiagnostics: React.FC<Screen4Props> = ({
  glossaries,
  diagnostics,
  hasProbedLlama,
  isLoadingDiagnostics,
  diagnosticsError,
  onRefreshDiagnostics,
  onCreateGlossary,
  onUpdateGlossary,
  onDeleteGlossary,
  onPurgeTempData,
  onRotateToken,
}) => {
  const [selectedGlossaryId, setSelectedGlossaryId] = useState<string | null>(
    glossaries[0]?.id || null
  );
  const [glossaryName, setGlossaryName] = useState<string>(
    glossaries[0]?.name || "新規学術用語集"
  );
  const [editableEntries, setEditableEntries] = useState<GlossaryEntry[]>(
    glossaries[0]?.entries || []
  );
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [newSource, setNewSource] = useState<string>("");
  const [newTarget, setNewTarget] = useState<string>("");
  const [newNotes, setNewNotes] = useState<string>("");

  const [importRawText, setImportRawText] = useState<string>("");
  const [importFormat, setImportFormat] = useState<"auto" | "tsv" | "json">("auto");
  const [importPreview, setImportPreview] = useState<GlossaryParseReport | null>(null);

  const [glossaryMessage, setGlossaryMessage] = useState<string | null>(null);
  const [glossaryError, setGlossaryError] = useState<string | null>(null);
  const [isSavingGlossary, setIsSavingGlossary] = useState<boolean>(false);
  const [systemActionFeedback, setSystemActionFeedback] = useState<string | null>(null);

  const selectGlossary = (g: Glossary | null) => {
    if (!g) {
      setSelectedGlossaryId(null);
      setGlossaryName("新規学術用語集");
      setEditableEntries([]);
      return;
    }
    setSelectedGlossaryId(g.id);
    setGlossaryName(g.name);
    setEditableEntries([...g.entries]);
    setGlossaryMessage(null);
    setGlossaryError(null);
  };

  const filteredEntries = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return editableEntries;
    return editableEntries.filter(
      (e) =>
        e.source.toLowerCase().includes(q) ||
        e.target.toLowerCase().includes(q) ||
        (e.notes || "").toLowerCase().includes(q)
    );
  }, [editableEntries, searchQuery]);

  // Display slice for fast rendering even with 1,200+ entries
  const visibleEntries = useMemo(() => filteredEntries.slice(0, 200), [filteredEntries]);

  const handlePreviewImport = () => {
    const report = parseGlossaryInput(importRawText, importFormat);
    setImportPreview(report);
  };

  const handleApplyImportPreview = () => {
    if (!importPreview || importPreview.entries.length === 0) return;
    const existingBySource = new Map<string, GlossaryEntry>();
    for (const e of editableEntries) {
      existingBySource.set(e.source, e);
    }
    for (const e of importPreview.entries) {
      existingBySource.set(e.source, e);
    }
    const merged = Array.from(existingBySource.values());
    setEditableEntries(merged);
    setGlossaryMessage(
      `プレビュー済みの ${importPreview.entries.length} 件を編集リストへ取り込みました（現在合計 ${merged.length} 語）。「用語集を保存」で確定してください。`
    );
    setImportPreview(null);
  };

  const handleAddSingleEntry = () => {
    const s = newSource.trim();
    const t = newTarget.trim();
    const n = newNotes.trim() || null;
    if (!s || !t) {
      setGlossaryError("原語と訳語の両方を入力してください。");
      return;
    }
    if (editableEntries.some((e) => e.source === s)) {
      setGlossaryError(`原語 "${s}" は既に登録されています。`);
      return;
    }
    setEditableEntries((prev) => [...prev, { source: s, target: t, notes: n }]);
    setNewSource("");
    setNewTarget("");
    setNewNotes("");
    setGlossaryError(null);
  };

  const handleSaveGlossary = async () => {
    if (!glossaryName.trim()) {
      setGlossaryError("用語集の名前を入力してください。");
      return;
    }
    setIsSavingGlossary(true);
    setGlossaryError(null);
    setGlossaryMessage(null);
    try {
      if (selectedGlossaryId) {
        const updated = await onUpdateGlossary(
          selectedGlossaryId,
          glossaryName.trim(),
          editableEntries
        );
        setGlossaryMessage(
          `用語集「${updated.name}」を更新しました (${updated.entries.length}語, v${updated.version})。`
        );
      } else {
        const created = await onCreateGlossary(glossaryName.trim(), editableEntries);
        setSelectedGlossaryId(created.id);
        setGlossaryMessage(
          `新規用語集「${created.name}」を作成しました (${created.entries.length}語, v${created.version})。`
        );
      }
    } catch (err) {
      setGlossaryError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSavingGlossary(false);
    }
  };

  const handleDeleteCurrentGlossary = async () => {
    if (!selectedGlossaryId) return;
    setIsSavingGlossary(true);
    setGlossaryError(null);
    try {
      await onDeleteGlossary(selectedGlossaryId);
      selectGlossary(null);
      setGlossaryMessage("用語集を削除しました。");
    } catch (err) {
      setGlossaryError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSavingGlossary(false);
    }
  };

  // Determine explicit connection label from server response (never hardcoded)
  const renderConnectionBadge = (diag: SystemDiagnostics) => {
    const status = diag.llama_connection_status;
    if (!hasProbedLlama && status === "not_connected") {
      return (
        <span data-testid="diag-llama-status" className="badge badge-neutral">
          未確認 (not_probed: `probe_llama=false` — 「推論サーバ疎通確認」を押してください)
        </span>
      );
    }
    if (status === "connected") {
      return (
        <span data-testid="diag-llama-status" className="badge badge-success">
          実機接続正常 (connected — モデル: {diag.loaded_model_name || "不明"})
        </span>
      );
    }
    if (status === "server_unreachable") {
      return (
        <span data-testid="diag-llama-status" className="badge badge-error">
          推論サーバ未接続 (server_unreachable)
        </span>
      );
    }
    return (
      <span data-testid="diag-llama-status" className="badge badge-warning">
        ステータス: {status}
      </span>
    );
  };

  return (
    <div className="screen-container" data-testid="screen4-glossary-diagnostics">
      <div className="grid-2col">
        {/* Left Column: Glossary Management (Section 8) */}
        <section className="card" data-testid="glossary-management-panel">
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: "10px",
            }}
          >
            <h2 className="card-title" style={{ margin: 0 }}>
              1. 学術用語集（Glossary）管理
            </h2>
            <button
              type="button"
              className="btn btn-secondary"
              data-testid="new-glossary-btn"
              onClick={() => selectGlossary(null)}
            >
              ＋ 新規用語集を作成
            </button>
          </div>

          {/* Saved Glossaries Selector */}
          <div className="form-group">
            <label className="form-label">登録済み用語集一覧 ({glossaries.length}件)</label>
            <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
              {glossaries.map((g) => (
                <button
                  key={g.id}
                  type="button"
                  data-testid={`select-glossary-${g.id}`}
                  className={`btn ${
                    selectedGlossaryId === g.id ? "btn-primary" : "btn-secondary"
                  }`}
                  onClick={() => selectGlossary(g)}
                >
                  {g.name} ({g.entries.length}語 / v{g.version})
                </button>
              ))}
            </div>
          </div>

          <div className="form-group">
            <label className="form-label" htmlFor="glossary-name-input">
              用語集名
            </label>
            <div style={{ display: "flex", gap: "8px" }}>
              <input
                id="glossary-name-input"
                data-testid="glossary-name-input"
                type="text"
                className="form-input"
                value={glossaryName}
                onChange={(ev) => setGlossaryName(ev.target.value)}
              />
              <button
                type="button"
                className="btn btn-primary"
                data-testid="save-glossary-btn"
                disabled={isSavingGlossary}
                onClick={() => void handleSaveGlossary()}
              >
                {isSavingGlossary
                  ? "保存中..."
                  : selectedGlossaryId
                  ? "💾 用語集を更新保存"
                  : "💾 新規用語集として保存"}
              </button>
              {selectedGlossaryId && (
                <button
                  type="button"
                  className="btn btn-danger-sm"
                  data-testid="delete-glossary-btn"
                  disabled={isSavingGlossary}
                  onClick={() => void handleDeleteCurrentGlossary()}
                >
                  削除
                </button>
              )}
            </div>
          </div>

          {glossaryMessage && (
            <div
              data-testid="glossary-message-banner"
              className="alert alert-info"
              style={{ marginBottom: "10px", fontSize: "12px" }}
            >
              {glossaryMessage}
            </div>
          )}
          {glossaryError && (
            <div
              role="alert"
              data-testid="glossary-error-banner"
              className="alert alert-error"
              style={{ marginBottom: "10px", fontSize: "12px" }}
            >
              {glossaryError}
            </div>
          )}

          {/* TSV / JSON Bulk Import with Preview & Per-Row Error Display */}
          <div
            style={{
              border: "1px solid #cbd5e1",
              borderRadius: "6px",
              padding: "12px",
              backgroundColor: "#f8fafc",
              marginBottom: "14px",
            }}
          >
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginBottom: "6px",
              }}
            >
              <strong style={{ fontSize: "13px" }}>
                TSV / JSON 一括インポート（保存前プレビュー・行単位エラー検査）
              </strong>
              <select
                data-testid="import-format-select"
                className="form-input"
                style={{ width: "auto" }}
                value={importFormat}
                onChange={(ev) =>
                  setImportFormat(ev.target.value as "auto" | "tsv" | "json")
                }
              >
                <option value="auto">自動判定 (TSV/JSON)</option>
                <option value="tsv">TSV (原語&lt;TAB&gt;訳語)</option>
                <option value="json">JSON ([&#123;source, target&#125;])</option>
              </select>
            </div>
            <textarea
              data-testid="glossary-import-textarea"
              className="form-input"
              rows={4}
              placeholder={"例 (TSV):\nstochastic gradient descent\t確率的勾配降下法\t最適化\nempirical risk\t経験リスク"}
              value={importRawText}
              onChange={(ev) => setImportRawText(ev.target.value)}
            />
            <div style={{ display: "flex", gap: "8px", marginTop: "8px" }}>
              <button
                type="button"
                className="btn btn-secondary"
                data-testid="preview-import-btn"
                onClick={handlePreviewImport}
              >
                🔍 インポート内容を検査・プレビュー
              </button>
              {importPreview && importPreview.validCount > 0 && (
                <button
                  type="button"
                  className="btn btn-primary"
                  data-testid="apply-import-btn"
                  onClick={handleApplyImportPreview}
                >
                  ✓ 有効な {importPreview.validCount} 件を用語集へ反映
                </button>
              )}
            </div>

            {importPreview && (
              <div
                data-testid="import-preview-panel"
                style={{ marginTop: "10px", fontSize: "12px" }}
              >
                <div>
                  検出形式: <code>{importPreview.detectedFormat.toUpperCase()}</code> | 有効:{" "}
                  <strong data-testid="import-valid-count">
                    {importPreview.validCount}件
                  </strong>{" "}
                  | 重複: {importPreview.duplicateCount}件 | 行エラー:{" "}
                  <strong data-testid="import-error-count">
                    {importPreview.rowErrors.length}件
                  </strong>
                </div>

                {importPreview.rowErrors.length > 0 && (
                  <div
                    role="alert"
                    data-testid="import-row-errors"
                    className="alert alert-error"
                    style={{ marginTop: "6px", maxHeight: "120px", overflowY: "auto" }}
                  >
                    <strong>行単位エラー検出:</strong>
                    <ul style={{ margin: "4px 0 0 18px", padding: 0 }}>
                      {importPreview.rowErrors.map((re, idx) => (
                        <li key={idx}>
                          {re.reason} (<code>{re.rawText}</code>)
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Single Term Addition & Search Filter */}
          <div style={{ display: "flex", gap: "6px", marginBottom: "10px" }}>
            <input
              type="text"
              className="form-input"
              data-testid="new-term-source"
              placeholder="原語 (例: attention mechanism)"
              value={newSource}
              onChange={(ev) => setNewSource(ev.target.value)}
            />
            <input
              type="text"
              className="form-input"
              data-testid="new-term-target"
              placeholder="訳語 (例: 注意機構)"
              value={newTarget}
              onChange={(ev) => setNewTarget(ev.target.value)}
            />
            <input
              type="text"
              className="form-input"
              data-testid="new-term-notes"
              placeholder="備考 (任意)"
              value={newNotes}
              onChange={(ev) => setNewNotes(ev.target.value)}
            />
            <button
              type="button"
              className="btn btn-secondary"
              data-testid="add-single-term-btn"
              onClick={handleAddSingleEntry}
            >
              追加
            </button>
          </div>

          <div className="form-group">
            <input
              type="search"
              className="form-input"
              data-testid="glossary-search-input"
              placeholder={`登録用語を検索・絞り込み (現在 ${editableEntries.length} 語登録中)...`}
              value={searchQuery}
              onChange={(ev) => setSearchQuery(ev.target.value)}
            />
          </div>

          <div
            data-testid="glossary-entries-count"
            style={{ fontSize: "12px", color: "#475569", marginBottom: "6px" }}
          >
            一致件数: {filteredEntries.length} / 総登録語数: {editableEntries.length}語
            {filteredEntries.length > 200 && " （先頭200件を表示中）"}
          </div>

          <div style={{ maxHeight: "260px", overflowY: "auto" }}>
            <table className="compact-table" data-testid="glossary-entries-table">
              <thead>
                <tr>
                  <th>原語 (source)</th>
                  <th>訳語 (target)</th>
                  <th>備考</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {visibleEntries.map((entry) => (
                  <tr key={entry.source}>
                    <td>{entry.source}</td>
                    <td>{entry.target}</td>
                    <td>{entry.notes || "-"}</td>
                    <td>
                      <button
                        type="button"
                        className="btn btn-danger-sm"
                        onClick={() =>
                          setEditableEntries((prev) =>
                            prev.filter((item) => item.source !== entry.source)
                          )
                        }
                      >
                        削除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {/* Right Column: Dynamic System Diagnostics & Security Controls (Section 9) */}
        <section className="card" data-testid="system-diagnostics-panel">
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: "12px",
            }}
          >
            <h2 className="card-title" style={{ margin: 0 }}>
              2. システム診断・推論サーバ接続・セキュリティ境界
            </h2>
            <div style={{ display: "flex", gap: "8px" }}>
              <button
                type="button"
                className="btn btn-secondary"
                data-testid="refresh-diag-btn"
                disabled={isLoadingDiagnostics}
                onClick={() => void onRefreshDiagnostics(false)}
              >
                診断情報を再取得
              </button>
              <button
                type="button"
                className="btn btn-primary"
                data-testid="probe-llama-btn"
                disabled={isLoadingDiagnostics}
                onClick={() => void onRefreshDiagnostics(true)}
              >
                {isLoadingDiagnostics ? "確認中..." : "🔌 推論サーバ疎通確認 (probe_llama=true)"}
              </button>
            </div>
          </div>

          <p className="card-subtitle">
            以下の診断項目はすべてバックエンド <code>GET /api/diagnostics</code>{" "}
            の実行時レスポンスのみを根拠に表示しています（UI側のハードコード値は一切使用していません）。
          </p>

          {diagnosticsError && (
            <div
              role="alert"
              data-testid="diagnostics-error-banner"
              className="alert alert-error"
              style={{ marginBottom: "12px" }}
            >
              {diagnosticsError}
            </div>
          )}

          {!diagnostics ? (
            <div data-testid="diagnostics-loading">診断情報を取得中...</div>
          ) : (
            <dl className="meta-grid" data-testid="diagnostics-dl">
              <div>
                <dt>OS / バージョン</dt>
                <dd data-testid="diag-os">
                  {diagnostics.os_name} ({diagnostics.os_version})
                </dd>
              </div>
              <div>
                <dt>CPU / コア・スレッド</dt>
                <dd data-testid="diag-cpu">
                  {diagnostics.cpu_name} ({diagnostics.cpu_cores}C / {diagnostics.cpu_threads}T)
                </dd>
              </div>
              <div>
                <dt>システム RAM</dt>
                <dd data-testid="diag-ram">{diagnostics.ram_total_mb.toLocaleString()} MB</dd>
              </div>
              <div>
                <dt>GPU / VRAM / ドライバ</dt>
                <dd data-testid="diag-gpu">
                  {diagnostics.gpu_name} ({diagnostics.gpu_vram_mb.toLocaleString()} MB / Driver:{" "}
                  {diagnostics.gpu_driver_version})
                </dd>
              </div>
              <div>
                <dt>推論サーバ（llama.cpp）</dt>
                <dd data-testid="diag-llama-runtime">
                  モデル: {diagnostics.llama_model_path ?? "不明"} / ビルド:{" "}
                  {diagnostics.llama_build_info ?? "不明"} / 計算:{" "}
                  {diagnostics.llama_compute_backend ?? "不明"} / PID:{" "}
                  {diagnostics.llama_server_pid ?? "不明"}
                </dd>
              </div>
              <div>
                <dt>Python / PDFエンジン</dt>
                <dd data-testid="diag-pdf-engine">
                  Python {diagnostics.python_version} / Primary: {diagnostics.pdf_engine_primary}
                </dd>
              </div>
              <div>
                <dt>OFL 日本語フォント</dt>
                <dd data-testid="diag-font">
                  {diagnostics.japanese_font_available
                    ? `利用可能 (${diagnostics.japanese_font_path || ""})`
                    : "未検出"}
                </dd>
              </div>
              <div>
                <dt>ローカル限定強制 (127.0.0.1)</dt>
                <dd data-testid="diag-loopback">
                  {diagnostics.loopback_only_enforced
                    ? "有効 (loopback_only_enforced = true)"
                    : "無効"}
                </dd>
              </div>
              <div>
                <dt>推論サーバ URL</dt>
                <dd data-testid="diag-llama-url">
                  <code>{diagnostics.llama_server_url}</code>
                </dd>
              </div>
              <div style={{ gridColumn: "1 / -1" }}>
                <dt>推論サーバ接続状態・ロード済みモデル</dt>
                <dd style={{ marginTop: "4px" }}>
                  {renderConnectionBadge(diagnostics)}
                  {diagnostics.llama_connection_status !== "connected" && (
                    <div
                      data-testid="diag-llama-disconnected-guide"
                      style={{
                        marginTop: "6px",
                        fontSize: "12px",
                        color: "#334155",
                        lineHeight: 1.5,
                      }}
                    >
                      実機翻訳を行う場合は、別ターミナルで PLaMo モデルを指定して{" "}
                      <code>llama-server</code>（既定 <code>127.0.0.1:8000</code>
                      ）を起動し、上部の「🔌 推論サーバ疎通確認 (probe_llama=true)」を押してください（疎通確認は{" "}
                      <code>GET /v1/models</code>{" "}
                      の軽量確認のみを行い、翻訳リクエストは送信しません）。モデル未導入で画面遷移のみ試す場合は画面1で「モック推論」を選択できますが、モックは決定論的な疑似翻訳であり実翻訳ではありません。
                    </div>
                  )}
                </dd>
              </div>
            </dl>
          )}

          <hr style={{ border: "none", borderTop: "1px solid #e2e8f0", margin: "16px 0" }} />

          <div
            data-testid="plamo-model-license-panel"
            style={{
              padding: "12px",
              backgroundColor: "#f8fafc",
              border: "1px solid #cbd5e1",
              borderRadius: "6px",
              fontSize: "12px",
              color: "#334155",
              lineHeight: 1.5,
              marginBottom: "16px",
            }}
          >
            <h3 style={{ fontSize: "14px", margin: "0 0 6px 0", color: "#0f172a" }}>
              モデル・ライセンス案内（Built with PLaMo）
            </h3>
            <ul style={{ margin: "0 0 8px 18px", padding: 0 }}>
              <li>
                <strong>使用を想定するモデル:</strong> PLaMo 3 Translate（
                <code>pfnet/plamo-3-translate-2b-GGUF</code>） / <strong>Built with PLaMo</strong>
              </li>
              <li>
                <strong>非公式ツール:</strong> 本ツールは Preferred Networks（PFN）の公式製品ではありません。
              </li>
              <li>
                <strong>ライセンスの分離と別途取得:</strong> アプリ本体のライセンス（GNU AGPL v3 /{" "}
                <code>AGPL-3.0-only</code>
                ）とモデルのライセンス（PLaMoコミュニティライセンス契約）は別です。モデル本体（GGUF）と推論サーバ（
                <code>llama.cpp</code>）は同梱しておらず、利用者が別途取得・起動する構成です。
              </li>
              <li>
                <strong>適用条件の確認:</strong>{" "}
                この画面案内だけでモデル利用や翻訳生成物の第三者提供・公表に関する全条件への対応が完了するものではありません。利用形態に応じた条件は公式ライセンス条文を確認してください（外部リンクはクリック時のみ開きます）。
              </li>
            </ul>
            <div style={{ display: "flex", gap: "14px", flexWrap: "wrap" }}>
              <a
                href="https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF"
                target="_blank"
                rel="noopener noreferrer"
                data-testid="link-plamo-model-repo"
              >
                公式モデル配布元 (pfnet/plamo-3-translate-2b-GGUF)
              </a>
              <a
                href="https://plamo.preferredai.jp/info/plamo-community-license-ja"
                target="_blank"
                rel="noopener noreferrer"
                data-testid="link-plamo-license-ja"
              >
                PLaMoコミュニティライセンス契約（日本語）
              </a>
              <a
                href="https://plamo.preferredai.jp/info/plamo-community-license-en"
                target="_blank"
                rel="noopener noreferrer"
                data-testid="link-plamo-license-en"
              >
                PLaMo Community License Agreement (English)
              </a>
            </div>
          </div>

          <h3 style={{ fontSize: "15px", margin: "0 0 8px 0" }}>
            セキュリティ・メンテナンス操作
          </h3>
          {systemActionFeedback && (
            <div
              data-testid="system-action-feedback"
              className="alert alert-info"
              style={{ marginBottom: "10px", fontSize: "12px" }}
            >
              {systemActionFeedback}
            </div>
          )}
          <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
            <button
              type="button"
              className="btn btn-secondary"
              data-testid="rotate-token-btn"
              onClick={async () => {
                await onRotateToken();
                setSystemActionFeedback(
                  "X-PaperBridge-Token をローテーションしました（旧トークンは即時失効済み）。"
                );
              }}
            >
              🔑 セッショントークンを更新 (Rotate Token)
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              data-testid="purge-temp-btn"
              onClick={async () => {
                const dir = await onPurgeTempData();
                setSystemActionFeedback(`一時作業ディレクトリを消去しました (${dir})。`);
              }}
            >
              🧹 一時作業データを削除 (Purge Working Data)
            </button>
          </div>
        </section>
      </div>
    </div>
  );
};
