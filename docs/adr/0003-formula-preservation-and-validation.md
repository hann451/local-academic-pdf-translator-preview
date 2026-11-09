# ADR-0003: 数式保護と訳文検証・フォールバック戦略

- **ステータス**: 承認済み (Accepted)
- **日付**: 2026-11-06
- **対象フェーズ**: Phase 0 / Phase 1 / Phase 3

---

## 1. 背景と課題

学術論文PDFの翻訳において最も致命的な品質事故は、「数式・変数・ギリシャ文字・上下付き・数値・文献参照番号が翻訳過程で消滅・改変されること」および「それが無警告で成功扱いのPDFとして出力されること」である。
ADR-0001 で確認した通り、既存エンジンの標準処理ではプレースホルダーの欠落や不整合を黙って削除して処理を続行する挙動がある。これを防ぐための厳格な保護・検証・フォールバックアーキテクチャを定義する。

---

## 2. 決定事項（Decision）

### 2.1 2段階の数式・非翻訳要素保護
1. **ブロックレベルの保護（非翻訳要素）**:
   - 独立数式ブロック（`isolate_formula`、式番号 `(1)` 等を含む）、図（`figure`）、表罫線・表セル（初期設定で非翻訳）、参考文献リスト（初期非翻訳）、およびユーザーが画面上で手動指定した `do_not_translate` / `protect_as_formula` / `protect_as_figure` の矩形領域は、**翻訳リクエスト自体に送らず、元PDFのベクター/文字要素をそのまま保持する**。
2. **インライン要素の保護（プレースホルダー化）**:
   - 翻訳対象の本文・見出し・キャプション内に含まれるインライン数式・変数・上下付き・ギリシャ文字（$\alpha, \beta, \sum, \int, x_i^2$ 等）、数式フォント文字、URL/DOI、および文献参照番号（`[1]`, `[2, 3]`, `(1)` 等）を検出し、一意の保護トークン（例: `{v1}`, `{v2}`）に置換してから翻訳モデルへ渡す。
   - 各 `Block` の `protected_items` フィールドに、プレースホルダーID・元の文字列・元の座標/フォント属性を保存する。

### 2.2 訳文検証（`TranslationValidator`）の10項目検査
翻訳モデルから返却されたテキストに対し、組版へ渡す前に以下の検査をすべて実行する：

| # | 検査コード | 重大度 | 検査内容 |
| --- | --- | --- | --- |
| 1 | `EMPTY_OR_WHITESPACE` | Error | 空出力または空白のみ。 |
| 2 | `TRUNCATED_BY_LENGTH` | Error | `finish_reason == "length"` 等の生成上限打ち切り。 |
| 3 | `PLACEHOLDER_MISSING` | Error | 入力にあった保護記号（`{v1}` 等）の欠落。 |
| 4 | `PLACEHOLDER_DUPLICATED_OR_HALLUCINATED` | Error | 保護記号の重複出現、または存在しない `{v99}` 等の捏造・改変。 |
| 5 | `SPECIAL_TOKEN_RESIDUE` | Error | `<|plamo:tag|>`, `<|plamo:msg|>`, `<|plamo:bos|>` 等の制御トークン残存。 |
| 6 | `ABNORMAL_REPETITION` | Error | 同一フレーズ・文字の異常反復（ループ生成）。 |
| 7 | `REFERENCE_MARKER_MISMATCH` | Warning / Error | `[1]`, `[2]` 等の文献参照番号の欠落。 |
| 8 | `NUMERIC_VALUE_MISMATCH` | Warning | 原文中の重要な数値・単位の欠落または値の変化（桁区切り `1,000` vs `1000` の書式差は許容）。 |
| 9 | `UNTRANSLATED_SOURCE_RETURNED` | Warning | 翻訳すべき英文がそのまま返却された可能性（固有名詞・略語のみの場合を除く）。 |
| 10 | `ABNORMAL_LENGTH_OR_EXPLANATION` | Warning / Error | 極端に短い訳文、または「以下が翻訳です：」等のメタ説明文の混入。 |

### 2.3 失敗時の再試行と「原文保持フォールバック」原則
1. Error 判定となった場合、**プレースホルダーを勝手に削除して修復済みにすることを厳禁**とする。
2. 原因を分類し、**最大2回まで再試行**（文脈の簡略化や文単位への再分割）を行う。
3. 再試行しても Error が解消しない場合は、**当該ブロックを「原文＋元PDF要素のまま保持（Fallback to Original）」**とし、`Block.status = "fallback_original"`、`ValidationIssue` を記録する。
4. 1つでも `ValidationIssue` やフォールバックが発生したジョブは、最終ステータスを `completed` ではなく必ず **`completed_with_warnings`** とし、結果確認画面の「要確認一覧」にページ番号・矩形座標・理由を表示する。

---

## 3. Phase 2 実機PDF統合検証による設計改善（2026-11-06）

1. **インライン数式マスク正規表現のバランス改善**:
   - 実機 PLaMo 3 Translate 2B での検証により、下付き波括弧や勾配記号を含む式（例：`θ_{t+1} = θ_t - η∇L(θ_t)`）が複数の `{vN}` に細分化されたり、外側の閉じ括弧 `)` や文末ピリオド `.` を巻き込むと、訳文側で括弧の重複が生じることを確認した。
   - [`mask_inline_protected_elements`](../../backend/pdf/adapters/native_preserving_adapter.py#L94-L163) を改善し、波括弧・関数引数を含む数式全体を単一の `{vN}` として保護しつつ、末尾の句読点（`.,;:`）および不釣り合いな外側の閉じ括弧を自動的に切り離すよう修正した。
2. **回転ページ（`/Rotate 90`）＋ `CropBox` における原文消去座標ずれの解決**:
   - PyMuPDF の `page.draw_rect()` は `/Rotate 90` かつ `CropBox` オフセットを持つページにおいて描画座標が `CropBox` 分ずれる現象が実機視覚検査で判明した。
   - これに対し、`page.add_redact_annot(rect, fill=(1, 1, 1))` と `page.apply_redactions(images=PDF_REDACT_IMAGE_NONE, graphics=PDF_REDACT_LINE_ART_NONE)` を組み合わせることで、`get_text("dict")` と完全同一の座標系で翻訳対象の英文のみを消去しつつ、画像・ベクター図形・独立数式を 100% 無傷で保持できることを確認し採用した。
3. **視覚差分検証の3区分明記**:
   - [`PageVisualComparisonReport`](../../backend/pdf/validation/visual_diff.py#L33-L41) に `full_page_diff_ratio` を追加し、(a) 保護領域のピクセル差分率 (`0.0000`)、(b) 翻訳段落を除いた非翻訳背景の差分率 (`0.0000`)、(c) 日本語組版に伴うページ全体の差分率（例：`single_col_math.pdf` で `0.023864`）を明確に区別して記録する構造とした。
