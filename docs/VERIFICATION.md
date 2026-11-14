# Verification status

## インライン数式保持・フォント統一・安全フォールバックの検証記録 — 2026-11-14

> 注記：以下の過去履歴（`2026-11-12` およびそれ以前）は当時の記録のまま保持しています。本節はブランチ `fix/inline-math-preservation`（起点：`origin/main` `06b3fafbd684e5876a157e18dd11452f570897b9`）におけるローカル実装・CI環境フォールバック修正・再検証の範囲のみを記録します。

- **ロックファイル準拠の分離検証環境（再検証環境）:**
  - 作業ディレクトリ：リポジトリルート（`uv sync --locked --extra dev` で新規作成した専用 `.venv`）
  - Python / PyMuPDF：`CPython 3.13.13`（`<repo>\.venv\Scripts\python.exe`）、`PyMuPDF 1.28.2`
  - 使用したシステムフォント：`C:\Windows\Fonts\NotoSerifJP-VF.ttf`（`NotoSerifJP-ExtraLight`）、`C:\Windows\Fonts\NotoSansJP-VF.ttf`（`NotoSansJP-Thin`）、`C:\Windows\Fonts\cambria.ttc`（`Cambria`）、および `NotoSerifJP-VF.ttf` 不在時フォールバック再現用の `C:\Windows\Fonts\YuGothR.ttc`
  - ランタイムデータ：`PAPERBRIDGE_RUNTIME_DIR` により既存の既定データ領域から分離したディレクトリを使用
- **自動テスト結果（オフライン／モック・合成PDF中心の回帰テスト）:**
  - 実行コマンド：`& '.\.venv\Scripts\python.exe' -m pytest tests -q -rs`（`PYTHONPATH=""`、分離済み `PAPERBRIDGE_RUNTIME_DIR`）
  - 結果：**`98 passed, 7 skipped, 1 warning`**（新規追加・拡充した `tests/unit/test_inline_math_preservation.py` の **25件すべて PASS** を含む。あわせて `NotoSerifJP-VF.ttf` 不在・`YuGothR.ttc` フォールバック再現環境でも `test_inline_math_preservation.py` 25件＋`test_visual_preservation.py` 3件の計 **28 passed** を確認）
  - skip 7件の理由：`tests/integration/test_real_plamo_phase2.py`（L51, L70, L100, L183, L224, L309, L384）は `--run-real-llm` / `-m real_llm` 明示時のみ実行される既定スキップ対象テストであり、pytest 実行内では実モデル推論を行っていないため。
- **改善・検証した内容と残存制限の区別（別途実施した検証の基盤仕様）:**
  - **数学用文字のグリフ欠落・文字化け改善:** 計測・描画・埋め込み時のフォールバックフォント選択を統一し、`_select_char_font()` において SMP 数学用英数字記号（`U+1D400`〜`U+1D7FF`）に加え、数学演算子（`−` 等）・ギリシャ文字・矢印・修飾記号（`ˆ` 等）でも `F_MATH`（`Cambria` / `Segoe UI Symbol`）を優先するよう調整した（`NotoSerifJP-VF.ttf` 不在環境の `YuGothR.ttc` が `U+2212`/`U+FF0D` や `U+02C6`/`U+0302` で同一グリフIDを共有し `/ToUnicode` 抽出が `－`/`\u0302` へ化ける事象の回避）。また、`ToUnicode` CMap において MuPDF が補助平面文字（`U+10000`〜`U+10FFFF`、今回の数学用英数字記号 `U+1D400`〜`U+1D7FF` を含む）を UTF-16BE サロゲートペアへ変換せず 5〜6 桁の 16 進トークン（例：`𝑎` [`U+1D44E`] を `<1d44e>`、`𝑃` [`U+1D443`] を `<1d443>`）のまま出力するため再読込・コピー時に別文字へ化ける事象に対し、元PDFの既存 `ToUnicode` CMap（事前収集した xref）を除外して新規埋め込みの `F_JA` / `F_MATH` フォントに限定し、8桁サロゲートペア（例：`<D835DC4E>`）へ変換する `_fix_embedded_tounicode_smp()` を実装。`beginbfchar` / `beginbfrange` への適用、正常 8 桁サロゲートペア・4桁 BMP・8桁合字や `begincidchar` / `begincidrange` の非変更、べき等性、BMP+補助平面文字混在、および `mono` / `dual` 保存再読込後の抽出を単体テストで確認。
  - **検証した上付き・下付き・ハット・根号上線の保持:** 構造化 `InlineMathSpan` により、検証対象ブロックにおける上付き・下付き添字（例：`𝑎2 + 𝑏2`, `𝐿𝑐𝑙𝑠`, `𝐿𝑘𝑝`, `𝑃𝑗`, `𝑖𝑝`, `𝐸1`, `𝑅1`）、合成アクセント付き記号（例：`ˆ𝑖𝑝`, `ˆ𝑀`）、および根号上線ベクトル（例：`𝐴=√(𝑎2+𝑏2)`）の保持と、スパン間空白復元・NUL（`U+0000`）/ TeX演算子由来異体字セレクタ（`U+FE00` / `U+FE01`）混入防止・異常系8項目（プレースホルダ削除/複製/未知/改変、PUA衝突、同一変数の通常文字・添字混在、`MANUALLY_EDITED` 再組版、`FALLBACK_ORIGINAL` 周辺4方向の赤枠保護）を単体テストで確認。
  - **組版できず原文保持（`FALLBACK_ORIGINAL` / `PROTECTED_SKIP`）になった箇所:** 最小フォントサイズ `8.0pt` でも元の英語枠に収まらない狭小・高密度ブロックは `TYPESETTING_OVERFLOW_FALLBACK` で赤枠消去前に原文保持し、1つの段落が数式途中（上付き添字や縦分数）で2ブロックへ分割され深く重複している組は `TRANSLATED_BLOCK_OVERLAP_FALLBACK` で文字衝突を防いで原文保持し、表示数式内の孤立記号は `PROTECTED_SKIP` / `PROTECTED_REGION_OVERLAP_FALLBACK` として保護する動作を確認。
  - **残存制限:** 縦積み分数（`\frac{1}{10}` 等）を含む分割段落、1〜2行相当の極端に狭い枠に長い日本語訳が入る箇所、複雑な多段数式、およびブロックをまたぐ連結再配分には未対応であり、日本語組版ではなく安全な原文保持へフォールバックする場合がある（すべての数式やレイアウトを維持するものではない）。
