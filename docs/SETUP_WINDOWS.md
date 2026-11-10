# local-academic-pdf-translator-preview（内部仮称: PaperBridge Local） — Windowsセットアップ・運用ガイド

本公開プレビュー版リポジトリの名称は `local-academic-pdf-translator-preview`（基本プロジェクト名: `local-academic-pdf-translator`）です。UI表示、パッケージ名（`paperbridge-local`）、環境変数（`PAPERBRIDGE_*`）、ローカル保存先（`%LOCALAPPDATA%\PaperBridgeLocal`）、認証ヘッダー（`X-PaperBridge-Token`）に残る `PaperBridge Local` / `PaperBridge` は開発中の内部仮称であり、同名の他プロジェクトとは無関係です。

本初版は **Windows向け・ソース導入型の限定プレビュー版** です。ワンクリックインストーラーは用意されておらず、翻訳モデル（PLaMo 3 Translate GGUF）、推論サーバー（`llama.cpp`）、利用者文書（PDF）、およびローカルSQLiteデータベースは同梱していません。**実際の翻訳には、本アプリ（FastAPI + UI）と推論サーバー（`llama-server`）の両方を起動する必要があります。** また、スキャンPDFのOCR、ブロックをまたぐ連結翻訳、別PC・他GPU・クリーンWindows環境での動作保証は未対応または未検証です（詳細は[公開向け制約](PUBLIC_LIMITATIONS.md)および[公開向け検証状況](VERIFICATION.md)を参照）。公開用プレビューリポジトリURLは `https://github.com/hann451/local-academic-pdf-translator-preview` です（Private期間中は読み取り権限を持つGitHubアカウントが必要です）。

## 1. 検証済み構成と要件の範囲

記録上の実機検証例（最低要件ではありません）:

- Windows 11 Education x64 (`10.0.26200`, Build 26200)
- AMD Ryzen 5 5600X、RAM 32,654 MB、AMD Radeon RX 7600（専用VRAM 8,176 MBとの実機記録）
- CPython 3.13.13、uv 0.11.15
- Node.js 22.20.0、npm 10.9.3
- llama.cpp `b12167` Vulkanビルド、PLaMo 3 Translate 2B GGUF Q4_K_M

Pythonのバージョン制約は [`pyproject.toml`](../pyproject.toml) の `>=3.12,<3.14` です。Node.js/npmの最低対応版はこのプロジェクトの `package.json` に宣言されていません。記録済みのNode.js/npmは検証例であり最低版ではありません。CPU・RAM・GPU・VRAM・空きディスク容量の最低要件、他GPU・CPU・Windows版での実機検証状況は未確定です。RX 7600の記録を全GPU対応の根拠にしないでください。

## 2. 前提ツールと確認

PowerShellを使用します。必要なツールの公式配布・導入説明:

