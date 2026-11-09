# PaperBridge Local — API 契約仕様書および受入テスト一覧 (API_CONTRACT.md)

最終更新日: 2026-11-06
対象フェーズ: Phase 3（React + TypeScript + PDF.js フロントエンド統合）

---

## 1. 通信・認証・セキュリティ境界仕様（Section 3, 4）

### 1.1 ループバック限定と許可 Origin
- **バインドアドレス**:
  - FastAPI バックエンド: `http://127.0.0.1:8080`
  - Vite 開発サーバ: `http://127.0.0.1:5173`
  - `llama-server` 推論サーバ: `http://127.0.0.1:8000`
- **許可 Origin (`allowed_origins`)**:
  - `http://127.0.0.1:5173`, `http://localhost:5173`, `http://127.0.0.1:8080`, `http://localhost:8080`
  - ワイルドカード（`*`）は使用しない。上記以外の `Origin` ヘッダを持つリクエストは `403 Forbidden` で拒否する。

### 1.2 セッション認証トークン (`X-PaperBridge-Token`) のライフサイクル
1. **初期受け渡し (`GET /api/auth/session`)**:
   - ループバッククライアントかつ許可 Origin からのリクエストに対してのみ、現在のセッショントークン・発行時刻・有効期限（既定 12 時間）を JSON で返却する（`Cache-Control: no-store`）。
2. **API 呼び出し時の検証**:
   - `/api/auth/session` を除くすべての `/api/*` エンドポイント（PDF 取得・成果物ダウンロードを含む）で `X-PaperBridge-Token` リクエストヘッダを必須とする。
   - ヘッダ欠落、トークン不一致、または有効期限切れの場合は `401 Unauthorized` を返す。
3. **トークン更新 (`POST /api/auth/rotate`)**:
   - 有効な現トークンを提示して呼び出すと、新しいトークンを発行して即座に切り替え、旧トークンを無効化する。
4. **トークン失効 (`POST /api/auth/revoke`)**:
   - 現トークンを即座に失効させ、以降の旧トークンによるリクエストをすべて `401 Unauthorized` で拒否する。
5. **漏洩防止規約**:
   - トークンを URL クエリパラメータ（`?token=...`）に含めることを禁止する。
   - PDF.js による PDF 取得には `getDocument({ url, httpHeaders: { 'X-PaperBridge-Token': token } })` を使用する。
   - 成果物ダウンロードには認証ヘッダ付き `fetch` ＋ `Blob` URL を使用し、利用完了時またはコンポーネント破棄時に `URL.revokeObjectURL` で解放する。

---

## 2. ページ番号・座標系・状態値の不変契約（Section 3, 6, 7）

### 2.1 ページ番号の分離
| 領域 | フィールド名 | 基準 | 例（全10ページの場合） |
|---|---|---|---|
| **UI 表示・ページ範囲入力** | `display_page_number`, `page_range_spec` | **1始まり** (`1..total_pages`) | `1`, `"1, 3-5"` |
| **バックエンド内部・API クエリ** | `page_index` | **0始まり** (`0..total_pages-1`) | `0`, `[0, 2, 3, 4]` |

### 2.2 座標系と回転・CropBox の扱い
- **バックエンド正規化座標 (`bbox: [x0, y0, x1, y1]`)**:
  - 単位: **PDF ポイント (`pt`, 1/72 inch)**
  - 原点: **未回転の可視 `CropBox` 左上 (`(0, 0)`)**
  - ページ寸法 (`Page.width_pt`, `Page.height_pt`): 未回転 `CropBox` の幅 (`crop_box[2] - crop_box[0]`) と高さ (`crop_box[3] - crop_box[1]`)。
