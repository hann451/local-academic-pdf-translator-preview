# PaperBridge Local - External Self-Updater Script
# Runs independently after FastAPI and llama-server stop, safely applies updates,
# preserves user data / local config / uat-artifacts, and relaunches the app.
param(
    [string]$ProjectRoot = "",
    [string]$UserDataDir = "",
    [int]$BackendPort = 8080,
    [int]$LlamaPort = 8000,
    [int]$BackendPid = 0,
    [string]$RepoSlug = "hann451/local-academic-pdf-translator-preview",
    [string]$Branch = "main",
    [string]$TargetCommitSha = "",
    [string]$ZipUrlOverride = "",
    [string]$LocalZipPath = "",
    [int]$PortWaitTimeoutSec = 45,
    [switch]$NoRebuild = $false,
    [switch]$NoRestart = $false,
    [switch]$UseCpuInference = $false,
    [switch]$DryRun = $false,
    [switch]$NoUi = $false
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProgressCommonScript = Join-Path $ScriptDir "progress_window_common.ps1"
if (Test-Path -LiteralPath $ProgressCommonScript) {
    . $ProgressCommonScript
}
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $ScriptDir
}
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot.Trim().Trim('"'))

$RuntimeProfilePath = Join-Path $ProjectRoot ".runtime_tools\runtime_profile.json"
$profileUserDataDir = ""
if (Test-Path -LiteralPath $RuntimeProfilePath) {
    try {
        $prof = Get-Content -LiteralPath $RuntimeProfilePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($prof -and -not [string]::IsNullOrWhiteSpace($prof.UserDataDir)) {
            $profileUserDataDir = [string]$prof.UserDataDir
        }
    } catch {}
}

if ([string]::IsNullOrWhiteSpace($UserDataDir)) {
    if (-not [string]::IsNullOrWhiteSpace($env:PAPERBRIDGE_RUNTIME_DIR)) {
        $UserDataDir = $env:PAPERBRIDGE_RUNTIME_DIR
    } elseif (-not [string]::IsNullOrWhiteSpace($profileUserDataDir)) {
        $UserDataDir = $profileUserDataDir
    } elseif (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $UserDataDir = Join-Path $env:LOCALAPPDATA "PaperBridgeLocal"
    } else {
        $UserDataDir = Join-Path ([System.IO.Path]::GetTempPath()) "PaperBridgeLocal"
    }
}
$UserDataDir = [System.IO.Path]::GetFullPath($UserDataDir.Trim().Trim('"'))

$LogsDir = Join-Path $UserDataDir "logs"
$UpdaterLogPath = Join-Path $LogsDir "updater.log"
$UpdateSignalPath = Join-Path $UserDataDir ".update_in_progress"
$UpdateProgressPath = Join-Path $UserDataDir "update_progress.json"
$script:ShowUpdateUi = (-not $NoUi) -and (-not $DryRun)
$script:ProgressHandle = $null
$script:LastUpdatePercent = 0

