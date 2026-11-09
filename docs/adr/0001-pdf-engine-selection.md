# ADR-0001: PDFエンジン選定とアダプター境界設計

- **ステータス**: 承認済み (Accepted)
- **日付**: 2026-11-06
- **対象フェーズ**: Phase 0 / Phase 1

---

## 1. 背景と課題

英語の学術論文PDFを、**原本を変更せず**、**図・独立数式・インライン数式・ギリシャ文字・上下付き・式番号・参照番号を欠落させずに**日本語へ翻訳・再配置する必要がある。
指示書に基づき、候補である `BabelDOC` (`funstory-ai/BabelDOC`) および `PDFMathTranslate-next` (`PDFMathTranslate/PDFMathTranslate-next`) の公式資料・PyPIメタデータ・ソースコードを調査した。

---

## 2. 調査結果の記録

### 2.1 リリース・バージョン・ライセンス
| 項目 | BabelDOC (`funstory-ai/BabelDOC`) | PDFMathTranslate-next (`pdf2zh-next`) | PyMuPDF (`pymupdf`) |
| --- | --- | --- | --- |
| 最新確認版 | `v0.6.4` (2026-07-16公開) | `v2.9.0` (`babeldoc>=0.6.2,<0.7.0` に依存) | `1.26.7` 〜 `1.28.2` (※ `2.0+` は `onnxruntime` 等を強制依存) |
| Python要件 | `>=3.10, <3.14` (推奨 3.12 / 実機 3.13.13 互換) | `>=3.10, <3.14` (ただし `pymupdf<1.25.3` 制約あり) | `>=3.11` |
| ライセンス | **AGPL-3.0** | **AGPL-3.0** | **AGPL-3.0** (または Artifex 商用ライセンス) |
| 役割 | PDF解析・中間表現(IL)・組版・PDF再生成コアエンジン | BabelDOCを呼び出すCLI / Gradio WebUI ラッパー | PDF構文解析・描画・レンダリング基盤 |

### 2.2 BabelDOC (`v0.6.4`) の詳細評価

1. **Windows導入条件**:
   - `uv` による仮想環境構築が可能。C拡張依存はホイール提供されている（`pymupdf`, `onnxruntime`, `uharfbuzz`, `freetype-py` 等）。
2. **日本語出力**:
   - `--lang-out ja` に対応。`babeldoc/assets/embedding_assets_metadata.py` 内の `JP_FONT_FAMILY` により、`SourceHanSerifJP`, `SourceHanSansJP`, `KleeOne`, `GoNotoKurrent` が選択される。
3. **翻訳サービス差し替え方法**:
   - CLIでは OpenAI 互換エンドポイント（`--openai`）のみサポート。
   - Python内部APIでは `babeldoc.translator.translator.BaseTranslator` を継承したカスタムクラスを `TranslationConfig.translator` に注入可能。
   - **重要発見**: `BaseTranslator` サブクラスに `do_llm_translate` を実装すると、BabelDOC の `ILTranslatorLLMOnly` が使われ、英語の汎用ルールプロンプト（`PROMPT_TEMPLATE`: `## Rules ... Now translate the following text:`）が組み立てられる。一方、`do_llm_translate` が `NotImplementedError` を送出する場合は `ILTranslator` が使われ、数式プレースホルダー（`{v1}` 等）付きの段落テキストが `translate()` / `do_translate()` に渡される。
4. **中間データへのアクセス**:
   - `babeldoc.format.pdf.document_il.il_version_1.Document` が中間表現（IL）として機能し、`debug=True` または各ステージ（`parse_prepared_pdf_with_new_parser_to_legacy_ir` → `LayoutParser` → `ParagraphFinder` → `StylesAndFormulas` → `ILTranslator` → `Typesetting` → `PDFCreater`）のオブジェクトとして直接参照・操作可能。
5. **本文・図・数式の区別**:
   - `DocLayout-YOLO` ONNXモデルでページ内のレイアウト枠（title, plain text, abandon, figure, figure_caption, table, table_caption, isolate_formula, formula_caption）を検出し、`StylesAndFormulas` ステージでフォント特性・文字種・空間包含関係から本文中のインライン数式（`PdfFormula`）を分離する。
