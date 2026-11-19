/**
 * Reusable Model Selection, Download, Local GGUF Registration, and Server Consistency Panel.
 *
 * Shared cleanly between Screen 4 (full model management) and Screen 1 (optional quick setup drawer).
 */

import React, { useEffect, useState } from "react";
import type {
  CatalogModel,
  ModelDownloadJob,
  ModelServerMatchResult,
  ModelServerRestartResponse,
  ModelsOverviewResponse,
  RegisteredModel,
  RegisterLocalModelRequest,
  StartModelDownloadRequest,
  VerificationStage,
} from "../types/api";
import { formatBytesHuman } from "./ModelLicenseNotice";

export interface ModelSelectionPanelProps {
  overview: ModelsOverviewResponse | null;
  activeDownload: ModelDownloadJob | null;
  isLoading: boolean;
  isSwitchingServer?: boolean;
  error: string | null;
  compact?: boolean;
  onRefreshModels: (probeLlama: boolean) => Promise<void>;
  onBrowseLocalFile: () => Promise<string | null>;
  onRegisterLocalModel: (payload: RegisterLocalModelRequest) => Promise<RegisteredModel>;
  onSelectModel: (registrationId: string) => Promise<void>;
  onDeleteModel: (registrationId: string) => Promise<void>;
  onStartDownload: (payload: StartModelDownloadRequest) => Promise<ModelDownloadJob>;
  onCancelDownload: (downloadId: string) => Promise<void>;
  onRestartLlamaServer?: (registrationId?: string | null) => Promise<ModelServerRestartResponse | void>;
}

const SOURCE_TYPE_LABELS: Record<RegisteredModel["source_type"], string> = {
  downloaded: "ダウンロード済",
  reference: "参照登録",
  managed_copy: "管理コピー",
  migrated_legacy: "既存2B自動移行",
};

export function resolveVerificationStage(
  verification: RegisteredModel["verification"]
): VerificationStage {
  const fileOk =
    verification.file_exists &&
    verification.format_valid &&
    verification.hash_status === "matched_known" &&
    verification.backend_compatible;
  if (!fileOk) {
    return "unverified";
  }
  if (verification.translation_verified) {
    return "translation_verified";
  }
  if (verification.launch_verified) {
    return "launch_verified";
  }
  return "file_verified";
}

export function verificationStatusLabel(
  reg: RegisteredModel
): {
  stage: VerificationStage;
  label: string;
  badgeClass: string;
  detailText: string;
} {
  const v = reg.verification;
  if (!v.file_exists || v.hash_status === "missing_file") {
    return {
      stage: "unverified",
      label: "未検証（ファイル欠損・要再指定）",
      badgeClass: "badge-error",
      detailText:
        "モデルファイルが見つかりません（欠損中は未検証・利用不可です）。場所の再指定または再ダウンロード後に再検証してください。",
    };
  }
  if (!v.format_valid || v.hash_status === "mismatch") {
    return {
      stage: "unverified",
      label: "未検証（整合性エラー）",
      badgeClass: "badge-error",
      detailText:
        "GGUF形式またはSHA-256がカタログ定義と一致しません（過去の検証実績はリセット済み・利用不可）。",
    };
  }
  if (v.hash_status === "unverified" || !v.backend_compatible) {
    return {
      stage: "unverified",
      label: "未検証",
      badgeClass: "badge-warning",
      detailText: "ファイル整合性または推論バックエンド互換性の確認が未完了です。",
    };
  }
  if (v.translation_verified) {
    return {
      stage: "translation_verified",
      label: "実翻訳確認済み",
      badgeClass: "badge-success",
      detailText:
        "この登録モデルについての過去の検証実績: SHA-256検証・実機接続・実翻訳推論の完了を確認済み（機械推論の完了実績であり、人間による訳文品質承認ではありません）。現在の接続・一致状態は別欄で確認してください。",
    };
  }
  if (v.launch_verified) {
    return {
      stage: "launch_verified",
      label: "起動確認済み",
      badgeClass: "badge-launch",
      detailText:
        "この登録モデルについての過去の検証実績: SHA-256検証と過去の llama-server 接続・モデル一致を確認済み（実翻訳推論の実績はまだありません）。現在この瞬間の接続・一致状態は別欄で確認してください。",
    };
  }
  return {
    stage: "file_verified",
    label: "ファイル検証済み",
    badgeClass: "badge-info",
    detailText:
      "この登録モデルについての検証実績: ファイル存在・GGUF形式・SHA-256確認済み（実機接続・実翻訳の実績はまだありません）。",
  };
}

