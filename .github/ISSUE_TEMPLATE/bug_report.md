---
name: バグ報告 (Bug Report)
about: 数式崩れ・未翻訳・レイアウト崩れ等の問題を報告する（未公開論文の本文は貼らないでください）
title: "[BUG] "
labels: bug
assignees: ''
---

> **プライバシー警告**: 未公開論文や著作権のある論文PDF・本文ログをそのままIssueに貼り付けないでください。可能な限り `scripts/generate_test_pdfs.py` の自作PDFまたは最小再現PDFで問題を再現してください。

## 1. 発生した事象
- [ ] 数式・記号の欠落または崩れ
- [ ] 図・表の欠落または重なり
- [ ] 訳文の途切れ・未翻訳・特殊トークン残存
- [ ] ジョブの中断・再開失敗
- [ ] その他

## 2. 実行環境
- OSバージョン:
- CPU / RAM / GPU:
- Python / uv バージョン:
- 使用アダプター (`NativePreservingAdapter` / `BabelDocAdapter`):
- 使用モデル・量子化 (例: `plamo-3-translate-2b-Q4_K_M.gguf` / モック):

## 3. 再現手順
1.
2.
3.

## 4. 期待される結果と実際の結果（ValidationIssue 等）
- ジョブ状態 (`completed` / `completed_with_warnings` / `failed`):
- 出力された警告コード:
