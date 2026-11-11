# PaperBridge Local - PLaMo 3 Translate Model Setup & Verification Tool
param(
    [string]$TargetModelDir = "",
    [switch]$NonInteractive = $false,
    [switch]$AcceptLicense = $false,
    [string]$PreselectedPath = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$EXPECTED_MODEL_NAME = "plamo-3-translate-2b-Q4_K_M.gguf"
$EXPECTED_SHA256 = "f36d0813dd95573a852ab5d76e374c543632d9e292929e922bcdbf7e53b494e0"
$EXPECTED_SIZE = 1807199392L
$MODEL_REVISION = "232fbd7916519edecddfdae0beae7e76fe70453f"
$DOWNLOAD_URL = "https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF/resolve/$($MODEL_REVISION)/$($EXPECTED_MODEL_NAME)"
$LICENSE_URL_JA = "https://plamo.preferredai.jp/info/plamo-community-license-ja"
$LICENSE_URL_EN = "https://plamo.preferredai.jp/info/plamo-community-license-en"

if ([string]::IsNullOrWhiteSpace($TargetModelDir)) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $ProjectRoot = Split-Path -Parent $ScriptDir
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

    if (-not [string]::IsNullOrWhiteSpace($env:PAPERBRIDGE_RUNTIME_DIR)) {
        $TargetModelDir = Join-Path $env:PAPERBRIDGE_RUNTIME_DIR "models\plamo-3-translate-2b-GGUF"
    } elseif (-not [string]::IsNullOrWhiteSpace($profileUserDataDir)) {
        $TargetModelDir = Join-Path $profileUserDataDir "models\plamo-3-translate-2b-GGUF"
    } else {
        $TargetModelDir = Join-Path $env:LOCALAPPDATA "PaperBridgeLocal\models\plamo-3-translate-2b-GGUF"
    }
}

if (-not (Test-Path -LiteralPath $TargetModelDir)) {
    New-Item -ItemType Directory -Path $TargetModelDir -Force | Out-Null
}

$finalModelPath = Join-Path $TargetModelDir $EXPECTED_MODEL_NAME

function Test-GgufHeader([string]$filePath) {
    if (-not (Test-Path -LiteralPath $filePath)) { return $false }
    try {
        $fs = [System.IO.File]::OpenRead($filePath)
        $buf = New-Object byte[] 4
        $read = $fs.Read($buf, 0, 4)
        $fs.Close()
        return ($read -eq 4 -and $buf[0] -eq 0x47 -and $buf[1] -eq 0x47 -and $buf[2] -eq 0x55 -and $buf[3] -eq 0x46)
    } catch {
        return $false
    }
}

function Get-FileSha256([string]$filePath) {
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    $fs = [System.IO.File]::OpenRead($filePath)
    try {
        $hashBytes = $sha256.ComputeHash($fs)
    } finally {
        $fs.Close()
        $sha256.Dispose()
    }
    $sb = New-Object System.Text.StringBuilder
    foreach ($b in $hashBytes) {
        $sb.Append($b.ToString("x2")) | Out-Null
    }
    return $sb.ToString()
}

function Test-ModelIntegrity([string]$filePath) {
    if (-not (Test-Path -LiteralPath $filePath)) { return $false }
    $info = Get-Item -LiteralPath $filePath
    if ($info.Length -ne $EXPECTED_SIZE) { return $false }
    if (-not (Test-GgufHeader $filePath)) { return $false }
    $actualHash = Get-FileSha256 $filePath
    return ($actualHash -eq $EXPECTED_SHA256)
}

# 1. Handle Preselected Path first
if (-not [string]::IsNullOrWhiteSpace($PreselectedPath)) {
    if (-not (Test-Path -LiteralPath $PreselectedPath)) {
        Write-Error "指定されたモデルファイルが見つかりません: $PreselectedPath"
        exit 1
    }
    if (-not (Test-GgufHeader $PreselectedPath)) {
        Write-Error "GGUF ファイルヘッダーが不正です: $PreselectedPath"
        exit 1
    }
    $info = Get-Item -LiteralPath $PreselectedPath
    if ($info.Length -ne $EXPECTED_SIZE) {
        Write-Error "ファイルサイズが一致しません。期待値: $EXPECTED_SIZE bytes / 実測: $($info.Length) bytes"
        exit 1
    }
    Write-Output "SHA-256 ハッシュを照合しています..."
    $hash = Get-FileSha256 $PreselectedPath
    if ($hash -ne $EXPECTED_SHA256) {
        Write-Error "SHA-256 ハッシュが一致しません。破損しているか別バージョンです。`n期待値: $EXPECTED_SHA256`n実測値: $hash"
        exit 1
    }
    $tmpCopy = Join-Path $TargetModelDir "$EXPECTED_MODEL_NAME.copying"
    try {
        if ((Resolve-Path -LiteralPath $PreselectedPath).Path -ne $finalModelPath) {
            Copy-Item -LiteralPath $PreselectedPath -Destination $tmpCopy -Force
            Move-Item -LiteralPath $tmpCopy -Destination $finalModelPath -Force
        }
        Write-Output "MODEL_SETUP_SUCCESS:$finalModelPath"
        exit 0
    } catch {
        if (Test-Path -LiteralPath $tmpCopy) { Remove-Item -LiteralPath $tmpCopy -Force -ErrorAction SilentlyContinue }
        Write-Error "モデルファイルの配置に失敗しました: $($_.Exception.Message)"
        exit 1
    }
}

