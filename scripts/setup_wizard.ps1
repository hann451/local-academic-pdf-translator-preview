# PaperBridge Local - First-Time Automated Setup Wizard (Source ZIP + Self-Contained Setup)
param(
    [switch]$AcceptAgreements = $false,
    [ValidateSet("", "Download", "Local", "Skip")]
    [string]$ModelMode = "",
    [string]$PreselectedModelPath = "",
    [switch]$SkipShortcut = $false,
    [switch]$ForceReinstallTools = $false,
    [string]$UserDataDir = "",
    [string]$ShortcutName = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$FrontendDir = Join-Path $ProjectRoot "frontend"
$RuntimeToolsDir = Join-Path $ProjectRoot ".runtime_tools"
$RuntimeProfilePath = Join-Path $RuntimeToolsDir "runtime_profile.json"
$CacheDownloadDir = Join-Path $RuntimeToolsDir "downloads"
$StampsDir = Join-Path $RuntimeToolsDir "stamps"
$ToolsDir = Join-Path $ProjectRoot "tools"
$LlamaBaseDir = Join-Path $ToolsDir "llama.cpp"

$DefaultUserDataDir = if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { Join-Path $env:LOCALAPPDATA "PaperBridgeLocal" } else { "" }
$DefaultShortcutName = "PaperBridge Local"
$loadedProfile = $null
if (Test-Path -LiteralPath $RuntimeProfilePath) {
    try {
        $loadedProfile = Get-Content -LiteralPath $RuntimeProfilePath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        $loadedProfile = $null
    }
}

if ([string]::IsNullOrWhiteSpace($UserDataDir)) {
    if (-not [string]::IsNullOrWhiteSpace($env:PAPERBRIDGE_RUNTIME_DIR)) {
        $UserDataDir = $env:PAPERBRIDGE_RUNTIME_DIR
    } elseif ($loadedProfile -and -not [string]::IsNullOrWhiteSpace($loadedProfile.UserDataDir)) {
        $UserDataDir = [string]$loadedProfile.UserDataDir
    } elseif (-not [string]::IsNullOrWhiteSpace($DefaultUserDataDir)) {
        $UserDataDir = $DefaultUserDataDir
    } else {
        throw "LOCALAPPDATA 環境変数が取得できませんでした。"
    }
}

if ([string]::IsNullOrWhiteSpace($ShortcutName)) {
    if ($loadedProfile -and -not [string]::IsNullOrWhiteSpace($loadedProfile.ShortcutName)) {
        $ShortcutName = [string]$loadedProfile.ShortcutName
    } else {
        $ShortcutName = $DefaultShortcutName
    }
}

$LogsDir = Join-Path $UserDataDir "logs"
$SetupLogPath = Join-Path $LogsDir "setup.log"
$UserModelDir = Join-Path $UserDataDir "models\plamo-3-translate-2b-GGUF"
$ProjectModelDir = Join-Path $ProjectRoot "models\plamo-3-translate-2b-GGUF"

# Pinned Tool Specifications (Matches docs/SETUP_WINDOWS.md and verified upstream digests)
$PINNED_TOOLS = @{
    uv = @{
        Name        = "uv 0.11.15"
        FileName    = "uv-x86_64-pc-windows-msvc.zip"
        Url         = "https://github.com/astral-sh/uv/releases/download/0.11.15/uv-x86_64-pc-windows-msvc.zip"
        Size        = 22872113L
        Sha256      = "04b98d414a9000e25e5e0e7c9f53749e66b790cdaffc582829e6f58c544ee11c"
        InstallDir  = (Join-Path $RuntimeToolsDir "uv")
        ExeName     = "uv.exe"
    }
    node = @{
        Name        = "Node.js v22.20.0 (win-x64)"
        FileName    = "node-v22.20.0-win-x64.zip"
        Url         = "https://nodejs.org/dist/v22.20.0/node-v22.20.0-win-x64.zip"
        Size        = 35500968L
        Sha256      = "bb819d6eb8f5bfda294bbc83a7e4ec6539da67c4233d54b0d655b9248b15e29d"
        InstallDir  = (Join-Path $RuntimeToolsDir "node22")
        ExeName     = "node.exe"
    }
    llama_vulkan = @{
        Name        = "llama.cpp b12167 (Vulkan GPU x64)"
        FileName    = "llama-b12167-bin-win-vulkan-x64.zip"
        Url         = "https://github.com/ggml-org/llama.cpp/releases/download/b12167/llama-b12167-bin-win-vulkan-x64.zip"
        Size        = 37685589L
        Sha256      = "f99b2fc83d93f4a36778e5966600c8a4a36e8a3a6660521601317c02f974ca9d"
        InstallDir  = (Join-Path $LlamaBaseDir "b12167-vulkan-x64")
        ExeName     = "llama-server.exe"
    }
    llama_cpu = @{
        Name        = "llama.cpp b12167 (CPU-only x64)"
        FileName    = "llama-b12167-bin-win-cpu-x64.zip"
        Url         = "https://github.com/ggml-org/llama.cpp/releases/download/b12167/llama-b12167-bin-win-cpu-x64.zip"
        Size        = 24726767L
        Sha256      = "430d22ba7693f2135f9d3e5301f7d773cf1b8ba89fdf5ca3a5cdc5d5a7173980"
        InstallDir  = (Join-Path $LlamaBaseDir "b12167-cpu-x64")
        ExeName     = "llama-server.exe"
    }
}

$MODEL_FILE_NAME = "plamo-3-translate-2b-Q4_K_M.gguf"
$MODEL_SIZE = 1807199392L
$MODEL_SHA256 = "f36d0813dd95573a852ab5d76e374c543632d9e292929e922bcdbf7e53b494e0"
$MODEL_URL = "https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF/resolve/232fbd7916519edecddfdae0beae7e76fe70453f/plamo-3-translate-2b-Q4_K_M.gguf"
$PLAMO_LICENSE_JA = "https://plamo.preferredai.jp/info/plamo-community-license-ja"
$PLAMO_LICENSE_EN = "https://plamo.preferredai.jp/info/plamo-community-license-en"

$script:IsInteractive = (-not $AcceptAgreements)
$script:CancelRequested = $false
$script:ProgressForm = $null
$script:ProgressBar = $null
$script:LblStep = $null
$script:TxtLog = $null
$script:BtnAbort = $null

function Write-SetupLog {
    param([string]$Message, [string]$Level = "INFO")
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$ts] [$Level] $Message"
    $logParent = Split-Path -Parent $SetupLogPath
    if (-not [string]::IsNullOrWhiteSpace($logParent) -and -not (Test-Path -LiteralPath $logParent)) {
        New-Item -ItemType Directory -Path $logParent -Force | Out-Null
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $sw = New-Object System.IO.StreamWriter($SetupLogPath, $true, $utf8NoBom)
    try {
        $sw.WriteLine($line)
    } finally {
        $sw.Close()
    }
    if (-not $script:IsInteractive) {
        Write-Host $line
    }
    if ($script:TxtLog -and -not $script:TxtLog.IsDisposed) {
        $script:TxtLog.AppendText("$line`r`n")
        $script:TxtLog.SelectionStart = $script:TxtLog.TextLength
        $script:TxtLog.ScrollToCaret()
        [System.Windows.Forms.Application]::DoEvents()
    }
}

function Update-SetupUiProgress {
    param([int]$Percent, [string]$StatusText)
    if ($script:LblStep -and -not $script:LblStep.IsDisposed) {
        $script:LblStep.Text = $StatusText
    }
    if ($script:ProgressBar -and -not $script:ProgressBar.IsDisposed) {
        $clamped = [Math]::Max(0, [Math]::Min(100, $Percent))
        $script:ProgressBar.Value = $clamped
    }
    if ($script:IsInteractive) {
        [System.Windows.Forms.Application]::DoEvents()
    }
}

function Test-CancelRequested {
    if ($script:IsInteractive) {
        [System.Windows.Forms.Application]::DoEvents()
    }
    if ($script:CancelRequested) {
        throw [System.OperationCanceledException]::new("ユーザーによりセットアップがキャンセルされました。")
    }
}

function Get-FileSha256Hex {
    param([string]$FilePath)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $fs = [System.IO.File]::OpenRead($FilePath)
    try {
        $bytes = $sha.ComputeHash($fs)
    } finally {
        $fs.Close()
        $sha.Dispose()
    }
    $sb = New-Object System.Text.StringBuilder
    foreach ($b in $bytes) {
        $sb.Append($b.ToString("x2")) | Out-Null
    }
    return $sb.ToString()
}

function Test-GgufMagic {
    param([string]$FilePath)
    if (-not (Test-Path -LiteralPath $FilePath)) { return $false }
    $fs = $null
    try {
        $fs = [System.IO.File]::OpenRead($FilePath)
        $buf = New-Object byte[] 4
        $read = $fs.Read($buf, 0, 4)
        return ($read -eq 4 -and $buf[0] -eq 0x47 -and $buf[1] -eq 0x47 -and $buf[2] -eq 0x55 -and $buf[3] -eq 0x46)
    } catch {
        return $false
    } finally {
        if ($fs) { $fs.Close() }
    }
}

function Test-VerifiedFile {
    param([string]$FilePath, [int64]$ExpectedSize, [string]$ExpectedSha256, [switch]$RequireGguf)
    if (-not (Test-Path -LiteralPath $FilePath)) { return $false }
    $item = Get-Item -LiteralPath $FilePath
    if ($item.Length -ne $ExpectedSize) { return $false }
    if ($RequireGguf -and -not (Test-GgufMagic -FilePath $FilePath)) { return $false }
    $actual = Get-FileSha256Hex -FilePath $FilePath
    return ($actual -eq $ExpectedSha256)
}

function Invoke-VerifiedDownload {
    param(
        [string]$Label,
        [string]$Url,
        [string]$DestPath,
        [int64]$ExpectedSize,
        [string]$ExpectedSha256,
        [int]$BaseProgress,
        [int]$SpanProgress,
        [switch]$RequireGguf
    )
    if (-not $ForceReinstallTools -and (Test-VerifiedFile -FilePath $DestPath -ExpectedSize $ExpectedSize -ExpectedSha256 $ExpectedSha256 -RequireGguf:$RequireGguf)) {
        Write-SetupLog "$Label は既にSHA-256検証済みのため再利用します: $DestPath"
        return
    }

    $tmpPath = "$DestPath.downloading"
    if (Test-Path -LiteralPath $tmpPath) {
        Remove-Item -LiteralPath $tmpPath -Force -ErrorAction SilentlyContinue
    }

    Write-SetupLog "$Label をダウンロードします: $Url"
    Update-SetupUiProgress -Percent $BaseProgress -StatusText "$Label を取得中..."

    $response = $null
    $resStream = $null
    $fs = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create($Url)
        $request.Method = "GET"
        $request.Timeout = 30000
        $request.ReadWriteTimeout = 30000
        $request.AllowAutoRedirect = $true
        $request.UserAgent = "PaperBridgeLocal-SetupWizard/1.0"

        $response = $request.GetResponse()
        $totalBytes = $response.ContentLength
        if ($totalBytes -le 0) { $totalBytes = $ExpectedSize }
        $resStream = $response.GetResponseStream()
        $fs = [System.IO.File]::Create($tmpPath)

        $buffer = New-Object byte[] 131072
        $downloaded = 0L
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        while (($readBytes = $resStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            Test-CancelRequested
            $fs.Write($buffer, 0, $readBytes)
            $downloaded += $readBytes
            if ($sw.ElapsedMilliseconds -ge 150) {
                $frac = [Math]::Min(1.0, $downloaded / [double]$totalBytes)
                $pct = [int]($BaseProgress + ($SpanProgress * $frac))
                $mbDone = [math]::Round($downloaded / 1MB, 1)
                $mbTot = [math]::Round($totalBytes / 1MB, 1)
                Update-SetupUiProgress -Percent $pct -StatusText "$Label をダウンロード中... ($mbDone MB / $mbTot MB)"
                $sw.Restart()
            }
        }
        $fs.Close(); $fs = $null
        $resStream.Close(); $resStream = $null
        $response.Close(); $response = $null

        Update-SetupUiProgress -Percent ($BaseProgress + $SpanProgress) -StatusText "$Label のSHA-256ハッシュを検証中..."
        Write-SetupLog "$Label の整合性を検証しています (サイズ: $ExpectedSize bytes / SHA-256照合)..."

        $dlInfo = Get-Item -LiteralPath $tmpPath
        if ($dlInfo.Length -ne $ExpectedSize) {
            throw "$Label のサイズが一致しません (期待: $ExpectedSize bytes / 実測: $($dlInfo.Length) bytes)"
        }
        if ($RequireGguf -and -not (Test-GgufMagic -FilePath $tmpPath)) {
            throw "$Label の GGUF ヘッダー検証に失敗しました。"
        }
        $actualHash = Get-FileSha256Hex -FilePath $tmpPath
        if ($actualHash -ne $ExpectedSha256) {
            throw "$Label の SHA-256 が一致しません (期待: $ExpectedSha256 / 実測: $actualHash)"
        }

        if (Test-Path -LiteralPath $DestPath) {
            Remove-Item -LiteralPath $DestPath -Force
        }
        Move-Item -LiteralPath $tmpPath -Destination $DestPath -Force
        Write-SetupLog "$Label の検証と保存が完了しました。"
    } catch {
        if ($fs) { try { $fs.Close() } catch {} }
        if ($resStream) { try { $resStream.Close() } catch {} }
        if ($response) { try { $response.Close() } catch {} }
        if (Test-Path -LiteralPath $tmpPath) { Remove-Item -LiteralPath $tmpPath -Force -ErrorAction SilentlyContinue }
        throw
    }
}

function Install-PinnedZipTool {
    param(
        [hashtable]$Spec,
        [int]$BaseProgress,
        [int]$SpanProgress
    )
    $stampFile = Join-Path $StampsDir "$($Spec.FileName).sha256"
    $targetExe = Join-Path $Spec.InstallDir $Spec.ExeName
    if (-not $ForceReinstallTools -and (Test-Path -LiteralPath $targetExe) -and (Test-Path -LiteralPath $stampFile)) {
        $recorded = (Get-Content -LiteralPath $stampFile -Raw).Trim()
        if ($recorded -eq $Spec.Sha256) {
            Write-SetupLog "$($Spec.Name) は導入済みです: $($Spec.InstallDir)"
            return
        }
    }

    $zipPath = Join-Path $CacheDownloadDir $Spec.FileName
    Invoke-VerifiedDownload -Label $Spec.Name -Url $Spec.Url -DestPath $zipPath -ExpectedSize $Spec.Size -ExpectedSha256 $Spec.Sha256 -BaseProgress $BaseProgress -SpanProgress ([int]($SpanProgress * 0.7))

    Update-SetupUiProgress -Percent ($BaseProgress + [int]($SpanProgress * 0.8)) -StatusText "$($Spec.Name) を展開中..."
    Write-SetupLog "$($Spec.Name) を展開しています..."

    $extractTmp = Join-Path $RuntimeToolsDir "xt_$([guid]::NewGuid().ToString('N').Substring(0, 6))"
    try {
        New-Item -ItemType Directory -Path $extractTmp -Force | Out-Null
        $tarExe = Join-Path $env:WINDIR "System32\tar.exe"
        if (Test-Path -LiteralPath $tarExe) {
            $prevEap = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            try {
                $tarOut = (& $tarExe -xf $zipPath -C $extractTmp 2>&1) -join "`n"
                $tarRc = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $prevEap
            }
            if ($tarRc -ne 0) {
                throw "$($Spec.Name) の展開 (tar.exe) が終了コード $tarRc で失敗しました: $tarOut"
            }
        } else {
            [System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $extractTmp)
        }

        $candidateDirs = New-Object System.Collections.Generic.List[string]
        $candidateDirs.Add($extractTmp)
        foreach ($d1 in (Get-ChildItem -LiteralPath $extractTmp -Directory -ErrorAction SilentlyContinue)) {
            $candidateDirs.Add($d1.FullName)
            if ($d1.Name -ne "node_modules") {
                foreach ($d2 in (Get-ChildItem -LiteralPath $d1.FullName -Directory -ErrorAction SilentlyContinue)) {
                    if ($d2.Name -ne "node_modules") {
                        $candidateDirs.Add($d2.FullName)
                    }
                }
            }
        }
        $foundExe = $null
        foreach ($candDir in $candidateDirs) {
            $candFile = Join-Path $candDir $Spec.ExeName
            if (Test-Path -LiteralPath $candFile) {
                $foundExe = Get-Item -LiteralPath $candFile
                break
            }
        }
        if (-not $foundExe) {
            throw "$($Spec.Name) のアーカイブ内に $($Spec.ExeName) が見つかりませんでした。"
        }
        $sourceRoot = Split-Path -Parent $foundExe.FullName
        if (Test-Path -LiteralPath $Spec.InstallDir) {
            try {
                Remove-Item -LiteralPath $Spec.InstallDir -Recurse -Force -ErrorAction Stop
            } catch {
                cmd.exe /c "rd /s /q `"$($Spec.InstallDir)`"" | Out-Null
            }
        }
        $parentDir = Split-Path -Parent $Spec.InstallDir
        if (-not (Test-Path -LiteralPath $parentDir)) {
            New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
        }
        Move-Item -LiteralPath $sourceRoot -Destination $Spec.InstallDir -Force
        Set-Content -LiteralPath $stampFile -Value $Spec.Sha256 -Encoding ASCII
        Write-SetupLog "$($Spec.Name) の配置が完了しました: $($Spec.InstallDir)"
    } finally {
        if (Test-Path -LiteralPath $extractTmp) {
            try {
                Remove-Item -LiteralPath $extractTmp -Recurse -Force -ErrorAction Stop
            } catch {
                cmd.exe /c "rd /s /q `"$extractTmp`"" | Out-Null
            }
        }
    }
}

function Format-ProcessArgs {
    param([string[]]$ArgArray)
    $quoted = foreach ($a in $ArgArray) {
        if ($a -match '[\s"]') {
            $escaped = $a -replace '"', '\"'
            '"' + $escaped + '"'
        } else {
            $a
        }
    }
    return ($quoted -join " ")
}

function Invoke-LoggedExternalCommand {
    param(
        [string]$StepLabel,
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory
    )
    $argString = Format-ProcessArgs -ArgArray $ArgumentList
    Write-SetupLog "実行 [$StepLabel]: `"$FilePath`" $argString (作業Dir: $WorkingDirectory)"

    $tmpOut = Join-Path $LogsDir "step_$([guid]::NewGuid().ToString('N')).out.log"
    $tmpErr = Join-Path $LogsDir "step_$([guid]::NewGuid().ToString('N')).err.log"
    $proc = $null
    try {
        $proc = Start-Process -FilePath $FilePath -ArgumentList $argString -WorkingDirectory $WorkingDirectory -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr -WindowStyle Hidden -PassThru
        $null = $proc.Handle
        while (-not $proc.HasExited) {
            if ($script:CancelRequested) {
                try { $proc.Kill() } catch {}
                throw [System.OperationCanceledException]::new("$StepLabel の実行中にユーザーによってキャンセルされました。")
            }
            if ($script:IsInteractive) {
                [System.Windows.Forms.Application]::DoEvents()
            }
            Start-Sleep -Milliseconds 120
        }
        $proc.WaitForExit()
        $exitCode = $proc.ExitCode

        foreach ($logF in @($tmpOut, $tmpErr)) {
            if (Test-Path -LiteralPath $logF) {
                $txt = Get-Content -LiteralPath $logF -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
                if (-not [string]::IsNullOrWhiteSpace($txt)) {
                    foreach ($ln in ($txt -split "`r?`n")) {
                        if (-not [string]::IsNullOrWhiteSpace($ln)) {
                            Write-SetupLog "  | $ln"
                        }
                    }
                }
            }
        }
        if ($exitCode -ne 0) {
            throw "$StepLabel が終了コード $exitCode で失敗しました。詳細はログを確認してください: $SetupLogPath"
        }
    } finally {
        if ($proc) { $proc.Dispose() }
        if (Test-Path -LiteralPath $tmpOut) { Remove-Item -LiteralPath $tmpOut -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $tmpErr) { Remove-Item -LiteralPath $tmpErr -Force -ErrorAction SilentlyContinue }
    }
}

function Check-DiskSpace {
    param([bool]$NeedsModelSpace)
    $projRootStr = [System.IO.Path]::GetPathRoot($ProjectRoot)
    $dataRootStr = [System.IO.Path]::GetPathRoot($UserDataDir)
    if ($projRootStr -notmatch '^[A-Za-z]:\\') {
        Write-SetupLog "プロジェクトパスがドライブレター形式ではないため空き容量チェックをスキップします: $ProjectRoot" "WARN"
        return
    }
    $projDrive = [System.IO.DriveInfo]::new($projRootStr)
    $sameDrive = ($projRootStr.ToUpperInvariant() -eq $dataRootStr.ToUpperInvariant())
    $reqProjBytes = if ($NeedsModelSpace -and $sameDrive) { 4500L * 1MB } else { 2500L * 1MB }
    if ($projDrive.AvailableFreeSpace -lt $reqProjBytes) {
        $availGb = [math]::Round($projDrive.AvailableFreeSpace / 1GB, 2)
        $reqGb = [math]::Round($reqProjBytes / 1GB, 2)
        throw "ドライブ $($projRootStr) の空き容量が不足しています (必要目安: $reqGb GB以上 / 現在: $availGb GB)。"
    }
    if ($NeedsModelSpace -and -not $sameDrive -and ($dataRootStr -match '^[A-Za-z]:\\')) {
        $dataDrive = [System.IO.DriveInfo]::new($dataRootStr)
        $reqDataBytes = 2000L * 1MB
        if ($dataDrive.AvailableFreeSpace -lt $reqDataBytes) {
            $availGb = [math]::Round($dataDrive.AvailableFreeSpace / 1GB, 2)
            throw "モデル保存先ドライブ $($dataRootStr) の空き容量が不足しています (必要目安: 2.0 GB以上 / 現在: $availGb GB)。"
        }
    }
}

function Show-ConsentDialog {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "PaperBridge Local - 初回自動セットアップ"
    $form.Size = New-Object System.Drawing.Size(680, 710)
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.Font = New-Object System.Drawing.Font("Yu Gothic UI", 9.5)

    $lblHead = New-Object System.Windows.Forms.Label
    $lblHead.Location = New-Object System.Drawing.Point(20, 14)
    $lblHead.Size = New-Object System.Drawing.Size(625, 28)
    $lblHead.Text = "初回セットアップの確認 (外部ツールの取得と環境構築)"
    $lblHead.Font = New-Object System.Drawing.Font("Yu Gothic UI", 12, [System.Drawing.FontStyle]::Bold)
    $form.Controls.Add($lblHead)

    $txtInfo = New-Object System.Windows.Forms.TextBox
    $txtInfo.Location = New-Object System.Drawing.Point(20, 48)
    $txtInfo.Size = New-Object System.Drawing.Size(625, 215)
    $txtInfo.Multiline = $true
    $txtInfo.ReadOnly = $true
    $txtInfo.ScrollBars = "Vertical"
    $txtInfo.BackColor = [System.Drawing.Color]::WhiteSmoke
    $txtInfo.Text = @"
このセットアップはコマンド手入力なしで PaperBridge Local を動作可能にします。
既存のシステム Python / Node.js / 環境変数 PATH / 管理者権限は変更せず、展開先フォルダー内および下で指定するデータ保存先フォルダーのみに構築します。

【自動取得する外部ツール・依存物 (SHA-256 検証付き)】
1. uv 0.11.15 (MIT / Apache-2.0): Python 実行環境・uv.lock 厳密同期
2. Python 3.13 (PSF License): uv 管理下の分離ランタイムとして導入
3. Node.js v22.20.0 (MIT License): フロントエンド (frontend/dist) ビルド用
4. llama.cpp b12167 (MIT License): 推論エンジン (Vulkan GPU版 + CPU専用版)
5. Python・npm パッケージ群 (PyMuPDF 等 AGPL-3.0 / MIT / Apache 依存を含む)

【必要な通信先・通信量・空き容量・対応範囲】
- 通信先: github.com / nodejs.org / pypi.org / python-build-standalone / huggingface.co
- 通信量目安: ツール一式 約 300 MB ＋ 翻訳モデル 約 1.81 GB (計 約 2.1〜2.2 GB)
- 空き容量目安: 一時展開領域を含め 5 GB 以上を推奨
- 自動対応外: オフライン環境、企業TLS検査/プロキシ遮断環境、GPUドライバー更新、Windows Defender SmartScreen 解除、Microsoft Edge 以外の特殊ブラウザ構成は自動解決できません。

【翻訳モデル (PLaMo 3 Translate 2B Q4_K_M) について (Built with PLaMo)】
- Preferred Networks (PFN) の PLaMo コミュニティライセンス契約が適用されます。
- 非商用・学術研究は無償（事前登録不要）。商用利用には事前登録・売上条件等があります。
- 長文公表物として翻訳PDFを公表する場合は PDF 内への明示等の義務があります。
"@
    $form.Controls.Add($txtInfo)

    $lnkJa = New-Object System.Windows.Forms.LinkLabel
    $lnkJa.Location = New-Object System.Drawing.Point(20, 270)
    $lnkJa.Size = New-Object System.Drawing.Size(270, 20)
    $lnkJa.Text = "PLaMoコミュニティライセンス契約 (日本語)"
    $lnkJa.Add_LinkClicked({ Start-Process $PLAMO_LICENSE_JA })
    $form.Controls.Add($lnkJa)

    $lnkEn = New-Object System.Windows.Forms.LinkLabel
    $lnkEn.Location = New-Object System.Drawing.Point(300, 270)
    $lnkEn.Size = New-Object System.Drawing.Size(270, 20)
    $lnkEn.Text = "PLaMo Community License (English)"
    $lnkEn.Add_LinkClicked({ Start-Process $PLAMO_LICENSE_EN })
    $form.Controls.Add($lnkEn)

    $grpModel = New-Object System.Windows.Forms.GroupBox
    $grpModel.Location = New-Object System.Drawing.Point(20, 296)
    $grpModel.Size = New-Object System.Drawing.Size(625, 130)
    $grpModel.Text = "翻訳モデル (plamo-3-translate-2b-Q4_K_M.gguf) の準備方法"
    $form.Controls.Add($grpModel)

    $rdoDownload = New-Object System.Windows.Forms.RadioButton
    $rdoDownload.Location = New-Object System.Drawing.Point(15, 24)
    $rdoDownload.Size = New-Object System.Drawing.Size(590, 22)
    $rdoDownload.Text = "公式 Hugging Face からダウンロードする (約 1.81 GB / PLaMoライセンス同意)"
    $rdoDownload.Checked = $true
    $grpModel.Controls.Add($rdoDownload)

    $rdoLocal = New-Object System.Windows.Forms.RadioButton
    $rdoLocal.Location = New-Object System.Drawing.Point(15, 52)
    $rdoLocal.Size = New-Object System.Drawing.Size(590, 22)
    $rdoLocal.Text = "手元にある既存 GGUF ファイルを SHA-256 検証して配置する"
    $grpModel.Controls.Add($rdoLocal)

    $txtLocalPath = New-Object System.Windows.Forms.TextBox
    $txtLocalPath.Location = New-Object System.Drawing.Point(35, 76)
    $txtLocalPath.Size = New-Object System.Drawing.Size(470, 24)
    $txtLocalPath.Enabled = $false
    $grpModel.Controls.Add($txtLocalPath)

    $btnBrowse = New-Object System.Windows.Forms.Button
    $btnBrowse.Location = New-Object System.Drawing.Point(512, 75)
    $btnBrowse.Size = New-Object System.Drawing.Size(95, 26)
    $btnBrowse.Text = "参照..."
    $btnBrowse.Enabled = $false
    $grpModel.Controls.Add($btnBrowse)

    $rdoSkip = New-Object System.Windows.Forms.RadioButton
    $rdoSkip.Location = New-Object System.Drawing.Point(15, 102)
    $rdoSkip.Size = New-Object System.Drawing.Size(590, 22)
    $rdoSkip.Text = "モデル設定はスキップしてアプリ実行環境のみ構築する (初回起動時に設定可能)"
    $grpModel.Controls.Add($rdoSkip)

    $rdoLocal.Add_CheckedChanged({
        $txtLocalPath.Enabled = $rdoLocal.Checked
        $btnBrowse.Enabled = $rdoLocal.Checked
    })

    $btnBrowse.Add_Click({
        $ofd = New-Object System.Windows.Forms.OpenFileDialog
        $ofd.Filter = "GGUF モデルファイル (*.gguf)|*.gguf|すべてのファイル (*.*)|*.*"
        $ofd.Title = "$MODEL_FILE_NAME を選択してください"
        if ($ofd.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            $txtLocalPath.Text = $ofd.FileName
        }
    })

    $grpProfile = New-Object System.Windows.Forms.GroupBox
    $grpProfile.Location = New-Object System.Drawing.Point(20, 434)
    $grpProfile.Size = New-Object System.Drawing.Size(625, 96)
    $grpProfile.Text = "保存先・デスクトップアイコン設定 (通常は既定のままで構いません / 既存環境分離用)"
    $form.Controls.Add($grpProfile)

    $lblDataDir = New-Object System.Windows.Forms.Label
    $lblDataDir.Location = New-Object System.Drawing.Point(15, 26)
    $lblDataDir.Size = New-Object System.Drawing.Size(150, 22)
    $lblDataDir.Text = "データ保存先フォルダー:"
    $grpProfile.Controls.Add($lblDataDir)

    $txtUserDataDir = New-Object System.Windows.Forms.TextBox
    $txtUserDataDir.Location = New-Object System.Drawing.Point(170, 24)
    $txtUserDataDir.Size = New-Object System.Drawing.Size(342, 24)
    $txtUserDataDir.Text = $UserDataDir
    $grpProfile.Controls.Add($txtUserDataDir)

    $btnBrowseDataDir = New-Object System.Windows.Forms.Button
    $btnBrowseDataDir.Location = New-Object System.Drawing.Point(520, 23)
    $btnBrowseDataDir.Size = New-Object System.Drawing.Size(90, 26)
    $btnBrowseDataDir.Text = "変更..."
    $grpProfile.Controls.Add($btnBrowseDataDir)

    $btnBrowseDataDir.Add_Click({
        $fbd = New-Object System.Windows.Forms.FolderBrowserDialog
        $fbd.Description = "PaperBridge Local のデータ保存先 (DB・ログ・設定) フォルダーを選択してください"
        $fbd.ShowNewFolderButton = $true
        if (-not [string]::IsNullOrWhiteSpace($txtUserDataDir.Text) -and (Test-Path -LiteralPath $txtUserDataDir.Text)) {
            $fbd.SelectedPath = $txtUserDataDir.Text
        }
        if ($fbd.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            $txtUserDataDir.Text = $fbd.SelectedPath
        }
    })

    $lblShortcut = New-Object System.Windows.Forms.Label
    $lblShortcut.Location = New-Object System.Drawing.Point(15, 60)
    $lblShortcut.Size = New-Object System.Drawing.Size(150, 22)
    $lblShortcut.Text = "デスクトップアイコン名:"
    $grpProfile.Controls.Add($lblShortcut)

    $txtShortcutName = New-Object System.Windows.Forms.TextBox
    $txtShortcutName.Location = New-Object System.Drawing.Point(170, 58)
    $txtShortcutName.Size = New-Object System.Drawing.Size(250, 24)
    $txtShortcutName.Text = $ShortcutName
    $txtShortcutName.Enabled = (-not $SkipShortcut)
    $grpProfile.Controls.Add($txtShortcutName)

    $lblShortcutHint = New-Object System.Windows.Forms.Label
    $lblShortcutHint.Location = New-Object System.Drawing.Point(426, 61)
    $lblShortcutHint.Size = New-Object System.Drawing.Size(190, 20)
    $lblShortcutHint.Text = "(.lnk / 試験分離時は別名可)"
    $lblShortcutHint.ForeColor = [System.Drawing.Color]::DimGray
    $grpProfile.Controls.Add($lblShortcutHint)

    $chkShortcut = New-Object System.Windows.Forms.CheckBox
    $chkShortcut.Location = New-Object System.Drawing.Point(20, 538)
    $chkShortcut.Size = New-Object System.Drawing.Size(625, 24)
    $chkShortcut.Text = "セットアップ完了後、デスクトップに上記名前の起動アイコンを作成する"
    $chkShortcut.Checked = (-not $SkipShortcut)
    $chkShortcut.Add_CheckedChanged({
        $txtShortcutName.Enabled = $chkShortcut.Checked
    })
    $form.Controls.Add($chkShortcut)

    $chkAgree = New-Object System.Windows.Forms.CheckBox
    $chkAgree.Location = New-Object System.Drawing.Point(20, 568)
    $chkAgree.Size = New-Object System.Drawing.Size(625, 38)
    $chkAgree.Text = "上記の外部ツール取得・通信量・空き容量・注意事項（およびモデル選択時は PLaMo コミュニティライセンス契約）を確認し、同意のうえセットアップを開始します。"
    $form.Controls.Add($chkAgree)

    $btnStart = New-Object System.Windows.Forms.Button
    $btnStart.Location = New-Object System.Drawing.Point(375, 616)
    $btnStart.Size = New-Object System.Drawing.Size(145, 38)
    $btnStart.Text = "同意して開始"
    $btnStart.Enabled = $false
    $form.Controls.Add($btnStart)

    $btnQuit = New-Object System.Windows.Forms.Button
    $btnQuit.Location = New-Object System.Drawing.Point(530, 616)
    $btnQuit.Size = New-Object System.Drawing.Size(115, 38)
    $btnQuit.Text = "キャンセル"
    $form.Controls.Add($btnQuit)

    $chkAgree.Add_CheckedChanged({
        $btnStart.Enabled = $chkAgree.Checked
    })

    $script:DialogSelection = $null
    $btnStart.Add_Click({
        $chosenDataDir = $txtUserDataDir.Text.Trim()
        if ([string]::IsNullOrWhiteSpace($chosenDataDir)) {
            [System.Windows.Forms.MessageBox]::Show("データ保存先フォルダーを入力してください。", "入力確認", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
            return
        }
        if ($chosenDataDir.Length -gt 3) {
            $chosenDataDir = $chosenDataDir.TrimEnd('\', '/')
        }
        $chosenShortcut = $txtShortcutName.Text.Trim()
        if ([string]::IsNullOrWhiteSpace($chosenShortcut)) {
            $chosenShortcut = $DefaultShortcutName
        }
        if ($chkShortcut.Checked) {
            $invalidChars = [System.IO.Path]::GetInvalidFileNameChars()
            foreach ($ch in $invalidChars) {
                if ($chosenShortcut.IndexOf($ch) -ge 0) {
                    [System.Windows.Forms.MessageBox]::Show("デスクトップアイコン名に使用できない文字が含まれています。", "入力確認", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
                    return
                }
            }
        }
        if ($rdoLocal.Checked) {
            if ([string]::IsNullOrWhiteSpace($txtLocalPath.Text) -or -not (Test-Path -LiteralPath $txtLocalPath.Text)) {
                [System.Windows.Forms.MessageBox]::Show("既存の GGUF ファイルを選択してください。", "入力確認", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
                return
            }
        }
        $mode = if ($rdoDownload.Checked) { "Download" } elseif ($rdoLocal.Checked) { "Local" } else { "Skip" }
        $script:DialogSelection = @{
            ModelMode      = $mode
            LocalPath      = $txtLocalPath.Text
            CreateShortcut = $chkShortcut.Checked
            UserDataDir    = $chosenDataDir
            ShortcutName   = $chosenShortcut
        }
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    })

    $btnQuit.Add_Click({
        $form.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
        $form.Close()
    })

    $res = $form.ShowDialog()
    if ($res -ne [System.Windows.Forms.DialogResult]::OK -or -not $script:DialogSelection) {
        return $null
    }
    return $script:DialogSelection
}

function Show-ProgressWindow {
    $pf = New-Object System.Windows.Forms.Form
    $pf.Text = "PaperBridge Local - 環境構築中..."
    $pf.Size = New-Object System.Drawing.Size(640, 420)
    $pf.StartPosition = "CenterScreen"
    $pf.FormBorderStyle = "FixedDialog"
    $pf.MaximizeBox = $false
    $pf.MinimizeBox = $false
    $pf.Font = New-Object System.Drawing.Font("Yu Gothic UI", 9.5)

    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Location = New-Object System.Drawing.Point(20, 18)
    $lbl.Size = New-Object System.Drawing.Size(585, 26)
    $lbl.Text = "準備中..."
    $lbl.Font = New-Object System.Drawing.Font("Yu Gothic UI", 10.5, [System.Drawing.FontStyle]::Bold)
    $pf.Controls.Add($lbl)

    $bar = New-Object System.Windows.Forms.ProgressBar
    $bar.Location = New-Object System.Drawing.Point(20, 50)
    $bar.Size = New-Object System.Drawing.Size(585, 24)
    $bar.Style = "Continuous"
    $pf.Controls.Add($bar)

    $tb = New-Object System.Windows.Forms.TextBox
    $tb.Location = New-Object System.Drawing.Point(20, 86)
    $tb.Size = New-Object System.Drawing.Size(585, 230)
    $tb.Multiline = $true
    $tb.ReadOnly = $true
    $tb.ScrollBars = "Vertical"
    $tb.BackColor = [System.Drawing.Color]::WhiteSmoke
    $pf.Controls.Add($tb)

    $btn = New-Object System.Windows.Forms.Button
    $btn.Location = New-Object System.Drawing.Point(485, 328)
    $btn.Size = New-Object System.Drawing.Size(120, 34)
    $btn.Text = "中止"
    $btn.Add_Click({
        $script:CancelRequested = $true
        $btn.Enabled = $false
        $lbl.Text = "キャンセル要求を送信中..."
    })
    $pf.Controls.Add($btn)

    $pf.Add_FormClosing({
        param($sender, $e)
        if ($e.CloseReason -eq [System.Windows.Forms.CloseReason]::UserClosing -and -not $script:SetupFinished) {
            $script:CancelRequested = $true
        }
    })

    $script:ProgressForm = $pf
    $script:ProgressBar = $bar
    $script:LblStep = $lbl
    $script:TxtLog = $tb
    $script:BtnAbort = $btn
    $script:SetupFinished = $false
    $pf.Show()
    [System.Windows.Forms.Application]::DoEvents()
}

# --- Main Setup Flow ---
$selectedMode = if ([string]::IsNullOrWhiteSpace($ModelMode)) { "Skip" } else { $ModelMode }
$selectedLocalModel = $PreselectedModelPath
$shouldCreateShortcut = (-not $SkipShortcut)
$selectedShortcutName = $ShortcutName

if ($script:IsInteractive) {
    $choice = Show-ConsentDialog
    if (-not $choice) {
        Write-Host "[setup_wizard] 初回セットアップがユーザーによりキャンセルされました。"
        exit 1
    }
    $selectedMode = $choice.ModelMode
    $selectedLocalModel = $choice.LocalPath
    $shouldCreateShortcut = $choice.CreateShortcut
    $UserDataDir = $choice.UserDataDir
    $selectedShortcutName = $choice.ShortcutName
}

if ($UserDataDir.Length -gt 3) {
    $UserDataDir = $UserDataDir.TrimEnd('\', '/')
}
$LogsDir = Join-Path $UserDataDir "logs"
$SetupLogPath = Join-Path $LogsDir "setup.log"
$UserModelDir = Join-Path $UserDataDir "models\plamo-3-translate-2b-GGUF"

foreach ($dirPath in @($LogsDir, $RuntimeToolsDir, $CacheDownloadDir, $StampsDir, $ToolsDir, $LlamaBaseDir, $UserModelDir)) {
    if (-not (Test-Path -LiteralPath $dirPath)) {
        New-Item -ItemType Directory -Path $dirPath -Force | Out-Null
    }
}

if ($UserDataDir -ne $DefaultUserDataDir -or $selectedShortcutName -ne $DefaultShortcutName) {
    $profileObj = @{
        UserDataDir  = $UserDataDir
        ShortcutName = $selectedShortcutName
    }
    $profileJson = $profileObj | ConvertTo-Json -Depth 3
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($RuntimeProfilePath, $profileJson, $utf8NoBom)
    Write-SetupLog "分離プロファイル設定を保存しました: $RuntimeProfilePath (UserDataDir=$UserDataDir, ShortcutName=$selectedShortcutName)"
} elseif (Test-Path -LiteralPath $RuntimeProfilePath) {
    Remove-Item -LiteralPath $RuntimeProfilePath -Force -ErrorAction SilentlyContinue
}

if ($script:IsInteractive) {
    Show-ProgressWindow
} else {
    Write-SetupLog "自動承認モードでセットアップを開始します (ModelMode=$selectedMode, UserDataDir=$UserDataDir, ShortcutName=$selectedShortcutName)"
}

$savedEnv = @{
    PATH                    = $env:PATH
    UV_PYTHON_INSTALL_DIR   = $env:UV_PYTHON_INSTALL_DIR
    UV_PYTHON_PREFERENCE    = $env:UV_PYTHON_PREFERENCE
    UV_MANAGED_PYTHON       = $env:UV_MANAGED_PYTHON
    UV_PYTHON_BIN_DIR       = $env:UV_PYTHON_BIN_DIR
    UV_CACHE_DIR            = $env:UV_CACHE_DIR
    UV_LINK_MODE            = $env:UV_LINK_MODE
    UV_PROJECT_ENVIRONMENT  = $env:UV_PROJECT_ENVIRONMENT
    PAPERBRIDGE_RUNTIME_DIR = $env:PAPERBRIDGE_RUNTIME_DIR
    npm_config_cache        = $env:npm_config_cache
    PYTHONUTF8              = $env:PYTHONUTF8
    PYTHONIOENCODING        = $env:PYTHONIOENCODING
}

try {
    Write-SetupLog "展開先プロジェクト: $ProjectRoot"
    Write-SetupLog "ユーザーデータ領域: $UserDataDir"

    # 1. Disk Space Preflight
    Update-SetupUiProgress -Percent 2 -StatusText "空きディスク容量を確認中..."
    Check-DiskSpace -NeedsModelSpace ($selectedMode -eq "Download" -or $selectedMode -eq "Local")

    # 2. Install uv 0.11.15
    Update-SetupUiProgress -Percent 5 -StatusText "uv 0.11.15 を準備中..."
    Install-PinnedZipTool -Spec $PINNED_TOOLS.uv -BaseProgress 5 -SpanProgress 10
    $uvExe = Join-Path $PINNED_TOOLS.uv.InstallDir "uv.exe"

    # 3. Install isolated Python 3.13 and sync uv.lock (zero changes to global bin or Windows registry)
    Test-CancelRequested
    Remove-Item -Path Env:UV_MANAGED_PYTHON -ErrorAction SilentlyContinue
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeToolsDir "python"
    $env:UV_PYTHON_PREFERENCE = "only-managed"
    $env:UV_PYTHON_BIN_DIR = Join-Path $RuntimeToolsDir "bin"
    $env:UV_CACHE_DIR = Join-Path $RuntimeToolsDir "uv-cache"
    $env:UV_LINK_MODE = "copy"
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $ProjectRoot ".venv"
    $env:npm_config_cache = Join-Path $RuntimeToolsDir "npm-cache"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"

    Update-SetupUiProgress -Percent 18 -StatusText "分離 Python 3.13 ランタイムを導入中..."
    Invoke-LoggedExternalCommand -StepLabel "uv python install 3.13" -FilePath $uvExe -ArgumentList @("python", "install", "3.13", "--no-bin", "--no-registry") -WorkingDirectory $ProjectRoot

    Test-CancelRequested
    Update-SetupUiProgress -Percent 28 -StatusText "uv.lock に従いバックエンド依存関係 (.venv) を構築中..."
    Invoke-LoggedExternalCommand -StepLabel "uv sync --locked" -FilePath $uvExe -ArgumentList @("sync", "--python", "3.13", "--locked", "--no-dev") -WorkingDirectory $ProjectRoot

    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw ".venv\Scripts\python.exe が見つかりません。Python 環境構築に失敗しました。"
    }
    # Pin PAPERBRIDGE_RUNTIME_DIR before importing backend.api.app (create_app() runs on import)
    $env:PAPERBRIDGE_RUNTIME_DIR = $UserDataDir
    Update-SetupUiProgress -Percent 42 -StatusText "バックエンドの読み込み動作を確認中..."
    Invoke-LoggedExternalCommand -StepLabel "backend import check" -FilePath $venvPython -ArgumentList @("-c", "import backend.api.app; print('BACKEND_IMPORT_OK')") -WorkingDirectory $ProjectRoot

    # 4. Install Node.js v22.20.0 & Build Frontend
    Test-CancelRequested
    Update-SetupUiProgress -Percent 45 -StatusText "Node.js v22.20.0 を準備中..."
    Install-PinnedZipTool -Spec $PINNED_TOOLS.node -BaseProgress 45 -SpanProgress 10

    $nodeDir = $PINNED_TOOLS.node.InstallDir
    $nodeExe = Join-Path $nodeDir "node.exe"
    $npmCliJs = Join-Path $nodeDir "node_modules\npm\bin\npm-cli.js"
    if (-not (Test-Path -LiteralPath $npmCliJs)) {
        throw "Node.js 内の npm-cli.js が見つかりません: $npmCliJs"
    }
    $env:PATH = "$nodeDir;$($savedEnv.PATH)"

    $lockFilePath = Join-Path $FrontendDir "package-lock.json"
    $nodeModulesDir = Join-Path $FrontendDir "node_modules"
    $feStampFile = Join-Path $StampsDir "frontend-deps.sha256"
    $lockHash = if (Test-Path -LiteralPath $lockFilePath) { Get-FileSha256Hex -FilePath $lockFilePath } else { "" }
    $needsNpmCi = $true
    if (-not $ForceReinstallTools -and -not [string]::IsNullOrWhiteSpace($lockHash) -and (Test-Path -LiteralPath $nodeModulesDir) -and (Test-Path -LiteralPath $feStampFile)) {
        $prevHash = (Get-Content -LiteralPath $feStampFile -Raw).Trim()
        if ($prevHash -eq $lockHash) {
            $needsNpmCi = $false
            Write-SetupLog "frontend/node_modules は package-lock.json と一致済みのため npm ci を省略します。"
        }
    }

    if ($needsNpmCi) {
        Test-CancelRequested
        Update-SetupUiProgress -Percent 56 -StatusText "フロントエンド依存関係 (npm ci) を導入中..."
        if (Test-Path -LiteralPath $lockFilePath) {
            Invoke-LoggedExternalCommand -StepLabel "npm ci" -FilePath $nodeExe -ArgumentList @($npmCliJs, "ci") -WorkingDirectory $FrontendDir
            Set-Content -LiteralPath $feStampFile -Value $lockHash -Encoding ASCII
        } else {
            Invoke-LoggedExternalCommand -StepLabel "npm install" -FilePath $nodeExe -ArgumentList @($npmCliJs, "install") -WorkingDirectory $FrontendDir
        }
    }

    Test-CancelRequested
    Update-SetupUiProgress -Percent 66 -StatusText "フロントエンド本番バンドル (npm run build) を生成中..."
    Invoke-LoggedExternalCommand -StepLabel "npm run build" -FilePath $nodeExe -ArgumentList @($npmCliJs, "run", "build") -WorkingDirectory $FrontendDir

    $distIndex = Join-Path $FrontendDir "dist\index.html"
    if (-not (Test-Path -LiteralPath $distIndex)) {
        throw "frontend\dist\index.html が生成されませんでした。"
    }

    # 5. Install llama.cpp b12167 (Vulkan + CPU-only)
    Test-CancelRequested
    Update-SetupUiProgress -Percent 72 -StatusText "llama.cpp b12167 (Vulkan GPU版) を導入中..."
    Install-PinnedZipTool -Spec $PINNED_TOOLS.llama_vulkan -BaseProgress 72 -SpanProgress 8

    Test-CancelRequested
    Update-SetupUiProgress -Percent 80 -StatusText "llama.cpp b12167 (CPU専用フォールバック版) を導入中..."
    Install-PinnedZipTool -Spec $PINNED_TOOLS.llama_cpu -BaseProgress 80 -SpanProgress 6

    # 6. Translation Model Provisioning
    $projModelFile = Join-Path $ProjectModelDir $MODEL_FILE_NAME
    $userModelFile = Join-Path $UserModelDir $MODEL_FILE_NAME
    if ($selectedMode -eq "Download") {
        if ((Test-VerifiedFile -FilePath $projModelFile -ExpectedSize $MODEL_SIZE -ExpectedSha256 $MODEL_SHA256 -RequireGguf) -or
            (Test-VerifiedFile -FilePath $userModelFile -ExpectedSize $MODEL_SIZE -ExpectedSha256 $MODEL_SHA256 -RequireGguf)) {
            Write-SetupLog "翻訳モデルは既に検証済みで配置されています。"
        } else {
            Test-CancelRequested
            Invoke-VerifiedDownload -Label "PLaMo 3 Translate 2B (Q4_K_M)" -Url $MODEL_URL -DestPath $userModelFile -ExpectedSize $MODEL_SIZE -ExpectedSha256 $MODEL_SHA256 -BaseProgress 86 -SpanProgress 10 -RequireGguf
        }
    } elseif ($selectedMode -eq "Local") {
        if ([string]::IsNullOrWhiteSpace($selectedLocalModel)) {
            throw "-ModelMode Local が指定されましたが、ファイルパスが指定されていません。"
        }
        Update-SetupUiProgress -Percent 88 -StatusText "既存の翻訳モデル (SHA-256) を検証して配置中..."
        Write-SetupLog "既存モデルを検証しています: $selectedLocalModel"
        if (-not (Test-VerifiedFile -FilePath $selectedLocalModel -ExpectedSize $MODEL_SIZE -ExpectedSha256 $MODEL_SHA256 -RequireGguf)) {
            throw "選択されたファイルは正しい $MODEL_FILE_NAME (サイズ/GGUF/SHA-256) と一致しません。"
        }
        $tmpCopy = "$userModelFile.copying"
        Copy-Item -LiteralPath $selectedLocalModel -Destination $tmpCopy -Force
        Move-Item -LiteralPath $tmpCopy -Destination $userModelFile -Force
        Write-SetupLog "既存モデルを配置しました: $userModelFile"
    } else {
        Write-SetupLog "モデル配置はスキップされました。初回起動時（または models フォルダ配置時）に設定できます。"
    }

    # 7. Desktop Shortcut Creation
    if ($shouldCreateShortcut) {
        Test-CancelRequested
        Update-SetupUiProgress -Percent 97 -StatusText "デスクトップアイコンを作成中..."
        $shortcutScript = Join-Path $ScriptDir "setup_desktop_shortcut.ps1"
        if (Test-Path -LiteralPath $shortcutScript) {
            Invoke-LoggedExternalCommand -StepLabel "desktop shortcut setup" -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $shortcutScript, "-ShortcutName", $selectedShortcutName, "-Silent") -WorkingDirectory $ProjectRoot
        } else {
            Write-SetupLog "setup_desktop_shortcut.ps1 が見つからないためアイコン作成をスキップしました。" "WARN"
        }
    }

    Update-SetupUiProgress -Percent 100 -StatusText "セットアップ完了！"
    Write-SetupLog "すべてのセットアップ工程が正常に完了しました。"
    $script:SetupFinished = $true

    if ($script:IsInteractive) {
        if ($script:ProgressForm -and -not $script:ProgressForm.IsDisposed) {
            $script:ProgressForm.Close()
        }
        $msg = "PaperBridge Local の初回セットアップが完了しました！`n`n"
        if ($shouldCreateShortcut) {
            $msg += "デスクトップの「$selectedShortcutName」アイコンをダブルクリックして起動できます。"
        } else {
            $msg += "「起動する.bat」をダブルクリックして起動できます。"
        }
        $msg += "`n保存先: $UserDataDir"
        $msg += "`n`n今すぐ PaperBridge Local を起動しますか？"
        $ans = [System.Windows.Forms.MessageBox]::Show($msg, "セットアップ完了", [System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Information)
        if ($ans -eq [System.Windows.Forms.DialogResult]::Yes) {
            $vbsPath = Join-Path $ScriptDir "run_silent.vbs"
            Start-Process "wscript.exe" -ArgumentList "//nologo `"$vbsPath`""
        }
    }
    exit 0
} catch {
    $script:SetupFinished = $true
    $errMsg = $_.Exception.Message
    Write-SetupLog "セットアップ中にエラーが発生しました: $errMsg" "ERROR"
    if ($script:IsInteractive) {
        if ($script:ProgressForm -and -not $script:ProgressForm.IsDisposed) {
            $script:ProgressForm.Close()
        }
        [System.Windows.Forms.MessageBox]::Show("セットアップを完了できませんでした。`n`n理由: $errMsg`n`nログファイル: $SetupLogPath", "セットアップエラー", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    } else {
        Write-Error $errMsg
    }
    exit 1
} finally {
    foreach ($kv in $savedEnv.GetEnumerator()) {
        if ($null -eq $kv.Value) {
            Remove-Item -Path "Env:$($kv.Key)" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$($kv.Key)" -Value $kv.Value
        }
    }
}