6. **保護領域指定・部分再翻訳**:
   - BabelDOC CLI 単体には、ユーザーが画面上で指定した手動保護矩形（`ProtectionRegion`）を注入する機能や、特定ブロックのみ部分再翻訳して他ブロックの推論を省略するAPIは存在しない。中間表現（IL）またはアプリ側DBキャッシュを介したアダプター制御が必要。
7. **モデル・フォント・初回取得容量**:
   - 初回実行（`warmup()` または翻訳実行）時に、外部から `~/.cache/babeldoc` へ以下の資産を自動ダウンロードする：
     - レイアウト解析モデル `doclayout_yolo_docstructbench_imgsz1024.onnx`: **71.84 MB**
     - 多言語フォント・CMap資産（`BabelDOC-Assets`）: 定義合計 **328.09 MB**
     - 合計最大 **約400 MB**。
8. **BabelDOCの重大な注意点（数式保持に関するリスク）**:
   - `babeldoc/format/pdf/document_il/midend/il_translator.py` の `parse_translate_output()` を調査した結果、LLMが数式プレースホルダー `{v1}` を欠落させた場合でも例外を送出せず `logger.debug` のみで続行し、**欠落した数式がPDFから無警告で消滅する**。また、余分なプレースホルダーや未マッチのプレースホルダーを `remove_placeholder()` で**無断削除してそのままPDF出力してしまう**ことが判明した。
   - さらに `--only-parse-generate-pdf` オプションは `LayoutParser`, `ParagraphFinder`, `StylesAndFormulas`, `Typesetting` をすべてスキップして `PDFCreater` に直行するため、このフラグの成功だけでは数式抽出・再配置の検証にならない。

---

## 3. 決定事項（Decision）

1. **CLI呼び出しではなく、アダプター層に隔離した Python パイプライン統合を採用する**:
   - BabelDOC の CLI は PLaMo の PFML3 プロンプト形式と非互換であり、手動保護領域の反映やプレースホルダー欠落時の厳格な原文フォールバックができないため、標準経路として CLI 外部実行は採用しない。
   - すべての PDF エンジン操作は `backend/pdf/adapters/base.py` で定義する `PdfEngineAdapter` 抽象インターフェースの背後に隔離する。
2. **2層のアダプター実装（`NativePreservingAdapter` ＋ `BabelDocAdapter`）**:
   - **`NativePreservingAdapter` (`backend/pdf/adapters/native_preserving_adapter.py`)**:
     - `PyMuPDF` (`>=1.26.7,<2.0.0`) と Windows 標準搭載の OFL フォント（`NotoSerifJP-VF.ttf` / `NotoSansJP-VF.ttf`）を用い、外部の約400MBアセットダウンロード無しで即座に動作するネイティブ保持エンジン。
     - 13工程すべて（ファイル検証 → ページ情報 → テキスト抽出検査 → 要素解析（見出し・本文・独立数式・インライン数式・図・表・式番号・参照番号） → 読み順判定（1段・2段組み対応） → 保護領域決定（手動矩形指定の反映含む） → 翻訳単位生成 → 分割・翻訳 → 訳文検証 → 日本語再配置（元PDFの図・独立数式・非翻訳要素を破壊せず、本文領域のみを保護領域を避けて再配置、最小8pt制限・はみ出し警告・原文フォールバック対応） → 保持検査 → 単一/対照PDF出力 → レポート生成）を実装する。
   - **`BabelDocAdapter` (`backend/pdf/adapters/babeldoc_adapter.py`)**:
     - `babeldoc==0.6.4` をラップするアダプター。無断での大容量アセット（約400MB）ダウンロードを防ぐため、実行前に `~/.cache/babeldoc` の資産存在検査を行い、`allow_asset_download=False`（初期値）の場合は必要容量と保存先を明示した `AssetDownloadRequiredError` を返す。
     - 翻訳実行時は BabelDOC 標準の無警告プレースホルダー削除をバイパスし、本アプリの `TranslationValidator` を通過した訳文（または検証失敗時に原文へロールバックしたデータ）のみを組版ステージへ渡す。
3. **PyMuPDF のバージョン固定**:
   - `pymupdf 2.0+` は `pymupdf-layout` および `onnxruntime` を強制依存として引き込むため、`pyproject.toml` では `pymupdf>=1.26.7,<2.0.0` に固定する。
