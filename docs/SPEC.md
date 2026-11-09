# PaperBridge Local — 詳細仕様書 (SPEC.md)

## 1. 優先順位（不変原則）
設計または実装で迷った場合、常に以下の優先順位に従う。

1. **数式・インライン記号・添字・ギリシャ文字・参照番号・図表を壊さないこと**
2. **壊れた PDF を「成功」として出力しないこと（検証と失敗報告）**
3. **元 PDF を絶対に書き換えないこと（SHA-256 事前・事後検証）**
4. **Windows ローカル環境で再現可能に動くこと**
5. **PLaMo 3 Translate のプロンプト形式（PFML3）・停止条件・コンテキスト長を守ること**
6. **日本語として読みやすく、学術論文として用語が一貫していること**
7. **UI の見た目や高度な編集機能**

---

## 2. スコープ定義

### 2.1 対象とする PDF（In-Scope）
- LaTeX / Word 等から生成された **ボーンデジタル PDF**（テキストレイヤーを持つ英語学術論文 PDF）
- 1 段組・2 段組の学術論文
- インライン数式（`$x_i$`, `$\alpha + \beta = \gamma$`, `$f(x) \in \mathbb{R}^d$` 等）および独立数式（番号付き数式 `(1)`, `(2)` 等）を含む PDF
- ベクタ図・ラスタ画像・表・アルゴリズム枠・脚注・参考文献を含む PDF
- ページ範囲指定（例: `1, 3-5`、UI 表示は 1 始まり、内部インデックスは 0 始まり）
- `CropBox != MediaBox` または `/Rotate` (`0`, `90`, `180`, `270`) を持つ PDF

### 2.2 初期スコープ外（Out-of-Scope / 明示的制限）
- **スキャン PDF（画像のみ、テキストレイヤーなし）**:
  - 自動 OCR フォールバックは **禁止**（OCR による数式破損を隠蔽しないため）。
  - テキスト抽出密度が閾値未満の場合、`SKIPPED_SCANNED_PDF` を返し明確に拒否する。
- **暗号化 PDF（パスワード保護）**:
  - `ENCRYPTED_PDF` として明確に拒否する。
- **破損 PDF**:
  - `CORRUPTED_PDF` として明確に拒否する。
- **図内部テキスト・複雑な表セル内テキスト**:
  - 初期既定では **非翻訳（原本保持）** とし、グラフ軸ラベルや表罫線の崩壊を防ぐ。

---

## 3. パイプラインの 13 ステージ (`JobStage`)

すべての翻訳・再構成ジョブは以下の 13 ステージを順序通りに遷移し、各ステージの進捗・警告・失敗理由を記録する（詳細は [docs/API_CONTRACT.md](API_CONTRACT.md) 第 5 節を参照）。

1. `1_register_and_hash`: 入力 PDF の受付、安全なファイル名正規化、SHA-256 計算。
2. `2_preflight_validation`: 暗号化・破損・ページ数上限検査。
3. `3_extractability_check`: スキャン PDF 判定・抽出文字密度検査。
4. `4_layout_and_block_extraction`: ページ座標系（未回転 `CropBox` 幅・高さ、`Rotation`）とテキストブロック・図表・表グリッド領域の抽出。
5. `5_formula_and_figure_protection`: 独立数式・図・表・手動保護領域の非翻訳保護（`protection_source = "auto" | "manual"`）。
6. `6_inline_placeholder_masking`: インライン数式・引用 `[1]`・参照 `Eq. (1)` のプレースホルダ `{v1}`, `{v2}`, ... への退避。
7. `7_glossary_and_context_attach`: 用語集動的抽出・前後文脈付与。
8. `8_plamo_translation`: PLaMo 3 Translate（PFML3 形式）または Mock アダプタによるブロック単位翻訳（10 要素複合キャッシュ照合含む）。
9. `9_translation_validation`: 10 項目の翻訳出力検証。
10. `10_retry_or_fallback`: 失敗時のリトライ（最大 2 回・文分割含む）または原文維持フォールバック (`FALLBACK_ORIGINAL`)。
11. `11_japanese_typesetting`: 日本語組版・フォント埋め込み（OFL `NotoSerifJP` / `NotoSansJP`）・段階的フォントサイズ縮小（最小 8.0pt）・Redaction 原文消去と再描画。
12. `12_postflight_and_visual_check`: 生成 PDF の再オープン検査・ページ数一致・元 PDF 不変性（SHA-256）・保護領域/非翻訳背景の 150 DPI 視覚差分検査。
13. `13_export_artifacts`: 単一言語 PDF・見開き対訳 PDF・QA JSON レポート・視覚検査 PNG の出力。

