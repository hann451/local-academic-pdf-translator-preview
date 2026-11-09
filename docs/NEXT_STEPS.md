# Next steps

## 公開前チェックリスト（Public切替・初版公開に向けた区分） — 2026-11-08

公開（GitHubリポジトリのPublic切替または `main` リリース統合）にあたっては、**A. 公開を妨げる未解決・要確認項目（ブロッカー）** と **B. 初版の既知制限として明示して残す項目** を区別して管理する。未確認事項を完了扱いにしないこと。

### A. 公開を妨げる項目（Public切替前に解決・判断・外部確認が必要な事項）

1. **名称・商標の確認（未完了 / 追加の外部調査が必要）**
   - 仮称「PaperBridge」は既存の学術論文関連プロジェクト名と重複しており、商標・既存サービス名との衝突調査および改称の要否判断が未完了（[`README.md`](../README.md)、[`PUBLIC_LIMITATIONS.md`](PUBLIC_LIMITATIONS.md) に注意書きあり）。公開前に最終名称を決定するか、追加の外部調査・法務確認を行う必要がある。
2. **ライセンスと第三者通知の最終監査（一部記録済み・網羅監査は未完了 / 追加確認が必要）**
   - 自作コードは `AGPL-3.0-only`（[`LICENSE`](../LICENSE)、[`COPYRIGHT.md`](../COPYRIGHT.md)）。
   - 主要な直接依存（PyMuPDF 1.28.2のAGPL-3.0等）の条文は [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) に収録済みだが、全推移的依存（transitive dependencies）の網羅的な法務監査は未完了であり、公開形態に応じた最終確認が必要。
