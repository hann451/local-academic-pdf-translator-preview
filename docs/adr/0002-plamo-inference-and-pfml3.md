# ADR-0002: PLaMo 3 Translate 推論方式と PFML3 アダプター設計

- **ステータス**: 承認済み (Accepted)
- **日付**: 2026-11-06
- **対象フェーズ**: Phase 0 / Phase 2

---

## 1. 背景と課題

ローカル環境（AMD Ryzen 5 5600X, RAM 32GB, AMD Radeon RX 7600 8GB VRAM, Windows 11）において、外部の有料APIに依存せず高品質な英日論文翻訳を行うため、`llama.cpp` と `PLaMo 3 Translate` GGUF モデルを採用する。
PLaMo 3 Translate は翻訳特化モデルであり、汎用チャットLLMとは異なる独自のプロンプト形式（**PFML3**）を持つため、公式仕様に基づく専用設計が必要である。

---

## 2. 公式仕様の確認結果（`pfnet/plamo-3-translate-2b` / `2b-GGUF`）

1. **モデルラインナップとGGUF容量**:
   - `0.8B GGUF Q4_K_M`: **0.57 GB**（軽量検証向け）
   - `2B GGUF Q4_K_M`: **1.81 GB**（基本採用。BF16比でPFTB +0.0001、FLORES+ 英↔多言語 +0.02 と精度劣化がほぼ無い）
   - `8B GGUF Q4_K_M`: **5.28 GB**（品質比較向け。8GB VRAM環境ではコンテキスト長に応じたオフロード層数調整が必要）
2. **学習時最大コンテキスト長**:
   - 入出力合計 **32,768 トークン**。
   - 本アプリの初期設定コンテキスト長はメモリ安全性と応答速度のため **8,192 トークン（8K）**、同時翻訳数 **1** とし、モデルの学習上限（32,768）と実行時の設定上限（`llama-server -c`）を明確に分離して管理する。
3. **PFML3 プロンプト構造**:
   - 構成要素は `<|plamo:tag|>` と `<|plamo:msg|>`、属性は `<|plamo:key|>` と `<|plamo:val|>` で表現する。
   - 要素の並び順（すべて同時指定する場合）:
     1. `<|plamo:tag|>dataset<|plamo:msg|>translation`
     2. `<|plamo:tag|>context<|plamo:msg|>{参考文脈}` （任意）
     3. 過去の対訳履歴（`<|plamo:tag|>input<|plamo:key|>lang<|plamo:val|>en<|plamo:msg|>{過去原文}<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>ja<|plamo:msg|>{過去訳文}` の組、任意）
     4. `<|plamo:tag|>glossary<|plamo:index|>{原語1}<|plamo:item|>{訳語1}...` （任意、今回の翻訳対象に出現する用語のみを抽出して付与）
     5. `<|plamo:tag|>input<|plamo:key|>lang<|plamo:val|>en<|plamo:msg|>{今回の翻訳対象原文}`
     6. `<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>ja<|plamo:key|>form<|plamo:val|>{plain|polite}<|plamo:msg|>`
4. **文頭トークンと停止トークン**:
   - `llama.cpp` は文頭トークン `<|plamo:bos|>` を自動付与するため、手書きの PFML3 文字列の先頭には `<|plamo:bos|>` を付けない（重複防止）。
   - 生成停止トークン（`stop`）には必ず `["<|plamo:tag|>", "<|plamo:bos|>"]` を指定し、万一出力文字列末尾に残存した場合は後処理で除去する。
5. **Chat Completions API vs Completions API**:
   - 公式ドキュメント（`README_ja.md`）において、「アプリケーションへの組み込みなど、本格的に活用する場合は、PFML3形式でプロンプトを直接組み立て、起動したサーバーの Completions API（`/v1/completions`）に渡す方法をおすすめします」と明記されている。
   - Chat Completions API は `llama-server` 起動時に `--jinja --no-prefill-assistant` フラグが必須になるが、Completions API に PFML3 を直接渡す方式であれば Jinja テンプレート解釈の差異に影響されず、トークン予算の計算も正確に行える。

---

## 3. 決定事項（Decision）

1. **PFML3 直接構築 ＋ Completions API (`/v1/completions`) を第一経路とする**:
   - `backend/translation/prompts/pfml3.py` にて PFML3 プロンプトを厳密に構築し、`llama-server` の `/v1/completions` エンドポイントへ送信する（フォールバックとして Chat Completions API 形式のペイロード生成関数も提供するが、標準は Completions API とする）。
2. **論文翻訳向けの初期設定**:
   - 方向: `en` → `ja`
   - 文体 (`form`): 学術論文に適した **`plain`（常体：だ・である調）を初期値**とし、設定で `polite`（敬体）へ切り替え可能にする。
   - 自動用語抽出: **初期無効**。PLaMo 3 Translate は翻訳専用モデルであり、任意のJSON出力による用語抽出タスクを仮定しない。ユーザー指定の用語集（`Glossary`）のうち、当該セグメントに出現するエントリのみを `<|plamo:tag|>glossary` に挿入する。
3. **コンテキスト・トークン予算管理**:
   - 全文を1リクエストで翻訳せず、段落単位で翻訳する。
   - `本文 + 参考文脈(直近見出し/直前対訳) + 用語集 + 出力予約(入力トークン数の1.8倍または最低256トークン)` が設定コンテキスト上限（初期8Kの安全マージン内）を超える場合：
     1. まず対訳履歴・参考文脈を古い順に削減する。
     2. それでも単一段落が予算を超える場合は、**数式プレースホルダー（`{v1}` 等）の途中で絶対に切断しない**文境界分割器（`SentenceSplitter`）で再分割する。

---

## 4. Phase 2 実機検証による追記（2026-11-06）

1. **`llama.cpp` Windows Vulkan ビルド (`b12167`) における GPU オフロードと Flash Attention**:
   - AMD Radeon RX 7600 (8GB VRAM) 上で `plamo-3-translate-2b-Q4_K_M.gguf` をロードした結果、`25/25 layers offloaded to GPU (Vulkan0)`（モデルバッファ `1573.33 MiB`、KVバッファ `153.00 MiB`、計算バッファ `90.52 MiB`、計 `1816.85 MiB`）を確認した。
   - `llama-server` のデフォルト並列スロット数（`-np 4`）では、統一KVキャッシュに対して Vulkan バックエンドの Flash Attention 制約（`K/V rows must be equal to context size`）が働き Flash Attention が無効化されることをログ（`-lv 4`）で特定した。
   - 本アプリは同時翻訳数 `1` を設計前提とするため、起動引数に **`-np 1`** を明示することで `resolve_fused_ops: Flash Attention enabled` が有効化され、約 `101 tokens/sec` の高速な生成速度が得られることを確認した。
2. **PFML3 トークナイズと停止トークンの実機検証**:
   - `llama-server` の `/tokenize` エンドポイント（ルート直下 `http://127.0.0.1:8000/tokenize`）にて、`include_bos=False` の PFML3 プロンプトに対して先頭に単一の `<|plamo:bos|>` (`id=1`) が自動付与され、`<|plamo:tag|>` (`id=16`)、`<|plamo:key|>` (`id=17`)、`<|plamo:val|>` (`id=18`)、`<|plamo:msg|>` (`id=19`) がそれぞれ 1 トークンの特殊トークンとして正確に認識されることを確認した。
