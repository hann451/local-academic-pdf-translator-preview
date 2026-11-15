# PaperBridge Local - Model Selection, Download, Local Registration & Switch Tool
param(
    [string]$TargetModelDir = "",
    [string]$UserDataDir = "",
    [string]$ModelId = "",
    [ValidateSet("", "Auto", "Download", "Local", "Select")]
    [string]$Mode = "",
    [ValidateSet("Reference", "Copy")]
    [string]$RegisterMode = "Reference",
    [switch]$NonInteractive = $false,
    [switch]$AcceptLicense = $false,
    [string]$PreselectedPath = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
. (Join-Path $ScriptDir "model_registry_common.ps1")

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
    } elseif (-not [string]::IsNullOrWhiteSpace($TargetModelDir)) {
        # Derive runtime dir cleanly if caller only supplied -TargetModelDir
        $parent1 = Split-Path -Parent $TargetModelDir
        if ((Split-Path -Leaf $parent1) -eq "models") {
            $UserDataDir = Split-Path -Parent $parent1
        } else {
            $UserDataDir = Join-Path $env:LOCALAPPDATA "PaperBridgeLocal"
        }
    } else {
        $UserDataDir = Join-Path $env:LOCALAPPDATA "PaperBridgeLocal"
    }
}

if (-not (Test-Path -LiteralPath $UserDataDir)) {
    New-Item -ItemType Directory -Path $UserDataDir -Force | Out-Null
}

$Catalog = Get-PaperBridgeModelCatalog -ProjectRoot $ProjectRoot
$syncState = Sync-ModelsRegistryWithLegacy -UserDataDir $UserDataDir -ProjectRoot $ProjectRoot -Catalog $Catalog

function Get-DestinationPathForModel {
    param($CatalogModel)
    if (-not [string]::IsNullOrWhiteSpace($TargetModelDir)) {
        return (Join-Path $TargetModelDir ([string]$CatalogModel.filename))
    }
    return (Join-Path (Join-Path (Join-Path $UserDataDir "models") ([string]$CatalogModel.relative_subdir)) ([string]$CatalogModel.filename))
}