3. **モデル利用条件の明示と確認（同梱なし・過去revision取得時点条文は未確認）**
   - PLaMo翻訳モデル（GGUF）および `llama.cpp` バイナリはリポジトリに同梱せず、モデルに本体のAGPLは自動適用されない。
   - 検証記録のモデルrevision `232fbd7916519edecddfdae0beae7e76fe70453f` のカードは [PLaMo Community License](https://plamo.preferredai.jp/info/plamo-community-license-en) を案内している（2026-11-07時点の現行ページ確認済み）。ただし、当該revision取得当時に適用された過去条文そのものは未確認であり、利用者が各自の取得時点で公式条文を確認する前提を維持する。
4. **公開対象データの衛生確認（コミットごとの継続ゲート / Public切替前に最終再点検が必要）**
   - 実論文PDF、利用者DB・ジョブ履歴、GGUFモデル、推論バイナリ（`llama-server.exe` 等）、認証情報、個人環境パス、未追跡のローカル作業記録を公開ブランチに含めないこと。
   - Public切替および `main` 統合の直前に、`git ls-files` とコミット履歴全体を再点検する必要がある（現時点では作業ブランチ上での差分確認のみ）。
5. **導入・起動手順とブランチ整合（公開用スナップショット向けに整理済み）**
   - [`README.md`](../README.md) と [`SETUP_WINDOWS.md`](SETUP_WINDOWS.md) にて、公開用プレビューリポジトリ（`local-academic-pdf-translator-preview`）の `main` ブランチからの導入手順を一本化し、GPU起動例（`-ngl 99`）とCPU起動例（`-ngl 0 -t 4 -tb 4`）を区別して記載済み（2026-11-08の分離検証・合成PDF実モデル試験を行った開発版コミット `6a55d6e31637f59f7b74b26d2d00efbc38977151` の結果は開発版の検証履歴として [`VERIFICATION.md`](VERIFICATION.md) に保持）。
6. **CI結果と実機再検証の範囲確認（オフライン回帰は完了 / CI・実機再試験は未完了）**
   - 診断プローブ修正後のローカル通常Pythonテスト（**73 passed, 7 skipped**）はモックで完了しているが、**診断修正後の実機モデル再試験は未実施**。
   - 最新コミットに対するGitHub Actions CIの完了確認、および必要に応じた実機再試験の要否判断が公開前に残っている。

### B. 初版の既知制限（機能仕様・検証範囲の制約として明示して残す事項）

以下は初版で解決済みと主張せず、[`PUBLIC_LIMITATIONS.md`](PUBLIC_LIMITATIONS.md) および [`VERIFICATION.md`](VERIFICATION.md) に明記して維持する制限事項である：

- **翻訳・レイアウト機能上の制限:**
  - 文字レイヤーのないスキャンPDF（OCR非対応）、暗号化・破損PDFは対象外。
  - 段・ページ境界の文脈継続はPFML3プロンプトへの近傍文脈付与のみであり、**ブロック結合（連結翻訳）や訳文の再配分、UI上での候補採否操作は未実装**。
  - 一般脚注の専用カテゴリは未実装。
  - 図表内テキストや複雑な数式・収まらない訳文では警告や原文保持フォールバックが発生し得る。また、提供されたdual PDFの過去ジョブにおける当時のモデル出力・描画そのものは再現・特定されていない。
- **診断・計算バックエンド記録の制限:**
  - `llama-server` のHTTP APIからは実際の演算バックエンド実測値を取得できないため、`compute_backend` / `llama_compute_backend` は `unverified_from_api` とし、ローカル待受PIDが一意特定できた場合のみ、安全に確認できた起動設定（`startup_arg: n_gpu_layers=...` 等）と、対象PIDの有効なCSV行で確認できた `ggml-vulkan.dll` ロード状態（`dll: ggml-vulkan.dll loaded`）のみを併記する（情報メッセージ・`N/A`・対象PID行なし等はすべて `unverified` 扱い）。
  - ローカルプロセス照会の各外部コマンドに設定したタイムアウト（各2.0秒）は個別コマンドの待機上限であり、OSのプロセス生成等を含む厳密な全処理時間を保証しない。
- **検証環境の制限:**
  - 確認済みの環境は同一Windows 11 PC（Ryzen 5 5600X / Radeon RX 7600 / CPython 3.13 / Node.js 22.20.0 または分離検証時の 24.18.0）に限られ、**クリーンな別Windows環境・別PC・他GPUでの導入確認や、CPU/RAM/VRAM/ディスク・Node.jsの最低要件確定、一般的な翻訳品質保証は未実施**。

---

## Current verification starting point — 2026-11-08

- Historical isolated-verification & synthetic real-model trial baseline:
  `6a55d6e31637f59f7b74b26d2d00efbc38977151` on
  `work/formula-protection-continuation`. The same-PC model-free isolation
  check and the 2-page CPU real-model trial were run from that source snapshot.
- Subsequent commits on `work/formula-protection-continuation` (`b18da47...`,
  `9da0d58...`, and current working-tree updates) record those verification
  results and fix endpoint-specific diagnostic/report provenance. Real-model
  trials have not been rerun after the diagnostic fix.
- The current source-side gap for the reported formula was reproduced and
  corrected: the dual PDF's equation is on physical PDF page 4 of 5, in the
  original panel's left column. A read-only parse now groups its label,
  numerator, denominator, equation number, and bar into one protected block.
  The synthetic visual regression passes with zero differing pixels in the
  formula region. Historical model output and rendering remain unknown; the
  later synthetic trial does not retranslate the user's document.
- High-confidence column/page continuation supplies bounded neighboring
  masked text as prompt context only. Source blocks remain independent
  translation/cache/edit/validation/layout units. This is not joined
  cross-box translation or Japanese output redistribution.
- Further product work to consider: safe cross-box joined translation,
  explicit boundary/placeholder ownership and cache/manual-edit/resume
  behavior, user accept/reject controls, and a dedicated general-footnote
  category. These were not prerequisites for the completed model-free check.
- Existing branch results and the separate same-PC isolation results are
  summarized in [VERIFICATION.md](VERIFICATION.md).

## 分離検証計画と実施結果 — 2026-11-08（完了）

目的は、同じPCの新しい作業場所で保存済みコミットを取得し、依存導入、
テスト、フロントエンドbuild、モデルなしAPI/UI起動を確認すること。
これは同じPCで既存環境を避ける分離検証であり、クリーンWindowsや別PCの
検証ではない。以下の計画を承認後に実行した。実際の件数・環境・警告は
[VERIFICATION.md](VERIFICATION.md)の同日検証記録を参照すること。

### 1. 保存先と分離境界

新しい場所は `%LOCALAPPDATA%\PaperBridge-Isolated\6a55d6e31637f59f7b74b26d2d00efbc38977151`
とする。実行前に同パスが存在しないことを確認し、存在する場合は再利用・
削除せず中止して別名を承認する。

| 用途 | 保存先 |
|---|---|
| 保存済みコミットのソースclone | `<base>\source` |
| 新規Python仮想環境 | `<base>\venv`（`UV_PROJECT_ENVIRONMENT`で指定） |
| Python依存共有キャッシュを避ける専用cache | `<base>\cache\uv` |
| npm依存共有キャッシュを避ける専用cache | `<base>\cache\npm` |
| Python一時データ・bytecode | `<base>\tmp`, `<base>\cache\pycache` |
| Hugging Face / XDG / Torch cache隔離先 | `<base>\cache\huggingface`, `<base>\cache\xdg`, `<base>\cache\torch` |
| アプリ専用ランタイムとSQLite DB | `<base>\runtime` |
| 合成PDF等pytest一時データ | `<base>\pytest-temp` |
| 起動ログ | `<base>\logs` |
| フロント依存とbuild成果物 | 新規clone内の`source\frontend\node_modules`、`source\frontend\dist` |

既存workspaceの`.venv`、`node_modules`、`dist`、利用者DBは参照・再利用
しない。Pythonは既に導入済みのCPython 3.13を選び、uvにはPython自体を
ダウンロードさせない。テストには合成データだけを使い、利用者PDFは使わない。

### 2. 通信・取得・cacheへの影響

- GitHub PrivateリポジトリからHTTPSで指定branchをcloneし、保存済みSHAをcheckout。
  Gitの通常の認証手段を使い、資格情報や環境変数一覧を表示しない。
- `uv.lock`のPython依存取得先は`https://pypi.org/simple`と
  `https://files.pythonhosted.org`。`package-lock.json`のnpm取得先は
  `https://registry.npmjs.org`。npm lifecycle scripts等が追加通信を試みる
  可能性があるため、実際の通信先をこの一覧だけに限定した保証はしない。
- uv/npm/Hugging Face/XDG/Torchのcache、Python bytecode、TEMPは上表の専用
  ディレクトリに設定し、共有cacheは使わない。依存導入時にこれら専用cacheと
  clone配下の`node_modules`、build時に`dist`を新規作成・変更する。
- モデル・llama.cpp・BabelDOC assetは取得しない。
  `PAPERBRIDGE_ALLOW_AUTO_ASSET_DOWNLOAD=false`、
  `PAPERBRIDGE_ALLOW_EXTERNAL_TRANSLATION_HOST=false`を設定し、
  llama接続先も`127.0.0.1:18082`に固定する（推論サーバーは起動しない）。
- 環境変数はこの作業用PowerShellプロセスとその子プロセスだけに設定し、
  ユーザー／システム環境変数やGit認証情報を変更しない。

### 3. 空きポートとloopback限定

API/UIには`127.0.0.1:18081`を使用し、未起動の推論先予約値は
`127.0.0.1:18082`とする。APIは`--host 127.0.0.1`でのみlistenする。
実行直前に両ポートを読み取り確認し、どちらかが使用中なら中止して別ポートを
決め直す。既存の8080、8000、5173や既存プロセスは変更・停止しない。

### 4. 実行コマンド例（今回の確認では同等の手順を実行済み）

次は再現・監査用のコマンド例。今回、保存先や実行環境に合わせて同等の操作を
実行した。再実行時にも、既存の分離先や使用中ポートがあれば中止する。

```powershell
$Commit = "6a55d6e31637f59f7b74b26d2d00efbc38977151"
$Base = Join-Path $env:LOCALAPPDATA "PaperBridge-Isolated\$Commit"
if (Test-Path $Base) { throw "Isolation path already exists; stop and choose a new path." }
$Source = Join-Path $Base "source"
$Venv = Join-Path $Base "venv"
$Runtime = Join-Path $Base "runtime"
$TempRoot = Join-Path $Base "tmp"
$PytestTemp = Join-Path $Base "pytest-temp"
$UvCache = Join-Path $Base "cache\uv"
$NpmCache = Join-Path $Base "cache\npm"
$Logs = Join-Path $Base "logs"
New-Item -ItemType Directory -Force $Base, $Runtime, $TempRoot, $PytestTemp,
  $UvCache, $NpmCache, $Logs, (Join-Path $Base "cache\pycache"),
  (Join-Path $Base "cache\huggingface"), (Join-Path $Base "cache\xdg"),
  (Join-Path $Base "cache\torch") | Out-Null

$Listeners = Get-NetTCPConnection -State Listen -LocalPort 18081,18082 `
  -ErrorAction SilentlyContinue
if (@($Listeners).Count -gt 0) { throw "A planned port is occupied; stop and choose another." }

git clone --branch work/formula-protection-continuation --single-branch `
  https://github.com/hann451/local-academic-pdf-translator.git $Source
if ($LASTEXITCODE -ne 0) { throw "Git clone failed; stop before continuing." }
git -C $Source checkout --detach $Commit
if ($LASTEXITCODE -ne 0) { throw "The saved commit was not available; stop." }
$Head = git -C $Source rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw "Could not read the cloned HEAD; stop." }
if ($Head.Trim() -ne $Commit) { throw "Unexpected commit; stop." }

# Select an already-installed uv-managed Python, not the current workspace venv.
$Python313 = (uv python find --managed-python --no-python-downloads 3.13).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Python313)) {
  throw "CPython 3.13 is not already available; do not install it as part of this check."
}
& $Python313 --version
uv --version
node --version
npm --version

$env:UV_PROJECT_ENVIRONMENT = $Venv
$env:UV_CACHE_DIR = $UvCache
$env:NPM_CONFIG_CACHE = $NpmCache
$env:TEMP = $TempRoot
$env:TMP = $TempRoot
$env:TMPDIR = $TempRoot
$env:PYTHONPYCACHEPREFIX = Join-Path $Base "cache\pycache"
$env:HF_HOME = Join-Path $Base "cache\huggingface"
$env:XDG_CACHE_HOME = Join-Path $Base "cache\xdg"
$env:TORCH_HOME = Join-Path $Base "cache\torch"
$env:PAPERBRIDGE_RUNTIME_DIR = $Runtime
$env:PAPERBRIDGE_HOST = "127.0.0.1"
$env:PAPERBRIDGE_PORT = "18081"
$env:PAPERBRIDGE_LLAMA_BASE_URL = "http://127.0.0.1:18082/v1"
$env:PAPERBRIDGE_ALLOW_EXTERNAL_TRANSLATION_HOST = "false"
$env:PAPERBRIDGE_ALLOW_AUTO_ASSET_DOWNLOAD = "false"
$env:PAPERBRIDGE_TELEMETRY_ENABLED = "false"

# CPython 3.13 must already be installed; do not download Python.
uv sync --project $Source --locked --extra dev --python $Python313 --no-python-downloads
if ($LASTEXITCODE -ne 0) { throw "uv sync failed; stop before continuing." }
Push-Location (Join-Path $Source "frontend")
npm ci --cache $NpmCache --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "npm ci failed; stop before continuing." }
npm test
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "Frontend tests failed; stop before continuing." }
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "Frontend build failed; stop before continuing." }
Pop-Location
Push-Location $Source
uv run --project $Source --locked --no-sync pytest -p no:cacheprovider `
  --basetemp $PytestTemp
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "Python tests failed; do not start the API." }
Pop-Location

