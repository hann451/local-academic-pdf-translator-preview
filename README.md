# local-academic-pdf-translator-preview

> **名称に関する注記:** 本公開プレビュー版リポジトリの名称は `local-academic-pdf-translator-preview`（基本プロジェクト名: `local-academic-pdf-translator`）です。UI表示、パッケージ名（`paperbridge-local`）、環境変数（`PAPERBRIDGE_*`）、ローカル保存先（`%LOCALAPPDATA%\PaperBridgeLocal`）、認証ヘッダー（`X-PaperBridge-Token`）等に残る `PaperBridge Local` / `PaperBridge` は開発中の内部仮称です。学術論文翻訳・閲覧分野には同名の別プロジェクトが複数存在しますが、本リポジトリとは無関係です（商標・法的安全性の確認は未完了です。[公開向け制約](docs/PUBLIC_LIMITATIONS.md)を参照）。

Windows向けの**ローカル完結・数式保護優先の学術論文PDF翻訳アプリ**（ソース導入型プレビュー版）です。原本PDFのSHA-256の不変性を検査し、数式・図・表・参照番号の保持、検証失敗の警告、ブラウザー上での確認・手動修正を重視します。PDF.jsを使う4画面のReact UIとローカルFastAPIバックエンドを備え、国産ローカルLLM「PLaMo 3 Translate」を外部サーバーへPDFを送信せずWindows PC上で完結して動かせます。

> 本 README のデモ動画はすべて軽量 GIF へ刷新しているため、**スマートフォン版 GitHub（iOS / Android）を含め、スマホ・PC どちらでもそのまま再生**できます。

---

## かんたん導入方法

Windows 環境では自動セットアップによる導入を推奨しています。Python、uv、Node.js、llama.cpp などを利用者が事前に個別インストールする必要はありません。`初回セットアップ.bat` が必要な実行環境と依存ライブラリの取得・構築をすべて行います。

### ZIP ダウンロード 〜 初回自動セットアップ手順

* **`<> Code` → `Download ZIP`** → すべて展開 → **`初回セットアップ.bat`** → モデル選択（既定は軽量な **0.8B**）・利用条件への同意 → ツール＆モデルの自動構築 → デスクトップからの起動まで、コマンド入力なしで完了します。