- [Git for Windows](https://git-scm.com/download/win) — リポジトリをGitで取得する場合
- [Python for Windows](https://www.python.org/downloads/windows/) — CPython 3.12または3.13
- [uvのWindows導入手順](https://docs.astral.sh/uv/getting-started/installation/) — Python依存と仮想環境の管理
- [Node.js](https://nodejs.org/en/download/) — npmを含む。最低対応版は未宣言のため、記録上の検証例はNode.js 22.20.0 / npm 10.9.3

導入後、PowerShellで確認します。

```powershell
git --version
python --version
uv --version
node --version
npm --version
```

`python --version` が3.12系または3.13系であることを確認してください。uvが使用するPythonは `uv run python --version` でも確認できます。必要なツールを揃えたことは、最低要件を満たす保証ではありません。

## 3. ソース取得と作業ディレクトリ（通常の一本道）

### 3.1 標準導入手順（`main` ブランチの取得）

空のフォルダーを用意し、`main` ブランチをクローンしてプロジェクトルートへ移動します（Gitの通常の認証方法を使い、資格情報をURLやコマンドへ埋め込まないでください）。

```powershell
git clone --branch main --single-branch https://github.com/hann451/local-academic-pdf-translator-preview.git PaperBridgeLocal
Set-Location .\PaperBridgeLocal
git rev-parse HEAD
```

取得後は `git rev-parse HEAD` で対象コミットを確認してください。以降のバックエンドコマンドはすべてプロジェクトルートで、フロントエンドコマンドは `frontend` ディレクトリで実行します。日本語や空白を含むパスでは、実行ファイル・ファイルパスを引用符で囲んでください。

### 3.2 （参考）開発版での過去検証履歴について

本リポジトリは公開用に切り出したソーススナップショットであり、開発用Privateリポジトリの過去コミット履歴は含みません（そのため、過去のコミットSHAを指定する `git checkout` 手順は本リポジトリでは使用しません）。2026-11-08に同一PC上の分離導入確認および2ページ合成PDFのCPU実モデル試験を実施した開発版スナップショット（当時の記録ID: `6a55d6e31637f59f7b74b26d2d00efbc38977151`）と、その後に統合された診断プローブ修正・アプリ内ライセンス案内・文書更新の詳細は[公開向け検証状況](VERIFICATION.md)を参照してください（診断修正後の実機モデル再試験は実施していません）。

## 4. Python環境

プロジェクトルートで、既存の `uv.lock` と整合させて開発用依存も同期します（`--python 3.13` は対応範囲 `>=3.12,<3.14` 内のCPython 3.13を明示指定する例です）。

```powershell
uv sync --python 3.13 --locked --extra dev
uv run python --version
```

`--locked` はロック内容を更新せずに同期する指定です。Python依存の初回取得には、パッケージ配布元へのインターネット通信が発生する場合があります。BabelDOCの大容量アセットを自動取得する設定を有効にする手順はありません。既定は `PAPERBRIDGE_ALLOW_AUTO_ASSET_DOWNLOAD=false` です。

2026-11-06の従来記録では、BabelDOC v0.6.4の初回実行時にONNXモデルとフォント群、合計約399.93 MBが自動取得され得るため遮断していました（ONNX約71.84 MB、フォント群約328.09 MB）。これは当時の記録であり、今回の取得・再検証値ではありません。

## 5. フロントエンド依存とビルド

プロジェクトルートから `frontend` に移動し、ロックファイルに基づく `npm ci` を使います。

```powershell
Push-Location .\frontend
npm ci
npm run build
Pop-Location
```

`npm ci` は `package-lock.json` に従って依存を導入し、通常は `node_modules` を作り直します。初回の依存取得にはインターネット通信が発生する場合があります。`npm run build` はTypeScriptビルド後に `frontend\dist` を生成し、UIとローカルPDF.js workerをまとめます。FastAPIがUIを静的配信するには `frontend\dist\index.html` が必要です。これがない場合、FastAPIのAPIプロセスが起動しても、ビルド済みUIの配信は有効になりません。

テストは起動には必須ではありません。実行する場合のコマンドは次のとおりです。

```powershell
# プロジェクトルート
uv run python scripts/generate_test_pdfs.py
uv run pytest

# frontend ディレクトリで実行
Push-Location .\frontend
npm test
Pop-Location
```

作業ブランチで2026-11-07に記録された結果は、Python全体テスト69 passed・実機LLM/GPUテスト7 skipped、フロントエンド15 passed（3ファイル）と本番ビルド成功です。翌11-08には、保存済みコミット `6a55d6e31637f59f7b74b26d2d00efbc38977151` を新しい同一PC上の分離環境で再検証し、さらに診断プローブ修正後のオフラインPythonテストで **73 passed・7 skipped** を確認しました。環境・警告・API/UI確認結果は[公開向け検証状況](VERIFICATION.md)に分けて記録しています。いずれも通常のテスト実行はテスト用mockを含むmodel-free確認であり、実機モデル検証やクリーンWindows／別PCの結果ではありません。

## 6. モデルなしでUIとローカルAPIを確認する

モデルのインストールや起動は、UIを表示して診断画面などを確認するための前提ではありません。プロジェクトルートで以下を実行し、ターミナルを開いたままにします。

```powershell
uv run uvicorn backend.api.app:app --host 127.0.0.1 --port 8080
```

ブラウザーで <http://127.0.0.1:8080/> を開きます。コード上、FastAPI起動時にセッショントークンを生成し、UIは `GET /api/auth/session` で取得します。UIはトークンをメモリ内にのみ保持し、API要求の `X-PaperBridge-Token` ヘッダーに付けます。通常のUI利用では利用者がトークンを設定・表示・コピーする操作は不要です。ページを再読み込みすると、UIはセッションを再取得します。APIを直接呼び出す場合は、先に同エンドポイントでセッション情報を取得し、保護されたAPIに `X-PaperBridge-Token` ヘッダーを付けてください。トークンをURLに含めないでください。

初期表示時の診断では推論サーバーをプローブせず、画面4「用語集管理・システム診断」の「🔌 推論サーバ疎通確認 (`probe_llama=true`)」ボタンで明示的に接続確認できます（疎通確認は `GET /v1/models` の軽量確認のみを行い、接続確認のために翻訳リクエストは送信しません）。モデル未導入の状態で画面・診断API等を確認することと、実機翻訳実行は別です。

- **モデル未接続時の対処とモック試用の選択方法:** 画面1の初期状態は実機推論（モック無効）です。推論サーバー未起動のまま実機推論で開始すると、接続エラーにより画面3でジョブが中断（`interrupted`）します（推論サーバーを起動して画面4で疎通確認した後に、画面3の「▶ 中断箇所からジョブを再開 (Resume)」で再開できます）。モデル未接続でUIの一連の画面遷移だけを試す場合は、画面1の「推論エンジン」で決定論的なモック推論（Mock Translator）を明示選択してから開始してください。モックはテスト用の疑似翻訳であり、PLaMoによる実翻訳やその品質を示しません（成果物には `is_mock_translation: true` が記録されます）。
- **ブラウザー利用時の推奨URLとOrigin制約:** バックエンドの `allowed_origins` は既定で `http://127.0.0.1:8080`、`http://localhost:8080`、`http://127.0.0.1:5173`、`http://localhost:5173` のみを許可しています。ブラウザーからは推奨URL **<http://127.0.0.1:8080/>**（またはVite開発サーバー併用時の `5173`）を使用してください。Uvicornの `--port` だけを別ポートへ変更してブラウザーからアクセスした場合、初期画面の `GET` は成功しても、ジョブ作成などの `POST` / `PUT` / `DELETE` が `Origin` 検証で拒否（HTTP `403`）され得ます（過去の分離検証での `127.0.0.1:18081` 確認はHTTP疎通とホーム画面表示の確認であり、ブラウザーからの全操作確認ではありません）。

フロントエンド開発サーバーが必要な場合だけ、上記FastAPIターミナルに加えて別ターミナルで起動します。

```powershell
# プロジェクトルートから実行
Push-Location .\frontend
npm run dev
```

Viteは `127.0.0.1:5173` にバインドし、`/api` を `127.0.0.1:8080` にプロキシします。<http://127.0.0.1:5173/> を開きます。停止時は各サーバーを起動したターミナルでCtrl+Cを押し、`Pop-Location` で元のディレクトリに戻れます。

## 7. 実機モデルと推論サーバー

翻訳には、バックエンドとは別プロセスのllama.cpp推論サーバーと、別ファイルのモデル本体の両方が必要です。検証記録上の構成はPLaMo 3 Translate 2B GGUF Q4_K_MとWindows Vulkan版llama.cpp `b12167` です。バイナリ・GGUFモデルは `.gitignore` 対象で、原則としてGitへ含めません。

### 7.1 過去の取得記録（現在の配布状況は未確認）

次の出典・版・ハッシュ・取得日時は、2026-11-06の取得記録に基づきます。今回、外部サイトへの通信や現在の配布状況の再確認は行っていません。取得時の情報と現時点の公開物・ライセンス条件が同一とは限らないため、実際に取得する時点で公式ページとライセンスを確認してください。

| 配布物 | 記録された取得元・版 | ファイル / 記録されたSHA-256 | リポジトリ内配置 |
|---|---|---|---|
| 推論バイナリ | [llama.cpp GitHub Release b12167](https://github.com/ggml-org/llama.cpp/releases/download/b12167/llama-b12167-bin-win-vulkan-x64.zip)、version `0.9.0-dev`、commit `240d68fcfe0c2a1f298c8e18ca181368b1914259` | `llama-b12167-bin-win-vulkan-x64.zip` / `f99b2fc83d93f4a36778e5966600c8a4a36e8a3a6660521601317c02f974ca9d`（当時のGitHub Release API digestと照合した記録） | 展開後の `tools\llama.cpp\b12167-vulkan-x64\llama-server.exe` |
| 翻訳モデル | [pfnet/plamo-3-translate-2b-GGUF](https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF/resolve/232fbd7916519edecddfdae0beae7e76fe70453f/plamo-3-translate-2b-Q4_K_M.gguf)、revision `232fbd7916519edecddfdae0beae7e76fe70453f` | `plamo-3-translate-2b-Q4_K_M.gguf` / `f36d0813dd95573a852ab5d76e374c543632d9e292929e922bcdbf7e53b494e0`（当時のHugging Face Git LFS oidと照合した記録） | `models\plamo-3-translate-2b-GGUF\plamo-3-translate-2b-Q4_K_M.gguf` |

記録上の展開後主要バイナリSHA-256は `llama-server.exe` が `5b5ec3b6cfec735720d1f1b929c40207c245bd5052b275afd0eaade214c34d36`、`ggml-vulkan.dll` が `d9c8e3c6d00bc4581b3011bffa39fe19960911eac78a8062a2fec9319020d523` です。取得日時の記録はzipが `2026-11-06T19:23:09+09:00`〜`19:23:10+09:00`、GGUFが `2026-11-06T19:23:48+09:00`〜`19:24:16+09:00` です。

上記の当時の取得サイズは、zipが37,685,589 bytes、GGUFが1,807,199,392 bytesでした。合計約1.845 GBです。サイズ・ハッシュ・日時は記録値であり、現在の配布状況を保証しません。PowerShellで手元のファイルを照合する場合（zipをプロジェクトルートへ保存した場合）:

```powershell
Get-FileHash -LiteralPath (Read-Host "ZIP file path") -Algorithm SHA256
Get-FileHash .\models\plamo-3-translate-2b-GGUF\plamo-3-translate-2b-Q4_K_M.gguf -Algorithm SHA256
```

zipをダウンロードした場所や展開方法は任意ですが、展開後に `llama-server.exe` が上記配置になるようにしてください。モデルは公式取得元からファイル名を変えずに入手し、上記のモデル配置先へ置きます。PowerShellで新しいディレクトリを作る場合は、例えば次のようにします。

```powershell
New-Item -ItemType Directory -Force .\tools\llama.cpp\b12167-vulkan-x64
New-Item -ItemType Directory -Force .\models\plamo-3-translate-2b-GGUF
```

モデル取得記録（2026-11-06）の対象revisionは `232fbd7916519edecddfdae0beae7e76fe70453f` です。そのrevisionの[モデルカード](https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF/resolve/232fbd7916519edecddfdae0beae7e76fe70453f/README.md)および現行 `main` のモデルカードは、PLaMoコミュニティライセンス契約（公式条文URL: [日本語版](https://plamo.preferredai.jp/info/plamo-community-license-ja) / [英語版](https://plamo.preferredai.jp/info/plamo-community-license-en)）を案内しています。2026-11-07 / 2026-11-08 時点の公開条文確認記録では、非商用・学術研究は無償（登録不要）、商用利用は事前登録かつ利用者または関連会社の年間売上高等が10億円以下であること等を条件としていました。なお、外部Webページ上の契約条文は改定され得るため、以下の **PLaMo 3 Translate** に関して2026年11月9日改定ライセンスに基づきPFNから受領した回答要旨と、公開条文の確認結果や過去のモデル取得時点（2026-11-06）の条件記録とは混同せず区別します（改定後公開条文そのものの再照合記録ではなく、また当時の過去版条文そのものはリポジトリ履歴からは確定できないため、実際に取得・利用する時点で公式条文を確認してください）。

- **PLaMo 3 Translate の非同梱・ライセンスの区別と契約条件の遵守（Built with PLaMo）:** 本ツールは翻訳モデルとして **PLaMo 3 Translate**（`pfnet/plamo-3-translate-2b-GGUF`）の使用を想定しており（**Built with PLaMo**）、Preferred Networks（PFN）の公式製品ではありません。本リポジトリはソースコードのみを提供し、**PLaMo 3 Translate** モデル本体・派生モデル・翻訳生成物を同梱・再配布しません。ツール本体のライセンス（`AGPL-3.0-only`）とモデルのライセンス（PLaMoコミュニティライセンス契約）は別であり、モデル非同梱であっても、利用者は公式リンク等で案内されるモデルの契約条件を確認・遵守する必要があります（`Built with PLaMo` の表示場所については、READMEやUIへの記載で足りる旨の回答に基づき、READMEおよびアプリ画面1・画面4に記載しています。外部リンクはクリック時のみ開き、起動時に自動通信しません）。
- **PLaMo 3 Translate による翻訳PDFの提供・公表に関する条件と注意事項（PFN回答に基づく案内）:** 書籍・論文などの独立した長文公表物に関する場合を除き、通常の翻訳PDFの提供・公表には、一律の出自表示や契約コピー・説明ファイルの添付は不要です。**ただし、書籍・論文などの独立した長文公表物の全部または実質的部分として公表する場合は、PLaMoを翻訳に利用した旨の明示が必要**であり、この明示はPDFとは別のファイルではなく**PDF内に含める必要があり、PDF末尾の説明ページでも対応可能**です。本ツールは出力PDFへ説明ページや出自表示を自動追加しないため、該当する公表を行う場合は利用者自身でPDF内に明示を含めてください。なお、モデルライセンスおよびPFNの回答は、翻訳元となる原著作物（元の論文・書籍等）の翻訳・配布・公表に関する著作権上の許諾まで与えるものではありません。llama.cppの記録上のライセンスはMITです。

### 7.2 llama-server起動

プロジェクトルートで新しいPowerShellターミナルを開き、実機推論を利用する間はこのターミナルを開いたままにします。引用符を付けた相対パスにより、プロジェクトパスに空白があっても実行できます。

```powershell
& ".\tools\llama.cpp\b12167-vulkan-x64\llama-server.exe" `
  -m ".\models\plamo-3-translate-2b-GGUF\plamo-3-translate-2b-Q4_K_M.gguf" `
  --alias "plamo-3-translate" `
  --host 127.0.0.1 `
  --port 8000 `
  -c 8192 `
  -np 1 `
  -ngl 99 `
  --jinja `
  --no-prefill-assistant
```

これは記録されたVulkan実機構成（GPUレイヤーオフロード `-ngl 99`）の起動例で、全GPUでの動作を保証しません。推論サーバーは `127.0.0.1:8000`、バックエンドの既定接続先は `http://127.0.0.1:8000/v1` です。推論サーバーのポートを変える場合は、バックエンド側の `PAPERBRIDGE_LLAMA_BASE_URL` も同じ接続先に合わせます。モデルファイルがない・ロードできない場合、またはサーバーが停止中は実機翻訳できません。画面が開くことだけでは接続済みの証拠にならず、画面4の診断で明示的に接続確認してください。

**CPU推論設定での起動例（2026-11-08 合成PDF試験時の構成）:**

2026-11-08の分離実モデル試験（コミット `6a55d6e31637f59f7b74b26d2d00efbc38977151` 時点、2ページ・9ブロックの合成PDF）では、GPUオフロードを無効化する `-ngl 0` とスレッド指定 `-t 4 -tb 4` を付けて起動しました（既定ポート `8000` を使う場合の例、当時の分離試験では `--port 18082` と `PAPERBRIDGE_LLAMA_BASE_URL="http://127.0.0.1:18082/v1"` を使用）:

```powershell
& ".\tools\llama.cpp\b12167-vulkan-x64\llama-server.exe" `
  -m ".\models\plamo-3-translate-2b-GGUF\plamo-3-translate-2b-Q4_K_M.gguf" `
  --alias "plamo-3-translate" `
  --host 127.0.0.1 `
  --port 8000 `
  -c 4096 `
  -np 1 `
  -ngl 0 `
  -t 4 `
  -tb 4 `
  --jinja `
  --no-prefill-assistant
```

- **検証条件と制限:** この構成は同一PC上で2ページの合成PDFに対して確認した1例であり、一般のCPU環境での互換性、最低CPU/RAM要件、長大文書での実用速度、または翻訳品質を保証しません。コンテキスト長 `-c 4096` はこの短文合成試験での設定値であり、バックエンド既定の `PAPERBRIDGE_CONTEXT_LENGTH=8192` と異なる場合はプロンプト長の上限に注意してください。
- **診断表示と provenance の扱い:** `llama-server` のHTTP API応答には実際の演算バックエンド実測値が含まれないため、アプリ側の診断・検証レポートでは計算バックエンドを `unverified_from_api` とし、ローカルの待受PIDから確認できた範囲で起動引数設定（`startup_arg: n_gpu_layers=0` 等）や `ggml-vulkan.dll` のロード確認のみを併記します（Vulkan版バイナリは `-ngl 0` 指定時でも `ggml-vulkan.dll` をロードします）。また、診断修正（`9da0d58` 以降）後の実機モデル再試験は未実施です。ローカルプロセス照会の各外部コマンドには個別の短いタイムアウト（2.0秒）を設定していますが、OSのプロセス生成やパイプ処理等を含む全体の処理時間上限を厳密に保証するものではありません。

## 8. フォント検出

[`backend/config.py`](../backend/config.py) はWindowsの `%SystemRoot%\Fonts` を検索します。明朝優先で `NotoSerifJP-VF.ttf`、`NotoSansJP-VF.ttf`、`BIZ-UDMinchoM.ttc`、`YuGothR.ttc`、`msmincho.ttc`、`msgothic.ttc` を候補にし、ゴシック優先の候補も別に定義しています。特定のNotoフォントがWindowsに必ず存在するとは限りません。診断画面では検出可否とパスを表示します。

現行コードに利用者向けのフォントパス環境変数設定はありません（`.env.example` の `PAPERBRIDGE_FONT_*_PATH` は `AppConfig` で読み込まれていません）。`.env.example` 自体を読み込む処理も確認できていないため、記載値が自動適用されるとはみなさないでください。候補フォントが見つからない場合は診断に未検出として表示され、組版処理はPyMuPDFの組み込み `japan-s` フォントへフォールバックします。フォールバック時のPDF表示・品質はこの文書では保証しません。このガイドではフォントの取得・導入・再配布を案内しません。フォントのライセンスや配布条件は各フォントの公式情報で確認してください。

## 9. 通信境界とポート

| 既定値 | 役割 | 設定上の注意 |
|---|---|---|
| `127.0.0.1:8080` | FastAPI APIと、ビルド済み `frontend\dist` がある場合のUI | ブラウザー利用時は原則 `127.0.0.1:8080`（または `localhost:8080`）を使用します。`backend/config.py` の `allowed_origins` は既定で `8080` と `5173` のみを許可するため、Uvicornの `--port` だけを別ポートへ変更するとブラウザーからの `POST` / `PUT` / `DELETE` が `403 Invalid origin` で拒否され得ます。 |
| `127.0.0.1:5173` | 任意のVite開発UI | `vite.config.ts` でホスト・ポート・厳格なポート利用・API proxy先（`127.0.0.1:8080`）が定義されています。別ポートへ変える場合はVite設定および `allowed_origins` との整合が必要です。 |
| `127.0.0.1:8000` | llama.cpp推論API。ブラウザー用画面ではない | llama-server引数と `PAPERBRIDGE_LLAMA_BASE_URL` を合わせます。 |

バックエンド設定はプロセス環境変数から読み込みますが、`.env.example` を自動で読み込む実装は確認できていません。推論サーバーのポートを変える例として、バックエンドを起動する同じPowerShellターミナルで環境変数を設定します。

```powershell
$env:PAPERBRIDGE_LLAMA_BASE_URL = "http://127.0.0.1:8001/v1"
uv run uvicorn backend.api.app:app --host 127.0.0.1 --port 8080
```

この場合はllama-serverも `--port 8001` で起動します。環境変数はそのPowerShellプロセスに適用されます。なお、FastAPI側の待受ポート（`8080`）だけをUvicorn引数で別ポートへ変更した場合、ブラウザーからの画面表示（`GET`）は通っても、`Origin` ヘッダーを伴う `POST` 等は `allowed_origins` 制約で拒否され得るため、ブラウザー操作では原則 `8080` を維持してください。アプリのコードと既定設定は、APIおよび推論接続をループバックに限定する方針です。外部通信をパケット監視等で完全監査したという意味ではありません。依存パッケージ、ソース、モデル、バイナリ取得時にはインターネット通信が発生し得ます。通常の翻訳要求は既定ではバックエンドからローカル推論サーバーへ送られます。自動外部フォールバックは既定で無効です。

## 10. 通常の停止方法・起動エラー・安全な確認

- **通常の停止方法（起動したターミナルでの `Ctrl+C`）:**
  - FastAPI（`127.0.0.1:8080`）、推論サーバー（`127.0.0.1:8000`）、または開発用Vite（`127.0.0.1:5173`）を停止するときは、**それぞれを起動したPowerShellターミナル上で `Ctrl+C` を押して通常停止**してください。停止したプロセスは自動では再起動しません。
  - **一括強制停止・DB削除の禁止:** `Stop-Process -Name python,llama-server` などの名前指定でプロセスを一括停止しないでください（他の用途で動作中のPythonや推論サーバーを誤って強制終了する原因になります）。また、`%LOCALAPPDATA%\PaperBridgeLocal` 配下の利用者データベース（SQLite）や過去ジョブ成果物を削除しないでください。
- **`ERR_CONNECTION_REFUSED`**: 指定したアドレス・ポートで待受がない場合に起こります。まず正しいURL（ブラウザー利用時は推奨URL `http://127.0.0.1:8080/`）か、対応するサーバーターミナルがまだ起動中かを確認してください。診断・テスト終了後にプロセスが停止している場合は、該当の起動コマンドを改めて実行します。
- **どのポートが待受中か確認する**（停止はしない）:

  ```powershell
  Get-NetTCPConnection -State Listen -LocalPort 5173,8080,8000 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress,LocalPort,OwningProcess
  ```

  所有PIDを確認する場合は `Get-Process -Id <OwningProcess>` を使います。所有者や用途を確認せず、無関係なプロセスを終了しないでください。自分が起動したサーバーは、それぞれのターミナルでCtrl+Cを押して停止します。
- **8080で画面が出ない**: FastAPIが起動しているか、`npm run build` が完了して `frontend\dist\index.html` があるかを確認します。
- **5173で画面が出ない**: 開発用途でViteを起動したかを確認します。Viteは通常利用に必須ではありません。
- **8000にブラウザー画面がない**: 8000は推論APIであり、操作画面ではありません。llama-serverの起動状態と画面4の診断結果（「推論サーバ疎通確認」）を確認します。
- **画面は出るが翻訳できない（`interrupted` になる）**: UI表示と実機推論サーバーの接続は別です。実機翻訳にはアプリと `llama-server` の両方が必要です。モデル配置、`llama-server` のロード完了、バックエンドの接続先・ポートを確認し、画面4で疎通確認（翻訳リクエストは送信しません）を行ってから画面3で再開してください。
- ファイアウォールや認証を無効にして回避しないでください。既存サービスを止める前にPIDと用途を特定してください。

## 11. 実機統合テスト（任意）

実機翻訳テストを行う場合に限り、llama-serverを起動した上でプロジェクトルートから実行します。これは新規環境で今回実施した確認ではありません。

```powershell
uv run pytest --run-real-llm
```

通常の `uv run pytest` は、実機LLMを使わないテストを対象とし、オプトインの実機テストは実行しません。過去の件数・結果はその時点の記録であり、新規環境での成功を保証しません。
