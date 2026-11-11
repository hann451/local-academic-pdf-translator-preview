# local-academic-pdf-translator-preview

> **名称に関する注記:** 本公開プレビュー版リポジトリの名称は `local-academic-pdf-translator-preview`（基本プロジェクト名: `local-academic-pdf-translator`）です。UI表示、パッケージ名（`paperbridge-local`）、環境変数（`PAPERBRIDGE_*`）、ローカル保存先（`%LOCALAPPDATA%\PaperBridgeLocal`）、認証ヘッダー（`X-PaperBridge-Token`）等に残る `PaperBridge Local` / `PaperBridge` は開発中の内部仮称です。学術論文翻訳・閲覧分野には同名の別プロジェクトが複数存在しますが、本リポジトリとは無関係です（商標・法的安全性の確認は未完了です。[公開向け制約](docs/PUBLIC_LIMITATIONS.md)を参照）。

Windows向けのローカル学術論文PDF翻訳アプリ（**Windows向け・ソース導入型プレビュー版**）です。原本PDFのSHA-256不変性を検査し、数式・図・参照番号の保持、検証失敗の警告、ブラウザー上での確認・手動修正を重視します。PDF.jsを使う4画面のReact UIと、ローカルFastAPIバックエンドを備えます。

## 配布形態と導入方法の位置づけ

- **配布形態（ソース導入型プレビュー）:** 本リポジトリは開発履歴を含まない公開用ソーススナップショットを提供します。従来の手動セットアップ手順に加え、**ソース ZIP ＋ 初回自動セットアップ方式（試験導入）**を追加しています（古い `v0.1.0-preview.2` タグのアーカイブには自動セットアップ機能は含まれません。新機能を利用する場合は、`main` ブランチまたは本機能を含む新しいリリース版から取得してください）。
- **非同梱物（別途取得が必要）:** 翻訳モデル本体（PLaMo 3 Translate GGUF）、推論サーバー実行ファイル（`llama.cpp`）、利用者文書（PDF）、およびローカルSQLiteデータベースは同梱していません。**実際の翻訳には、本アプリ（FastAPI + UI）と推論サーバー（`llama-server`）の両方を起動する必要があります。**
- **制限および未対応範囲の概要:**
  - 文字レイヤーのないスキャンPDFのOCR、暗号化・破損PDFの翻訳は未対応です。
  - 段・ページ境界の文脈補助はプロンプトへの近傍文脈付与のみであり、複数ブロックを結合する**連結翻訳（訳文の結合・再配分）は未実装**です。
  - 記録された従来の手動検証例（Windows 11 x64、CPython 3.13.13、uv 0.11.15、Node.js 22.20.0 / npm 10.9.3、AMD Radeon RX 7600、llama.cpp Vulkan版）も 1 つの実機確認例であり、最低ハードウェア要件の確定は未検証です（Python許容範囲は `pyproject.toml` 上 `>=3.12,<3.14`）。詳細は[Windowsセットアップガイド](docs/SETUP_WINDOWS.md)、[公開向け制約](docs/PUBLIC_LIMITATIONS.md)、[公開向け検証状況](docs/VERIFICATION.md)を参照してください。

## 初回自動セットアップについて

本機能は、Windows x64向けの試験導入機能です。

Python、uv、Node.js、llama.cppなどを利用者が事前に
個別インストールする必要はありません。
「初回セットアップ.bat」が、必要な実行環境と依存ライブラリの
取得・構築を行います。

### 使い方

1. 自動セットアップ機能を含む版のソースZIPをダウンロードする。
2. ZIPをすべて展開する。
3. 「初回セットアップ.bat」をダブルクリックする。
4. 取得内容と利用条件を確認し、モデルの準備方法を選んで開始する。
5. 完了後は、デスクトップの「PaperBridge Local」から起動する。

ZIP内から直接実行せず、必ず展開してから使用してください。
展開先は通常利用する場所に決め、セットアップ後の移動は避けてください。

### 必要な環境・通信

- 対象はWindows x64です。すべてのWindows・CPU・GPU構成での
  動作を保証するものではありません。