export interface ServerMatchDisplayInfo {
  statusCode: string;
  labelJa: string;
  summaryJa: string;
  detailJa: string;
  badgeClass: string;
  alertClass: string;
}

const MATCH_STATUS_LABELS: Record<
  string,
  { labelJa: string; badgeClass: string; alertClass: string }
> = {
  matched: {
    labelJa: "接続可・一致 (matched)",
    badgeClass: "badge-success",
    alertClass: "alert-success",
  },
  matched_same_model_different_path: {
    labelJa: "接続可・同一モデル別パス (matched_same_model_different_path)",
    badgeClass: "badge-success",
    alertClass: "alert-warning",
  },
  mismatch: {
    labelJa: "不一致 (mismatch)",
    badgeClass: "badge-error",
    alertClass: "alert-error",
  },
  server_unreachable: {
    labelJa: "接続できない (server_unreachable)",
    badgeClass: "badge-error",
    alertClass: "alert-warning",
  },
  selected_model_invalid: {
    labelJa: "選択モデル利用不可 (selected_model_invalid)",
    badgeClass: "badge-error",
    alertClass: "alert-error",
  },
  no_active_model: {
    labelJa: "モデル未選択 (no_active_model)",
    badgeClass: "badge-neutral",
    alertClass: "alert-warning",
  },
  server_path_unverified: {
    labelJa: "接続可・パス未確認 (server_path_unverified)",
    badgeClass: "badge-info",
    alertClass: "alert-info",
  },
  not_probed: {
    labelJa: "未確認 (not_probed)",
    badgeClass: "badge-neutral",
    alertClass: "alert-info",
  },
};

export function describeServerMatchStatus(
  serverMatch: Pick<ModelServerMatchResult, "match_status" | "message_ja" | "is_matched"> | null | undefined,
  fallbackStatus?: string | null
): ServerMatchDisplayInfo {
  const code = serverMatch?.match_status || fallbackStatus || "not_probed";
  const meta = MATCH_STATUS_LABELS[code] || {
    labelJa: `状態: ${code}`,
    badgeClass: "badge-neutral",
    alertClass: "alert-warning",
  };

  if (!serverMatch || code === "not_probed") {
    return {
      statusCode: "not_probed",
      labelJa: "未確認 (not_probed)",
      summaryJa: "未確認",
      detailJa:
        "現在の推論エンドポイント接続・一致状態は未確認です（初期表示では自動接続確認を行っていません）。「再確認」または画面4の「推論サーバ疎通確認」で確認できます。",
      badgeClass: "badge-neutral",
      alertClass: "alert-info",
    };
  }

  if (code === "server_unreachable") {
    const backendMsg = serverMatch.message_ja?.trim();
    return {
      statusCode: code,
      labelJa: meta.labelJa,
      summaryJa: "接続できない",
      detailJa: backendMsg
        ? `${backendMsg}（接続できない事実のみの報告であり、未起動のほかにポート・URL設定差異や接続制限の可能性があります）`
        : "推論エンドポイントへ接続できません（未起動のほか、ポート・URL設定差異や接続制限の可能性があります）。",
      badgeClass: meta.badgeClass,
      alertClass: meta.alertClass,
    };
  }

  return {
    statusCode: code,
    labelJa: meta.labelJa,
    summaryJa: meta.labelJa,
    detailJa: serverMatch.message_ja || meta.labelJa,
    badgeClass: meta.badgeClass,
    alertClass: meta.alertClass,
  };
}

