/**
 * Dynamic per-model license & provenance notice shared between Screen 1 and Screen 4.
 *
 * Drives displayed model names, repositories, license terms, and links from the active
 * CatalogModel / RegisteredModel metadata so PLaMo-specific text is shown only when a
 * PLaMo model is active and never leaks onto unrelated non-PLaMo models.
 */

import React from "react";
import type {
  CatalogModel,
  ModelLicenseInfo,
  ModelsOverviewResponse,
  RegisteredModel,
} from "../types/api";

export function formatBytesHuman(bytes: number): string {
  if (bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1000 * 1000 * 1000) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / 1000000000).toFixed(2)} GB`;
}

export function resolveActiveCatalogModel(
  overview: ModelsOverviewResponse | null | undefined
): CatalogModel | null {
  if (!overview || !overview.active_model) return null;
  const activeCatId = overview.active_model.catalog_model_id;
  return overview.catalog.find((c) => c.model_id === activeCatId) ?? null;
}

interface EffectiveNoticeData {
  displayName: string;
  shortName: string;
  provider: string;
  family: string;
  sourceRepo: string;
  modelCardUrl: string | null;
  license: ModelLicenseInfo;
}

const DEFAULT_FALLBACK_NOTICE_DATA: EffectiveNoticeData = {
  displayName: "PLaMo 3 Translate",
  shortName: "PLaMo 3 Translate",
  provider: "Preferred Networks, Inc.",
  family: "plamo-3-translate",
  sourceRepo: "pfnet/plamo-3-translate-2b-GGUF",
  modelCardUrl: "https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF",
  license: {
    id: "plamo-community-license",
    name: "PLaMoコミュニティライセンス契約",
    tagline: "Built with PLaMo",
    url_ja: "https://plamo.preferredai.jp/info/plamo-community-license-ja",
    url_en: "https://plamo.preferredai.jp/info/plamo-community-license-en",
    model_card_url: "https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF",
    summary_ja:
      "アプリ本体のライセンス（GNU AGPL v3 / AGPL-3.0-only）とモデルのライセンス（PLaMoコミュニティライセンス契約）は別です。モデル本体（GGUF）と推論サーバ（llama.cpp）は同梱せず別途取得する構成です。",
    disclosure_note_ja:
      "ただし書籍・論文などの独立した長文公表物の全部または実質的部分として公表する場合は、PLaMoを翻訳に利用した旨をPDF内に含めて明示する必要があります",
    requires_agreement: true,
  },
};

function resolveNoticeData(
  catalogModel?: CatalogModel | null,
  activeModel?: RegisteredModel | null
): EffectiveNoticeData {
  if (catalogModel) {
    return {
      displayName: catalogModel.display_name,
      shortName: catalogModel.short_name,
      provider: catalogModel.provider,
      family: catalogModel.family,
      sourceRepo: catalogModel.source_repo,
      modelCardUrl: catalogModel.license.model_card_url || catalogModel.model_card_url || null,
      license: catalogModel.license,
    };
  }
  if (activeModel) {
    const looksLikePlamo = activeModel.catalog_model_id.startsWith("plamo-3-translate");
    return {
      displayName: activeModel.display_name,
      shortName: activeModel.short_name,
      provider: activeModel.provider,
      family: looksLikePlamo ? "plamo-3-translate" : activeModel.catalog_model_id,
      sourceRepo: activeModel.file_name,
      modelCardUrl: looksLikePlamo
        ? `https://huggingface.co/pfnet/plamo-3-translate-${activeModel.parameter_size.toLowerCase()}-GGUF`
        : null,
      license: looksLikePlamo
        ? {
            ...DEFAULT_FALLBACK_NOTICE_DATA.license,
            model_card_url: `https://huggingface.co/pfnet/plamo-3-translate-${activeModel.parameter_size.toLowerCase()}-GGUF`,
          }
        : {
            id: "registered-model-license",
            name: "登録モデルのライセンス条件",
            tagline: null,
            url_ja: null,
            url_en: null,
            model_card_url: null,
            summary_ja: `${activeModel.display_name}（提供元: ${activeModel.provider}）の利用条件に従ってください。`,
            disclosure_note_ja: null,
            requires_agreement: false,
          },
    };
  }
  return DEFAULT_FALLBACK_NOTICE_DATA;
}