- 初回セットアップにはインターネット接続が必要です。
- モデルを新規取得する場合、通信量の目安は合計約2.1～2.2GBです。
  パッケージ更新や再試行などにより変動します。
- 空き容量は5GB以上を目安にしてください。
  利用するPDFや成果物に応じて追加容量が必要です。
- GPUが利用できない場合に備えてCPUモードを用意しています。
  CPUでの翻訳は時間がかかる場合があります。

### 既存のPython環境について

アプリ用の実行環境と仮想環境を使用し、
既存のPythonや他プロジェクトのライブラリを変更しない設計です。
PC全体のPATH変更やPythonのレジストリ登録は行わない設計ですが、
Condaや独自の環境変数を含むすべての既存環境との組み合わせを
検証したものではありません。

既存のPaperBridgeを利用している場合は、初回設定で
データ保存先とデスクトップアイコン名を分けてください。

### 制限とトラブル時の対応

- SmartScreen、ウイルス対策、組織の実行制限、
  プロキシやTLS検査などにより実行・取得できない場合があります。
- セキュリティ機能や認証を無効化して回避しないでください。
- ポート8000・8080が使用中の場合は起動を中断します。
  無関係なプロセスを終了しないでください。
- 起動できない場合は「起動診断用コンソール.bat」を使用してください。
- ログにはユーザー名・ファイルパスなどが含まれる場合があります。
  提出前に確認・マスキングし、認証トークンや個人PDFを
  公開Issueへ投稿しないでください。

### 検証状況

開発環境内での自動セットアップ、GPU・CPUによる実翻訳、
PDF出力、通常終了を確認しています。
作者による手動操作でも正常動作の報告があります。

ただし、開発ツール未導入の別Windows PC、すべてのGUI操作、
モデルの全量ネットワーク取得、GPU失敗時の自動CPU切り替えなどは、
実施済みの範囲を検証記録で確認してください。
未検証の構成での動作は保証しません。

### ライセンスと成果物

本体のAGPL-3.0-onlyと、PLaMoモデルの利用条件は別です。
モデル取得前に公式の利用条件を確認してください。
また、元PDFの翻訳・配布・公表に必要な許諾は利用者が確認してください。

数式・図表・レイアウト・翻訳内容の完全な保持や正確性は保証しません。
原本を保存し、出力PDFを目視確認してください。

バッチ一覧・ログ確認先などの詳細は [Windowsセットアップガイド](docs/SETUP_WINDOWS.md) を参照してください。

## 従来の手動セットアップ手順（Windowsで始める）

詳細な前提条件、モデル・推論バイナリの配置、フォント、エラー対処は[Windowsセットアップガイド](docs/SETUP_WINDOWS.md)を参照してください。

### 1. 前提ツールの確認