# Recheck that 18081/18082 are still unused before starting only the API/UI.
$Listeners = Get-NetTCPConnection -State Listen -LocalPort 18081,18082 `
  -ErrorAction SilentlyContinue
if (@($Listeners).Count -gt 0) { throw "A planned port is occupied; do not start the API." }
$ApiPython = Join-Path $Venv "Scripts\python.exe"
$ApiProcess = Start-Process -FilePath $ApiPython `
  -ArgumentList @("-m", "uvicorn", "backend.api.app:app", "--host",
    "127.0.0.1", "--port", "18081") `
  -WorkingDirectory $Source -PassThru `
  -RedirectStandardOutput (Join-Path $Logs "api.stdout.log") `
  -RedirectStandardError (Join-Path $Logs "api.stderr.log")

$Deadline = (Get-Date).AddSeconds(30)
do {
  Start-Sleep -Seconds 1
  $ApiProcess.Refresh()
  if ($ApiProcess.HasExited) { throw "The isolated API exited; inspect only its dedicated logs." }
  $Listener = @(Get-NetTCPConnection -State Listen -LocalPort 18081 `
    -ErrorAction SilentlyContinue)
} until ($Listener.Count -eq 1 -or (Get-Date) -ge $Deadline)
if ($Listener.Count -ne 1 -or $Listener[0].LocalAddress -ne "127.0.0.1" `
    -or $Listener[0].OwningProcess -ne $ApiProcess.Id) {
  throw "The isolated API listener did not match the started PID and loopback address."
}
```

起動後はlistenerが`127.0.0.1:18081`かつ所有PIDが`$ApiProcess.Id`であることを
確認する。root画面とsession endpointはstatusだけを取得し、response body
（tokenを含む）は表示・保存しない。

```powershell
$HomeResponse = Invoke-WebRequest -UseBasicParsing `
  -Uri "http://127.0.0.1:18081/"
$SessionResponse = Invoke-WebRequest -UseBasicParsing `
  -Uri "http://127.0.0.1:18081/api/auth/session"
if ($HomeResponse.StatusCode -ne 200 -or $SessionResponse.StatusCode -ne 200) {
  throw "The isolated UI/API smoke check failed."
}
Write-Output "home=$($HomeResponse.StatusCode) session=$($SessionResponse.StatusCode)"
Start-Process "http://127.0.0.1:18081/"
```

ブラウザーのNetwork表示でUI、JavaScript/CSS、PDF.js workerの各応答がHTTP 200
であることを確認し、ホーム画面を目視する。DB確認は
`<base>\runtime\db\paperbridge.sqlite3`のみを見る。モデル翻訳は行わない。

### 5. この検証での停止方法

起動したプロセスのPIDを`$ApiProcess.Id`として控え、停止前にそのPIDの
`ExecutablePath`が分離venvのPythonで報告される`sys._base_executable`と一致し、
command lineが今回のUvicornコマンドを示すこと、対象listenerのloopbackアドレス・
port・所有PIDが一致することを確認する。Windowsではvenvのpython launcherを起動しても、
プロセス情報上はbase CPythonのパスとして表示される場合がある。一致した場合に限り
`Stop-Process -Id $ApiProcess.Id`で停止し、18081 listenerが消えたことを確認する。
一致しない・識別できない場合は停止操作をせず、調査してから判断する。
既存8080/8000サービスは触れない。

```powershell
$ApiPid = $ApiProcess.Id
$ExpectedPython = (& $ApiPython -c "import sys; print(sys._base_executable)").Trim()
$Listener = @(Get-NetTCPConnection -State Listen -LocalPort 18081 `
  -ErrorAction SilentlyContinue)
