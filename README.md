# local-academic-pdf-translator-preview

> **名称に関する注記:** 本公開プレビュー版リポジトリの名称は `local-academic-pdf-translator-preview`（基本プロジェクト名: `local-academic-pdf-translator`）です。UI表示、パッケージ名（`paperbridge-local`）、環境変数（`PAPERBRIDGE_*`）、ローカル保存先（`%LOCALAPPDATA%\PaperBridgeLocal`）、認証ヘッダー（`X-PaperBridge-Token`）等に残る `PaperBridge Local` / `PaperBridge` は開発中の内部仮称です。学術論文翻訳・閲覧分野には同名の別プロジェクトが複数存在しますが、本リポジトリとは無関係です（商標・法的安全性の確認は未完了です。[公開向け制約](docs/PUBLIC_LIMITATIONS.md)を参照）。

Windows向けのローカル学術論文PDF翻訳アプリ（**Windows向け・ソース導入型プレビュー版**）です。原本PDFのSHA-256不変性を検査し、数式・図・参照番号の保持、検証失敗の警告、ブラウザー上での確認・手動修正を重視します。PDF.jsを使う4画面のReact UIと、ローカルFastAPIバックエンドを備えます。

## 初版の位置づけ・同梱範囲・未対応事項

- **配布形態（ソース導入型プレビュー）:** 本リポジトリは開発履歴を含まない公開用ソーススナップショットを提供します。ワンクリックインストーラーやビルド済み配布物は未対応です。
- **非同梱物（別途取得が必要）:** 翻訳モデル本体（PLaMo 3 Translate GGUF）、推論サーバー実行ファイル（`llama.cpp`）、利用者文書（PDF）、およびローカルSQLiteデータベースは同梱していません。**実際の翻訳には、本アプリ（FastAPI + UI）と推論サーバー（`llama-server`）の両方を起動する必要があります。**
- **未対応・未検証の範囲:**
  - 文字レイヤーのないスキャンPDFのOCR、暗号化・破損PDFの翻訳は未対応です。
  - 段・ページ境界の文脈補助はプロンプトへの近傍文脈付与のみであり、複数ブロックを結合する**連結翻訳（訳文の結合・再配分）は未実装**です。
  - 記録された検証例（Windows 11 x64、CPython 3.13.13、uv 0.11.15、Node.js 22.20.0 / npm 10.9.3、AMD Radeon RX 7600、llama.cpp Vulkan版）は1つの実機確認例であり、**他PC・他GPU・クリーンWindows環境での動作保証や最低ハードウェア要件の確定は未検証**です（Python許容範囲は `pyproject.toml` 上 `>=3.12,<3.14`）。詳細は[公開向け制約](docs/PUBLIC_LIMITATIONS.md)と[公開向け検証状況](docs/VERIFICATION.md)を参照してください。

## Windowsで始める（導入・起動・停止の一本道）

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
- **モデルの非同梱と独立したライセンス:** 本リポジトリはPLaMoモデル本体・派生モデル・翻訳生成物を同梱・再配布しません。モデル自体には本体のAGPLが自動適用されず、Preferred Networksが定めるPLaMoコミュニティライセンス契約（公式条文URL: [日本語版](https://plamo.preferredai.jp/info/plamo-community-license-ja) / [英語版](https://plamo.preferredai.jp/info/plamo-community-license-en)、条文確認日: 2026-11-08、初回確認日: 2026-11-07）が別途適用されます（検証済みモデルrevision `232fbd7916519edecddfdae0beae7e76fe70453f` と現行 `main` のモデルカード内のライセンス説明文は一致していますが、外部Webページ上の契約本則の過去版自体はリポジトリ履歴からは確定できません）。
- **第4条・第5条の確認対象と注意事項:**
  - 公式条文の第4条（再配布及び表示義務）は「本モデル等（派生モデルやその生成物を含む）」を第三者に提供する場合の条件として、(i) 本契約のコピーの提供と遵守、(ii) 関連ウェブサイト・UI・ドキュメント等への **`Built with PLaMo`** の明示、(iii) 作成したAIモデル名への `PLaMo` の包含を定めています。また第5条（生成物の利用）は、生成物を公表する場合の出自明示や、生成物で学習したモデルへの同契約の適用を定めています。
  - 本アプリは設定・診断画面（画面1・画面4）および本文書に `Built with PLaMo` と公式リンクを表示していますが（リンクは利用者がクリックしたときのみ開き、起動時に自動通信しません）、出力PDFへの透かし・出自表示や契約コピーの自動添付は行っていません。
  - 開発者は配布元へ問い合わせを行ったうえで公開準備を進めていますが、本プロジェクトとして「公式に個別承認された」「無条件に利用可能」「表示義務はない」とは主張せず、またアプリ内・文書内の案内表示だけでライセンス上の全条件への対応が完了するとも表現しません。利用者は自身の利用形態（商用・非商用、翻訳生成物の第三者提供・公表等）に応じて公式ライセンス条文を確認してください（詳細は[公開向け制約](docs/PUBLIC_LIMITATIONS.md)を参照）。

## ドキュメント

- [Windows環境セットアップ手順](docs/SETUP_WINDOWS.md)
- [全体仕様書](docs/SPEC.md)
- [アーキテクチャ設計](docs/ARCHITECTURE.md)
- [API契約・座標・認証仕様書](docs/API_CONTRACT.md)
- [公開向け検証状況](docs/VERIFICATION.md)
- [公開向け制約](docs/PUBLIC_LIMITATIONS.md)
- [第三者ライセンス通知](THIRD_PARTY_NOTICES.md)
- ADR: [PDFエンジン](docs/adr/0001-pdf-engine-selection.md)、[PLaMo推論](docs/adr/0002-plamo-inference-and-pfml3.md)、[数式保護](docs/adr/0003-formula-preservation-and-validation.md)