### 3.1 境界で続く本文（初版）

- `reading_order` と `column_index` は物理ブロックの読み順メタデータであり、ブロックを連結するものではない。
- 初版では物理ブロックのID・ページ・bboxを保持し、翻訳・検証・編集・描画を独立させる。日本語訳を文字数比で分割せず、複数領域へ自動配分しない。
- 左段末→右段冒頭、隣接する選択ページの右段末→左段冒頭、一段組ページ末→次の一段組ページ冒頭を候補とする。前後双方が本文段落で、段組が一致し、境界に見出し・参考文献・キャプション・図表・保護数式・全幅要素がなく、句読点・開始文字・フォントサイズ・必要な場合のインデント等が複数合う場合のみ、高確信の隣接テキストをPFML3の文脈として使う。
- ピリオドの有無だけでは決めない。略語終端や閉じ引用符・括弧を考慮する。曖昧な候補は理由付きで記録するだけで、文脈に適用しない。
- 数式等の保護プレースホルダーは隣接文脈内では中立マーカーに置換する。ページ範囲外のブロックは候補にも文脈にも使用しない。
- この方式は**連結翻訳ではなく文脈補助**であり、UIの候補承認・解除操作は未実装。複数領域への安全な配置と利用者選択を実装するまでは、物理ブロック結合を行わない。
- `total_blocks` / `processed_blocks` は物理ブロック数であり、翻訳候補や論理単位の数ではない。再開時は完了状態から再計算し、警告は同一位置・コード・内容を重複集計しない。

---

## 4. 座標系仕様 (`backend/pdf/coordinates.py`, `frontend/src/utils/coordinates.ts`)

PDF 原生座標系、内部正規化座標系、フロントエンド Canvas/SVG 座標系の混同による BBox ずれを防ぐため、以下の 3 座標系を厳密に分離する。

1. **PDF Native Coordinate (`PDFRect`)**:
   - 原点: **左下 (Bottom-Left)**、単位: `pt` (`1/72 inch`)。
   - `CropBox` が `MediaBox` と異なる場合、`CropBox` の左下 `(crop_x0, crop_y0)` をオフセット基準とする。
2. **Backend Normalized Coordinate (`BBox`)**:
   - 原点: **左上 (Top-Left)**、単位: `pt`、**未回転の `CropBox` 幅・高さ (`float(mpage.cropbox.width)`, `float(mpage.cropbox.height)`)** を基準とする。
   - API・SQLite・フロントエンド通信はすべてこの座標系を使用する。
3. **Frontend Viewport Coordinate (`ViewportRect`)**:
   - 原点: **左上 (Top-Left)**、単位: CSS `px`（`scale` 倍率およびページ回転 `rotation in {0, 90, 180, 270}` 適用後）。
   - `rotation in {0, 90, 180, 270}` および任意の `scale` (`0.5〜2.5x`) に対して `backendToViewportBBox` ⇄ `viewportToBackendBBox` が誤差 `< 1e-4 pt` で往復一致することを バックエンド (Pytest) およびフロントエンド (Vitest) の双方の単体テストで保証する。

---

## 5. PLaMo 3 Translate (PFML3) プロンプト仕様 (`backend/translation/prompts/pfml3.py`)

### 5.1 PFML3 タグ構造
`pfnet/plamo-3-translate-2b` / `pfnet/plamo-3-translate-2b-GGUF` の公式仕様（`chat_template.jinja` および公式 `README_ja.md`）に準拠し、OpenAI Chat Completions の汎用システムプロンプトではなく、以下の PFML3 フォーマット（タグ間に改行を含まない単一文字列）を `/v1/completions` (`prompt`) に直接送信する。