$OwnedProcess = Get-CimInstance Win32_Process `
  -Filter "ProcessId = $ApiPid"
if ($Listener.Count -ne 1 -or $Listener[0].LocalAddress -ne "127.0.0.1" `
    -or $Listener[0].OwningProcess -ne $ApiPid `
    -or $OwnedProcess.ExecutablePath -ne $ExpectedPython `
    -or $OwnedProcess.CommandLine -notmatch `
      ' -m uvicorn backend\.api\.app:app --host 127\.0\.0\.1 --port 18081(?:\s|$)') {
  throw "The listener is not the exact isolated API process; do not stop it."
}
Stop-Process -Id $ApiPid
if (Get-NetTCPConnection -State Listen -LocalPort 18081 `
    -ErrorAction SilentlyContinue) {
  throw "The isolated API port is still listening; investigate without killing other processes."
}
```

### 6. 合格条件

1. cloneされた`HEAD`が保存済みSHAと完全一致する。
2. 新しいvenvへの`uv sync --locked`、専用cacheへの`npm ci`、フロントテストとbuildが成功し、既存環境を参照していない。
3. Python通常テストが終了コード0。件数・skip・warningは実出力から記録し、real-LLM/GPUテストが誤って有効化されていない。
4. 合成PDF等のテスト生成物とアプリSQLite DBが専用ディレクトリ配下にあり、利用者DB・PDFは読み書きされていない。
5. UIと静的assetがHTTP 200で表示され、APIはloopbackの18081のみlistenする。18082に推論サーバーはなく、実機翻訳は試みない。
6. 記録した当該PIDだけを停止でき、18081が解放される。既存サービス・利用者ランタイム・共有cacheに変更がない。

この計画を同じPCで成功させても、クリーンWindows／別PCでの導入・動作、別GPU対応、
実機翻訳、翻訳品質を検証したことにはならない。

### 実施結果

- 保存先の不存在と空きポートを事前確認し、保存済みSHAをcheckout。
  CPython 3.13.13、uv 0.11.15、Node.js 24.18.0、npm 11.16.0を使用した。
- uv依存導入、npm ci、フロントエンド15テスト、本番build、Python全体テスト69件
  が成功。実機LLM/GPUテスト7件はskip、Pythonのdeprecation warningが1件。
- model-free API/UIを`127.0.0.1:18081`で確認し、画面・session API・JS/CSS・PDF.js
  workerがHTTP 200。ブラウザーでホーム画面を確認した。18082は未使用。
- 起動したAPI PIDだけを識別して停止し、両ポートの解放を確認。利用者PDF・翻訳
  ジョブ・実機モデルは使っていない。分離環境と検証生成物は専用パスに保持する。

## 次の製品検討事項

1. 真のクロスボックス連結翻訳が必要なら、領域境界対応、placeholder所有権、
   cache invalidation、手動編集、resume accounting、accept/reject UXを設計・検証する。
2. 一般脚注用分類と候補レビューUIを検討する。現状、一般脚注カテゴリはなく、
   候補metadataを確認するUIもない。
3. 複数のllama-serverが稼働する環境では、検証レポートのengine/backendが別portの
   プロセスを誤認しないよう、endpoint単位のprovenance調査・修正・回帰試験を行う。
4. 今回は合成PDFだけを使った実モデル試験であり、利用者文書や追加の実モデル試験は
   別途明示承認を得る。通常CIはmodel-freeを保つ。

## 実モデルによる合成PDF試験 — 2026-11-08（完了）

- 保存済みコミット`6a55d6e31637f59f7b74b26d2d00efbc38977151`の同PC分離cloneで、
  専用`real-model-test`領域を新規作成。2ページの合成PDFだけを使い、通常英文、
  式(7)付きの英語分数式、二段組の段またぎ継続文を確認した。利用者PDF・既存DB・
  既存ジョブは使っていない。
- 既存モデルとllama.cppを読み取り利用し、推論serverを`127.0.0.1:18082`、
  `-ngl 0 -t 4 -tb 4`で起動。分離APIは`127.0.0.1:18081`。空きRAMは開始時
  11.82 GiBだった。8 GiBはこの試験の開始閾値に限り、製品要件ではない。
- 翻訳前に9ブロックを解析し、通常英文、`RULE_SPATIAL_FRACTION_EQUATION`で保護された
  数式1件、同一ページ段間の高信頼継続候補を確認してから翻訳した。推論serverの
  起動から検証後の停止まで207.6秒、翻訳job自体は8.13秒で完了。5本文ブロックと
  2見出しの計7ブロックが実モデル推論、保護数式1件と
  header/footer 1件、原文fallback 0、retry 0、警告0、validation issue 0、進捗9/9。
- 2ページの日本語PDFを保存して開き、明らかな重なり・ページ欠落がないことを表示確認。
  数式領域は分数線・式番号を含めpixel差0.0、入力SHA-256も不変。段またぎ文は
  context候補として補助し、2ブロックの個別翻訳を確認しただけで、品質向上や連結翻訳を
  主張しない。
- llama-serverの起動ログには4 CPU threadsと7推論記録がある一方、当時のコミット `6a55d6e` で生成された検証レポートは
  engine/backendに既存8000番serverのPID 38816／Vulkan GPUを誤記した。18082の専用
  server PID 39696、起動引数、loopback listener、モデル応答、serverログが当時の実行証跡。
  その後、接続先endpointに対応する単一待受PIDの照合および起動設定・DLL確認の分離修正を行い、モックによる決定論的回帰テスト（通常Pythonスイート73 passed, 7 skipped）を実施済み（診断修正後の実機再試験は未実施）。
- 試験で起動したAPI listener/launcher PID 4564/11928と推論server PID 39696だけを停止し、
  18081/18082解放を確認。既存8000番PID 38816は継続稼働し、利用も変更もしていない。
  実行証跡と生成物は`%LOCALAPPDATA%\PaperBridge-Isolated\<commit>\real-model-test`に保持。
  詳細と制約は[VERIFICATION.md](VERIFICATION.md)を参照。

## 保存前の履歴 — 2026-11-08（旧チェックポイント）

以下は保存前に記録された履歴で、現行の検証開始点ではありません。特に旧HEAD、
旧作業ツリー件数、当時の保存・pushに関する記述を現在の状態と混同しないでください。

### Git checkpoint

- Working branch: `work/formula-protection-continuation`.
- Recorded HEAD and branch base at that earlier checkpoint:
  `20dd5870280310ff8e4b19687323be7be0552f3a`
  (`Remove machine-specific GPU assertion from security test`). The local
  `main` and `origin/main` references and the read-only remote `main` query
  matched this commit at checkpoint time.
- No upstream is configured for the working branch. The read-only remote query
  found no `work/formula-protection-continuation` branch. The proposed future
  destination is `origin/work/formula-protection-continuation`; verify remote
  state again before any push.
- The worktree contains 12 modified tracked files and 7 untracked source,
  test, and documentation files. No files are staged. All 19 are candidates for
  review together so the new implementation tests and handoff records are not
  omitted. Nothing has been committed or pushed.
- A name/size scan found no staged candidate named as a PDF, image, database,
  log, model, archive, environment file, key, or binary; no candidate exceeded
  1 MiB. Automated checks found no user-specific absolute paths or common
  credential patterns. These checks are a screening aid, not a legal or
  exhaustive secret review.

### Implemented work

- Adds conservative column/page-boundary continuation candidates. High-
  confidence neighboring text is PFML3 context only; physical blocks remain
  separate translation, cache, edit, validation, and layout units.
- Recomputes physical-block progress for resume and avoids reinference of
  fallback-original blocks; identical validation issues are deduplicated.
- Protects equation references such as `Eq. (7)` as reference placeholders.
- Adds spatial grouping for stacked English-labeled fractions based on a
  horizontal vector bar, aligned English numerator/denominator, left-side
  expression ending in `=`, and equation-number marker. The reported equation
  is on physical PDF page 4 of 5, in the original panel's left column. A
  read-only parser now groups the label, numerator, denominator, number, and
  bar into one protected formula block. A printed paper page number was not
  detected.
- The equation-reference regex is not the fraction-body fix. The historical
  translation job/log was not located, so the exact past model output and
  rendering behavior remain unverified. No real-model retranslation was run.
- Do not describe continuation context as joined translation: merging blocks,
  splitting a Japanese translation back into multiple source regions, and a
  user accept/reject flow are not implemented.

### Verification at checkpoint

- Latest full offline Python suite: **69 passed, 7 skipped, 1 warning**.
  Skips are opt-in real-LLM/GPU tests; the warning is a Starlette/httpx
  deprecation notice.
- Synthetic fraction visual regression passes: the equation region has zero
  differing pixels in the mock-translated output, and adjacent ordinary
  English paragraphs are translated by the mock adapter.
- The provided PDF was opened read-only for extraction and classification;
  its SHA-256 remained unchanged. It was not added to the repository.
- These results do not certify a clean Windows environment, real-model
  translation, other GPUs, or translation quality. Frontend tests/build were
  previously recorded as successful before the latest backend/test-only
  changes. An attempted recheck during this checkpoint did not launch because
  the tool required approval that could not be obtained; this is not a test
  failure, and no alternate execution route was used.

### Runtime and next priority

- Treat the already-running API as pre-change code. It was not restarted and
  must not be used as evidence that it loaded this branch.
- Do not change existing servers, user runtime data, databases, or jobs.
- Next priority: separately verify installation, startup, model-free UI, and
  then an explicitly authorized real-model translation in a clean environment.
  Record mock/synthetic verification separately from real-model results.
- This checkpoint requests review and saving only. Do not commit or push until
  the user approves the exact file list, commit message, and destination.
- Proposed commit message: `Checkpoint formula protection and continuation context`.