# 2. Check existing valid model
if (Test-ModelIntegrity $finalModelPath) {
    Write-Output "VALID_MODEL_PRESENT:$finalModelPath"
    exit 0
}

# 3. Non-interactive mode without valid model or preselected path
if ($NonInteractive) {
    Write-Output "MODEL_MISSING_NON_INTERACTIVE"
    exit 2
}

# 4. Interactive GUI Setup Form with PLaMo License Agreement
$form = New-Object System.Windows.Forms.Form
$form.Text = "PaperBridge Local - 翻訳モデル設定 (Built with PLaMo)"
$form.Size = New-Object System.Drawing.Size(600, 480)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.Font = New-Object System.Drawing.Font("Yu Gothic UI", 9.5)

$lblTitle = New-Object System.Windows.Forms.Label
$lblTitle.Location = New-Object System.Drawing.Point(20, 15)
$lblTitle.Size = New-Object System.Drawing.Size(540, 28)
$lblTitle.Text = "PLaMo 3 Translate 2B (Q4_K_M) モデルのセットアップ"
$lblTitle.Font = New-Object System.Drawing.Font("Yu Gothic UI", 12, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($lblTitle)

$txtNotice = New-Object System.Windows.Forms.TextBox
$txtNotice.Location = New-Object System.Drawing.Point(20, 50)
$txtNotice.Size = New-Object System.Drawing.Size(540, 155)
$txtNotice.Multiline = $true
$txtNotice.ReadOnly = $true
$txtNotice.ScrollBars = "Vertical"
$txtNotice.BackColor = [System.Drawing.Color]::WhiteSmoke
$txtNotice.Text = @"
本ツールは翻訳モデルとして Preferred Networks (PFN) の PLaMo 3 Translate 2B を使用します (Built with PLaMo)。モデル本体はソフトウェアとは別配布であり、PLaMoコミュニティライセンス契約が適用されます。

- 取得元: Hugging Face (pfnet/plamo-3-translate-2b-GGUF, rev: 232fbd79...)
- ファイルサイズ: 約 1.81 GB (1,807,199,392 bytes)
- 保存先: $finalModelPath

【契約上の主な注意点】
- 非商用・学術研究は無償（登録不要）。商用利用時は事前登録・売上高条件等があります。
- 書籍・論文など独立した長文公表物の全部または実質的部分として翻訳PDFを公表する場合は、PDF内にPLaMo利用の明示が必要です。
- 翻訳元となる原著作物の著作権上の許諾は含まれません。
"@
$form.Controls.Add($txtNotice)

$lnkJa = New-Object System.Windows.Forms.LinkLabel
$lnkJa.Location = New-Object System.Drawing.Point(20, 212)
$lnkJa.Size = New-Object System.Drawing.Size(260, 22)
$lnkJa.Text = "PLaMoコミュニティライセンス契約 (日本語版)"
$lnkJa.Add_LinkClicked({ Start-Process $LICENSE_URL_JA })
$form.Controls.Add($lnkJa)

$lnkEn = New-Object System.Windows.Forms.LinkLabel
$lnkEn.Location = New-Object System.Drawing.Point(290, 212)
$lnkEn.Size = New-Object System.Drawing.Size(250, 22)
$lnkEn.Text = "PLaMo Community License (English)"
$lnkEn.Add_LinkClicked({ Start-Process $LICENSE_URL_EN })
$form.Controls.Add($lnkEn)

$chkAgree = New-Object System.Windows.Forms.CheckBox
$chkAgree.Location = New-Object System.Drawing.Point(20, 245)
$chkAgree.Size = New-Object System.Drawing.Size(540, 38)
$chkAgree.Text = "上記の PLaMo コミュニティライセンス契約の適用条件および約 1.81 GB の通信・保存容量を確認し、利用条件に同意します。"
$chkAgree.Checked = [bool]$AcceptLicense
$form.Controls.Add($chkAgree)

$lblStatus = New-Object System.Windows.Forms.Label
$lblStatus.Location = New-Object System.Drawing.Point(20, 292)
$lblStatus.Size = New-Object System.Drawing.Size(540, 40)
$lblStatus.Text = "状態: モデルが未設定です。同意チェック後、取得方法を選択してください。"
$form.Controls.Add($lblStatus)

$progBar = New-Object System.Windows.Forms.ProgressBar
$progBar.Location = New-Object System.Drawing.Point(20, 338)
$progBar.Size = New-Object System.Drawing.Size(540, 24)
$progBar.Style = "Continuous"
$form.Controls.Add($progBar)

$btnDownload = New-Object System.Windows.Forms.Button
$btnDownload.Location = New-Object System.Drawing.Point(20, 380)
$btnDownload.Size = New-Object System.Drawing.Size(210, 38)
$btnDownload.Text = "公式からダウンロード (1.81GB)"
$btnDownload.Enabled = $chkAgree.Checked
$form.Controls.Add($btnDownload)

$btnSelect = New-Object System.Windows.Forms.Button
$btnSelect.Location = New-Object System.Drawing.Point(242, 380)
$btnSelect.Size = New-Object System.Drawing.Size(195, 38)
$btnSelect.Text = "既存 GGUF ファイルを選択..."
$btnSelect.Enabled = $chkAgree.Checked
$form.Controls.Add($btnSelect)

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Location = New-Object System.Drawing.Point(450, 380)
$btnCancel.Size = New-Object System.Drawing.Size(110, 38)
$btnCancel.Text = "キャンセル"
$form.Controls.Add($btnCancel)

$script:SetupOutcome = 1
$script:CancelRequested = $false
$script:DownloadActive = $false

$chkAgree.Add_CheckedChanged({
    $enabled = $chkAgree.Checked
    $btnDownload.Enabled = $enabled
    $btnSelect.Enabled = $enabled
})

$btnCancel.Add_Click({
    $script:CancelRequested = $true
    $btnCancel.Enabled = $false
    $lblStatus.Text = "キャンセルしています..."
    if (-not $script:DownloadActive) {
        $script:SetupOutcome = 1
        $form.Close()
    }
})

$btnSelect.Add_Click({
    $ofd = New-Object System.Windows.Forms.OpenFileDialog
    $ofd.Filter = "GGUF モデルファイル (*.gguf)|*.gguf|すべてのファイル (*.*)|*.*"
    $ofd.Title = "$EXPECTED_MODEL_NAME を選択してください"
    if ($ofd.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        $selPath = $ofd.FileName
        $btnDownload.Enabled = $false
        $btnSelect.Enabled = $false
        $chkAgree.Enabled = $false
        $lblStatus.Text = "選択されたモデルのヘッダー・サイズ・SHA-256を検証中..."
        [System.Windows.Forms.Application]::DoEvents()

        try {
            if (-not (Test-GgufHeader $selPath)) {
                throw "無効なファイル形式です (GGUF マジックヘッダーが見つかりません)。"
            }
            $info = Get-Item -LiteralPath $selPath
            if ($info.Length -ne $EXPECTED_SIZE) {
                throw "ファイルサイズが一致しません (期待: $EXPECTED_SIZE bytes / 実測: $($info.Length) bytes)。"
            }
            $lblStatus.Text = "SHA-256 ハッシュを計算して照合中 (数秒かかります)..."
            [System.Windows.Forms.Application]::DoEvents()
            $hash = Get-FileSha256 $selPath
            if ($hash -ne $EXPECTED_SHA256) {
                throw "SHA-256 ハッシュが一致しません。改変・破損または別モデルの可能性があります。"
            }

            $lblStatus.Text = "検証合格！安全にコピー配置しています..."
            $progBar.Value = 50
            [System.Windows.Forms.Application]::DoEvents()

            $tmpCopy = Join-Path $TargetModelDir "$EXPECTED_MODEL_NAME.copying"
            Copy-Item -LiteralPath $selPath -Destination $tmpCopy -Force
            Move-Item -LiteralPath $tmpCopy -Destination $finalModelPath -Force

            $progBar.Value = 100
            $script:SetupOutcome = 0
            [System.Windows.Forms.MessageBox]::Show("モデルの検証および配置が完了しました。", "PaperBridge Local", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
            $form.Close()
        } catch {
            if (Test-Path -LiteralPath "$TargetModelDir\$EXPECTED_MODEL_NAME.copying") {
                Remove-Item -LiteralPath "$TargetModelDir\$EXPECTED_MODEL_NAME.copying" -Force -ErrorAction SilentlyContinue
            }
            [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "モデル検証エラー", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
            $lblStatus.Text = "エラー: $($_.Exception.Message)"
            $btnDownload.Enabled = $true
            $btnSelect.Enabled = $true
            $chkAgree.Enabled = $true
        }
    }
})

$btnDownload.Add_Click({
    $btnDownload.Enabled = $false
    $btnSelect.Enabled = $false
    $chkAgree.Enabled = $false
    $script:CancelRequested = $false
    $script:DownloadActive = $true

    $tmpDl = Join-Path $TargetModelDir "$EXPECTED_MODEL_NAME.downloading"
    $lblStatus.Text = "公式 Hugging Face へ接続中..."
    [System.Windows.Forms.Application]::DoEvents()

    $resStream = $null
    $fs = $null
    $response = $null
    try {
        if (Test-Path -LiteralPath $tmpDl) { Remove-Item -LiteralPath $tmpDl -Force }

        $request = [System.Net.HttpWebRequest]::Create($DOWNLOAD_URL)
        $request.Method = "GET"
        $request.Timeout = 30000
        $request.ReadWriteTimeout = 30000
        $request.AllowAutoRedirect = $true
        $request.UserAgent = "PaperBridgeLocal-Setup/1.0"

        $response = $request.GetResponse()
        $totalBytes = $response.ContentLength
        if ($totalBytes -le 0) { $totalBytes = $EXPECTED_SIZE }
        $resStream = $response.GetResponseStream()
        $fs = [System.IO.File]::Create($tmpDl)

        $buffer = New-Object byte[] 131072 # 128KB chunks
        $downloaded = 0L
        $uiTimer = [System.Diagnostics.Stopwatch]::StartNew()

        while (($readBytes = $resStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            if ($script:CancelRequested) {
                throw [System.OperationCanceledException]::new("ユーザーによってキャンセルされました。")
            }
            $fs.Write($buffer, 0, $readBytes)
            $downloaded += $readBytes

            if ($uiTimer.ElapsedMilliseconds -ge 120) {
                $pct = [int][Math]::Min(100, ($downloaded * 100.0) / $totalBytes)
                $progBar.Value = $pct
                $mbDone = [math]::Round($downloaded / 1MB, 1)
                $mbTotal = [math]::Round($totalBytes / 1MB, 1)
                $lblStatus.Text = "ダウンロード中... $mbDone MB / $mbTotal MB ($pct%)"
                [System.Windows.Forms.Application]::DoEvents()
                $uiTimer.Restart()
            }
        }
        $fs.Close(); $fs = $null
        $resStream.Close(); $resStream = $null
        $response.Close(); $response = $null

        $progBar.Value = 100
        $lblStatus.Text = "ダウンロード完了！GGUFヘッダーと SHA-256 ハッシュを照合中..."
        [System.Windows.Forms.Application]::DoEvents()

        if (-not (Test-GgufHeader $tmpDl)) {
            throw "ダウンロードされたファイルの GGUF ヘッダーが破損しています。"
        }
        $dlInfo = Get-Item -LiteralPath $tmpDl
        if ($dlInfo.Length -ne $EXPECTED_SIZE) {
            throw "ファイルサイズが一致しません (期待: $EXPECTED_SIZE bytes / 実測: $($dlInfo.Length) bytes)。"
        }
        $hash = Get-FileSha256 $tmpDl
        if ($hash -ne $EXPECTED_SHA256) {
            throw "SHA-256 ハッシュ検証に失敗しました。通信エラー等でファイルが破損した可能性があります。"
        }

        Move-Item -LiteralPath $tmpDl -Destination $finalModelPath -Force
        $script:DownloadActive = $false
        $script:SetupOutcome = 0
        [System.Windows.Forms.MessageBox]::Show("モデルのダウンロードおよび整合性検証が完了しました。", "PaperBridge Local", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
        $form.Close()
    } catch {
        $script:DownloadActive = $false
        if ($fs) { try { $fs.Close() } catch {} }
        if ($resStream) { try { $resStream.Close() } catch {} }
        if ($response) { try { $response.Close() } catch {} }
        if (Test-Path -LiteralPath $tmpDl) { Remove-Item -LiteralPath $tmpDl -Force -ErrorAction SilentlyContinue }

        if ($script:CancelRequested) {
            $script:SetupOutcome = 1
            $form.Close()
        } else {
            [System.Windows.Forms.MessageBox]::Show("ダウンロードに失敗しました:`n$($_.Exception.Message)`n`n再試行するか、手元のモデルファイルを選択してください。", "ダウンロードエラー", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
            $lblStatus.Text = "エラー: $($_.Exception.Message)"
            $progBar.Value = 0
            $btnDownload.Enabled = $true
            $btnSelect.Enabled = $true
            $chkAgree.Enabled = $true
            $btnCancel.Enabled = $true
        }
    }
})

$form.Add_FormClosing({
    param($sender, $e)
    if ($script:DownloadActive -and -not $script:CancelRequested -and $script:SetupOutcome -ne 0) {
        $script:CancelRequested = $true
    }
})

$form.ShowDialog() | Out-Null
exit $script:SetupOutcome