PowerShellを開き、[Git](https://git-scm.com/download/win)、[Python 3.13（または3.12）](https://www.python.org/downloads/windows/)、[uv](https://docs.astral.sh/uv/getting-started/installation/)、[Node.js / npm](https://nodejs.org/en/download/) が利用できることを確認します。

```powershell
git --version
python --version
uv --version
node --version
npm --version
```

### 2. ソースコードの取得

空の作業用ディレクトリへクローンして移動します（リポジトリがPrivateの間は読み取り権限のあるアカウントでのGitHub認証が必要です。認証情報をURLやコマンドへ埋め込まないでください）。

```powershell
git clone --branch main --single-branch https://github.com/hann451/local-academic-pdf-translator-preview.git PaperBridgeLocal
Set-Location .\PaperBridgeLocal
git rev-parse HEAD
```

（※本リポジトリは公開用スナップショットであり、開発用Privateリポジトリの過去コミット履歴は含みません。開発版での検証履歴は[公開向け検証状況](docs/VERIFICATION.md)を参照してください。）

### 3. ロックファイルによる依存導入とUIビルド

検証済みのPython版（例: `3.13`）とロックファイル（`uv.lock` / `frontend/package-lock.json`）を使ってPython仮想環境とフロントエンドをビルドします（初回はパッケージ取得のためインターネット通信が発生し得ます）。

```powershell
uv sync --python 3.13 --locked --extra dev
Push-Location .\frontend
npm ci
npm run build
Pop-Location
```

`npm run build` により `frontend\dist\index.html` と静的アセットが生成され、FastAPIからUIを配信できるようになります。

### 4. 推論サーバーとモデルの起動（実機翻訳を行う場合）

実機翻訳を行うには、本アプリとは別プロセスで `llama-server.exe`（既定 `127.0.0.1:8000`）と `plamo-3-translate-2b-Q4_K_M.gguf` を起動しておく必要があります。取得元・配置パス・GPU/CPU別の起動コマンドは[Windowsセットアップガイドの「7. 実機モデルと推論サーバー」節](docs/SETUP_WINDOWS.md)を参照し、**推論サーバー用ターミナル（ターミナル1）** で起動したままにしてください（実行ファイルとモデル本体はGitに含めないでください）。

### 5. アプリ本体の起動とブラウザー接続

プロジェクトルートで **アプリ用ターミナル（ターミナル2）** を開き、次を起動します（起動中はターミナルを開いたままにします）。

```powershell
uv run uvicorn backend.api.app:app --host 127.0.0.1 --port 8080
```

ブラウザーで推奨URL **<http://127.0.0.1:8080/>** を開きます。
- **ブラウザー推奨URLとOrigin制約:** バックエンドの `allowed_origins` は既定で `http://127.0.0.1:8080`、`http://localhost:8080`、`http://127.0.0.1:5173`、`http://localhost:5173` のみを許可しています。Uvicornの `--port` 引数だけを別ポートへ変更してブラウザーから開くと、初期画面表示（`GET`）は通っても、ジョブ開始等の `POST` / `PUT` / `DELETE` が `Origin` 検証で拒否（HTTP `403`）され得ます。ブラウザーからは原則 `http://127.0.0.1:8080/` を使用してください。

### 6. モデル未接続時の対処とモック試用の区別

- **疎通確認（翻訳リクエスト非送信）:** ブラウザーで画面が開くことと、推論サーバーが接続されていることは別です。推論サーバーの接続状態は、画面4「用語集管理・システム診断」の **「🔌 推論サーバ疎通確認 (`probe_llama=true`)」** ボタンで確認できます。この確認は `GET /v1/models` の軽量確認のみを行い、接続確認のために翻訳リクエストは送信しません。
- **未接続のまま開始した場合の対処:** 画面1の初期選択は「実機推論」です。推論サーバー未起動のまま開始すると、接続エラーにより画面3でジョブが中断（`interrupted`）状態になります。その場合は別ターミナルで推論サーバーを起動し、画面4で疎通確認後に画面3の **「▶ 中断箇所からジョブを再開 (Resume)」** を押してください。
- **モック推論（実翻訳ではありません）:** モデル未導入の環境でUIの画面遷移やレイアウト処理だけを試す場合は、画面1の「推論エンジン」で **「モック推論」** を明示選択してから開始してください。モックはテスト用の決定論的な疑似翻訳であり、PLaMoによる実翻訳や翻訳品質の確認には使えません（成果物にも `is_mock_translation: true` が記録されます）。

### 7. 通常の停止方法と安全な扱い

- **通常の停止方法:** アプリ（`127.0.0.1:8080`）や推論サーバー（`127.0.0.1:8000`）を終了するときは、**それぞれを起動したターミナルで `Ctrl+C` を押して停止**してください。停止したサーバーは自動では再起動しません。
- **既存プロセス・利用者DBの保護:** `Stop-Process -Name python,llama-server` などの名前指定でプロセスを一括強制終了しないでください（他の用途で稼働中のプロセスを巻き込む恐れがあります）。また、ローカル保存先（`%LOCALAPPDATA%\PaperBridgeLocal`）内の利用者データベースや過去ジョブ成果物を無断で削除しないでください。

## ライセンスとモデル利用条件（Built with PLaMo）

- **Built with PLaMo / 想定モデル:** 本ツールは翻訳モデルとして **PLaMo 3 Translate**（公式配布元: [pfnet/plamo-3-translate-2b-GGUF](https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF)）の使用を想定して設計されています（**Built with PLaMo**）。本ツールは Preferred Networks（PFN）の公式製品ではありません。
- **ソースコードと直接依存:** アプリ本体の自作部分は [GNU AGPL version 3](LICENSE)（SPDX: `AGPL-3.0-only`）でライセンスします。適用範囲と著作権表示は [COPYRIGHT.md](COPYRIGHT.md)、直接依存パッケージの通知は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) を参照してください。
- **モデルの非同梱・独立したライセンスと契約条件の遵守（PLaMo 3 Translate）:** 本リポジトリは **PLaMo 3 Translate** モデル本体・派生モデル・翻訳生成物を同梱・再配布しません。ツール本体（`AGPL-3.0-only`）とモデルのライセンスは別であり、**PLaMo 3 Translate** にはPreferred Networks（PFN）が定める **PLaMoコミュニティライセンス契約**（公式条文URL: [日本語版](https://plamo.preferredai.jp/info/plamo-community-license-ja) / [英語版](https://plamo.preferredai.jp/info/plamo-community-license-en)）が別途適用されます。モデル非同梱であっても、利用者は公式リンク等で案内されるモデルの契約条件を確認・遵守する必要があります（`Built with PLaMo` の表示場所については、READMEやUIへの記載で足りる旨の回答に基づき、本READMEおよびアプリ画面1・画面4に記載しています。リンクは利用者がクリックしたときのみ開き、起動時に自動通信しません）。なお、以下は **PLaMo 3 Translate** に関して2026年11月9日改定のPLaMoコミュニティライセンス契約に基づきPFNから受領した回答要旨を反映した案内であり、公開条文ページの再照合記録や、過去のモデル取得時点（2026-11-06、revision `232fbd7916519edecddfdae0beae7e76fe70453f`）および改定前（2026-11-07 / 2026-11-08）の条文確認記録とは区別します（利用者は自身のモデル取得・利用時点における公式条文を確認してください）。
- **PLaMo 3 Translate による翻訳PDFの提供・公表に関する条件と注意事項（PFN回答に基づく案内）:**
  - **翻訳PDFの提供・公表と長文公表物の例外:** 書籍・論文などの独立した長文公表物に関する場合を除き、通常の翻訳PDFの提供・公表には、一律の出自表示や契約コピー・説明ファイルの添付は不要です。**ただし、書籍・論文などの独立した長文公表物の全部または実質的部分として公表する場合は、PLaMoを翻訳に利用した旨の明示が必要です。** この明示はPDFとは別のファイルではなく**PDF内に含める必要があり、PDF末尾の説明ページでも対応可能**です。本ツールは出力PDFへ説明ページや出自表示を自動追加しないため、該当する公表を行う場合は利用者自身でPDF内に明示を含めてください。
  - **商用利用条件・原著作物の著作権との区別:** 商用・非商用の区分や生成物を用いたモデル学習等の条件は公式条文に従ってください。なお、モデルのライセンスおよびPFNの回答は **PLaMo 3 Translate** とその生成物の利用条件に関するものであり、**翻訳元となる原著作物（元の論文・書籍等）の翻訳・配布・公表に関する著作権上の許諾まで与えるものではありません**（詳細は[公開向け制約](docs/PUBLIC_LIMITATIONS.md)を参照）。

## ドキュメント

- [Windows環境セットアップ手順](docs/SETUP_WINDOWS.md)
- [全体仕様書](docs/SPEC.md)
- [アーキテクチャ設計](docs/ARCHITECTURE.md)
- [API契約・座標・認証仕様書](docs/API_CONTRACT.md)
- [公開向け検証状況](docs/VERIFICATION.md)
- [公開向け制約](docs/PUBLIC_LIMITATIONS.md)
- [第三者ライセンス通知](THIRD_PARTY_NOTICES.md)
- ADR: [PDFエンジン](docs/adr/0001-pdf-engine-selection.md)、[PLaMo推論](docs/adr/0002-plamo-inference-and-pfml3.md)、[数式保護](docs/adr/0003-formula-preservation-and-validation.md)