# 1. Non-interactive / CLI path for -PreselectedPath
if (-not [string]::IsNullOrWhiteSpace($PreselectedPath)) {
    $regModeLower = $RegisterMode.ToLowerInvariant()
    try {
        $insp = Inspect-GgufFile -FilePath $PreselectedPath -ComputeHash
        $validated = Test-ModelMatchesCatalog -Catalog $Catalog -Inspection $insp -ExpectedCatalogModelId $ModelId
        if ($validated.CatalogModel.license.requires_agreement -and -not $AcceptLicense) {
            throw "「$($validated.CatalogModel.display_name)」を登録するには -AcceptLicense の指定（利用条件への同意）が必要です。"
        }
        $regRes = Register-PaperBridgeLocalModel `
            -UserDataDir $UserDataDir `
            -ProjectRoot $ProjectRoot `
            -FilePath $PreselectedPath `
            -ExpectedCatalogModelId $ModelId `
            -RegisterMode $regModeLower `
            -LicenseAccepted:$true `
            -SetActive:$true
        Write-Output "MODEL_SETUP_SUCCESS:$($regRes.Registration.file_path)"
        Write-Output "MODEL_ID:$($regRes.Registration.catalog_model_id)"
        Write-Output "REGISTRATION_ID:$($regRes.Registration.registration_id)"
        exit 0
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
}

# 2. Non-interactive / CLI selection of already registered model
if ($Mode -eq "Select" -and -not [string]::IsNullOrWhiteSpace($ModelId)) {
    try {
        $syncNow = Sync-ModelsRegistryWithLegacy -UserDataDir $UserDataDir -ProjectRoot $ProjectRoot -Catalog $Catalog
        $matchReg = $null
        foreach ($m in $syncNow.Registry.models) {
            if ($m.registration_id -eq $ModelId -or $m.catalog_model_id -eq $ModelId) {
                $matchReg = $m
                break
            }
        }
        if (-not $matchReg) {
            throw "指定されたモデル ($ModelId) は未登録です。"
        }
        $switched = Set-PaperBridgeActiveModel -UserDataDir $UserDataDir -ProjectRoot $ProjectRoot -RegistrationId ([string]$matchReg.registration_id)
        Write-Output "MODEL_SETUP_SUCCESS:$($switched.file_path)"
        Write-Output "MODEL_ID:$($switched.catalog_model_id)"
        Write-Output "REGISTRATION_ID:$($switched.registration_id)"
        exit 0
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
}

# 3. Non-interactive check or headless download
if ($NonInteractive) {
    $resolved = Resolve-PaperBridgeActiveModelForLaunch -UserDataDir $UserDataDir -ProjectRoot $ProjectRoot
    if ($Mode -eq "Download") {
        $targetCat = Get-CatalogModelById -Catalog $Catalog -ModelId $ModelId
        if (-not $targetCat) {
            Write-Error "指定されたカタログモデルが見つかりません: $ModelId"
            exit 1
        }
        if ($targetCat.license.requires_agreement -and -not $AcceptLicense) {
            Write-Error "ダウンロードには -AcceptLicense の指定（ライセンス同意）が必要です。"
            exit 1
        }
        try {
            $dest = Get-DestinationPathForModel -CatalogModel $targetCat
            $null = Invoke-PaperBridgeModelDownload -Catalog $Catalog -CatalogModel $targetCat -DestPath $dest
            $regRes = Register-PaperBridgeLocalModel `
                -UserDataDir $UserDataDir `
                -ProjectRoot $ProjectRoot `
                -FilePath $dest `
                -ExpectedCatalogModelId ([string]$targetCat.model_id) `
                -RegisterMode "reference" `
                -LicenseAccepted:$true `
                -SetActive:$true `
                -SourceTypeOverride "downloaded"
            Write-Output "MODEL_SETUP_SUCCESS:$($regRes.Registration.file_path)"
            Write-Output "MODEL_ID:$($regRes.Registration.catalog_model_id)"
            exit 0
        } catch {
            Write-Error $_.Exception.Message
            exit 1
        }
    }

    if ($resolved.Status -eq "Ready") {
        if ([string]::IsNullOrWhiteSpace($ModelId) -or $resolved.Entry.catalog_model_id -eq $ModelId) {
            Write-Output "VALID_MODEL_PRESENT:$($resolved.Entry.file_path)"
            exit 0
        }
    }
    Write-Output "MODEL_MISSING_NON_INTERACTIVE"
    exit 2
}

# 4. Interactive GUI Model Setup & Switcher Form
$form = New-Object System.Windows.Forms.Form
$form.Text = "PaperBridge Local - 翻訳モデルの選択・導入・切替"
$form.Size = New-Object System.Drawing.Size(700, 680)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.Font = New-Object System.Drawing.Font("Yu Gothic UI", 9.5)

$lblTitle = New-Object System.Windows.Forms.Label
$lblTitle.Location = New-Object System.Drawing.Point(20, 14)
$lblTitle.Size = New-Object System.Drawing.Size(645, 28)
$lblTitle.Text = "使用する翻訳モデルの選択・ダウンロード・ローカル登録"
$lblTitle.Font = New-Object System.Drawing.Font("Yu Gothic UI", 12, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($lblTitle)

# --- Registered Models Switcher Section ---
$grpRegistered = New-Object System.Windows.Forms.GroupBox
$grpRegistered.Location = New-Object System.Drawing.Point(20, 48)
$grpRegistered.Size = New-Object System.Drawing.Size(645, 92)
$grpRegistered.Text = "登録済みモデル（再ダウンロードなしで即時切替・再指定が可能）"
$form.Controls.Add($grpRegistered)

$cmbRegistered = New-Object System.Windows.Forms.ComboBox
$cmbRegistered.Location = New-Object System.Drawing.Point(14, 26)
$cmbRegistered.Size = New-Object System.Drawing.Size(400, 26)
$cmbRegistered.DropDownStyle = "DropDownList"
$grpRegistered.Controls.Add($cmbRegistered)

$btnUseRegistered = New-Object System.Windows.Forms.Button
$btnUseRegistered.Location = New-Object System.Drawing.Point(424, 24)
$btnUseRegistered.Size = New-Object System.Drawing.Size(110, 28)
$btnUseRegistered.Text = "選択して使用"
$grpRegistered.Controls.Add($btnUseRegistered)

$btnRelocate = New-Object System.Windows.Forms.Button
$btnRelocate.Location = New-Object System.Drawing.Point(542, 24)
$btnRelocate.Size = New-Object System.Drawing.Size(90, 28)
$btnRelocate.Text = "場所を再指定..."
$grpRegistered.Controls.Add($btnRelocate)

$lblRegisteredInfo = New-Object System.Windows.Forms.Label
$lblRegisteredInfo.Location = New-Object System.Drawing.Point(14, 58)
$lblRegisteredInfo.Size = New-Object System.Drawing.Size(618, 26)
$lblRegisteredInfo.ForeColor = [System.Drawing.Color]::DimGray
$grpRegistered.Controls.Add($lblRegisteredInfo)

# --- Catalog Model Selection Section ---
$lblCatLabel = New-Object System.Windows.Forms.Label
$lblCatLabel.Location = New-Object System.Drawing.Point(20, 152)
$lblCatLabel.Size = New-Object System.Drawing.Size(185, 22)
$lblCatLabel.Text = "新規導入 / ダウンロード対象:"
$lblCatLabel.Font = New-Object System.Drawing.Font("Yu Gothic UI", 9.5, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($lblCatLabel)

$cmbCatalog = New-Object System.Windows.Forms.ComboBox
$cmbCatalog.Location = New-Object System.Drawing.Point(210, 149)
$cmbCatalog.Size = New-Object System.Drawing.Size(455, 26)
$cmbCatalog.DropDownStyle = "DropDownList"
$form.Controls.Add($cmbCatalog)

$catalogModels = @($Catalog.models)
$defaultIndex = 0
for ($idx = 0; $idx -lt $catalogModels.Count; $idx++) {
    $cm = $catalogModels[$idx]
    $sizeTxt = Format-ModelSizeText -Bytes ([int64]$cm.known_size_bytes)
    $recTag = if ($cm.is_default -eq $true) { "推奨・標準" } elseif ($cm.catalog_verification_status -eq "catalog_verified_runtime_unverified") { "候補・カタログ検証済" } else { "候補" }
    $cmbCatalog.Items.Add("$($cm.display_name)  [$recTag / $sizeTxt]") | Out-Null
    if (-not [string]::IsNullOrWhiteSpace($ModelId) -and $cm.model_id -eq $ModelId) {
        $defaultIndex = $idx
    } elseif ([string]::IsNullOrWhiteSpace($ModelId) -and $cm.is_default -eq $true) {
        $defaultIndex = $idx
    }
}

$txtNotice = New-Object System.Windows.Forms.TextBox
$txtNotice.Location = New-Object System.Drawing.Point(20, 184)
$txtNotice.Size = New-Object System.Drawing.Size(645, 195)
$txtNotice.Multiline = $true
$txtNotice.ReadOnly = $true
$txtNotice.ScrollBars = "Vertical"
$txtNotice.BackColor = [System.Drawing.Color]::WhiteSmoke
$form.Controls.Add($txtNotice)

$lnkJa = New-Object System.Windows.Forms.LinkLabel
$lnkJa.Location = New-Object System.Drawing.Point(20, 388)
$lnkJa.Size = New-Object System.Drawing.Size(220, 22)
$form.Controls.Add($lnkJa)

$lnkEn = New-Object System.Windows.Forms.LinkLabel
$lnkEn.Location = New-Object System.Drawing.Point(248, 388)
$lnkEn.Size = New-Object System.Drawing.Size(210, 22)
$form.Controls.Add($lnkEn)

$lnkCard = New-Object System.Windows.Forms.LinkLabel
$lnkCard.Location = New-Object System.Drawing.Point(466, 388)
$lnkCard.Size = New-Object System.Drawing.Size(195, 22)
$form.Controls.Add($lnkCard)

$script:CurrentUrlJa = ""
$script:CurrentUrlEn = ""
$script:CurrentUrlCard = ""
$lnkJa.Add_LinkClicked({ if ($script:CurrentUrlJa) { Start-Process $script:CurrentUrlJa } })
$lnkEn.Add_LinkClicked({ if ($script:CurrentUrlEn) { Start-Process $script:CurrentUrlEn } })
$lnkCard.Add_LinkClicked({ if ($script:CurrentUrlCard) { Start-Process $script:CurrentUrlCard } })

$chkCopyMode = New-Object System.Windows.Forms.CheckBox
$chkCopyMode.Location = New-Object System.Drawing.Point(20, 418)
$chkCopyMode.Size = New-Object System.Drawing.Size(645, 24)
$chkCopyMode.Text = "ローカル選択時は管理フォルダーへコピーして配置する（OFF時は元ファイルをコピー・移動せず参照登録）"
$chkCopyMode.Checked = ($RegisterMode -eq "Copy")
$form.Controls.Add($chkCopyMode)

$chkAgree = New-Object System.Windows.Forms.CheckBox
$chkAgree.Location = New-Object System.Drawing.Point(20, 448)
$chkAgree.Size = New-Object System.Drawing.Size(645, 42)
$chkAgree.Checked = [bool]$AcceptLicense
$form.Controls.Add($chkAgree)

$lblStatus = New-Object System.Windows.Forms.Label
$lblStatus.Location = New-Object System.Drawing.Point(20, 498)
$lblStatus.Size = New-Object System.Drawing.Size(645, 42)
$lblStatus.Text = "状態: モデルを選択し、利用条件へ同意したうえで取得方法を選んでください。"
$form.Controls.Add($lblStatus)

$progBar = New-Object System.Windows.Forms.ProgressBar
$progBar.Location = New-Object System.Drawing.Point(20, 544)
$progBar.Size = New-Object System.Drawing.Size(645, 24)
$progBar.Style = "Continuous"
$form.Controls.Add($progBar)

$btnDownload = New-Object System.Windows.Forms.Button
$btnDownload.Location = New-Object System.Drawing.Point(20, 584)
$btnDownload.Size = New-Object System.Drawing.Size(240, 40)
$btnDownload.Enabled = $chkAgree.Checked
$form.Controls.Add($btnDownload)

$btnSelect = New-Object System.Windows.Forms.Button
$btnSelect.Location = New-Object System.Drawing.Point(272, 584)
$btnSelect.Size = New-Object System.Drawing.Size(235, 40)
$btnSelect.Text = "手元の GGUF をファイル選択..."
$btnSelect.Enabled = $chkAgree.Checked
$form.Controls.Add($btnSelect)

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Location = New-Object System.Drawing.Point(520, 584)
$btnCancel.Size = New-Object System.Drawing.Size(145, 40)
$btnCancel.Text = "閉じる / キャンセル"
$form.Controls.Add($btnCancel)

$script:SetupOutcome = 1
$script:CancelRequested = $false
$script:DownloadActive = $false
$script:RegisteredItems = @()

$script:RefreshRegisteredUi = {
    $syncLatest = Sync-ModelsRegistryWithLegacy -UserDataDir $UserDataDir -ProjectRoot $ProjectRoot -Catalog $Catalog
    $script:RegisteredItems = @($syncLatest.Registry.models)
    $activeRegId = [string]$syncLatest.Registry.active_model_id
    $cmbRegistered.Items.Clear()
    if ($script:RegisteredItems.Count -eq 0) {
        $cmbRegistered.Items.Add("（まだ登録済みモデルはありません）") | Out-Null
        $cmbRegistered.SelectedIndex = 0
        $cmbRegistered.Enabled = $false
        $btnUseRegistered.Enabled = $false
        $btnRelocate.Enabled = $false
        $lblRegisteredInfo.Text = "下からモデルをダウンロードするか、手元の GGUF ファイルを参照登録してください。"
        return
    }
    $cmbRegistered.Enabled = $true
    $selIdx = 0
    for ($i = 0; $i -lt $script:RegisteredItems.Count; $i++) {
        $rm = $script:RegisteredItems[$i]
        $isActiveMark = if ($rm.registration_id -eq $activeRegId) { "[使用中] " } else { "" }
        $existsOk = Test-Path -LiteralPath ([string]$rm.file_path) -PathType Leaf
        $stMark = Get-PaperBridgeVerificationStageLabel -Verification $rm.verification -FileExistsOverride $existsOk
        $cmbRegistered.Items.Add("$isActiveMark$($rm.display_name) [$stMark / $($rm.source_type)]") | Out-Null
        if ($rm.registration_id -eq $activeRegId) { $selIdx = $i }
    }
    $cmbRegistered.SelectedIndex = $selIdx
}

$script:UpdateSelectedRegisteredDetails = {
    if ($script:RegisteredItems.Count -eq 0 -or $cmbRegistered.SelectedIndex -lt 0 -or $cmbRegistered.SelectedIndex -ge $script:RegisteredItems.Count) {
        $btnUseRegistered.Enabled = $false
        $btnRelocate.Enabled = $false
        return
    }
    $rm = $script:RegisteredItems[$cmbRegistered.SelectedIndex]
    $existsOk = Test-Path -LiteralPath ([string]$rm.file_path) -PathType Leaf
    $lblRegisteredInfo.Text = "パス: $($rm.file_path)  (登録ID: $($rm.registration_id))"
    $btnUseRegistered.Enabled = $existsOk
    $btnRelocate.Enabled = $true
}

$script:RefreshCatalogDetails = {
    if ($cmbCatalog.SelectedIndex -lt 0 -or $cmbCatalog.SelectedIndex -ge $catalogModels.Count) { return }
    $cm = $catalogModels[$cmbCatalog.SelectedIndex]
    $destPath = Get-DestinationPathForModel -CatalogModel $cm
    $sizeTxt = Format-ModelSizeText -Bytes ([int64]$cm.known_size_bytes)
    $tagline = if ($cm.license.tagline) { " ($($cm.license.tagline))" } else { "" }
    $disclosure = if ($cm.license.disclosure_note_ja) { "`r`n- $($cm.license.disclosure_note_ja)" } else { "" }

    $txtNotice.Text = @"
対象モデル: $($cm.display_name)$tagline
提供元: $($cm.provider) / 形式: $($cm.prompt_format) ($($cm.quantization))
取得元: $($cm.source_repo) (rev: $($cm.pinned_revision))
ファイル名: $($cm.filename) ($sizeTxt / $($cm.known_size_bytes) bytes)
ダウンロード時の保存先: $destPath

【ハードウェア・検証状況】
$($cm.hardware_note_ja)

【ライセンス・利用条件 ($($cm.license.name))】
- $($cm.license.summary_ja)$disclosure
- 翻訳元となる原著作物の著作権上の許諾は含まれません。
"@
    $script:CurrentUrlJa = [string]$cm.license.url_ja
    $script:CurrentUrlEn = [string]$cm.license.url_en
    $script:CurrentUrlCard = [string]$cm.license.model_card_url
    $lnkJa.Text = "$($cm.license.name) (日本語)"
    $lnkJa.Visible = -not [string]::IsNullOrWhiteSpace($script:CurrentUrlJa)
    $lnkEn.Text = "License Terms (English)"
    $lnkEn.Visible = -not [string]::IsNullOrWhiteSpace($script:CurrentUrlEn)
    $lnkCard.Text = "Hugging Face モデルページ"
    $lnkCard.Visible = -not [string]::IsNullOrWhiteSpace($script:CurrentUrlCard)

    $chkAgree.Text = "選択中モデル「$($cm.display_name)」の利用条件（$($cm.license.name)）およびサイズ（$sizeTxt）を確認し、同意します。"
    $btnDownload.Text = "公式からダウンロード ($sizeTxt)"
    $agreeOk = (-not $cm.license.requires_agreement) -or $chkAgree.Checked
    $btnDownload.Enabled = $agreeOk
    $btnSelect.Enabled = $agreeOk
}

$cmbRegistered.Add_SelectedIndexChanged($script:UpdateSelectedRegisteredDetails)
$cmbCatalog.Add_SelectedIndexChanged($script:RefreshCatalogDetails)
$chkAgree.Add_CheckedChanged({
    if ($cmbCatalog.SelectedIndex -ge 0 -and $cmbCatalog.SelectedIndex -lt $catalogModels.Count) {
        $cm = $catalogModels[$cmbCatalog.SelectedIndex]
        $ok = (-not $cm.license.requires_agreement) -or $chkAgree.Checked
        $btnDownload.Enabled = $ok
        $btnSelect.Enabled = $ok
    }
})

$btnUseRegistered.Add_Click({
    if ($script:RegisteredItems.Count -eq 0 -or $cmbRegistered.SelectedIndex -lt 0) { return }
    $rm = $script:RegisteredItems[$cmbRegistered.SelectedIndex]
    try {
        $lblStatus.Text = "登録済みモデルの整合性を検証して切り替えています..."
        [System.Windows.Forms.Application]::DoEvents()
        $switched = Set-PaperBridgeActiveModel -UserDataDir $UserDataDir -ProjectRoot $ProjectRoot -RegistrationId ([string]$rm.registration_id)
        $script:SetupOutcome = 0
        [System.Windows.Forms.MessageBox]::Show("アクティブモデルを「$($switched.display_name)」に切り替えました。`n$($switched.file_path)", "PaperBridge Local", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
        $form.Close()
    } catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "モデル切替エラー", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
        $lblStatus.Text = "エラー: $($_.Exception.Message)"
        & $script:RefreshRegisteredUi
        & $script:UpdateSelectedRegisteredDetails
    }
})