- `<|plamo:bos|>` の扱い:
  - `llama.cpp` が自動で先頭に `<|plamo:bos|>` (`id=1`) を付与するため、直接組み立てたプロンプト側では `include_bos=False` を既定とし、`<|plamo:bos|>` の二重付与を防ぐ。
- データセットタグ:
  - `<|plamo:tag|>dataset<|plamo:msg|>translation`
- 入力・出力タグ:
  - 原文入力: `<|plamo:tag|>input<|plamo:key|>lang<|plamo:val|>{source_lang}<|plamo:msg|>{source_text}`
  - 訳文出力プレフィックス（日本語時）: `<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>ja<|plamo:key|>form<|plamo:val|>{plain|polite}<|plamo:msg|>`
- 補助情報タグ（任意、トークン予算超過時は以下の優先順で自動トリミング）:
  1. 直前翻訳履歴（`<|plamo:tag|>input...<|plamo:tag|>output...` のペア、最大 2 ターン）を古い順に削除
  2. 前後文脈（`<|plamo:tag|>context<|plamo:msg|>{context}`）を削除
  3. 用語集（`<|plamo:tag|>glossary<|plamo:index|>{src}<|plamo:item|>{tgt}...`、当該ブロックに出現する用語のみ動的抽出）
  4. ブロック自体の文分割（`{v1}` 等のプレースホルダ内部は絶対に分割しない）

### 5.2 停止トークンとサンプリング既定値
- `stop = ["<|plamo:tag|>", "<|plamo:bos|>"]`
- `temperature = 0.0`（リトライ 1 回目は `0.0` + 厳格文脈、リトライ 2 回目は `0.1`）

---

## 6. 翻訳出力の 10 項目検証 (`backend/translation/validation/validator.py`)

各ブロックの翻訳結果および手動修正テキストに対し、以下の 10 項目を機械検証する。

1. **空出力検査**: 空白のみの出力は `EMPTY_OR_WHITESPACE` (ERROR)。
2. **制御トークン残留検査**: `<|plamo:...|>` の混入を検出し除去・エラー検出 (`SPECIAL_TOKEN_RESIDUE`)。
3. **プレースホルダ欠落検査**: 原文の `{v1}`, `{v2}` 等が 1 つでも欠けていれば `PLACEHOLDER_MISSING` (ERROR)。**勝手に削除して成功扱いすることは厳禁。**
4. **プレースホルダ重複検査**: 原文より出現回数が増加していれば `PLACEHOLDER_DUPLICATED` (ERROR)。
5. **プレースホルダ未定義混入検査**: 原文に存在しない `{v99}` 等が生成されていれば `PLACEHOLDER_INVENTED` (ERROR)。
6. **プレースホルダ順序変更検査**: 日英語順差による順序入れ替わりは `PLACEHOLDER_ORDER_CHANGED` (WARNING) として記録しつつ保持。
7. **反復暴走（Repetition Loop）検査**: 同一フレーズの異常連続を `ABNORMAL_REPETITION` (ERROR) として検出。
8. **異常文字数比検査**: 原文に対し出力文字数比が極端な場合 `EXTREME_LENGTH_RATIO` (ERROR)。
9. **未翻訳・言語不一致検査**: 英文がそのまま残った場合 `UNTRANSLATED_SAME_AS_SOURCE` / `TARGET_LANGUAGE_MISMATCH` (ERROR)。
10. **用語集・数値・引用番号遵守検査**: 数値欠落 (`NUMERIC_MISMATCH`)、引用番号欠落 (`CITATION_MISMATCH`)、必須用語集未反映 (`GLOSSARY_TERM_NOT_APPLIED`) を検査。

---

## 7. フロントエンド 4 画面仕様（Phase 3）

### 7.1 画面1：ホーム・プリフライト・ジョブ設定 (`Screen1HomePreflight.tsx`)
- PDF ドラッグ＆ドロップおよびファイル選択アップロード、二重送信防止、失敗時再試行。
- ページ数、ファイルサイズ、SHA-256、抽出可否、スキャン/破損/暗号化判定をサーバー応答に基づき表示。
- ページ範囲（全ページまたは 1 始まりの `1, 3-5`）、文体（`plain` 初期値 / `polite`）、出力形式（単一 PDF / 見開き対訳 PDF / 非翻訳ラウンドトリップ）、推論モード（**実機 `plamo_llama_cpp` を初期値**、モックは明示選択時のみ）を設定。