![ZIP取得から初回セットアップ・起動までの流れ](https://raw.githubusercontent.com/hann451/local-academic-pdf-translator-preview/docs/qiita-v03-assets/08_zip_setup_walkthrough.gif)

1. リポジトリの [ダウンロードリンク（ZIP）](https://github.com/hann451/local-academic-pdf-translator-preview/archive/refs/heads/main.zip) から最新版 ZIP をダウンロードします（`<> Code` → `Download ZIP`）。
2. ZIP ファイルを右クリックして **「すべて展開」** で通常使うフォルダーに展開します。ZIP内から直接実行せず、必ず展開してから利用してください。また、セットアップ後の移動は避けてください。
3. 展開先フォルダーにある **`初回セットアップ.bat`** をダブルクリックします。
4. 取得内容と利用条件（`PLaMo` ライセンス）を確認し、同意したうえで「同意して開始」を押します（既定は軽量な 0.8B モデルです）。
5. 完了後、デスクトップの **`PaperBridge Local`** から起動します。

> **すでに旧バージョンをご利用の場合:** 画面4の「ソフトウェア更新」から更新できます（ただし更新機能修正（PR #9）より前に取得した旧版では Windows 上で更新プロセスが起動しない不具合があったため、初回のみ最新版 ZIP の上書き展開または `git pull origin main` で手動更新してください。取り込み後は進捗ウィンドウ付きのワンクリック更新を利用できます）。

### ショートカットからの起動と進捗ウィンドウ

* 起動時のGGUF検証をC#の高速スキップで **約 23.9 秒 → 約 0.29 秒（約 82 倍）** に短縮し、**0〜100% の起動進捗ウィンドウ**を追加しました。ショートカットからは約 7.1 秒で起動します。

![起動進捗ウィンドウGIF](https://raw.githubusercontent.com/hann451/local-academic-pdf-translator-preview/docs/qiita-v03-assets/03_startup_progress_window.gif)

### 必要な環境・通信

* 対象は **Windows x64** です（すべての Windows・CPU・GPU 構成での動作を保証するものではありません）。
* 初回セットアップにはインターネット接続が必要です。
* 既定の 0.8B モデル（約 0.57 GB）を新規取得する場合、通信量の目安は合計約 0.8〜0.9 GB です（0.8B 約0.57GB / 2B 約1.81GB / 8B 約5.28GB といったモデルや、既存GGUFの参照登録などにより変動します）。
* 空き容量は **5GB 以上** を目安にしてください（利用するPDFや成果物に応じて追加容量が必要です）。
* GPU が利用できない場合に備えて **CPUモード（`CPUモードで起動.bat`）** を用意しています（CPUでの翻訳は時間がかかる場合があります）。

### 既存 Python 環境の保護とトラブル時の対応

* アプリ用の実行環境と仮想環境を使用し、既存のシステム Python や他プロジェクトのライブラリ、環境変数 PATH、レジストリは変更しない設計です（ただしすべての既存環境との組み合わせを検証したものではありません。既存 PaperBridge を併用する場合は初回設定でデータ保存先とアイコン名を分けてください）。
* SmartScreen、ウイルス対策、企業プロキシ/TLS検査等により実行・取得できない場合があります（セキュリティ機能や認証を無効化して回避しないでください）。
* ポート `8000`・`8080` が使用中の場合は起動を中断します（無関係なプロセスを強制終了しないでください）。
* 起動できない場合は **`起動診断用コンソール.bat`** を使用してください。ログを提出する際はユーザー名・ファイルパス等を確認してマスキングし、認証トークンや個人PDFを公開Issueへ投稿しないでください。

詳細は [Windowsセットアップガイド](docs/SETUP_WINDOWS.md) を参照してください。

---

## アプリの使い方・全体ワークフロー

アプリ起動後は、ブラウザー上の 4 画面に沿って**「PDF選択 → 数式・表の保護確認 → 進捗確認＆手動修正（LLMなし高速再組版）→ 成果物出力」**までを一貫して行えます。

1. **画面1（ホーム・設定）:** 翻訳したい英語論文 PDF を選択すると、原本 SHA-256 ロック・文字レイヤー有無・推論サーバーとのモデル一致を自動検査してジョブを開始できます。
2. **画面2（PDF・保護編集）:** 自動検出された独立数式・表を確認し、必要に応じてドラッグ操作による数式ブロック分割やブロック単位の翻訳／非翻訳切替を行えます。
3. **画面3（進行・対訳レビュー）:** 翻訳中の進捗率（％）・経過時間・予測残り時間（ETA）をリアルタイム表示し、完了後は見開き対訳の確認や気になる訳文の手動修正（LLM 呼び出し 0 回での高速再組版）を経て、日本語単一 PDF・見開き対訳 PDF・検証レポート JSON を保存できます。

![アプリ全体ワークフローデモ（画面1〜画面3の操作手順）](https://raw.githubusercontent.com/hann451/local-academic-pdf-translator-preview/docs/qiita-v03-assets/09_app_full_workflow_demo.gif)

---

## 詳細な機能と最新アップデートの見どころ

| 改善項目 | 従来 | 最新版 (`main`) |
| --- | --- | --- |
| **起動時のモデル検証（GGUF解析）** | 約 14〜24 秒（単体実測 23.9 秒） | **約 0.29 秒（約 82 倍高速化）** |
| **アプリ起動完了までの時間** | 約 17〜25 秒（無表示） | **約 7.1 秒（0〜100% 進捗ウィンドウ付き）** |
| **日本語組版の折り返し（650文字段落）** | 12.592 秒 | **0.0145 秒（約 868 倍高速化）** |
| **視覚保持検証（36ページ論文）** | 6.61 秒 | **2.845 秒（約 2.3 倍高速化）** |
| **36ページ論文のフォールバック件数** | 592 件 | **22 件（96.3% 削減）** |
| **特定PDFの1ページ目白紙化** | Edge/Chrome/PDF.js で白紙 | **原本と同等に復元（2,808文字）** |

![実機ベンチマーク：速度と出力品質のビフォー／アフター](https://raw.githubusercontent.com/hann451/local-academic-pdf-translator-preview/docs/qiita-v03-assets/02_speedup_benchmark_chart.png)

### ① 画面デザインの刷新 ＆ 進捗（％）・予測残り時間（ETA）表示

* 画面全体をライト基調へ整理し、13ステージの詳細や診断ログを折りたたみドロワーへ収納しました。画面3には進捗状況カードを新設し、進捗（％）・経過時間・予測残り時間・ページ数をリアルタイムで表示します。実行中のPDF重複再取得や `0/36` の張り付きも解消しました。

![翻訳進行と残り時間表示の刷新UIデモ](https://raw.githubusercontent.com/hann451/local-academic-pdf-translator-preview/docs/qiita-v03-assets/01_hero_new_ui_progress.gif)

<details>
<summary>刷新前後の画面比較（ビフォー／アフター画像）を見る</summary>

![UI刷新・進捗表示のビフォー／アフター](https://raw.githubusercontent.com/hann451/local-academic-pdf-translator-preview/docs/qiita-v03-assets/01_ui_before_after.png)

</details>

### ② 出力 PDF の品質不具合の修正

* リダクション後に `/Separation /Black`（`/CS1 cs`）直後の `1 scn` / `1 SCN` オペレータが脱落して1ページ目が白紙になっていた問題を、自動復元で解消しました。
* 独立数式を自動認識するようにし、次段落の先頭へ落ちた式番号 `(4)` / `(6)` によって本文と結合した箇所を自動で分離し、複数パートに分断された数式も1ブロックへ統合しました（手動分割も併用できます）。
* ACM系数式の文字化け（`☒`）・表・ランニングヘッダ保護: `txex` / `txsy` / `rtxmi` 等の数学フォント対応・制御文字の除外・`underbrace` の一括保護に加え、格子表・Booktabs表の `TABLE` 自動保護と `46:3` 形式のヘッダ保護で誤翻訳フォールバックを大幅に削減しました。

<details>
<summary>出力PDF修正のビフォー／アフター画像を見る（1ページ目白紙化・ACM数式文字化け・式番号自動分離）</summary>

![1ページ目白紙化修正のビフォー／アフター](https://raw.githubusercontent.com/hann451/local-academic-pdf-translator-preview/docs/qiita-v03-assets/04_pdf_fix_page1_blank_before_after.png)

![ヘッダ・ACM数式文字化け修正のビフォー／アフター](https://raw.githubusercontent.com/hann451/local-academic-pdf-translator-preview/docs/qiita-v03-assets/05_pdf_fix_math_header_before_after.png)

![式番号落下による数式＋本文結合の自動分離](https://raw.githubusercontent.com/hann451/local-academic-pdf-translator-preview/docs/qiita-v03-assets/06_math_separation_before_after.png)

</details>

### ③ モデル選択・推論サーバー自動切替 ＆ ワンクリック自己アップデート

* **初回既定モデルは 0.8B（約 0.57 GB）** で初期構築が高速です。高精度な翻訳が必要な場合は、導入後に Web UI（PLaMo 3 Translate 0.8B / 2B / 8B カタログ）から **2B（約 1.81 GB・実機検証済み）** 等へワンクリックで自動再起動して切り替えられます（**8B はカタログに収録していますが、実機では未検証です**）。
* 選択したモデルと稼働中の `llama-server` が一致しない場合、本アプリは翻訳の開始を拒否します。不一致警告バナーから稼働中ジョブがないことを確認して再起動・切替できます。
* 画面4「用語集・診断」の **「ソフトウェア更新」** から GitHub 最新版を取得し、進捗ウィンドウを表示しながら自己アップデート・自動再起動できます（`%LOCALAPPDATA%\PaperBridgeLocal` のユーザーデータ・GGUF モデル・`.env`・`uat-artifacts/` は保護されます）。

![モデル選択・推論サーバー自動切替＆ワンクリック自己アップデートGIF](https://raw.githubusercontent.com/hann451/local-academic-pdf-translator-preview/docs/qiita-v03-assets/10_model_switch_and_update.gif)

詳細は [モデル選択・導入機能の概要](docs/MODEL_SELECTION.md) を参照してください。

### ④ レイアウト・数式崩れを防ぐ 4 つの自動回帰検証指標

* ジョブごとに `report.json` および専用 CLI（`scripts/check_layout_regression.py`）で「数式ブロック数一致・桁区切り/単位保持・欄順単調性・非翻訳ブロック座標維持」の4指標を自動検査できます。

![4つの機械的回帰検証指標図解](https://raw.githubusercontent.com/hann451/local-academic-pdf-translator-preview/docs/qiita-v03-assets/07_four_regression_metrics_diagram.png)

---

## 配布形態・制限および未対応範囲の概要

* 配布形態（ソース導入型プレビュー）: 本リポジトリは公開用ソーススナップショットを提供します（古い `v0.1.0-preview.2` タグのアーカイブには自動セットアップ機能は含まれません）。
* 非同梱物（別途取得が必要）: 翻訳モデル本体（PLaMo 3 Translate GGUF）、推論サーバー実行ファイル（`llama.cpp`）、利用者文書（PDF）、およびローカル SQLite データベースは同梱していません。**実際の翻訳には本アプリ（FastAPI + UI）と推論サーバー（`llama-server`）の両方を起動する必要があります。**
* 制限および未対応範囲:
  * 文字レイヤーのないスキャン PDF の OCR、暗号化・破損 PDF の翻訳は未対応です。
  * 段・ページ境界の文脈補助はプロンプトへ近傍文脈を付与するのみであり、複数ブロックを結合する**連結翻訳（訳文の結合・再配分）は未実装**です。
  * インライン数式・数学記号の扱い: 数学用イタリック・ギリシャ文字・補助平面文字（`U+1D400`〜`U+1D7FF` 等）を `{v1}` 等で保護し、Windows の `Cambria` / `Segoe UI Symbol` 等のフォールバックと `ToUnicode` 補正で混在組版できます。一方、縦積み分数、分割重複ブロック（`TRANSLATED_BLOCK_OVERLAP_FALLBACK`）、狭小・高密度段落（`TYPESETTING_OVERFLOW_FALLBACK`）、曖昧な同一変数競合（`AMBIGUOUS_INLINE_MATH_FALLBACK`）、独立数式や式番号周辺（`PROTECTED_SKIP`）、複雑な多段数式等は原文保持（フォールバック＋警告）または翻訳対象外となり、すべての数式やレイアウトの保持は保証されません。
* 検証状況: 開発環境および手動環境（Windows 11 x64、Python 3.13、AMD Radeon RX 7600、llama.cpp Vulkan版等）での検証実績に基づいていますが、未検証環境での動作は保証しません。詳細は [公開向け検証状況](docs/VERIFICATION.md) と [公開向け制約](docs/PUBLIC_LIMITATIONS.md) を参照してください。

---

## 開発者・手動セットアップ手順

Windows で Git クローンから手動でセットアップする手順です。詳細な前提条件・推論サーバー単体起動コマンド・フォント設定は [Windowsセットアップガイド](docs/SETUP_WINDOWS.md) を参照してください。

### 1. 前提ツールの確認

PowerShell で [Git](https://git-scm.com/download/win)、[Python 3.13（または3.12）](https://www.python.org/downloads/windows/)、[uv](https://docs.astral.sh/uv/getting-started/installation/)、[Node.js / npm](https://nodejs.org/en/download/) が使えることを確認します。

```powershell
git --version
python --version
uv --version
node --version
npm --version
```

### 2. クローンとロックファイルによる依存導入・ビルド

```powershell
git clone --branch main --single-branch https://github.com/hann451/local-academic-pdf-translator-preview.git PaperBridgeLocal
Set-Location .\PaperBridgeLocal
git rev-parse HEAD

uv sync --python 3.13 --locked --extra dev
Push-Location .\frontend
npm ci
npm run build
Pop-Location
```

### 3. 推論サーバーとアプリ本体の起動・接続確認

1. ターミナル1（推論サーバー）: [Windowsセットアップガイドの「7. 実機モデルと推論サーバー」節](docs/SETUP_WINDOWS.md) を参照し `llama-server.exe`（既定 `127.0.0.1:8000`）を起動したままにします。
2. ターミナル2（アプリ本体）: 次を実行し、ブラウザーで **[http://127.0.0.1:8080/](http://127.0.0.1:8080/)** を開きます（`Origin` 制約により原則 `127.0.0.1:8080` を使用してください）。

```powershell
uv run uvicorn backend.api.app:app --host 127.0.0.1 --port 8080
```

* 疎通確認 / 中断再開: 画面4の「🔌 推論サーバ疎通確認 (`probe_llama=true`)」で疎通を確認できます。推論サーバーが未起動のまま開始して中断（`interrupted`）した場合は、推論サーバー起動後に画面3「▶ 中断箇所からジョブを再開 (Resume)」で再開できます。
* モック推論（実翻訳ではありません）: モデル未導入でUIや組版動作のみ試す場合は画面1で「モック推論」を明示的に選択してください（`is_mock_translation: true` が記録されます）。
* 停止・保護: 各ターミナルで `Ctrl+C` により停止してください。`Stop-Process -Name python,llama-server` による一括終了や `%LOCALAPPDATA%\PaperBridgeLocal` の無断削除は行わないでください。

---

## ライセンスとモデル利用条件（Built with PLaMo）

* Built with PLaMo / 想定モデル: 本ツールは翻訳モデルとして **PLaMo 3 Translate**（初回既定は軽量な 0.8B、高精度推奨の 2B の公式配布元: [pfnet/plamo-3-translate-2b-GGUF](https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF)、8B もカタログ収録）の使用を想定して設計されています（**Built with PLaMo**）。本ツールは Preferred Networks（PFN）の公式製品ではありません。
* ソースコードと直接依存: アプリ本体の自作部分は [GNU AGPL version 3](LICENSE)（SPDX: `AGPL-3.0-only`）でライセンスします。適用範囲と著作権表示は [COPYRIGHT.md](COPYRIGHT.md)、直接依存パッケージの通知は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) を参照してください。
* モデルの非同梱・独立したライセンスと契約条件の遵守（PLaMo 3 Translate）: 本リポジトリは **PLaMo 3 Translate** モデル本体・派生モデル・翻訳生成物を同梱・再配布しません。ツール本体（`AGPL-3.0-only`）とモデルのライセンスは別であり、**PLaMo 3 Translate** には Preferred Networks（PFN）が定める **PLaMoコミュニティライセンス契約**（公式条文URL: [日本語版](https://plamo.preferredai.jp/info/plamo-community-license-ja) / [英語版](https://plamo.preferredai.jp/info/plamo-community-license-en)）が別途適用されます。モデルを同梱していなくても、利用者は公式リンク等で案内されるモデルの契約条件を確認・遵守する必要があります（`Built with PLaMo` の表示場所については、READMEやUIへの記載で足りる旨の回答に基づき、本READMEおよびアプリ画面1・画面4に記載しています。リンクは利用者がクリックしたときのみ開き、起動時に自動通信しません）。なお、以下は **PLaMo 3 Translate** に関して2026年11月9日改定のPLaMoコミュニティライセンス契約に基づきPFNから受領した回答要旨を反映した案内であり、公開条文ページの再照合記録や、過去のモデル取得時点（2026-11-06、revision `232fbd7916519edecddfdae0beae7e76fe70453f`）および改定前（2026-11-07 / 2026-11-08）の条文確認記録とは区別します（利用者は自身のモデル取得・利用時点における公式条文を確認してください）。
* PLaMo 3 Translate による翻訳PDFの提供・公表に関する条件と注意事項（PFN回答に基づく案内）:
  * 翻訳PDFの提供・公表と長文公表物の例外: 書籍・論文などの独立した長文公表物に関する場合を除き、通常の翻訳PDFの提供・公表には、一律の出自表示や契約コピー・説明ファイルの添付は不要です。**ただし、書籍・論文などの独立した長文公表物の全部または実質的部分として公表する場合は、PLaMoを翻訳に利用した旨の明示が必要です。** この明示はPDFとは別のファイルではなく**PDF内に含める必要があり、PDF末尾の説明ページでも対応できます**。本ツールは出力PDFへ説明ページや出自表示を自動追加しないため、該当する公表を行う場合は利用者自身でPDF内に明示を含めてください。
  * 商用利用条件・原著作物の著作権との区別: 商用・非商用の区分や生成物を用いたモデル学習等の条件は公式条文に従ってください。なお、モデルのライセンスおよびPFNの回答は **PLaMo 3 Translate** とその生成物の利用条件に関するものであり、**翻訳元となる原著作物（元の論文・書籍等）の翻訳・配布・公表に関する著作権上の許諾まで与えるものではありません**（詳細は[公開向け制約](docs/PUBLIC_LIMITATIONS.md)を参照）。

---

## ドキュメント

* [Windows環境セットアップ手順](docs/SETUP_WINDOWS.md)
* [モデル選択・導入機能の概要](docs/MODEL_SELECTION.md)
* [全体仕様書](docs/SPEC.md)
* [アーキテクチャ設計](docs/ARCHITECTURE.md)
* [API契約・座標・認証仕様書](docs/API_CONTRACT.md)
* [公開向け検証状況](docs/VERIFICATION.md)
* [公開向け制約](docs/PUBLIC_LIMITATIONS.md)
* [第三者ライセンス通知](THIRD_PARTY_NOTICES.md)
* ADR: [PDFエンジン](docs/adr/0001-pdf-engine-selection.md)、[PLaMo推論](docs/adr/0002-plamo-inference-and-pfml3.md)、[数式保護](docs/adr/0003-formula-preservation-and-validation.md)