- **別途実施した実論文PDF（13ページ構成の英語論文の Pages 3–7）での検証（上記 pytest 98件とは別実施）:**
  - **補助テスト（前回の固定訳文・短縮訳による描画確認）との区別:** 初期の組版確認スクリプトでは、新規解析したプレースホルダ構造に対する描画確認としてスクリプト内の固定訳文および Page 4 狭小枠向けの短縮訳を使用していたため、これは**実モデルによる新規翻訳E2Eではない補助的な描画確認**として区別する。
  - **保存済み訳文を変更しない再組版（`retypeset`）の確認結果:** 既存の保存済みDBスナップショットの訳文を1文字も編集せず再組版を実行し、主要15ブロック（対象14箇所＋分割対1箇所）を確認：
    - `translated`（警告なし、`Cambria` + `NotoSerifJP-ExtraLight`、NUL [`U+0000`] および `U+FE00` / `U+FE01` 残存 `0`）：`page-3:block-14`, `page-3:block-16`, `page-3:block-20`, `page-5:block-19`, `page-7:block-12`（保存済みDB上の `"∑"` 描画）, `page-7:block-16`, `page-7:block-23`（計7ブロック）
    - `fallback_original`（安全な原文保持＋警告）：`page-3:block-27`（`PROTECTED_REGION_OVERLAP_FALLBACK`）、`page-4:block-17`, `page-4:block-18`, `page-6:block-5`（`TYPESETTING_OVERFLOW_FALLBACK`）、`page-5:block-13`, `page-5:block-14`, `page-7:block-26`, `page-7:block-27`（`TRANSLATED_BLOCK_OVERLAP_FALLBACK`、うち `block-27` は保存時既存の `NUMERIC_VALUE_MISMATCH` を併記）（計8ブロック）
  - **実モデル（`plamo-3-translate-2b-Q4_K_M.gguf` + `llama-server.exe`）による新規翻訳の確認結果（選定23ブロック／推論21ブロック限定）:** Pages 3–7 の全抽出 `115` ブロックの中から、主要対象15ブロック＋視覚確認用の周辺文脈8ブロックの **計 `23` ブロック** を検証対象として選定した（全13ページ通しの新規翻訳E2Eではない）。内訳は、**保護によりLLMを未呼出 `2` ブロック**（`page-3:block-27`, `page-7:block-12`、抽出時に `protected_skip`、呼出回数 `0`）、**実際にモデル推論を実施 `21` ブロック**（主要対象内 `13` 件＋周辺文脈 `8` 件）、**再試行を含めた実際の呼出回数合計 `21` 回**（推論実施 `21` ブロックすべてが初回 `calls=1` でプレースホルダ検証を通過し再試行 `0` 回）であった。主要15ブロックの最終組版状態は以下の通りとなった（推論21ブロック全体では `translated` 14件 / `fallback_original` 7件）：
    - `translated`（警告なし、`Cambria` + `NotoSerifJP-ExtraLight`、NUL [`U+0000`] および `U+FE00` / `U+FE01` 残存 `0`、`mono`/`dual` とも正常抽出）：`page-3:block-14`, `page-3:block-16`, `page-3:block-20`, `page-5:block-19`, `page-7:block-16`, `page-7:block-23`（計6ブロック）
    - `protected_skip`（抽出時に独立数式内記号として自動保護、LLM未呼出）：`page-3:block-27`, `page-7:block-12`（計2ブロック）
    - `fallback_original`（正規の安全フォールバック＋警告）：`page-4:block-16`（旧 `block-17` 相当）, `page-4:block-17`（旧 `block-18` 相当）, `page-6:block-5`（`TYPESETTING_OVERFLOW_FALLBACK`）、および分割重複の `page-5:block-13`, `page-5:block-14`, `page-7:block-26`, `page-7:block-27`（`TRANSLATED_BLOCK_OVERLAP_FALLBACK`）（計7ブロック）
- **既定データ領域（本番環境側）の取り扱いに関する補足と訂正:**
  - 調査初期の読み取り専用URI接続（`?mode=ro`, `uri=True`）および分離スナップショット取得後の照合において、永続ファイル全 `2,119` 件（DB本体ファイルを含む）のサイズ・更新時刻・SHA-256 は一致していた一方、開始時点で `0` bytes だった空の `-wal` ファイルと `32,768` bytes の `-shm` 一時ファイルが作業後に消失していた。これが読み取り専用接続クローズ時の SQLite の解放挙動によるものか別プロセスによるものかは未確定であるため、**ディレクトリ全体の完全不変は主張しない**。その後は既定データ領域へ再接続せず、すべて分離ディレクトリ上で検証を実施した。
- **今回未再実施・範囲外の項目（過去の検証結果を流用しない）:**
  - 本13ページ論文の全ページ（13ページ全体）を通した実モデル新規翻訳E2E、別ブロックや他論文での網羅評価
  - `初回セットアップ.bat` によるクリーンな別Windows PC環境での再セットアップ、フロントエンド `npm test` / `npm run build` の再実行、および他CPU/GPU構成・GPU失敗時CPU自動フォールバックの再試験

### PR #3 レビュー指摘5項目の修正・追加検証（ローカル再検証 — 2026-11-14 追記）

> 注記：上記 `2026-11-14` 初版記録（`98 passed` / `28 passed` / 初期の実モデル推論検証記録）および下記の過去履歴は当時の記録のまま保持しています。本節は PR #3 の実差分レビュー指摘5項目に対する追加コミット（`05f08c45b50fd7eab95bd438111d4be827e76228` および `0ac3a5f1e7db24a0cce21275b5f35b9d9a2e455e`）時点のローカル修正・検証結果のみを記録します。

- **追加修正した5項目と主な内容:**
  1. **曖昧な添字の平文化防止（`AMBIGUOUS_INLINE_MATH_FALLBACK`）**: `extract_inline_math_spans()` において同一 `context_text` 内に上付き・下付き・通常表記等の競合構造や分割上の曖昧性がある場合に、対象スパンを除外して平文出力（例: `x^2` と `x_2` がどちらも `x2` になる問題）せず、`AMBIGUOUS_INLINE_MATH_FALLBACK` で元の英語ブロックを安全に原文保持＋警告記録するように修正（`test_item6_case4_repeated_identical_math_string_with_conflicting_roles` 更新および E2E 再現テスト `test_review_item1_conflicting_math_roles_e2e_fallback_and_warning` で検証）。
  2. **水平フィット判定と最小フォントサイズ候補・原子トークン保護の修正**: `fit_japanese_text_in_bbox()` で高さだけでなく `_measure_rendered_line_width()` による実描画幅・禁則ぶら下げ余白を含めて水平収まりを判定し、最小フォントサイズ（`8.0pt`）候補を含めた探索とスペース・Unicode 演算子を含む数式トークンの途中分割防止を修正。
  3. **複数文字の上付き・下付き配置とカーソル進行の修正**: `_build_styled_line_plan()` において複数文字の上付き・下付き（`P^{abc}_{i}`, `P^{*}_{index}`, `P^{ab}_{cd}` 等）の開始 x 座標整合とグループ幅に基づくカーソル進行を修正。
  4. **保護領域に対する処理順非依存化**: フェーズ1・フェーズ2の不動点収束処理を導入し、ページ内ブロックの並び順に依存せず同一の結果へ収束するように修正。
  5. **根号上線（overbar）白塗りによる保護対象消去の防止**: `orig_overbar_bbox` の白塗り領域から保護領域を差し引く `_subtract_protected_boxes` および重なりチェックを導入し、隣接する保護線画・図表の誤消去を防止。