- **ビューポート座標 (`viewport_bbox_px: [vx0, vy0, vx1, vy1]`)**:
  - 単位: **CSS ピクセル (`px`)**（Canvas の物理ピクセルサイズ `width = cssWidth * devicePixelRatio` と CSS 表示サイズ `style.width = cssWidth + 'px'` を明確に分離し、SVG オーバーレイおよびマウスイベントは CSS `px` 空間で統一する）。
  - 回転角の合成: ページ固有の PDF `/Rotate` (`page.rotation_deg`: `0 | 90 | 180 | 270`) と UI 上の追加表示回転 (`user_view_rotation_deg`: `0 | 90 | 180 | 270`) の合計回転角 `total_rotation_deg = (page.rotation_deg + user_view_rotation_deg) % 360` を 1 回だけ適用する（**回転の二重適用を厳禁とする**）。
  - 変換関数: [`backend_to_viewport_bbox`](../backend/pdf/coordinates.py#L125-L160) および [`viewport_to_backend_bbox`](../backend/pdf/coordinates.py#L163-L198) と完全一致する双方向変換を使用する。

### 2.3 ジョブ状態 (`JobState`) と 13 ステージ
- `JobState`:
  - `created`, `preflight`, `parsing`, `awaiting_review`, `translating`, `validating`, `typesetting`, `completed`, `completed_with_warnings`, `failed`, `canceled`, `interrupted`
- 推論エンジン状態の 4 区分:
  1. **実機接続済 (`connected`, `is_mock=false`)**: `llama-server` 接続成功・実モデル名を表示
  2. **推論サーバ未接続 / エラー (`server_unreachable` | `model_not_found` | `model_load_failed`)**: エラー詳細を表示
  3. **テスト用モックモード (`use_mock_translator=true`, `is_mock_run=true`)**: 「モック（テスト用擬似翻訳）」バッジを明示
  4. **未確認 (`not_probed` / `unknown`)**: 「未確認（診断未実行）」と表示し、勝手に GPU 接続済と断定しない

---

## 3. エンドポイント一覧（既存 API ＋ Phase 3 追加 API）

### 3.1 認証・診断・ランタイム管理
| メソッド & パス | 区分 | リクエスト | レスポンス | エラー |
|---|---|---|---|---|
| `GET /api/auth/session` | 新設 | なし（ループバック＆許可 Origin 必須） | `SessionTokenResponse { token, issued_at, expires_at }` | `403` |
| `POST /api/auth/rotate` | 新設 | ヘッダ `X-PaperBridge-Token` | `SessionTokenResponse` | `401`, `403` |
| `POST /api/auth/revoke` | 新設 | ヘッダ `X-PaperBridge-Token` | `{ revoked: true }` | `401`, `403` |
| `GET /api/diagnostics?probe_llama={bool}` | 既存 | クエリ `probe_llama` (既定 `false`) | `SystemDiagnostics` | `401`, `403` |
| `DELETE /api/runtime/temp-data` | 既存 | なし | `{ status: "purged", directory }` | `401`, `403` |

### 3.2 ドキュメント・PDF配信・ブロック・保護領域・手動修正
| メソッド & パス | 区分 | リクエスト | レスポンス | エラー |
|---|---|---|---|---|
| `POST /api/documents/upload` | 既存 | `multipart/form-data` (`file`) | `Document` | `400`, `401`, `403` |
| `GET /api/documents/{doc_id}` | 既存 | パス `doc_id` | `Document` | `400`, `401`, `404` |
| `GET /api/documents/{doc_id}/pdf` | **新設** | パス `doc_id`（`Range` ヘッダ任意対応） | `application/pdf` バイナリ (`200` または `206`) | `400`, `401`, `404`, `416` |
| `POST /api/documents/{doc_id}/parse` | **新設** | 任意 `TranslationJobConfig` | `ParseDocumentResponse { document_id, blocks, protection_revision, edit_revision }` | `400`, `401`, `404`, `409` |
| `GET /api/documents/{doc_id}/blocks?page_index={int}&auto_parse={bool}` | 既存拡張 | クエリ `page_index`, `auto_parse` | `list[Block]` | `401`, `404` |
| `GET /api/documents/{doc_id}/protection-regions?page_index={int}` | 既存 | クエリ `page_index` | `list[ProtectionRegion]` | `401`, `404` |
| `POST /api/documents/{doc_id}/protection-regions` | 既存拡張 | `CreateProtectionRegionRequest { page_index, bbox, mode, note }` | `ProtectionRegion`（ドキュメントの `protection_revision` を更新） | `400`, `401`, `404`, `422` |
| `DELETE /api/documents/{doc_id}/protection-regions/{region_id}` | 既存拡張 | パス `doc_id`, `region_id` | `{ deleted: true, protection_revision: int }` | `401`, `404` |
| `PUT /api/documents/{doc_id}/blocks/{block_id}/manual-edit` | 既存拡張 | `ManualEditBlockRequest { manual_translation_text, expected_edit_revision? }` | `Block`（保存時プレースホルダ/数値検証結果 `manual_validation_warnings` 等および更新後 `edit_revision` を付与） | `401`, `404`, `409`, `422` |

### 3.3 ジョブ実行・進捗・キャンセル・再開・再組版・成果物取得
| メソッド & パス | 区分 | リクエスト | レスポンス | エラー |
|---|---|---|---|---|
| `POST /api/documents/{doc_id}/jobs?background={bool}` | 既存拡張 | `TranslationJobConfig` | `Job`（`background=true` 時は非同期スレッド起動） | `400`, `401`, `404`, `409` |
| `GET /api/jobs/{job_id}` | 既存 | パス `job_id` | `Job` | `401`, `404` |
| `POST /api/jobs/{job_id}/cancel` | **新設** | パス `job_id` | `CancelJobResponse { job_id, cancel_requested, state, message }` | `401`, `404` |
| `POST /api/jobs/{job_id}/resume?background={bool}` | 既存拡張 | パス `job_id` | `Job` | `401`, `404`, `409` |
| `POST /api/jobs/{job_id}/retranslate-blocks` | 既存 | `PartialRetranslateRequest { block_ids }` | `Job` | `401`, `404`, `409` |
| `POST /api/jobs/{job_id}/retypeset` | **新設** | `RetypesetRequest { expected_edit_revision?, allow_validation_warnings? }` | `RetypesetResponse { job, retypeset_revision, source_job_id, based_on_edit_revision, applied_block_revisions, artifacts, issues, llm_calls_made: 0, requires_reparse_or_retranslate }` | `400`, `401`, `404`, `409`, `422` |
| `GET /api/jobs/{job_id}/issues` | 既存 | パス `job_id` | `list[ValidationIssue]` | `401`, `404` |
| `GET /api/jobs/{job_id}/artifacts` | 既存 | パス `job_id` | `list[ExportArtifact]` | `401`, `404` |
| `GET /api/jobs/{job_id}/artifacts/{artifact_type}/download` | **新設** | パス `job_id`, `artifact_type` (`mono_ja_pdf` \| `dual_bilingual_pdf` \| `roundtrip_pdf` \| `validation_report_json`) | ファイルバイナリ (`application/pdf` または `application/json`) | `400`, `401`, `404` |

### 3.4 用語集（Glossary）管理
| メソッド & パス | 区分 | リクエスト | レスポンス | エラー |
|---|---|---|---|---|
| `GET /api/glossaries` | **新設** | なし | `list[Glossary]` | `401` |
| `POST /api/glossaries` | 既存 | `CreateGlossaryRequest { name, entries }` | `Glossary` | `401`, `422` |
| `GET /api/glossaries/{glossary_id}` | **新設** | パス `glossary_id` | `Glossary` | `401`, `404` |
| `PUT /api/glossaries/{glossary_id}` | **新設** | `CreateGlossaryRequest { name, entries }` | `Glossary`（`version` インクリメント） | `401`, `404`, `422` |
| `DELETE /api/glossaries/{glossary_id}` | **新設** | パス `glossary_id` | `{ deleted: true }` | `401`, `404` |

---

## 4. 受入テスト一覧（Section 11 対応）

1. **バックエンド API・セキュリティ・再組版テスト (`tests/integration/test_phase3_api_and_retypeset.py`)**:
   - 認証ヘッダなし（`401`）、不正トークン（`401`）、トークンローテーション・失効後の旧トークン拒否（`401`）、不正 Origin（`403`）
   - `/api/documents/{doc_id}/pdf` および `/api/jobs/{job_id}/artifacts/{artifact_type}/download` の正常配信・Range 対応・不正 ID / 未許可 `artifact_type` / パストラバーサル拒否（`400` / `404`）
   - 手動編集時のプレースホルダ欠落・数値変更検証と編集リビジョン管理
   - `POST /api/jobs/{job_id}/retypeset` において **LLM 呼び出し回数が `0` 回であること（スパイ検証）**、編集リビジョン不一致・実行中競合の `409 Conflict` 検出、再組版失敗時に直前の成果物が無傷で保持されること
   - 保護領域変更時に未翻訳ブロックが露出する場合の `requires_reparse_or_retranslate` 検出
2. **フロントエンド単体テスト (`frontend/src/__tests__/`)**:
   - ページ範囲文字列（`1, 3-5`、範囲外、逆順 `5-3`、不正文字、重複、空入力）のバリデーション
   - 回転 `0/90/180/270°` × ズーム `0.5〜2.5` × `CropBox` × `devicePixelRatio` における座標往復変換と回転の二重適用防止検証
   - TSV / JSON 用語集パーサ（空欄、重複、列不足、不正 JSON、成功行・失敗行の分離、1,200 件規模データ処理）
   - 主要 UI コンポーネントと状態表示（実機・モック・未接続・不明の区別、未知の警告コード表示、未保存編集警告）
3. **E2E・実機統合テスト**:
   - 合成PDFを用いた「登録 → プリフライト → 保護領域追加 → 翻訳実行 → 警告確認 → 手動修正 → 無推論再組版 → 成果物ダウンロード」の一貫検証（モック E2E と実機 `PLaMo 3 Translate 2B` E2E を明確に区別して記録）


## 追加API (2026-11-06)
- PUT /api/documents/{document_id}/blocks/{block_id}/protection  body: {user_protection_override: force_translate|force_protect|null} -> Block
- POST /api/documents/{document_id}/reapply-auto-protection  body: {reset_user_overrides: bool=true, clear_manual_regions: bool=false}
- Job.execution_provenance / Block.semantic_category, protection_reason, user_protection_override / SystemDiagnostics.llama_* を追加。

### 翻訳境界候補（2026-11-07）
- `Block.continuation_candidates` は、同ページの左段→右段、隣接する選択ページ間の右段→左段、または隣接する明確な一段組ページ間について、本文段落の境界候補を返す。
- 各候補は相手 `block_id`、方向、関係、信頼度、根拠、`use_as_context` を含む。低確信候補は記録のみで、翻訳へ自動適用しない。
- `use_as_context=true` の高確信候補だけ、隣接する本文をマスク済み・長さ制限付きのPFML3文脈として使う。これは連結翻訳ではない。物理Block、bbox、翻訳・検証・キャッシュ・手動編集単位は引き続き個別で、複数領域へ訳文を分配しない。
- 見出し、参考文献、キャプション、図表、独立数式、候補を分断する全幅コンテンツ、手動編集・保護との衝突、非連続ページ、段組変更は候補から除外する。一般脚注専用の分類はなく、脚注除外を完全には保証しない。利用者向け候補の承認・解除UIは未実装である。
- `Job.total_blocks` / `processed_blocks` は従来どおり物理ブロック数であり、論理翻訳単位数ではない。