export const ModelSelectionPanel: React.FC<ModelSelectionPanelProps> = ({
  overview,
  activeDownload,
  isLoading,
  isSwitchingServer = false,
  error,
  compact = false,
  onRefreshModels,
  onBrowseLocalFile,
  onRegisterLocalModel,
  onSelectModel,
  onDeleteModel,
  onStartDownload,
  onCancelDownload,
  onRestartLlamaServer,
}) => {
  const catalog = overview?.catalog ?? [];
  const defaultCatalogId =
    overview?.active_model?.catalog_model_id ||
    catalog.find((c) => c.is_default)?.model_id ||
    catalog[0]?.model_id ||
    "";

  const [selectedCatalogId, setSelectedCatalogId] = useState<string>(defaultCatalogId);
  const [licenseAgreed, setLicenseAgreed] = useState<boolean>(false);
  const [localFilePath, setLocalFilePath] = useState<string>("");
  const [copyToManagedDir, setCopyToManagedDir] = useState<boolean>(false);
  const [strictCatalogMatch, setStrictCatalogMatch] = useState<boolean>(false);
  const [relocateTarget, setRelocateTarget] = useState<RegisteredModel | null>(null);
  const [actionBusy, setActionBusy] = useState<boolean>(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedCatalogId && defaultCatalogId) {
      setSelectedCatalogId(defaultCatalogId);
    }
  }, [defaultCatalogId, selectedCatalogId]);

  const selectedCatalog: CatalogModel | null =
    catalog.find((c) => c.model_id === selectedCatalogId) ?? catalog[0] ?? null;

  const isDownloading =
    activeDownload !== null &&
    (activeDownload.status === "downloading" || activeDownload.status === "verifying");

  const hasActiveJob = Boolean(overview?.has_active_translation_job);
  const serverMatch = overview?.server_match ?? null;
  const displayError = actionError || error;

  const destinationPreview =
    selectedCatalog && overview?.managed_models_dir
      ? `${overview.managed_models_dir}\\${selectedCatalog.relative_subdir}\\${selectedCatalog.filename}`
      : "";

  const handleBrowse = async () => {
    setActionError(null);
    setActionMessage(null);
    setActionBusy(true);
    try {
      const picked = await onBrowseLocalFile();
      if (picked) {
        setLocalFilePath(picked);
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(false);
    }
  };

  const handleRegisterLocal = async (overridePath?: string, replaces?: RegisteredModel | null) => {
    const pathToRegister = (overridePath ?? localFilePath).trim();
    if (!pathToRegister) {
      setActionError("登録する GGUF ファイルのパスを指定するか「参照...」から選択してください。");
      return;
    }
    const targetReg = replaces !== undefined ? replaces : relocateTarget;
    setActionError(null);
    setActionMessage(null);
    setActionBusy(true);
    try {
      const reg = await onRegisterLocalModel({
        file_path: pathToRegister,
        expected_catalog_model_id: targetReg
          ? targetReg.catalog_model_id
          : strictCatalogMatch && selectedCatalog
          ? selectedCatalog.model_id
          : null,
        register_mode: targetReg ? "reference" : copyToManagedDir ? "copy" : "reference",
        license_accepted: targetReg ? true : licenseAgreed,
        set_active: !hasActiveJob,
        replaces_registration_id: targetReg?.registration_id ?? null,
      });
      setRelocateTarget(null);
      setActionMessage(
        `${targetReg ? "モデルパスを再指定しました" : "ローカルモデルを検証・登録しました"}: ${
          reg.display_name
        } (${SOURCE_TYPE_LABELS[reg.source_type]})`
      );
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(false);
    }
  };

  const handleStartDownload = async (catModelId?: string) => {
    const targetId = catModelId || selectedCatalog?.model_id;
    if (!targetId) return;
    setActionError(null);
    setActionMessage(null);
    setActionBusy(true);
    try {
      await onStartDownload({
        catalog_model_id: targetId,
        license_accepted: licenseAgreed,
        set_active: !hasActiveJob,
        background: true,
      });
      setActionMessage("モデルのダウンロードを開始しました（SHA-256 検証付き）。");
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(false);
    }
  };

  const handleSelect = async (registrationId: string) => {
    setActionError(null);
    setActionMessage(null);
    setActionBusy(true);
    try {
      await onSelectModel(registrationId);
      setActionMessage("使用モデルを切り替えました。推論サーバー状態を確認してください。");
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(false);
    }
  };

  const handleRestartServer = async () => {
    if (!onRestartLlamaServer) return;
    setActionError(null);
    setActionMessage(null);
    setActionBusy(true);
    try {
      const res = await onRestartLlamaServer(overview?.active_model_id ?? null);
      if (res && typeof res === "object" && "message_ja" in res && res.message_ja) {
        setActionMessage(res.message_ja);
      } else {
        setActionMessage("選択モデルで推論サーバーを再起動し、接続・一致状態を確認しました。");
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(false);
    }
  };

  const handleDelete = async (reg: RegisteredModel) => {
    setActionError(null);
    setActionMessage(null);
    setActionBusy(true);
    try {
      await onDeleteModel(reg.registration_id);
      if (relocateTarget?.registration_id === reg.registration_id) {
        setRelocateTarget(null);
      }
      setActionMessage(
        `登録を解除しました: ${reg.display_name}（参照元の GGUF ファイル自体は削除されません）`
      );
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(false);
    }
  };

  const handleRelocateWithDialog = async (reg: RegisteredModel) => {
    setRelocateTarget(reg);
    setActionError(null);
    setActionMessage(null);
    setActionBusy(true);
    try {
      const picked = await onBrowseLocalFile();
      if (picked) {
        setLocalFilePath(picked);
        await handleRegisterLocal(picked, reg);
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(false);
    }
  };

  if (!overview) {
    return (
      <div data-testid="model-selection-panel" className="alert alert-info">
        モデルカタログと登録状態を読み込み中...
      </div>
    );
  }

  return (
    <div data-testid="model-selection-panel">
      {/* Header & Refresh */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: "8px",
          marginBottom: "10px",
        }}
      >
        <div>
          <strong style={{ fontSize: compact ? "14px" : "15px" }}>
            翻訳モデル選択・取得・登録管理
          </strong>
          {overview.active_model ? (
            <span
              data-testid="active-model-summary-badge"
              className="badge badge-info"
              style={{ marginLeft: "8px" }}
            >
              使用中: {overview.active_model.display_name}
            </span>
          ) : (
            <span
              data-testid="active-model-summary-badge"
              className="badge badge-warning"
              style={{ marginLeft: "8px" }}
            >
              使用モデル未選択
            </span>
          )}
        </div>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          data-testid="refresh-models-btn"
          disabled={isLoading || actionBusy || isSwitchingServer}
          onClick={() => void onRefreshModels(true)}
        >
          {isLoading ? "確認中..." : "🔄 登録状態・サーバ一致を再確認"}
        </button>
      </div>

      {/* Active Job Switch Lock */}
      {hasActiveJob && (
        <div
          role="alert"
          data-testid="model-active-job-lock"
          className="alert alert-warning"
          style={{ marginBottom: "10px", fontSize: "12px" }}
        >
          <strong>ジョブ実行中ロック:</strong>{" "}
          翻訳ジョブの進行中は使用モデルの切替を行えません。完了または停止後に切り替えてください。
        </div>
      )}

      {/* Running Server vs Selected Model Current Connection & Consistency */}
      {(() => {
        const matchDisplay = describeServerMatchStatus(serverMatch);
        const headingPrefix = "現在の接続・一致状態:";
        if (!serverMatch) {
          return (
            <div
              data-testid="model-current-server-status"
              className="alert alert-info"
              style={{ marginBottom: "10px", fontSize: "12px" }}
            >
              <strong>{headingPrefix}</strong>{" "}
              <span className={`badge ${matchDisplay.badgeClass}`} style={{ marginRight: "6px" }}>
                {matchDisplay.labelJa}
              </span>
              {matchDisplay.detailJa}
            </div>
          );
        }
        return (
          <div
            role={serverMatch.match_status === "mismatch" ? "alert" : undefined}
            data-testid={
              serverMatch.match_status === "mismatch"
                ? "model-server-mismatch-alert"
                : "model-server-match-banner"
            }
            className={`alert ${matchDisplay.alertClass}`}
            style={{ marginBottom: "10px", fontSize: "12px" }}
          >
            <strong>{headingPrefix}</strong>{" "}
            <span className={`badge ${matchDisplay.badgeClass}`} style={{ marginRight: "6px" }}>
              {matchDisplay.labelJa}
            </span>
            {matchDisplay.detailJa}
            {serverMatch.match_status === "mismatch" && (
              <div style={{ marginTop: "4px" }}>
                <div>
                  選択中: <code>{serverMatch.active_display_name ?? "-"}</code>（
                  <code>{serverMatch.active_file_path ?? "-"}</code>） / サーバー応答側:{" "}
                  <code>
                    {serverMatch.running_identified_display_name ??
                      serverMatch.running_loaded_alias ??
                      "不明"}
                  </code>
                  （<code>{serverMatch.running_model_path ?? serverMatch.running_model_file ?? "-"}</code>）
                </div>
                {onRestartLlamaServer && (
                  <div style={{ marginTop: "8px", display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      data-testid="restart-llama-server-btn"
                      disabled={isSwitchingServer || actionBusy || hasActiveJob}
                      onClick={() => void handleRestartServer()}
                    >
                      {isSwitchingServer
                        ? "推論サーバーを再起動・モデルをロード中..."
                        : "このモデルで推論サーバーを再起動して切り替える"}
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })()}

      {/* Action Feedback / Error */}
      {displayError && (
        <div
          role="alert"
          data-testid="model-panel-error"
          className="alert alert-error"
          style={{ marginBottom: "10px", fontSize: "12px" }}
        >
          <strong>モデル操作エラー:</strong> {displayError}
        </div>
      )}
      {actionMessage && (
        <div
          data-testid="model-panel-feedback"
          className="alert alert-info"
          style={{ marginBottom: "10px", fontSize: "12px" }}
        >
          {actionMessage}
        </div>
      )}

      {/* Registered Models Section */}
      <div style={{ marginBottom: "14px" }}>
        <div style={{ fontWeight: 600, fontSize: "13px", marginBottom: "6px" }}>
          登録済みローカルモデル一覧（複数保持・即時切替可能）
        </div>
        {overview.registered.length === 0 ? (
          <div
            data-testid="registered-models-empty"
            style={{
              padding: "10px",
              border: "1px dashed #cbd5e1",
              borderRadius: "6px",
              fontSize: "12px",
              color: "#475569",
            }}
          >
            登録済みモデルはありません。下から公式モデルをダウンロードするか、手元の GGUF
            ファイルを参照登録してください。
          </div>
        ) : (
          <div
            data-testid="registered-models-list"
            style={{ display: "flex", flexDirection: "column", gap: "8px" }}
          >
            {overview.registered.map((reg) => {
              const isActive = reg.registration_id === overview.active_model_id;
              const cat = catalog.find((c) => c.model_id === reg.catalog_model_id);
              const verInfo = verificationStatusLabel(reg);
              const isUsable =
                reg.verification.file_exists &&
                reg.verification.format_valid &&
                reg.verification.hash_status === "matched_known" &&
                reg.verification.backend_compatible;

              return (
                <div
                  key={reg.registration_id}
                  data-testid={`registered-model-item-${reg.registration_id}`}
                  style={{
                    padding: "10px",
                    border: isActive ? "2px solid #2563eb" : "1px solid #cbd5e1",
                    borderRadius: "6px",
                    backgroundColor: isActive ? "#eff6ff" : "#ffffff",
                    fontSize: "12px",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      flexWrap: "wrap",
                      gap: "6px",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
                      <strong>{reg.display_name}</strong>
                      {isActive && (
                        <span
                          data-testid={`active-badge-${reg.registration_id}`}
                          className="badge badge-info"
                        >
                          使用中
                        </span>
                      )}
                      <span style={{ color: "#475569", fontSize: "11px" }}>
                        過去の検証実績:
                      </span>
                      <span
                        data-testid={`verification-badge-${reg.registration_id}`}
                        data-verification-stage={verInfo.stage}
                        title="この登録モデルについての過去の検証実績（現在の接続状態は上の欄を参照）"
                        className={`badge ${verInfo.badgeClass}`}
                      >
                        {verInfo.label}
                      </span>
                      <span className="badge badge-neutral">
                        {SOURCE_TYPE_LABELS[reg.source_type]}
                      </span>
                    </div>
                    <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        data-testid={`select-model-btn-${reg.registration_id}`}
                        disabled={isActive || !isUsable || hasActiveJob || actionBusy || isSwitchingServer}
                        onClick={() => void handleSelect(reg.registration_id)}
                      >
                        {isActive ? "選択中" : "使用モデルに切替"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        data-testid={`relocate-model-btn-${reg.registration_id}`}
                        disabled={actionBusy}
                        onClick={() => void handleRelocateWithDialog(reg)}
                      >
                        場所を再指定...
                      </button>
                      {!reg.verification.file_exists && cat && (
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          data-testid={`redownload-model-btn-${reg.registration_id}`}
                          disabled={isDownloading || actionBusy || hasActiveJob}
                          onClick={() => {
                            setSelectedCatalogId(cat.model_id);
                            setLicenseAgreed(true);
                            void handleStartDownload(cat.model_id);
                          }}
                        >
                          再ダウンロード
                        </button>
                      )}
                      <button
                        type="button"
                        className="btn btn-danger-sm"
                        data-testid={`delete-model-btn-${reg.registration_id}`}
                        disabled={actionBusy || (isActive && hasActiveJob)}
                        onClick={() => void handleDelete(reg)}
                      >
                        登録解除
                      </button>
                    </div>
                  </div>
                  <div style={{ marginTop: "4px", color: "#475569", wordBreak: "break-all" }}>
                    ファイル: <code>{reg.file_path}</code> （
                    {formatBytesHuman(reg.file_size_bytes)} / SHA-256:{" "}
                    <code>{reg.measured_sha256 ? reg.measured_sha256.slice(0, 12) : "未計測"}</code>）
                  </div>
                  <div
                    data-testid={`verification-detail-${reg.registration_id}`}
                    style={{ marginTop: "2px", color: "#475569", fontSize: "11px" }}
                  >
                    {verInfo.detailText}
                  </div>
                  {reg.verification.validation_error && (
                    <div style={{ marginTop: "4px", color: "#b91c1c" }}>
                      {reg.verification.validation_error}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Add / Provision Model From Catalog or Local File */}
      <div
        style={{
          padding: "12px",
          border: "1px solid #cbd5e1",
          borderRadius: "6px",
          backgroundColor: "#f8fafc",
        }}
      >
        <div style={{ fontWeight: 600, fontSize: "13px", marginBottom: "8px" }}>
          新しいモデルを導入・追加登録（ダウンロード または 手元ファイルの参照登録）
        </div>

        <div className="form-group" style={{ marginBottom: "8px" }}>
          <label className="form-label" htmlFor="catalog-model-select">
            カタログからモデルを選択
          </label>
          <select
            id="catalog-model-select"
            data-testid="catalog-model-select"
            className="form-input"
            value={selectedCatalog?.model_id ?? ""}
            disabled={isDownloading}
            onChange={(ev) => {
              setSelectedCatalogId(ev.target.value);
              setLicenseAgreed(false);
            }}
          >
            {catalog.map((c) => {
              const tag =
                c.catalog_verification_status === "verified_runtime"
                  ? "標準推奨・カタログ実機確認済"
                  : "カタログ検証済・実機未検証";
              return (
                <option key={c.model_id} value={c.model_id}>
                  {c.display_name} [{tag} / {formatBytesHuman(c.known_size_bytes)}]
                </option>
              );
            })}
          </select>
        </div>

        {selectedCatalog && (
          <div
            data-testid="selected-catalog-details"
            style={{
              padding: "8px 10px",
              backgroundColor: "#ffffff",
              border: "1px solid #e2e8f0",
              borderRadius: "6px",
              fontSize: "12px",
              color: "#334155",
              marginBottom: "8px",
              lineHeight: 1.5,
            }}
          >
            <div>
              <strong>サイズ / 保存予定先:</strong> {formatBytesHuman(selectedCatalog.known_size_bytes)}{" "}
              / <code>{destinationPreview}</code>
            </div>
            <div>
              <strong>ハードウェア目安・検証状況:</strong> {selectedCatalog.hardware_note_ja}
            </div>
            <div>
              <strong>ライセンス ({selectedCatalog.license.name}):</strong>{" "}
              {selectedCatalog.license.summary_ja}
              {selectedCatalog.license.disclosure_note_ja && (
                <> {selectedCatalog.license.disclosure_note_ja}</>
              )}
            </div>
          </div>
        )}

        {selectedCatalog?.license.requires_agreement && (
          <label
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: "8px",
              fontSize: "12px",
              marginBottom: "10px",
              cursor: "pointer",
            }}
          >
            <input
              type="checkbox"
              data-testid="model-license-agree-checkbox"
              checked={licenseAgreed}
              disabled={isDownloading}
              onChange={(ev) => setLicenseAgreed(ev.target.checked)}
              style={{ marginTop: "3px" }}
            />
            <span>
              選択中モデル「{selectedCatalog.display_name}」の利用条件（
              {selectedCatalog.license.name}）およびファイルサイズを確認し、同意します。
            </span>
          </label>
        )}

        {/* Download Progress / Controls */}
        <div
          style={{
            display: "flex",
            gap: "8px",
            alignItems: "center",
            flexWrap: "wrap",
            marginBottom: "10px",
          }}
        >
          <button
            type="button"
            className="btn btn-primary btn-sm"
            data-testid="start-model-download-btn"
            disabled={
              isDownloading ||
              actionBusy ||
              hasActiveJob ||
              !selectedCatalog ||
              (selectedCatalog.license.requires_agreement && !licenseAgreed)
            }
            onClick={() => void handleStartDownload()}
          >
            ⬇ 公式からダウンロード ({selectedCatalog ? formatBytesHuman(selectedCatalog.known_size_bytes) : "-"})
          </button>

          {isDownloading && activeDownload && (
            <button
              type="button"
              className="btn btn-danger-sm"
              data-testid="cancel-model-download-btn"
              onClick={() => void onCancelDownload(activeDownload.download_id)}
            >
              ダウンロードをキャンセル
            </button>
          )}
        </div>

        {activeDownload && (
          <div
            data-testid="model-download-progress"
            className={`alert ${
              activeDownload.status === "failed"
                ? "alert-error"
                : activeDownload.status === "canceled"
                ? "alert-warning"
                : activeDownload.status === "completed"
                ? "alert-success"
                : "alert-info"
            }`}
            style={{ marginBottom: "10px", fontSize: "12px" }}
          >
            <div>
              <strong>ダウンロード状態 [{activeDownload.display_name}]:</strong>{" "}
              {activeDownload.status} ({activeDownload.percent.toFixed(1)}% /{" "}
              {formatBytesHuman(activeDownload.bytes_downloaded)} /{" "}
              {formatBytesHuman(activeDownload.total_bytes)})
            </div>
            {activeDownload.error && <div>{activeDownload.error}</div>}
          </div>
        )}

        {/* Local File Registration */}
        <div style={{ borderTop: "1px solid #e2e8f0", paddingTop: "10px" }}>
          {relocateTarget && (
            <div
              data-testid="relocate-target-banner"
              className="alert alert-info"
              style={{ marginBottom: "8px", fontSize: "12px" }}
            >
              <strong>場所の再指定モード:</strong> {relocateTarget.display_name}（旧パス:{" "}
              <code>{relocateTarget.file_path}</code>）の新しい場所を指定してください。{" "}
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => setRelocateTarget(null)}
              >
                再指定を終了
              </button>
            </div>
          )}

          <div style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
            <input
              type="text"
              data-testid="local-model-path-input"
              className="form-input"
              placeholder="C:\path\to\plamo-3-translate-2b-Q4_K_M.gguf"
              value={localFilePath}
              onChange={(ev) => setLocalFilePath(ev.target.value)}
              style={{ flex: "1 1 280px" }}
            />
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              data-testid="browse-local-model-btn"
              disabled={actionBusy}
              onClick={() => void handleBrowse()}
            >
              📁 参照...
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              data-testid="register-local-model-btn"
              disabled={
                actionBusy ||
                isDownloading ||
                (!relocateTarget &&
                  Boolean(selectedCatalog?.license.requires_agreement) &&
                  !licenseAgreed)
              }
              onClick={() => void handleRegisterLocal()}
            >
              {relocateTarget ? "再指定したファイルを検証登録" : "手元モデルを検証・登録（再DL不要）"}
            </button>
          </div>

          <div style={{ marginTop: "6px", display: "flex", flexDirection: "column", gap: "4px", fontSize: "12px" }}>
            <label style={{ display: "flex", alignItems: "center", gap: "6px", cursor: "pointer" }}>
              <input
                type="checkbox"
                data-testid="local-copy-mode-checkbox"
                checked={copyToManagedDir}
                disabled={Boolean(relocateTarget)}
                onChange={(ev) => setCopyToManagedDir(ev.target.checked)}
              />
              <span>
                管理フォルダーへコピーして配置する（OFFの既定値では元ファイルを移動・複製せず参照登録します）
              </span>
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: "6px", cursor: "pointer" }}>
              <input
                type="checkbox"
                data-testid="local-strict-catalog-match-checkbox"
                checked={strictCatalogMatch}
                disabled={Boolean(relocateTarget)}
                onChange={(ev) => setStrictCatalogMatch(ev.target.checked)}
              />
              <span>
                上で選んだカタログモデル（{selectedCatalog?.short_name ?? "-"}
                ）との一致を厳密チェックする（OFF時は 0.8B/2B/8B の SHA-256 から自動識別）
              </span>
            </label>
          </div>
        </div>
      </div>
    </div>
  );
};