$btnRelocate.Add_Click({
    if ($script:RegisteredItems.Count -eq 0 -or $cmbRegistered.SelectedIndex -lt 0) { return }
    $rm = $script:RegisteredItems[$cmbRegistered.SelectedIndex]
    $ofd = New-Object System.Windows.Forms.OpenFileDialog
    $ofd.Filter = "GGUF モデルファイル (*.gguf)|*.gguf|すべてのファイル (*.*)|*.*"
    $ofd.Title = "$($rm.display_name) の GGUF ファイル場所を再指定してください"
    if ($ofd.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        try {
            $lblStatus.Text = "再指定されたファイルを GGUF ヘッダー・SHA-256 検証中..."
            [System.Windows.Forms.Application]::DoEvents()
            $regRes = Register-PaperBridgeLocalModel `
                -UserDataDir $UserDataDir `
                -ProjectRoot $ProjectRoot `
                -FilePath $ofd.FileName `
                -ExpectedCatalogModelId ([string]$rm.catalog_model_id) `
                -RegisterMode "reference" `
                -LicenseAccepted:$true `
                -SetActive:$true `
                -ReplacesRegistrationId ([string]$rm.registration_id)
            $script:SetupOutcome = 0
            & $script:RefreshRegisteredUi
            & $script:UpdateSelectedRegisteredDetails
            [System.Windows.Forms.MessageBox]::Show("ファイルの場所を再指定して検証を完了しました:`n$($regRes.Registration.file_path)", "PaperBridge Local", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
            $form.Close()
        } catch {
            [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "モデル検証エラー", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
            $lblStatus.Text = "エラー: $($_.Exception.Message)"
        }
    }
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
    $cm = $catalogModels[$cmbCatalog.SelectedIndex]
    $ofd = New-Object System.Windows.Forms.OpenFileDialog
    $ofd.Filter = "GGUF モデルファイル (*.gguf)|*.gguf|すべてのファイル (*.*)|*.*"
    $ofd.Title = "$($cm.display_name) ($($cm.filename)) を選択してください"
    if ($ofd.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        $selPath = $ofd.FileName
        $btnDownload.Enabled = $false
        $btnSelect.Enabled = $false
        $chkAgree.Enabled = $false
        $cmbCatalog.Enabled = $false
        $lblStatus.Text = "選択されたファイルの GGUF 構造・アーキテクチャ・SHA-256 を検証中..."
        [System.Windows.Forms.Application]::DoEvents()

        try {
            $modeStr = if ($chkCopyMode.Checked) { "copy" } else { "reference" }
            $regRes = Register-PaperBridgeLocalModel `
                -UserDataDir $UserDataDir `
                -ProjectRoot $ProjectRoot `
                -FilePath $selPath `
                -ExpectedCatalogModelId ([string]$cm.model_id) `
                -RegisterMode $modeStr `
                -LicenseAccepted:($chkAgree.Checked -or (-not $cm.license.requires_agreement)) `
                -SetActive:$true
            $progBar.Value = 100
            $script:SetupOutcome = 0
            [System.Windows.Forms.MessageBox]::Show("モデルの検証と登録が完了しました（$modeStr）:`n$($regRes.Registration.display_name)`n$($regRes.Registration.file_path)", "PaperBridge Local", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
            $form.Close()
        } catch {
            [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "モデル検証エラー", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
            $lblStatus.Text = "エラー: $($_.Exception.Message)"
            $cmbCatalog.Enabled = $true
            $chkAgree.Enabled = $true
            $ok = (-not $cm.license.requires_agreement) -or $chkAgree.Checked
            $btnDownload.Enabled = $ok
            $btnSelect.Enabled = $ok
        }
    }
})

$btnDownload.Add_Click({
    $cm = $catalogModels[$cmbCatalog.SelectedIndex]
    $destPath = Get-DestinationPathForModel -CatalogModel $cm
    $btnDownload.Enabled = $false
    $btnSelect.Enabled = $false
    $chkAgree.Enabled = $false
    $cmbCatalog.Enabled = $false
    $btnUseRegistered.Enabled = $false
    $script:CancelRequested = $false
    $script:DownloadActive = $true
    $progBar.Value = 0
    $lblStatus.Text = "公式 Hugging Face へ接続中... ($($cm.display_name))"
    [System.Windows.Forms.Application]::DoEvents()

    try {
        $progressCb = {
            param($done, $tot, $pct)
            $progBar.Value = $pct
            $mbDone = [math]::Round($done / 1MB, 1)
            $mbTot = [math]::Round($tot / 1MB, 1)
            $lblStatus.Text = "ダウンロード中 [$($cm.short_name)]... $mbDone MB / $mbTot MB ($pct%)"
            [System.Windows.Forms.Application]::DoEvents()
        }
        $cancelCb = { return $script:CancelRequested }

        $savedPath = Invoke-PaperBridgeModelDownload `
            -Catalog $Catalog `
            -CatalogModel $cm `
            -DestPath $destPath `
            -ProgressCallback $progressCb `
            -CancelCheck $cancelCb

        $lblStatus.Text = "ダウンロード検証完了！レジストリに登録中..."
        $progBar.Value = 100
        [System.Windows.Forms.Application]::DoEvents()

        $regRes = Register-PaperBridgeLocalModel `
            -UserDataDir $UserDataDir `
            -ProjectRoot $ProjectRoot `
            -FilePath $savedPath `
            -ExpectedCatalogModelId ([string]$cm.model_id) `
            -RegisterMode "reference" `
            -LicenseAccepted:($chkAgree.Checked -or (-not $cm.license.requires_agreement)) `
            -SetActive:$true `
            -SourceTypeOverride "downloaded"

        $script:DownloadActive = $false
        $script:SetupOutcome = 0
        [System.Windows.Forms.MessageBox]::Show("ダウンロードおよび GGUF/SHA-256 検証・登録が完了しました:`n$($regRes.Registration.display_name)`n$savedPath", "PaperBridge Local", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
        $form.Close()
    } catch {
        $script:DownloadActive = $false
        if ($script:CancelRequested) {
            $script:SetupOutcome = 1
            $form.Close()
        } else {
            [System.Windows.Forms.MessageBox]::Show("ダウンロードまたは検証に失敗しました:`n$($_.Exception.Message)`n`n再試行するか、手元のモデルファイルを選択してください。", "ダウンロードエラー", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
            $lblStatus.Text = "エラー: $($_.Exception.Message)"
            $progBar.Value = 0
            $cmbCatalog.Enabled = $true
            $chkAgree.Enabled = $true
            $btnCancel.Enabled = $true
            $ok = (-not $cm.license.requires_agreement) -or $chkAgree.Checked
            $btnDownload.Enabled = $ok
            $btnSelect.Enabled = $ok
            & $script:RefreshRegisteredUi
            & $script:UpdateSelectedRegisteredDetails
        }
    }
})

$form.Add_FormClosing({
    param($sender, $e)
    if ($script:DownloadActive -and -not $script:CancelRequested -and $script:SetupOutcome -ne 0) {
        $script:CancelRequested = $true
    }
})

$cmbCatalog.SelectedIndex = $defaultIndex
& $script:RefreshRegisteredUi
& $script:UpdateSelectedRegisteredDetails
& $script:RefreshCatalogDetails

$form.ShowDialog() | Out-Null
exit $script:SetupOutcome