- **最新ローカルテスト結果（コミット `0ac3a5f1`・分離ランタイム `PAPERBRIDGE_RUNTIME_DIR` 使用・本番データ非接続）:**
  - ローカル全 `pytest`: `103 passed, 7 skipped, 1 warning`（スキップ7件は実モデル推論・認証を要する `tests/integration/test_real_plamo_phase2.py` のオプトイン統合テスト）
  - CI 環境 `YuGothR.ttc` フォールバック模擬（`test_inline_math_preservation.py` + `test_visual_preservation.py`）: `33 passed`
- **実論文影響確認（対象: DensePose 論文の以前の実モデル出力21ブロック）:**
  - **実施性質の明記**: 本確認は**「以前の実モデル出力21ブロックを最新コード（`0ac3a5f1`）で再組版した影響確認」であり、最新コードによる新規実モデル推論や全13ページの新規翻訳E2Eではありません。**
  - **結果**: `translated` 14ブロック / `fallback_original` 7ブロック、曖昧添字フォールバック検出（`ambiguous_blocks`）0件、処理順依存差分（`order_independence_diffs`）0件、置換文字・NUL 文字混入（`has_replacement_or_null`）`False`。5ページ目 block-17 は `8.0pt` で bbox 内に収まり右方向はみ出しが解消され、3ページ目の上付き `2`（2箇所）も保持されました。
- **今回未実施・残存範囲:**
  - 最新コード上での新規実モデル推論（`page-7:block-27` のような実推論出力に対する数値検証の再評価を含む）および全13ページの新規翻訳E2E
  - `初回セットアップ.bat` によるクリーンな別 Windows PC 環境での再セットアップ

### 日本語直後の添字抑止・狭小bbox下限クランプ修正の検証（コミット `c71acf9` — 2026-11-14 追記）

> 注記：本節はコミット `1589f6f` を起点とするローカル追加修正（コミット `c71acf9`、ブランチ `fix/inline-math-preservation`）における限定修正 2 点および再組版検証の結果のみを記録します。本番データ領域へは接続せず、分離ランタイム上で実施しています。

- **今回修正した 2 点（スコープ限定修正）:**
  1. **日本語助詞等が直後に続く場合のインライン数式添字抑止の修正**: [`_build_styled_line_plan()`](backend/pdf/adapters/native_preserving_adapter.py) の末尾境界ガードにおいて Unicode 全般の `str.isalnum()` を用いていた箇所を [`_is_math_ident_continuation_char()`](backend/pdf/adapters/native_preserving_adapter.py) に置換し、ASCII 英数字・ラテン拡張・ギリシャ文字・文字様記号・上付き下付き・数学用英数字記号のみを数式識別子の継続文字として判定するよう限定しました（ひらがな・カタカナ・漢字等の直前では添字抑止を解除し、先頭側境界ガード・Unicode 非 ASCII 化・`MANUALLY_EDITED`・`AMBIGUOUS_INLINE_MATH_FALLBACK` の既存挙動は維持）。
  2. **狭小 bbox の幅・高さ下限クランプ撤去**: [`fit_japanese_text_in_bbox()`](backend/pdf/adapters/native_preserving_adapter.py) の `box_w = max(10.0, ...)` / `box_h = max(8.0, ...)` を撤去し、元ブロックの実際の幅・高さで収まり判定を行うよう修正しました（`raw_w <= 0.0` または `raw_h <= 0.0` の場合は `False` を返却、`min_font_size_pt = 8.0` は不変）。
- **最新ローカル自動テスト結果（コミット `c71acf9`・分離 `PAPERBRIDGE_RUNTIME_DIR` 使用）:**
  - ローカル全 `pytest`: **`105 passed, 7 skipped, 1 warning`**（回帰テスト `test_fix_a_japanese_following_subscript_and_superscript_not_suppressed` および `test_fix_b_narrow_bbox_subcaptions_fallback_without_overhang` を追加、修正前 `1589f6f` 上での FAIL 確認後に PASS を確認。スキップ 7 件は `--run-real-llm` 指定時のみ実行される `tests/integration/test_real_plamo_phase2.py`）。
  - CI 環境 `YuGothR.ttc` フォールバック模擬（`NotoSerifJP-VF.ttf` 不在模擬下での `test_inline_math_preservation.py` + `test_visual_preservation.py`）: **`35 passed`**。
- **保存済み 13 ページ訳文を使った再組版確認（新規推論ではないことを明記）:**
  - **実施性質の明記**: 本確認は 13 ページ全編の保存済み実モデル訳文（Stage 1 出力スナップショットの分離複製）を最新コード（`c71acf9`）の組版器で再組版（`retypeset_job` / `POST /api/jobs/{job_id}/retypeset`）した確認であり、**新規の LLM 推論は実施していません（`llm_calls_made = 0`、`last_retypeset_llm_calls_made = 0`）**。
  - **対象 5 箇所の下付き文字の再現（`mono` / `dual` 両PDF確認）**: `page-5:block-19` の `𝐿𝑡𝑟は`・`分類損失𝐿𝑐𝑙𝑠および`（`6.341pt`、ベースライン比 `+1.46pt`）および `page-7:block-16` の `𝑃𝑗は`・`𝑖𝑝および`・`ˆ𝑖𝑝は`（`6.825pt`、ベースライン比 `+1.57pt`、ハット `ˆ` は `-1.61pt` 上移動）について、修正前 `mono` では平文大のままだった 5 箇所すべてが修正後 `mono` / `dual` 双方で縮小下付き（+ハット）として描画されることを座標・レンダリング画像で確認しました。
  - **p.10 サブキャプションと全 239 ブロックへの影響**: 全 239 ブロックのうちステータス変化が生じたのは **p.10 の 2 ブロック（`page-10:block-2` `(a)` および `page-10:block-6` `(c)`）のみ**であり、いずれも `translated` から原文保持（`fallback_original`、`TYPESETTING_OVERFLOW_FALLBACK`）へ遷移しました（`translated: 117 -> 115`、`fallback_original: 25 -> 27`、`protected_skip: 97 -> 97`）。p.10 `(a)`–`(d)` 全 4 枠で元英語 `LinLibertineTB 6.974pt` が枠内に収まり、13 ページ全 `115` 翻訳ブロックのブロック内横超過 `0` 件・保護領域視覚保全全ページ通過を確認しました。