if (-not (Test-Path -LiteralPath $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}

function Report-UpdateProgress {
    param(
        [int]$Percent,
        [string]$Stage,
        [string]$Message,
        [string]$Detail = "",
        [string]$Status = "running",
        [string]$ErrorMessage = ""
    )
    $clamped = [Math]::Max(0, [Math]::Min(100, $Percent))
    $script:LastUpdatePercent = $clamped
    if (Get-Command Write-PaperBridgeProgressFile -ErrorAction SilentlyContinue) {
        Write-PaperBridgeProgressFile `
            -FilePath $UpdateProgressPath `
            -Percent $clamped `
            -Stage $Stage `
            -Status $Status `
            -Message $Message `
            -Detail $Detail `
            -ErrorMessage $ErrorMessage `
            -LogPath $UpdaterLogPath
    }
    if ($script:ProgressHandle -and $script:ProgressHandle.IsActive -and (Get-Command Update-PaperBridgeProgressWindow -ErrorAction SilentlyContinue)) {
        Update-PaperBridgeProgressWindow `
            -Handle $script:ProgressHandle `
            -Percent $clamped `
            -Message $Message `
            -Detail $Detail `
            -Status $Status
    }
}

function Write-UpdateLog {
    param([string]$Message, [string]$Level = "INFO")
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$ts] [$Level] $Message"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $sw = New-Object System.IO.StreamWriter($UpdaterLogPath, $true, $utf8NoBom)
    try {
        $sw.WriteLine($line)
    } finally {
        $sw.Close()
    }
    Write-Output $line
}

function Test-PortInUse {
    param([int]$Port)
    if ($Port -le 0) { return $false }
    try {
        $ipProps = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()
        foreach ($ep in $ipProps.GetActiveTcpListeners()) {
            if ($ep.Port -eq $Port) {
                return $true
            }
        }
    } catch {}

    $sock = $null
    try {
        $sock = New-Object System.Net.Sockets.Socket(
            [System.Net.Sockets.AddressFamily]::InterNetwork,
            [System.Net.Sockets.SocketType]::Stream,
            [System.Net.Sockets.ProtocolType]::Tcp
        )
        $sock.ExclusiveAddressUse = $true
        $sock.Bind((New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Loopback, $Port)))
        return $false
    } catch {
        return $true
    } finally {
        if ($sock) {
            try { $sock.Close() } catch {}
            try { $sock.Dispose() } catch {}
        }
    }
}

function Wait-ProcessExitSafe {
    param(
        [int]$TargetPid,
        [int]$GraceSeconds = 10
    )
    if ($TargetPid -le 0 -or $TargetPid -eq $PID) { return }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $GraceSeconds) {
        $proc = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
        if (-not $proc -or $proc.HasExited) {
            Write-UpdateLog "バックエンドプロセス (PID=$TargetPid) の終了を確認しました。"
            return
        }
        Start-Sleep -Milliseconds 250
    }

    # Verify process identity before fallback termination
    $procFinal = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
    if ($procFinal -and (-not $procFinal.HasExited)) {
        $pname = $procFinal.ProcessName.ToLowerInvariant()
        if ($pname -eq "python" -or $pname -eq "pythonw" -or $pname -eq "uvicorn") {
            Write-UpdateLog "バックエンドプロセス (PID=$TargetPid, Name=$pname) が待機時間内に終了しなかったため停止を試行します。" "WARN"
            try {
                Stop-Process -Id $TargetPid -Force -ErrorAction SilentlyContinue
                Start-Sleep -Milliseconds 500
            } catch {}
        } else {
            Write-UpdateLog "安全性保護: PID=$TargetPid ($pname) は Python/uvicorn ではないため強制終了をスキップしました。" "WARN"
        }
    }
}

function Wait-PortsReleased {
    param(
        [int[]]$Ports,
        [int]$TimeoutSeconds = 45
    )
    $validPorts = @($Ports | Where-Object { $_ -gt 0 } | Select-Object -Unique)
    if ($validPorts.Count -eq 0) { return }

    Write-UpdateLog "ポート解放を待機中: $($validPorts -join ', ') (最大 ${TimeoutSeconds}秒)..."
    Report-UpdateProgress -Percent 10 -Stage "waiting_ports" -Message "サーバーポート ($($validPorts -join ', ')) の解放を待機しています..." -Detail "最大 ${TimeoutSeconds} 秒待機中"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $reportSw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $busy = @()
        foreach ($p in $validPorts) {
            if (Test-PortInUse -Port $p) {
                $busy += $p
            }
        }
        if ($busy.Count -eq 0) {
            Write-UpdateLog "対象ポート ($($validPorts -join ', ')) の完全解放を確認しました。"
            Report-UpdateProgress -Percent 14 -Stage "ports_released" -Message "サーバーポートの解放を確認しました。" -Detail "ポート $($validPorts -join ', ') 解放完了"
            return
        }
        if ($reportSw.ElapsedMilliseconds -ge 450) {
            $elapsedSec = [int][Math]::Floor($sw.Elapsed.TotalSeconds)
            $pPct = [Math]::Min(14, 10 + [int][Math]::Floor(($sw.Elapsed.TotalSeconds / [Math]::Max(1, $TimeoutSeconds)) * 4.0))
            Report-UpdateProgress -Percent $pPct -Stage "waiting_ports" -Message "サーバーポートの終了処理を待機しています..." -Detail ("待機中ポート: {0} ({1}秒経過)" -f ($busy -join ", "), $elapsedSec)
            $reportSw.Restart()
        }
        Start-Sleep -Milliseconds 300
    }

    $stillBusy = @()
    foreach ($p in $validPorts) {
        if (Test-PortInUse -Port $p) { $stillBusy += $p }
    }
    throw "ポート ($($stillBusy -join ', ')) が ${TimeoutSeconds}秒以内に解放されなかったためアップデートを中止しました。"
}

function Wait-LauncherMutexReleased {
    param(
        [string]$DataDir,
        [int]$TimeoutSeconds = 12
    )
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $dirBytes = [System.Text.Encoding]::UTF8.GetBytes($DataDir.ToLowerInvariant())
        $hashBytes = $sha.ComputeHash($dirBytes)
        $sha.Dispose()
        $shortId = ([BitConverter]::ToString($hashBytes) -replace "-", "").Substring(0, 12)
        $mutexName = "Local\PaperBridgeLocal_$shortId"

        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        while ($sw.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
            $createdNew = $false
            $m = $null
            try {
                $m = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
                if ($createdNew) {
                    $m.ReleaseMutex()
                    $m.Dispose()
                    Write-UpdateLog "ランチャー Mutex ($mutexName) の解放を確認しました。"
                    return
                }
            } finally {
                if ($m) { try { $m.Dispose() } catch {} }
            }
            Start-Sleep -Milliseconds 300
        }
        Write-UpdateLog "ランチャー Mutex ($mutexName) の解放確認がタイムアウトしました（続行します）。" "WARN"
    } catch {
        Write-UpdateLog "ランチャー Mutex 確認中に非致命的エラー: $($_.Exception.Message)" "WARN"
    }
}

function Test-IsProtectedRelativePath {
    param([string]$RelativePath)
    $norm = ($RelativePath -replace '/', '\').TrimStart('\')
    if ([string]::IsNullOrWhiteSpace($norm)) { return $true }
    $parts = $norm.Split('\')
    $top = $parts[0].ToLowerInvariant()

    # Protected top-level directories (never overwrite or delete)
    $protectedDirs = @(
        ".git",
        ".venv",
        ".runtime_tools",
        "tools",
        "models",
        "uat-artifacts",
        "runtime_data",
        "uploads",
        "outputs",
        "cache",
        "working",
        "temp"
    )
    if ($protectedDirs -contains $top) {
        return $true
    }

    # Protected nested directories
    if ($parts.Length -ge 2) {
        $sub2 = ($parts[0] + "\" + $parts[1]).ToLowerInvariant()
        if ($sub2 -eq "frontend\node_modules" -or $sub2 -eq "frontend\dist") {
            return $true
        }
    }

    # Protected local configuration / secret / data files
    $leaf = $parts[$parts.Length - 1].ToLowerInvariant()
    if ($leaf -eq ".env" -or $leaf -eq ".env.local" -or $leaf -eq ".env.development" -or $leaf -eq ".env.production") {
        return $true
    }
    if ($leaf -like ".env.*" -and $leaf -ne ".env.example") {
        return $true
    }
    if ($leaf -like "*.gguf" -or $leaf -like "*.sqlite" -or $leaf -like "*.sqlite3" -or $leaf -like "*.db" -or $leaf -like "*.key" -or $leaf -like "*.pem") {
        return $true
    }
    return $false
}

function Copy-FileWithRetry {
    param(
        [string]$SourcePath,
        [string]$DestPath,
        [int]$MaxRetries = 4
    )
    $destParent = Split-Path -Parent $DestPath
    if (-not [string]::IsNullOrWhiteSpace($destParent) -and -not (Test-Path -LiteralPath $destParent)) {
        New-Item -ItemType Directory -Path $destParent -Force | Out-Null
    }
    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        try {
            [System.IO.File]::Copy($SourcePath, $DestPath, $true)
            return
        } catch {
            if ($attempt -ge $MaxRetries) {
                throw "ファイル上書きに失敗しました ($DestPath): $($_.Exception.Message)"
            }
            Start-Sleep -Milliseconds 350
        }
    }
}

function Invoke-GitUpdate {
    param(
        [string]$RepoRoot,
        [string]$TargetBranch
    )
    $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    if (-not $gitCmd) {
        throw "Git 管理下のディレクトリですが git コマンドが見つかりません。"
    }
    Write-UpdateLog "Git 管理環境を検出しました。'git pull origin $TargetBranch' を実行します..."
    Report-UpdateProgress -Percent 25 -Stage "git_pull" -Message "Git リポジトリから最新版を取得しています..." -Detail "ブランチ: origin/$TargetBranch"
    if ($DryRun) {
        Write-UpdateLog "[DRY_RUN] git -C `"$RepoRoot`" pull origin $TargetBranch"
        return ""
    }
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $pullOut = (& git -C $RepoRoot pull origin $TargetBranch 2>&1) -join "`n"
        $pullRc = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    if (-not [string]::IsNullOrWhiteSpace($pullOut)) {
        Write-UpdateLog "git pull 出力: $pullOut"
    }
    if ($pullRc -ne 0) {
        throw "git pull origin $TargetBranch が終了コード $pullRc で失敗しました: $pullOut"
    }

    $headSha = ""
    try {
        $headSha = (& git -C $RepoRoot rev-parse HEAD 2>$null).Trim()
    } catch {}
    Write-UpdateLog "Git 更新が完了しました (HEAD=$headSha)。"
    Report-UpdateProgress -Percent 82 -Stage "git_applied" -Message "最新コードの取得が完了しました。" -Detail "HEAD: $headSha"
    return $headSha
}

function Download-ZipFile {
    param(
        [string]$Url,
        [string]$DestPath,
        [scriptblock]$ProgressCallback = $null
    )
    Write-UpdateLog "ZIP アーカイブをダウンロード中: $Url"
    $req = [System.Net.HttpWebRequest]::Create($Url)
    $req.Method = "GET"
    $req.Timeout = 30000
    $req.ReadWriteTimeout = 60000
    $req.AllowAutoRedirect = $true
    $req.UserAgent = "PaperBridgeLocal-Updater/1.0"

    $resp = $null
    $resStream = $null
    $fs = $null
    try {
        $resp = $req.GetResponse()
        $totalBytes = [int64]$resp.ContentLength
        $resStream = $resp.GetResponseStream()
        $fs = [System.IO.File]::Create($DestPath)
        $buf = New-Object byte[] 131072
        $downloaded = 0L
        $dlSw = [System.Diagnostics.Stopwatch]::StartNew()
        while (($n = $resStream.Read($buf, 0, $buf.Length)) -gt 0) {
            $fs.Write($buf, 0, $n)
            $downloaded += $n
            if ($ProgressCallback -and $dlSw.ElapsedMilliseconds -ge 120) {
                & $ProgressCallback $downloaded $totalBytes
                $dlSw.Restart()
            }
        }
        if ($ProgressCallback) {
            & $ProgressCallback $downloaded $totalBytes
        }
    } finally {
        if ($fs) { try { $fs.Close() } catch {} }
        if ($resStream) { try { $resStream.Close() } catch {} }
        if ($resp) { try { $resp.Close() } catch {} }
    }
}

function Resolve-RemoteHeadShaBestEffort {
    param(
        [string]$Slug,
        [string]$TargetBranch
    )
    try {
        $apiUrl = "https://api.github.com/repos/$Slug/commits/$TargetBranch"
        $req = [System.Net.HttpWebRequest]::Create($apiUrl)
        $req.Method = "GET"
        $req.Timeout = 8000
        $req.Accept = "application/vnd.github+json"
        $req.UserAgent = "PaperBridgeLocal-Updater/1.0"
        $resp = $req.GetResponse()
        try {
            $sr = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
            $body = $sr.ReadToEnd()
            $sr.Close()
            $parsed = $body | ConvertFrom-Json
            if ($parsed -and $parsed.sha) {
                return ([string]$parsed.sha).Trim()
            }
        } finally {
            $resp.Close()
        }
    } catch {}
    return ""
}

function Invoke-ZipUpdate {
    param(
        [string]$TargetRoot,
        [string]$Slug,
        [string]$TargetBranch,
        [string]$OverrideUrl,
        [string]$ZipOverridePath,
        [string]$KnownSha
    )
    Write-UpdateLog "ZIP 展開環境を検出しました。一時ディレクトリへ最新版を取得して保護対象を除き上書きします..."
    $zipUrl = if (-not [string]::IsNullOrWhiteSpace($OverrideUrl)) {
        $OverrideUrl
    } else {
        "https://github.com/$Slug/archive/refs/heads/$TargetBranch.zip"
    }

    if ($DryRun) {
        Write-UpdateLog "[DRY_RUN] ZIP URL=$zipUrl LocalZipPath=$ZipOverridePath TargetRoot=$TargetRoot"
        return $KnownSha
    }

    $tmpBase = Join-Path ([System.IO.Path]::GetTempPath()) ("pb_update_" + [guid]::NewGuid().ToString("N").Substring(0, 10))
    $zipFile = Join-Path $tmpBase "main.zip"
    $extractDir = Join-Path $tmpBase "extracted"
    New-Item -ItemType Directory -Path $tmpBase -Force | Out-Null
    New-Item -ItemType Directory -Path $extractDir -Force | Out-Null

    try {
        if (-not [string]::IsNullOrWhiteSpace($ZipOverridePath)) {
            if (-not (Test-Path -LiteralPath $ZipOverridePath -PathType Leaf)) {
                throw "指定されたローカル ZIP ファイルが見つかりません: $ZipOverridePath"
            }
            Write-UpdateLog "指定されたローカル ZIP を使用します: $ZipOverridePath"
            Report-UpdateProgress -Percent 55 -Stage "downloading" -Message "アップデートパッケージを準備しています..." -Detail $ZipOverridePath
            $zipFile = $ZipOverridePath
        } else {
            Report-UpdateProgress -Percent 20 -Stage "downloading" -Message "GitHub から最新版パッケージをダウンロードしています..." -Detail $zipUrl
            Download-ZipFile -Url $zipUrl -DestPath $zipFile -ProgressCallback {
                param([int64]$dlBytes, [int64]$totalBytes)
                $mbDl = $dlBytes / 1048576.0
                if ($totalBytes -gt 0) {
                    $ratio = [Math]::Min(1.0, $dlBytes / [double]$totalBytes)
                    $pct = 20 + [int][Math]::Floor($ratio * 35.0)
                    $mbTot = $totalBytes / 1048576.0
                    Report-UpdateProgress -Percent $pct -Stage "downloading" -Message "GitHub から最新版パッケージをダウンロードしています..." -Detail ("{0:N1} MB / {1:N1} MB ダウンロード済み" -f $mbDl, $mbTot)
                } else {
                    $pct = [Math]::Min(53, 20 + [int][Math]::Floor($mbDl * 1.2))
                    Report-UpdateProgress -Percent $pct -Stage "downloading" -Message "GitHub から最新版パッケージをダウンロードしています..." -Detail ("{0:N1} MB ダウンロード済み..." -f $mbDl)
                }
            }
        }

        Write-UpdateLog "ZIP アーカイブを展開中: $extractDir"
        Report-UpdateProgress -Percent 58 -Stage "extracting" -Message "アップデートパッケージを展開しています..." -Detail "ZIP アーカイブを展開中"
        [System.IO.Compression.ZipFile]::ExtractToDirectory($zipFile, $extractDir)

        # GitHub main.zip typically wraps everything inside a single top-level directory
        $topDirs = @(Get-ChildItem -LiteralPath $extractDir -Directory)
        $topFiles = @(Get-ChildItem -LiteralPath $extractDir -File)
        $sourceRoot = if ($topDirs.Count -eq 1 -and $topFiles.Count -eq 0) {
            $topDirs[0].FullName
        } else {
            $extractDir
        }

        $copiedCount = 0
        $skippedCount = 0
        $processedCount = 0
        $srcPrefix = $sourceRoot.TrimEnd('\') + "\"
        $allFiles = @(Get-ChildItem -LiteralPath $sourceRoot -File -Recurse -Force)
        $totalFiles = $allFiles.Count
        $copySw = [System.Diagnostics.Stopwatch]::StartNew()
        Report-UpdateProgress -Percent 62 -Stage "copying_files" -Message "新しいプログラムファイルを適用しています..." -Detail "全 $totalFiles 件（設定・モデル・UAT成果物は保護）"
        foreach ($f in $allFiles) {
            $processedCount++
            $rel = $f.FullName.Substring($srcPrefix.Length)
            if (Test-IsProtectedRelativePath -RelativePath $rel) {
                $skippedCount++
            } else {
                $dest = Join-Path $TargetRoot $rel
                Copy-FileWithRetry -SourcePath $f.FullName -DestPath $dest
                $copiedCount++
            }
            if (($processedCount % 25 -eq 0) -or ($copySw.ElapsedMilliseconds -ge 120)) {
                $pct = 62 + [int][Math]::Floor(($processedCount / [double][Math]::Max(1, $totalFiles)) * 20.0)
                Report-UpdateProgress -Percent $pct -Stage "copying_files" -Message "新しいプログラムファイルを適用しています..." -Detail ("$processedCount / $totalFiles 件処理（更新 $copiedCount 件 / 保護 $skippedCount 件）")
                $copySw.Restart()
            }
        }
        Report-UpdateProgress -Percent 82 -Stage "copying_files" -Message "プログラムファイルの適用が完了しました。" -Detail "更新 $copiedCount 件 / 保護スキップ $skippedCount 件"
        Write-UpdateLog "ZIP 上書き完了: 更新ファイル $copiedCount 件 / 保護スキップ $skippedCount 件。"

        Report-UpdateProgress -Percent 84 -Stage "recording_version" -Message "バージョン情報を記録しています..."
        $recordedSha = $KnownSha
        if ([string]::IsNullOrWhiteSpace($recordedSha) -and [string]::IsNullOrWhiteSpace($ZipOverridePath)) {
            $recordedSha = Resolve-RemoteHeadShaBestEffort -Slug $Slug -TargetBranch $TargetBranch
        }
        if (-not [string]::IsNullOrWhiteSpace($recordedSha)) {
            $shortSha = if ($recordedSha.Length -ge 7) { $recordedSha.Substring(0, 7) } else { $recordedSha }
            $verObj = [ordered]@{
                commit_sha = $recordedSha
                short_sha  = $shortSha
                branch     = $TargetBranch
                source     = "zip"
                updated_at = ([DateTime]::UtcNow.ToString("o"))
            }
            $verPath = Join-Path $TargetRoot ".paperbridge_version.json"
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($verPath, ($verObj | ConvertTo-Json -Depth 3), $utf8NoBom)
            Write-UpdateLog "ローカルバージョン記録を更新しました: $verPath ($shortSha)"
        }
        return $recordedSha
    } finally {
        if (Test-Path -LiteralPath $tmpBase) {
            try {
                Remove-Item -LiteralPath $tmpBase -Recurse -Force -ErrorAction SilentlyContinue
            } catch {}
        }
    }
}

function Invoke-PostUpdateRebuildIfApplicable {
    param([string]$TargetRoot)
    if ($NoRebuild -or $DryRun) { return }

    # Rebuild frontend/dist only when node_modules is already provisioned
    $feDir = Join-Path $TargetRoot "frontend"
    $feNodeModules = Join-Path $feDir "node_modules"
    if (Test-Path -LiteralPath $feNodeModules) {
        $bundledNodeDir = Join-Path $TargetRoot ".runtime_tools\node22"
        $bundledNode = Join-Path $bundledNodeDir "node.exe"
        $bundledNpmCli = Join-Path $bundledNodeDir "node_modules\npm\bin\npm-cli.js"
        try {
            if ((Test-Path -LiteralPath $bundledNode) -and (Test-Path -LiteralPath $bundledNpmCli)) {
                Write-UpdateLog "同梱 Node.js を使用して frontend/dist を再ビルドします..."
                Report-UpdateProgress -Percent 86 -Stage "rebuilding_frontend" -Message "UI アセットを再ビルドしています..." -Detail "同梱 Node.js で npm run build を実行中"
                $prevPath = $env:PATH
                $env:PATH = "$bundledNodeDir;$prevPath"
                try {
                    $pBuild = Start-Process -FilePath $bundledNode -ArgumentList @("`"$bundledNpmCli`"", "run", "build") -WorkingDirectory $feDir -WindowStyle Hidden -Wait -PassThru
                    Write-UpdateLog "frontend 再ビルド終了 (exit=$($pBuild.ExitCode))。"
                } finally {
                    $env:PATH = $prevPath
                }
            } else {
                $sysNpm = Get-Command npm.cmd -ErrorAction SilentlyContinue
                if (-not $sysNpm) { $sysNpm = Get-Command npm -ErrorAction SilentlyContinue }
                if ($sysNpm) {
                    Write-UpdateLog "システム npm を使用して frontend/dist を再ビルドします..."
                    Report-UpdateProgress -Percent 86 -Stage "rebuilding_frontend" -Message "UI アセットを再ビルドしています..." -Detail "npm run build を実行中"
                    $pBuild = Start-Process -FilePath $sysNpm.Source -ArgumentList @("run", "build") -WorkingDirectory $feDir -WindowStyle Hidden -Wait -PassThru
                    Write-UpdateLog "frontend 再ビルド終了 (exit=$($pBuild.ExitCode))。"
                }
            }
        } catch {
            Write-UpdateLog "frontend 再ビルド中に非致命的エラーが発生しました: $($_.Exception.Message)" "WARN"
        }
    }
}

function Start-RelaunchedApplication {
    param(
        [string]$TargetRoot,
        [string]$DataDir,
        [int]$BPort,
        [int]$LPort,
        [bool]$CpuMode
    )
    if ($NoRestart -or $DryRun) {
        Write-UpdateLog "再起動をスキップしました (NoRestart=$NoRestart, DryRun=$DryRun)。"
        return
    }

    $launcherPs1 = Join-Path $TargetRoot "scripts\launcher.ps1"
    $startBat = Join-Path $TargetRoot "起動する.bat"
    if (Test-Path -LiteralPath $launcherPs1 -PathType Leaf) {
        $argList = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden",
            "-File", "`"$launcherPs1`"",
            "-UserDataDir", "`"$DataDir`"",
            "-BackendPort", "$BPort",
            "-LlamaPort", "$LPort"
        )
        if ($CpuMode) {
            $argList += "-UseCpuInference"
        }
        Write-UpdateLog "アプリを再起動します: powershell.exe $($argList -join ' ')"
        Start-Process -FilePath "powershell.exe" -ArgumentList $argList -WorkingDirectory $TargetRoot -WindowStyle Hidden | Out-Null
    } elseif (Test-Path -LiteralPath $startBat -PathType Leaf) {
        Write-UpdateLog "アプリを再起動します: $startBat"
        Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "`"$startBat`"") -WorkingDirectory $TargetRoot -WindowStyle Hidden | Out-Null
    } else {
        Write-UpdateLog "再起動対象スクリプト (scripts/launcher.ps1 / 起動する.bat) が見つかりませんでした。" "WARN"
    }
}

try {
    Write-UpdateLog "=== PaperBridge Local 自己アップデート開始 ==="
    Write-UpdateLog "ProjectRoot=$ProjectRoot | UserDataDir=$UserDataDir | BackendPort=$BackendPort | LlamaPort=$LlamaPort | DryRun=$DryRun | NoUi=$NoUi"

    if ($script:ShowUpdateUi -and (Get-Command Start-PaperBridgeProgressWindow -ErrorAction SilentlyContinue)) {
        $script:ProgressHandle = Start-PaperBridgeProgressWindow `
            -Title "PaperBridge Local - ソフトウェア更新" `
            -HeaderText "ソフトウェアを更新しています" `
            -InitialMessage "アップデートの準備をしています..." `
            -InitialDetail "対象ディレクトリ: $ProjectRoot" `
            -InitialPercent 4 `
            -LogPath $UpdaterLogPath
    }
    Report-UpdateProgress -Percent 4 -Stage "initializing" -Message "アップデートの準備をしています..." -Detail "対象ディレクトリ: $ProjectRoot"

    # 1. Wait for backend process exit & port release (8080 and 8000)
    if (-not $DryRun) {
        Report-UpdateProgress -Percent 6 -Stage "waiting_backend" -Message "アプリの終了を待機しています..." -Detail "実行中サーバーの安全な停止を確認中"
        Wait-ProcessExitSafe -TargetPid $BackendPid -GraceSeconds 10
        Wait-PortsReleased -Ports @($BackendPort, $LlamaPort) -TimeoutSeconds $PortWaitTimeoutSec
        Report-UpdateProgress -Percent 16 -Stage "waiting_launcher" -Message "起動制御ロックの解放を確認しています..." -Detail "ランチャーの完全終了を確認中"
        Wait-LauncherMutexReleased -DataDir $UserDataDir -TimeoutSeconds 12
    }

    # 2. Fetch and apply latest code (Git vs ZIP) without touching protected paths
    $isGit = Test-Path -LiteralPath (Join-Path $ProjectRoot ".git")
    $appliedSha = ""
    if ($isGit) {
        $appliedSha = Invoke-GitUpdate -RepoRoot $ProjectRoot -TargetBranch $Branch
    } else {
        $appliedSha = Invoke-ZipUpdate `
            -TargetRoot $ProjectRoot `
            -Slug $RepoSlug `
            -TargetBranch $Branch `
            -OverrideUrl $ZipUrlOverride `
            -ZipOverridePath $LocalZipPath `
            -KnownSha $TargetCommitSha
    }

    # 3. Rebuild frontend dist if provisioned
    Invoke-PostUpdateRebuildIfApplicable -TargetRoot $ProjectRoot
    Report-UpdateProgress -Percent 92 -Stage "rebuild_done" -Message "更新プログラムの準備が完了しました。"

    # 4. Clear update-in-progress marker before relaunch
    Report-UpdateProgress -Percent 94 -Stage "clearing_marker" -Message "一時ロックファイルを整理しています..."
    if (Test-Path -LiteralPath $UpdateSignalPath) {
        Remove-Item -LiteralPath $UpdateSignalPath -Force -ErrorAction SilentlyContinue
    }

    # 5. Relaunch application and open browser
    if (-not $NoRestart -and -not $DryRun) {
        Report-UpdateProgress -Percent 96 -Stage "relaunching" -Message "PaperBridge Local を再起動しています..." -Detail "新しいバージョンの起動ウィンドウが開きます"
    }
    Start-RelaunchedApplication `
        -TargetRoot $ProjectRoot `
        -DataDir $UserDataDir `
        -BPort $BackendPort `
        -LPort $LlamaPort `
        -CpuMode ([bool]$UseCpuInference)

    $doneDetail = if ($NoRestart -or $DryRun) { "更新ファイルの適用が正常に完了しました。" } else { "新しいバージョンのアプリを起動しています..." }
    Report-UpdateProgress -Percent 100 -Stage "completed" -Status "completed" -Message "アップデートが完了しました。" -Detail $doneDetail
    if ($script:ShowUpdateUi -and (-not $NoRestart)) {
        Start-Sleep -Milliseconds 700
    }

    Write-UpdateLog "=== PaperBridge Local 自己アップデート正常完了 (SHA=$appliedSha) ==="
    Write-Output "UPDATE_APPLY_SUCCESS"
    exit 0
} catch {
    $errMsg = $_.Exception.Message
    Write-UpdateLog "自己アップデート中にエラーが発生しました: $errMsg" "ERROR"
    if (Test-Path -LiteralPath $UpdateSignalPath) {
        Remove-Item -LiteralPath $UpdateSignalPath -Force -ErrorAction SilentlyContinue
    }
    Report-UpdateProgress -Percent $script:LastUpdatePercent -Stage "failed" -Status "failed" -Message "アップデートに失敗しました。" -Detail "ログ: $UpdaterLogPath" -ErrorMessage $errMsg
    if ($script:ShowUpdateUi -and $script:ProgressHandle -and $script:ProgressHandle.IsActive -and (Get-Command Show-PaperBridgeProgressErrorAndWait -ErrorAction SilentlyContinue)) {
        Show-PaperBridgeProgressErrorAndWait -Handle $script:ProgressHandle -ErrorTitle "アップデートに失敗しました" -ErrorMessage $errMsg -LogPath $UpdaterLogPath
    }
    Write-Error $errMsg
    exit 1
} finally {
    if (Get-Command Close-PaperBridgeProgressWindow -ErrorAction SilentlyContinue) {
        Close-PaperBridgeProgressWindow -Handle $script:ProgressHandle
    }
}