### 7.2 画面2：レイアウト・保護領域エディタ (`Screen2ProtectionEditor.tsx`)
- ローカル `pdfjs-dist` ワーカーによる PDF Canvas 描画と SVG オーバーレイ。
- ズーム（`50%〜250%`）、ページ送り、ページ番号直接移動、回転角度（`0/90/180/270°`）表示。
- **色だけに頼らない視覚識別**: 線種（実線 / 破線 / 点線 / 二重線）＋ 記号バッジ（`Σ 数式`, `▣ 図・グラフ`, `▦ 表・罫線`, `🔒 非翻訳保護`, `↩ 原文維持`）＋ 由来タグ（`自動` / `手動`）の凡例と表示切替。
- マウスドラッグによる保護領域の追加（逆方向ドラッグ正規化、最小サイズ `< 4pt` 誤操作防止、ページ境界クランプ）、個別削除、手動一括クリア、サーバー保存（`protection_revision` 更新）。

### 7.3 画面3：翻訳進捗・対訳検査・手動修正・再組版 (`Screen3ProgressReview.tsx`)
- 13 ステージ進捗バー、成功/警告/失敗の区別、キャンセル要求中（`cancel_requested=true`）と安全停止済み（`state=canceled`）の明確な区別、中断ジョブ（`interrupted`）の再開ボタン。
- 原文 PDF / 翻訳後 PDF の並列プレビューとブロック一覧、検索・状態フィルタ。
- 要確認（`ValidationIssue` / `fallback_original`）一覧からの該当ページ・ブロックへのワンクリックジャンプとハイライト。
- **モデル出力 (`translation_text`) と手動修正 (`manual_translation_text`) の分離保持**:
  - 編集中の `{v1}` 等プレースホルダ整合性をリアルタイム検査し、欠落・未定義時は保存をブロック。
  - 未保存変更がある状態で別ブロックや別画面へ移動しようとした場合は確認ダイアログを表示。
  - 「モデル訳へ戻す」操作でいつでも `manual_translation_text = null` に復元可能。
- **LLM 推論なしの再組版 (`POST /api/jobs/{job_id}/retypeset`)**:
  - 手動修正または保護領域変更後に未反映バナーを表示し、LLM 推論 0 回で PDF・見開き PDF・QA レポート・視覚検査 PNG をアトミックに再生成。

### 7.4 画面4：用語集管理・環境診断 (`Screen4GlossaryDiagnostics.tsx`)
- 用語集の作成・更新・削除、1,000 件超（自動テストで 1,200 件検証）に対応する検索・ページネーション付きエントリテーブル。
- TSV / JSON インポート（取り込み前プレビュー、行番号付きエラー一覧、上書き/マージ選択）およびエクスポート。
- `/api/diagnostics?probe_llama=true` から動的取得した OS・CPU・RAM・GPU・VRAM・日本語フォント・`llama-server` 接続状態・セッション認証トークン更新 UI。

---

## 8. セキュリティ・プライバシー境界 (`backend/config.py`, `backend/api/app.py`)

- サーババインド先既定値: `127.0.0.1`（外部 LAN 公開しない）
- 翻訳推論エンドポイント: 既定で `127.0.0.1` / `localhost` / `::1` のみ許可。外部ホスト指定時は `SecurityPolicyViolationError` で即座にブロック。
- Origin 検証 + `X-PaperBridge-Token` セッション認証: 許可されたローカル Origin 以外からの状態変更リクエストを `403 Forbidden` で拒否し、トークン未付与リクエストを `401 Unauthorized`、URL クエリトークン指定を `400 Bad Request` で拒否。
- ファイル名・ID サニタイズ: パストラバーサル (`../`, `..\\`) や Windows 予約デバイス名 (`CON`, `NUL` 等) を無害化し、`runtime/` 配下からの逸脱を防止。
- PDF 内外部リンク・スクリプト無害化: フロントエンド PDF ビューアでは `isEvalSupported: false`, `disableAutoFetch: true` を設定し、PDF 内の外部リンク自動遷移を行わない。