- **別問題として確認された再組版時の既存警告消失事象（A・B の改善とは区別）:**
  - `POST /api/jobs/{job_id}/retypeset` 経路において、`retypeset_job()` が処理冒頭で全ブロックの `warnings` を初期化し、`TRANSLATED` / `MANUALLY_EDITED` のみを再検証・再組版して `replace_validation_issues_for_job()` でジョブの issue テーブルを全置換するため、修正前の翻訳時に既に `fallback_original` へ遷移していた 25 ブロック（`NUMERIC_VALUE_MISMATCH` や `UNTRANSLATED_SOURCE_RETURNED` などの翻訳検証警告、および既存の `TRANSLATED_BLOCK_OVERLAP_FALLBACK` / `TYPESETTING_OVERFLOW_FALLBACK` を含む）が再評価対象から外れて `warnings` から消失し、再組版で新たに組版評価された 2 件の `TYPESETTING_OVERFLOW_FALLBACK` のみが残る現象（`validation_issues_count: 26 -> 2`、`warning_count: 26 -> 2`、ジョブ状態は `has_fallbacks=True` により `completed_with_warnings` 維持）を確認しました。本事象は未修正の未解決事項として別途切り分け管理します。
- **公開用の既知制限（本修正スコープ外）:**
  - **分割キャプションの日英混在**: 1 つのキャプションが複数ブロックへ分割抽出された場合（例：`page-3:block-9` と `block-10` の `(a) Layout of WiFi devices` / `and human bodies`）、片方のブロックのみが `TYPESETTING_OVERFLOW_FALLBACK` 等で原文保持になると訳文と原文の日英混在が残ります。
  - **`translate_tables=False` 時の表段落誤検出**: レイアウト解析で表領域が `table` ではなく `paragraph` として抽出された場合（今回の 13 ページ論文 239 ブロックでは `kind='table'` が `0` 件で、`page-8:block-1`, `block-5` 等が `paragraph` 扱い）、`translate_tables=False` 設定であっても段落翻訳の対象となり表セル・列配置の整列が失われる場合があります。

## Experimental Windows one-click setup & source-ZIP check — 2026-11-12

> Notice: Past historical verification records below (2026-11-08 and earlier) are preserved as originally recorded. This section covers only the experimental source-ZIP one-click setup additions on branch `feat/one-click-setup`. The archive/CLI tests described below were run on pre-push local verification commits prior to rebuilding those unpushed commits with GitHub noreply commit metadata (`285997610+hann451@users.noreply.github.com`), and the runtime tests were not re-run solely for the rebuilt commit SHAs; subsequent `docs/VERIFICATION.md` SHA-alignment edits sit cleanly on top without altering functional code.

- **Pre-push noreply metadata rebuild correspondence (identical Git tree SHA, zero file/BOM/CRLF diffs):**
  - `2ee27f9d63d48ce65ea5fa7e30a36ee7c64cb79f` (pre-push local feature commit) → `3c283b069c34e8e136ff6a1036fc12fdbe046583` (`tree 04944aad0d0d0d093d3fcf60c0ba692392ea4ee0`)
  - `af572ac7f29f6c350e1b4e9b8219ad7767a23600` (pre-push CLI E2E baseline) → `04575ee35cc1e60624a7206b9ce0f2bf63db1cc8` (`tree 8f46625616955db50e65f59cef8379fc0a31395c`)
  - `667b65e22d9b60fff36a7b73a507e5af7669ed93` (pre-push README update) → `f1434fec5f3cb451e9bae14b19f6dc80a9f5b364` (`tree 87f4d07c582f6d5ebd11aab6063271f991005bb2`)
  - `8aeeb3f72ef8549398ffed7df849d8beb39688dd` (pre-push initial `VERIFICATION.md` commit) → `f3b6d86cdf808b2984e07f912ff1f632196614b1` (`tree b462cb604f6dd2655d0a952fc258adecf0cca041`)
- **Archive packaging, encoding, and syntax checks (performed on pre-push commits `af572ac`, `667b65e`, and `8aeeb3f`):**
  - Exported clean ZIP archives with `git archive --format=zip --prefix=local-academic-pdf-translator-preview/` (`556,637` bytes for `af572ac`, `557,260` bytes for `667b65e`, `559,448` bytes for `8aeeb3f`) and verified extraction into paths containing Japanese characters and spaces using both Windows `Shell.Application` (`CopyHere`) and `.NET ZipFile::ExtractToDirectory` (for `af572ac`, and `.NET ZipFile` for `667b65e` and `8aeeb3f`).
  - Confirmed all five root `.bat` wrappers (`NonASCII=0`, `BareLF=0`, CRLF), `scripts/run_silent.vbs` (`NonASCII=0`, `BareLF=0`, CRLF), and all four `.ps1` scripts (`UTF-8 BOM=True`, `BareLF=0`, `ParseErrors=0` under Windows PowerShell `5.1` `[System.Management.Automation.Language.Parser]::ParseFile`).
- **Same-PC `PATH`-restricted CLI checks on `af572ac` `git archive` extraction (2026-11-11/12; not re-executed after noreply rebuild to `04575ee`):**
  - With `PATH` restricted to the four default Windows system folders (`System32`, Windows root, `Wbem`, `WindowsPowerShell\v1.0`) and isolated `UserDataDir` inside a Japanese+space path, unattended `初回セットアップ.bat -AcceptAgreements -ModelMode Local -PreselectedModelPath <existing GGUF> -UserDataDir <isolated> -ShortcutName "PaperBridge FinalCheck" -SkipShortcut` exited `0`: fetched SHA-256-verified `uv 0.11.15`, installed isolated CPython `3.13.13` (`--no-bin --no-registry`), synced `.venv` (`24` packages), extracted `Node.js v22.20.0` with `tar.exe` into `.runtime_tools\node22` (resolving a `MAX_PATH` 260 extraction error observed in an earlier run), built `frontend/dist/index.html` (`1.62s`), installed `llama.cpp b12167` Vulkan GPU + CPU binaries, validated/copied the local GGUF model, and wrote `.runtime_tools\runtime_profile.json`.
  - Unattended `起動する.bat` (`-NoBrowser`, isolated ports `8084`/`8004`, reading `runtime_profile.json` without `-UserDataDir`) exited `0`: launched Vulkan `llama-server` (`n_gpu_layers=99`) and FastAPI backend, returned `llama_connection_status=connected` and `japanese_font_available=True` (`NotoSerifJP-VF.ttf`) on `/api/diagnostics?probe_llama=true`, completed non-mock real translation of a synthetic 1-page English PDF via `/api/documents/upload` and `/api/documents/{id}/jobs` (`state=completed`, `is_mock_run=False`) generating `['mono_ja_pdf', 'validation_report_json', 'dual_bilingual_pdf']`, verified `scripts/check_active_jobs.py` exited `0`, and verified clean `CTRL_BREAK_EVENT` shutdown with ports `8004`/`8084` released (`PORTS_AFTER_STOP=0`).
  - Unattended `デスクトップにショートカットを作成.bat` (default and `-CpuMode` targeting an isolated temp directory) exited `0`, creating both `.lnk` shortcuts. `CPUモードで起動.bat` (`-NoBrowser`, isolated ports `8084`/`8004`) exited `0`, started `CPU専用 (b12167-cpu-x64, -ngl 0)`, and stopped cleanly via `CTRL_BREAK_EVENT`.
  - Existing production runtime directory (`%LOCALAPPDATA%\PaperBridgeLocal`) and desktop shortcut timestamps remained unchanged after the tests, and temporary extraction/data directories were removed.
  - Earlier workspace checks prior to `af572ac` (not individually re-run on `af572ac` ZIP) also observed idempotent setup re-run (`6.59s`), corrupted `.downloading` file recovery (`RECOVERY_TEST=PASSED`), and `check_active_jobs.py` exit codes (`0`, `1`, `2`, `3`).