export interface ModelLicenseNoticeProps {
  variant: "screen1" | "screen4";
  catalogModel?: CatalogModel | null;
  activeModel?: RegisteredModel | null;
}

export const ModelLicenseNotice: React.FC<ModelLicenseNoticeProps> = ({
  variant,
  catalogModel,
  activeModel,
}) => {
  const data = resolveNoticeData(catalogModel, activeModel);
  const isPlamo =
    data.family === "plamo-3-translate" || data.license.id === "plamo-community-license";

  const panelTestId =
    variant === "screen1" ? "plamo-model-notice-screen1" : "plamo-model-license-panel";
  const repoLinkTestId =
    variant === "screen1" ? "screen1-link-plamo-model" : "link-plamo-model-repo";
  const jaLinkTestId =
    variant === "screen1" ? "screen1-link-plamo-license-ja" : "link-plamo-license-ja";
  const enLinkTestId =
    variant === "screen1" ? "screen1-link-plamo-license-en" : "link-plamo-license-en";

  const panelStyle: React.CSSProperties = {
    marginTop: variant === "screen1" ? "10px" : undefined,
    marginBottom: variant === "screen4" ? "16px" : undefined,
    padding: variant === "screen1" ? "10px 12px" : "12px",
    backgroundColor: "#f8fafc",
    border: "1px solid #cbd5e1",
    borderRadius: "6px",
    fontSize: "12px",
    color: "#334155",
    lineHeight: 1.5,
  };

  if (!isPlamo) {
    return (
      <div data-testid={panelTestId} data-model-family={data.family} style={panelStyle}>
        <div>
          {data.license.tagline && (
            <>
              <strong>{data.license.tagline}</strong> —{" "}
            </>
          )}
          対象モデル：<strong>{data.displayName}</strong>（提供元: {data.provider} /{" "}
          <code>{data.sourceRepo}</code>）
        </div>
        <div style={{ marginTop: "4px" }}>
          アプリ本体のライセンス（<code>AGPL-3.0-only</code>）とモデルのライセンス（
          <strong>{data.license.name}</strong>）は別です。{data.license.summary_ja}
          {data.license.disclosure_note_ja && (
            <>
              {" "}
              <strong>{data.license.disclosure_note_ja}</strong>
            </>
          )}
          （本ツールは原著作物の翻訳・配布・公表の許諾まで与えるものではありません。）
        </div>
        {(data.modelCardUrl || data.license.url_ja || data.license.url_en) && (
          <div style={{ marginTop: "6px", display: "flex", gap: "12px", flexWrap: "wrap" }}>
            {data.modelCardUrl && (
              <a
                href={data.modelCardUrl}
                target="_blank"
                rel="noopener noreferrer"
                data-testid={repoLinkTestId}
              >
                モデル配布元 ({data.sourceRepo})
              </a>
            )}
            {data.license.url_ja && (
              <a
                href={data.license.url_ja}
                target="_blank"
                rel="noopener noreferrer"
                data-testid={jaLinkTestId}
              >
                {data.license.name}（日本語）
              </a>
            )}
            {data.license.url_en && (
              <a
                href={data.license.url_en}
                target="_blank"
                rel="noopener noreferrer"
                data-testid={enLinkTestId}
              >
                {data.license.name} (English)
              </a>
            )}
          </div>
        )}
      </div>
    );
  }

  return (
    <div data-testid={panelTestId} data-model-family={data.family} style={panelStyle}>
      {variant === "screen4" ? (
        <h3 style={{ fontSize: "14px", margin: "0 0 6px 0", color: "#0f172a" }}>
          モデル・ライセンス案内（Built with PLaMo）
        </h3>
      ) : (
        <div>
          <strong>Built with PLaMo</strong> — 使用モデル：
          <strong>{data.displayName}</strong>（<code>{data.sourceRepo}</code>）
        </div>
      )}

      {variant === "screen4" ? (
        <ul style={{ margin: "0 0 8px 18px", padding: 0 }}>
          <li>
            <strong>使用中のモデル:</strong> {data.displayName}（<code>{data.sourceRepo}</code>
            ） / <strong>Built with PLaMo</strong>
          </li>
          <li>
            <strong>非公式ツール:</strong> 本ツールは Preferred Networks（PFN）の公式製品ではありません。
          </li>
          <li>
            <strong>ライセンスの分離と別途取得:</strong> アプリ本体のライセンス（GNU AGPL v3 /{" "}
            <code>AGPL-3.0-only</code>
            ）とモデルのライセンス（{data.license.name}）は別です。モデル本体（GGUF）と推論サーバ（
            <code>llama.cpp</code>）は同梱しておらず、利用者が別途取得・起動する構成です。
          </li>
          <li>
            <strong>PLaMo 3 Translate の適用条件（2026年11月9日改定に関するPFN回答に基づく案内）:</strong>{" "}
            モデル非同梱であっても、利用者は公式リンク先の契約条件を確認・遵守する必要があります（本案内は受領回答に基づくものであり、公開条文の確認結果や過去のモデル取得時点の条件記録とは区別します）。書籍・論文などの独立した長文公表物に関する場合を除き、通常の翻訳PDFの提供・公表には一律の出自表示や契約コピー・説明ファイルの添付は不要ですが、
            <strong>
              ただし書籍・論文などの独立した長文公表物の全部または実質的部分として公表する場合は、PLaMoを翻訳に利用した旨をPDF内に含めて明示する必要があります
            </strong>
            （PDF末尾の説明ページでも対応可能です。本ツールは説明ページを自動追加しません。また原著作物の翻訳・配布・公表の許諾まで与えるものではありません。外部リンクはクリック時のみ開きます）。
          </li>
        </ul>
      ) : (
        <div style={{ marginTop: "4px" }}>
          本ツールは Preferred Networks（PFN）の公式製品ではありません。アプリ本体のライセンス（
          <code>AGPL-3.0-only</code>
          ）と <strong>PLaMo 3 Translate</strong> のライセンス（{data.license.name}
          ）は別であり、モデル本体と推論サーバ（<code>llama.cpp</code>
          ）は同梱せず別途取得する構成です。利用者は公式リンク先の契約条件を確認・遵守してください（以下は2026年11月9日改定ライセンスに関してPFNから受領した回答に基づく{" "}
          <strong>PLaMo 3 Translate</strong>{" "}
          の案内であり、公開条文の確認結果や過去のモデル取得時点の条件記録とは区別します）。書籍・論文などの独立した長文公表物に関する場合を除き、通常の翻訳PDFの提供・公表には一律の出自表示や契約コピー・説明ファイルの添付は不要ですが、
          <strong>
            ただし書籍・論文などの独立した長文公表物の全部または実質的部分として公表する場合は、PLaMoを翻訳に利用した旨をPDF内に含めて明示する必要があります
          </strong>
          （PDF末尾の説明ページでも対応可能です。本ツールは説明ページを自動追加しません。また原著作物の翻訳・配布・公表の許諾まで与えるものではありません）。
        </div>
      )}

      <div
        style={{
          marginTop: variant === "screen1" ? "6px" : undefined,
          display: "flex",
          gap: variant === "screen1" ? "12px" : "14px",
          flexWrap: "wrap",
        }}
      >
        {data.modelCardUrl && (
          <a
            href={data.modelCardUrl}
            target="_blank"
            rel="noopener noreferrer"
            data-testid={repoLinkTestId}
          >
            {variant === "screen1"
              ? "公式モデル配布元 (Hugging Face)"
              : `公式モデル配布元 (${data.sourceRepo})`}
          </a>
        )}
        {data.license.url_ja && (
          <a
            href={data.license.url_ja}
            target="_blank"
            rel="noopener noreferrer"
            data-testid={jaLinkTestId}
          >
            {variant === "screen1"
              ? "公式ライセンス（日本語）"
              : "PLaMoコミュニティライセンス契約（日本語）"}
          </a>
        )}
        {data.license.url_en && (
          <a
            href={data.license.url_en}
            target="_blank"
            rel="noopener noreferrer"
            data-testid={enLinkTestId}
          >
            {variant === "screen1"
              ? "Official License (English)"
              : "PLaMo Community License Agreement (English)"}
          </a>
        )}
      </div>
    </div>
  );
};