- **Environment-variable pinning fix & isolation check on `scripts/setup_wizard.ps1` (2026-11-12, pre-PR #2 merge):**
  - Pinned `UV_PROJECT_ENVIRONMENT` to `$ProjectRoot\.venv` prior to `uv python install` / `uv sync --locked` and `PAPERBRIDGE_RUNTIME_DIR` to the selected `$UserDataDir` prior to `backend import check` (`import backend.api.app`), adding both variables to `$savedEnv` so `finally` restores caller process state without modifying `HKCU:\Environment`.
  - Confirmed `scripts/setup_wizard.ps1` (`50,333` bytes) retains `UTF-8 BOM=True`, `BareLF=0`, and PowerShell `5.1` `ParseErrors=0`.
  - On an archive extracted to an isolated Japanese+space path with `$env:UV_PROJECT_ENVIRONMENT` preset to an external dummy venv and `$env:PAPERBRIDGE_RUNTIME_DIR` first unset (Run 1) then preset to a decoy directory (Run 2), unattended `-AcceptAgreements -ModelMode Skip -UserDataDir <isolated> -SkipShortcut` exited `0` in both runs: created/synced all `24` packages into in-app `.venv` (leaving external venv files/stub untouched), initialized `db\paperbridge.sqlite3` inside each isolated `UserDataDir` (`BACKEND_IMPORT_OK`), left `%LOCALAPPDATA%\PaperBridgeLocal` (`3,215` items, `PROD_DIR_DIFF_COUNT=0` in both runs) and the decoy directory (`0` diffs) untouched, restored caller process environment variables after each run, and left `HKCU:\Environment` unchanged (`HKCU_ENV_UNCHANGED=True`). (`npm`/`build`, `.venv` creation, and `llama.cpp` steps completed; full translation E2E was not re-run in this check.)
- **Author manual test note:**
  - The repository author additionally reported normal operation during manual testing on their environment. Because individual GUI dialogs/screens, live full-model download, and separate machines were not enumerated via automated step-by-step records, this does not certify all GUI items or external environments.
- **Unverified / remaining out-of-scope items:**
  - Comprehensive interactive mouse/GUI testing across all wizard dialogs, Edge `--app=` window rendering/closing, and interactive unfinished-job close confirmation prompts
  - Full ~1.81 GB `plamo-3-translate-2b-Q4_K_M.gguf` live network download completion from Hugging Face (CLI E2E used SHA-256-verified copy of an existing local GGUF)
  - Automatic CPU fallback triggered by an actual Vulkan server startup failure on hardware lacking Vulkan support (explicit CPU launch via `CPUモードで起動.bat` was tested on the GPU-equipped host)
  - Clean Windows 10/11 PCs with no developer tools installed, SmartScreen prompts, corporate TLS inspection/proxies, or other GPU/CPU configurations

## Current handoff baseline — 2026-11-08

- The verification starting point is commit
  `6a55d6e31637f59f7b74b26d2d00efbc38977151` on
  `work/formula-protection-continuation`. The documentation edits for this
  handoff are made after that baseline and are not contained in that commit.
- The latest recorded source-branch test results are from 2026-11-07: Python
  **69 passed, 7 opt-in real-LLM/GPU tests skipped**, and frontend **15 passed
  across 3 files**, with the TypeScript/Vite production build passing. These
  are existing results, not tests rerun on 2026-11-08.
- The same-PC staged snapshot check dated 2026-11-07 is a separate, earlier
  environment result: offline dependency setup, frontend 15 tests/build, Python
  **45 passed, 7 skipped**, and model-free UI HTTP 200. It did not use a model
  or certify a clean Windows installation.
- A same-PC isolated installation and model-free startup check from the saved
  commit completed on 2026-11-08. Python **69 passed, 7 opt-in real-LLM/GPU
  skipped**, frontend **15 passed**, build passed, and UI/API/asset smoke checks
  returned HTTP 200. This is not a clean Windows/other-PC test, real-model
  translation, or translation-quality certification.

## Same-PC isolated verification — 2026-11-08

- **Source and environment:** Cloned the Private work branch into a new
  `%LOCALAPPDATA%\PaperBridge-Isolated\<commit>\source` directory, detached at
  `6a55d6e31637f59f7b74b26d2d00efbc38977151`. Created a new Python environment
  and dedicated uv/npm/Python/Hugging Face/XDG/Torch caches, temporary paths,
  pytest data directory, logs, runtime, and SQLite DB under the same isolated
  root. The source workspace's `.venv`, `node_modules`, `dist`, and user DB
  were not reused. The isolated interpreter reported its `sys.prefix` as the
  dedicated `venv` and `include-system-site-packages = false`; its base
  interpreter was the already-installed uv-managed CPython, not the old
  virtual environment.
- **Tool versions:** Windows host with CPython **3.13.13**, uv **0.11.15**,
  Node.js **24.18.0**, and npm **11.16.0**. Node.js/npm minimum versions are not
  declared by the project; these are the versions used for this check, not a
  support-floor declaration.
- **Dependency setup:** `uv sync --locked --extra dev --python 3.13
  --no-python-downloads` succeeded and installed 31 packages into the new
  environment. `npm ci --no-audit --no-fund` succeeded and added 189 packages
  to the new clone. npm reported three unapproved `esbuild` install scripts
  and a deprecated `whatwg-encoding`; no script approval or global install was
  performed. The frontend test/build still completed successfully.
- **Frontend:** `npm test` passed **15 tests across 3 files**. `npm run build`
  (`tsc -b && vite build`) passed and generated the UI assets and PDF.js worker.
  Vite emitted its existing large-chunk advisory; jsdom tests printed
  environment warnings without failing.
- **Python:** The ordinary suite completed with **69 passed, 7 skipped, 1
  Starlette/httpx deprecation warning**. The seven skips were opt-in real
  LLM/GPU cases; they were not enabled. Synthetic tests used their normal
  generated fixtures under the isolated pytest temporary directory.
- **Model-free UI/API:** Started the isolated FastAPI process at
  `127.0.0.1:18081`; home page, session endpoint, JavaScript/CSS assets, and the
  PDF.js worker each returned HTTP 200. The browser displayed the home screen.
  This check verified HTTP `GET` reachability and initial home-screen display on
  port `18081`, not full interactive browser operations (`POST`/`PUT`/`DELETE`
  requests from a browser on port `18081` would be rejected with HTTP `403` by
  the default `allowed_origins` list unless port `8080` or `5173` is used).
  The session response body/token was not printed or saved. The dedicated
  runtime SQLite DB was created; no user PDF was uploaded, no translation job
  was submitted, and no model was installed or contacted. Port 18082 had no
  listener.
- **Stop and scope:** Verified the isolated listener's loopback address, owner
  PID, interpreter, and Uvicorn command, then stopped only that process. Ports
  18081 and 18082 were confirmed released. Existing 8080/8000 services and
  user runtime were not changed. Python/npm caches, clone, dependencies,
  frontend build, test temporary data, logs, and isolated runtime remain under
  the dedicated directory for inspection.
- **Limits:** This is a same-PC isolated-folder check, not a clean Windows
  environment or another-PC test. It does not certify other GPUs, minimum
  hardware requirements, real-model translation, or translation quality.
  Dependency downloads used network access; runtime network traffic was not
  comprehensively monitored. This result pertains to the saved source commit;
  documentation edits in the original workspace are outside that commit.

## Isolated synthetic real-model trial — 2026-11-08

- **Baseline and isolation:** The trial used the isolated clone at
  `6a55d6e31637f59f7b74b26d2d00efbc38977151`. Before creating
  `%LOCALAPPDATA%\PaperBridge-Isolated\<commit>\real-model-test`, its
  nonexistence was checked. The dedicated area contains its own runtime/SQLite
  data, synthetic inputs, outputs, logs, and evidence. No user PDF, existing
  database, or existing job was used. No repository source, dependency, or
  lockfile was changed for the trial.
- **Model and runtime:** Reused the existing PLaMo 3 Translate 2B GGUF
  Q4_K_M model read-only (SHA-256
  `F36D0813DD95573A852AB5D76E374C543632D9E292929E922BCDBF7E53B494E0`) and
  llama.cpp `b12167`, commit
  `240d68fcfe0c2a1f298c8e18ca181368b1914259` (llama-server SHA-256
  `5B5EC3B6CFEC735720D1F1B929C40207C245BD5052B275AFD0EAADE214C34D36`).
  Both hashes matched again after the trial. The trial server ran on
  `127.0.0.1:18082` with `-ngl 0 -t 4 -tb 4`, context 4096, and one slot.
  Its process command, loopback listener, model endpoint response, four-
  thread startup log, and seven inference-task timing records identify the
  dedicated CPU-only server. The API ran on `127.0.0.1:18081`, with its
  runtime and model endpoint set only for that process. The existing
  `127.0.0.1:8000` server, PID 38816, was not connected to or changed.
- **Input and pretranslation gate:** Generated a two-page synthetic PDF only
  inside the trial area. It contains ordinary English prose, a stacked
  English-labeled fraction with its horizontal bar and equation number (7),
  and two-column prose continuing across columns. Pretranslation parsing
  produced 9 blocks, identified one formula block using
  `RULE_SPATIAL_FRACTION_EQUATION`, and found two high-confidence
  same-page cross-column context candidates. Both body fragments remained
  separate blocks. The input SHA-256 was
  `2D927ADD26BD590E27313DD883496E48EC9A6BBE39C904304F37F1FB4061857A`.
- **Job result:** The inference server's full active interval, from start to
  stop after inspection, was **207.6 seconds**; the non-mock translation job
  itself completed in **8.13 seconds**. It used
  **7 real inference calls** for 7 blocks: 5 body blocks and 2 headings. The
  formula and one running header were protected; there were **0
  fallback-original blocks**, **0 retries**, **0 job warnings**, and **0
  validation issues**. Progress was **9/9**, never above the total. The
  translated body blocks contain Japanese text and have
  `translation_provenance=llm_inference`; original-text fallbacks were not
  counted as translations.
- **Output and preservation:** The finalized mono-Japanese PDF is 2 pages
  (8,144,206 bytes) and opened/rendered for visual inspection. No obvious text
  overlap or missing page was observed. The fraction label, numerator,
  denominator, bar, and `(7)` remain visible; the validation report and a
  separate 3x render comparison both found a **0.0** differing-pixel ratio in
  the formula region. The original PDF SHA-256 was unchanged and
  `original_hash_verified` was true. The two continuation fragments received
  individual translations; this test does not demonstrate improved
  translation quality or joined-sentence translation.
- **Warnings and provenance limitation:** The inference-server startup logged
  a no-API-key/all-CORS warning (while bound only to loopback) and GGUF token
  metadata warnings concerning `</s>` and EOS/EOT/EOM IDs. The API log had no
  warning/error, and the job recorded no warning. However, the application
  validation report incorrectly records PID 38816 and `Vulkan GPU` as the
  engine/backend. That is the separate existing server at port 8000, not this
  trial's port-18082 process. The source process/backend probe is not
  endpoint-specific when more than one llama server is present. For this
  trial, use the dedicated process command, listener, `/v1/models` response,
  and inference-server log as the compute evidence; do not rely on that
  historical report's `engine_process` or `compute_backend` fields.
- **Follow-up endpoint-provenance fix (2026-11-08):** Updated
  `backend/diagnostics.py` and `backend/jobs/manager.py` after commit
  `b18da47b130c76472d1b9e99292118a545b59ce7` to match the configured loopback
  endpoint address and port against local TCP listeners, deduplicate same-PID
  dual-stack/wildcard entries, return `unverified_from_api` when zero or
  multiple distinct PIDs match or when endpoints are remote, keep PC GPU
  hardware metadata separate, and report actual compute backend as
  `unverified_from_api` alongside any verified startup flags (`n_gpu_layers`)
  and `ggml-vulkan.dll` loaded confirmation (recorded only when a valid CSV row
  for the target PID confirms `ggml-vulkan.dll`; informational messages, `N/A`,
  empty output, or missing target PID rows remain `unverified`) without claiming
  CPU or GPU execution from missing DLLs or startup arguments alone. Verified
  deterministically with offline mock regressions: focused suite **7 passed**,
  ordinary Python suite **73 passed, 7 skipped, 1 warning**. The real-model
  trial was not rerun.
- **Resources, stop, and scope:** Free RAM before start was 11.82 GiB; the
  8-GiB threshold was only this trial's start gate, not a product minimum.
  The model server became ready in about 5.1 seconds. Only the recorded API
  listener/launcher PIDs 4564/11928 and inference-server PID 39696 were
  stopped after command, start time, and port-owner checks. Ports 18081/18082
  were released; the existing port-8000 owner remained PID 38816. The
  read-only model and executable hashes remained unchanged. This is a
  same-PC synthetic CPU-only example, not a clean-Windows/other-PC check,
  minimum-hardware declaration, broad CPU compatibility result, or
  translation-quality certification. No additional download or external
  translation service was used.

## This staged snapshot

This directory was assembled from an explicit file allowlist and checked statically. The staged checks below verified offline dependency setup, frontend tests/build, the ordinary Python suite, and a model-free home-screen browser render on this PC. Real-model/GPU translation, a separate Windows environment, and legal compatibility were not verified.

Earlier staged-copy and CI outcomes below are historical records and distinct
from the later source-branch results above. Detailed internal logs and audit
originals are intentionally not included here.

## Environment scope

The only recorded real-hardware example is Windows 11 x64 with an AMD Radeon RX 7600, Vulkan llama.cpp, and PLaMo 3 Translate 2B GGUF Q4_K_M. It does not establish minimum requirements or compatibility with other hardware or operating systems. A separate clean Windows environment has not been verified by this staging operation.

## Staged validation — 2026-11-07

- **Python dependency sync:** `uv sync --locked --extra dev --offline --python <existing CPython 3.13 path> --no-python-downloads` succeeded using an already installed CPython 3.13.2. It created this stage's `.venv`; it did not reuse the original project's virtual environment or download Python. The locked environment installed 31 packages.
- **Frontend dependencies:** `npm ci --offline --no-audit --no-fund` succeeded from the local npm cache, adding 189 packages. npm warned that install scripts for esbuild packages were not approved; the subsequent TypeScript/Vite build nevertheless succeeded. No online retry or lockfile update was performed.
- **Frontend tests and build:** The package script `npm test` (`vitest run`) passed **15 tests across 3 files**. `npm run build` passed (`tsc -b && vite build`) and generated `frontend/dist/index.html`, the JavaScript/CSS bundles, and the local PDF.js worker. Vite emitted a bundle-size advisory for JavaScript chunks above 500 kB. Test output included jsdom environment warnings; they did not fail the suite.
- **Python tests:** After the frontend build, `uv run --offline --locked --no-python-downloads --python <existing CPython 3.13 path> pytest` completed with **45 passed, 7 skipped** and exit code 0. The skips were the opt-in real-LLM/GPU tests; they were not enabled. One Starlette/httpx deprecation warning was reported. This rerun resolved the earlier `test_static_frontend_dist_serving_and_local_pdfjs_worker` failure caused by running before the frontend build.
- **Model-free UI:** The stage's FastAPI served `http://127.0.0.1:18081/` with HTTP 200. The existing browser displayed the PaperBridge Local home screen. The HTML, JavaScript, CSS, and local PDF.js worker each returned HTTP 200. This verified HTTP `GET` reachability and initial home-screen rendering on port `18081`, not full interactive browser operations (which require an origin in `allowed_origins`, by default port `8080` or `5173`). No PDF was registered, no translation or model connection was attempted, and this is not a real-model or translation-quality test.
- **Generated state and cleanup:** The stage retains `.venv`, `frontend/node_modules`, `frontend/dist`, and `.pytest_cache`; the stage `.gitignore` lists these and generated fixture PDFs as exclusions. No generated test PDF was left in the source tree. The server listener was confirmed to have exited and port 18081 was released. The unique TEMP runtime created for this check contained one 81,920-byte SQLite file and was removed after confirming it had no reparse points. Environment variables were restored.
- **Integrity and scope:** All staged application source, configuration, lockfiles, and `LICENSE` retain their creation hashes. The authorized CI preparation changes `.github/workflows/ci.yml`; this report also changed to record the results. No application source or lockfile changed. Python and npm dependency operations were offline. No Git history or remote content was modified. The source workspace and its virtual environment were not modified or started.
- **CI workflow preparation:** A separate authorized, stage-only change adds Node.js 22.20.0 setup, `npm ci`, `npm test`, and `npm run build` before Python tests, because the Python API suite includes a test that requires `frontend/dist`. Step order was checked statically; the GitHub Actions workflow itself has not run or been parsed by an installed YAML parser.

## Remaining validation after the staged/model-free checks

The earlier staged/model-free results are limited to this PC and do not establish another Windows machine's compatibility, minimum hardware requirements, or support for other GPUs and operating systems. The automated opt-in real-model/GPU tests remain skipped. The later synthetic trial above demonstrates one local CPU-only run, not general model compatibility, translation quality, or GPU behavior.

## Initial GitHub Actions run — 2026-11-07

The first Windows Actions run in the development repository for commit `0b845b05fb9e15aea6fb01eebfb7525b4f5e09f3` failed in **Generate synthetic test PDFs**. The frontend tests and build, uv setup, Python setup, and dependency installation completed successfully. The synthetic PDF generator created fixtures, then its final status output raised `UnicodeEncodeError` because the Windows runner's default stdout encoding could not encode Japanese characters. The Python pytest step was skipped, so that run provides no Python CI test result (development repository Actions run ID `42252410244`).

The workflow now sets `PYTHONIOENCODING: utf-8` only for the synthetic-PDF generation step. The stage's existing Python environment successfully reran the synthetic-only generator with this encoding; it produced the six ignored fixtures under `tests/fixtures/generated/`. This is a local generator check, not a post-change GitHub Actions run. The updated workflow's CI and Python test results are still pending. Do not treat the initial frontend success as a successful CI run or as evidence that Python CI tests passed.

## Follow-up CI result and GPU-independent security test — 2026-11-07

The next CI run in the development repository for commit `919baf125e5352a30e892310ba0ffd882bedf5b4` completed frontend tests (15 passed), frontend build, dependency setup, and synthetic-PDF generation successfully. Python pytest then reported **44 passed, 7 skipped, 1 failed**. The failure was `test_security_blocks_external_translation_host_and_untrusted_origin`: the Windows hosted runner reported `Microsoft Hyper-V Video`, while the test hard-coded `AMD Radeon RX 7600`. This is a test-environment GPU-name mismatch, not a failed external-host/origin/token check. The seven opt-in real-model/GPU tests remained skipped (development repository Actions run ID `42253862727`).

The security test now continues to verify external translation host rejection, disallowed Origin `403`, invalid token `401`, valid loopback request `200`, and `loopback_only_enforced is True`, without requiring a particular installed GPU. The diagnostic model represents `gpu_name` as a string, but the security test makes no assertion about its hardware-specific value.

- **Focused regression test:** `uv run --offline --locked --no-python-downloads --python .venv\Scripts\python.exe pytest tests/integration/test_integration_pipeline.py::test_security_blocks_external_translation_host_and_untrusted_origin` passed (**1 passed**).
- **Ordinary Python suite:** `uv run --offline --locked --no-python-downloads --python .venv\Scripts\python.exe pytest` passed (**45 passed, 7 skipped**; exit code 0). The seven opt-in real-LLM/GPU tests were not enabled. One Starlette/httpx deprecation warning was reported.
- **Scope:** Both commands used the stage's existing Python 3.13.2 virtual environment, offline mode, and the existing frontend build. No model or real paper was used. At the time of that local check, the GitHub Actions run for the GPU-independent test change had not run yet.
- **Subsequent development-repository CI runs (checked 2026-11-08 and 2026-11-09):**
  - **Historical `main` success (`20dd587`):** Commit `20dd5870280310ff8e4b19687323be7be0552f3a` ran development repository GitHub Actions Run `42254838637` on 2026-11-07 and **succeeded across all workflow steps** (Node.js 22.20.0 setup, `npm ci`, `npm test`, `npm run build`, uv and Python 3.12 setup, `uv sync --extra dev`, `scripts/generate_test_pdfs.py`, and `uv run pytest`).
  - **Subsequent PR and `main` CI verification:** Following integration of the formula protection, continuation context, endpoint-provenance diagnostics, and preview UI/documentation updates in the development repository, both PR CI runs (`42399415858`, `42614496539`) and `main` push CI runs (`42399919422`, `42616156593` on commit `d1e4ac974f5ff7d7497e97edbbcd7a44604e9a11`) succeeded across all steps (frontend **16 passed across 3 files**, `npm run build` passed, Python **73 passed, 7 skipped**). Note that this public preview repository (`local-academic-pdf-translator-preview`) is initialized as a clean source snapshot from `d1e4ac974f5ff7d7497e97edbbcd7a44604e9a11` without carrying over the private development `.git` commit history.

## Formula protection and continuation context — 2026-11-07

- **Implementation:** Added conservative same-page left-to-right and adjacent-page continuation candidates. Only high-confidence body pairs provide bounded, masked neighboring context to PFML3. High confidence now requires an open grammatical construction plus a lower-case continuation or recognized acronym/variable start, compatible font size, and no detected paragraph indentation. Synthetic negative cases cover unpunctuated independent paragraphs, list markers, small-font body-misclassified footnotes, indented starts, and full-width figures/equations. Positive cases cover CSI, RSSI, and variable starts. These remain heuristics, not semantic paragraph recovery. Blocks keep their original IDs, page indexes, bounding boxes, translation/cache/edit units, and individual rendering. No concatenated Japanese output is split across boxes.
- **Formula handling at that point:** The added `Eq. (n)` / `Equation (n)` regular expression protects the equation-reference label only; it does not repair or preserve an equation body. Synthetic checks cover inline math masking, per-block placeholder identity, exact reference-label restoration, and RSSI/CSI non-masking. The later read-only diagnosis and spatial-fraction correction are recorded in the next section; do not treat this earlier note as the latest finding.
- **Progress and warnings:** Resume progress is recomputed from completed physical-block states; fallback-original blocks are not reinferred, and identical validation issues are deduplicated. A legacy JSON block payload without continuation metadata defaults to an empty candidate list. No schema migration was added.
- **Accounting tests:** `test_connection_drop_interrupt_and_resume_without_retranslating_completed_blocks` checks progress against completed physical-block states before and after interruption/resume. `test_targeted_block_retranslation_excludes_target_from_in_progress_count` verifies a selected block is excluded while pending and counted after completion. `test_placeholder_failure_retries_and_falls_back_to_original_without_silent_deletion` now resumes the warning-complete job and confirms fallback blocks are not reinferred and warnings remain consistent. `test_identical_resume_warnings_are_deduplicated_but_distinct_findings_remain` checks duplicate warning identity handling.
- **Python tests:** The focused suite passed (**57 passed**). The final full command, `uv run --offline --locked --python .venv\Scripts\python.exe pytest`, passed (**68 passed, 7 skipped**, exit code 0). The seven skipped tests are opt-in real-LLM/GPU tests. One Starlette/httpx deprecation warning was reported.
- **Frontend:** `npm test` passed **15 tests across 3 files**; `npm run build` passed, including `tsc -b`. No frontend source changed after this build.
- **Scope and limits:** Tests used synthetic PDFs and a mock translator; no real model or user PDF was used as a test input. The reported PDF was separately inspected read-only, not modified or copied into the repository. No translation job, source workspace, or user runtime was used or changed. The parser has no dedicated general-footnote category, and there is no UI for reviewing or rejecting candidate metadata. Cross-box joined translation and safe Japanese text redistribution remain unimplemented.
- **Running API process:** The 8080 API process predates the source changes. Its process arguments show no reload option, and no reload watcher was detected; its parent/child process relation was consistent with a normal non-reload launch. The service was not restarted, and it cannot be claimed to have loaded the changed code.
- **Workspace:** Changes remain uncommitted on `work/formula-protection-continuation`. No GitHub operation was performed. `git diff --check` passed; application source, tests, and generated fixtures remain stage-local.

## Synthetic English fraction equation — 2026-11-07

- **Reported issue scope:** The provided five-page dual PDF has the equation on physical PDF page 4 (zero-based index 3), in the original panel's left column—not physical page 5. Its text bounds were `[61.9, 499.2, 284.6, 522.7]` pt; the fraction bar was from x=175.7 to 260.9 pt at y=510.9 pt. A printed paper page number was not detectable in extracted page-edge text.
- **Read-only diagnosis:** Before the change, a deterministic parser run produced `readonly-formula-check:page-4:block-11` and `...block-12`, both ordinary pending paragraphs. The first inline-masked only `Ratio = Detected`; the denominator and `(7)` had no protected items. The reference-label regex did not match because `(7)` was attached to body text. This reproduces a source-side classification/protection gap. The historical job log was not found, so the exact model output and contribution of rendering remain unverified; the visual appearance alone does not establish that the model output was at fault.
- **Stage-only change:** Added span-level grouping for a horizontal fraction bar, English text above and below it, a left-side expression ending in `=`, and an equation-number marker. It groups the target's mixed-baseline text spans into a protected formula block whose bounds include the line and all equation text. A read-only parse of the provided PDF then classified the equation as one `RULE_SPATIAL_FRACTION_EQUATION` protected block. A structurally matching synthetic PDF and visual integration test check zero differing pixels in the equation area and verify that two adjacent ordinary English paragraphs are translated by the mock adapter.
- **Focused validation:** `uv run --offline --locked --no-python-downloads --python .venv\Scripts\python.exe pytest tests\visual\test_visual_preservation.py::test_spatial_english_fraction_is_preserved_and_surrounding_prose_translates -q` passed (**1 passed**). A read-only parse of the provided dual PDF now classifies all equation text as one `RULE_SPATIAL_FRACTION_EQUATION` protected block. The input file SHA-256 was unchanged.
- **Full Python suite:** `uv run --offline --locked --no-python-downloads --python .venv\Scripts\python.exe pytest -q` passed (**69 passed, 7 skipped**, one Starlette/httpx deprecation warning). The seven skips are opt-in real-LLM/GPU tests; the user document was not used as a test fixture.
- **Limits:** The source-side classification/protection gap is reproduced and covered by the parser change and synthetic regression. No real-model retranslation or historical runtime job replay was performed; exact historical model output and rendering behavior remain unknown. The printed paper page number remains unidentified. The provided PDF was read-only opened but not copied into the repository or modified. No API process, user job, or database was changed. No frontend or dependency files were changed, and no commit or push was made.
