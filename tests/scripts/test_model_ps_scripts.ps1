# PaperBridge Local - PowerShell Model Provisioning & Launcher Logic Test Suite
# Runs purely against an isolated temp runtime directory and temp catalog without network access.
$ErrorActionPreference = "Stop"

$TestsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $TestsDir)
$ScriptsDir = Join-Path $ProjectRoot "scripts"

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw "ASSERTION FAILED: $Message"
    }
}

function Assert-Throws {
    param(
        [scriptblock]$Block,
        [string]$Message
    )
    $threw = $false
    try {
        & $Block | Out-Null
    } catch {
        $threw = $true
    }
    if (-not $threw) {
        throw "EXPECTED EXCEPTION WAS NOT THROWN: $Message"
    }
}

function New-SyntheticGgufFile {
    param(
        [string]$Path,
        [uint32]$Version = 3,
        [uint64]$TensorCount = 4,
        [string]$Architecture = "plamo3",
        [string]$ModelName = "plamo-3-translate-2b",
        [uint16]$SplitCount = 0,
        [string]$Tag = "TESTDATA"
    )
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $ms = New-Object System.IO.MemoryStream
    $bw = New-Object System.IO.BinaryWriter($ms)
    try {
        $bw.Write([System.Text.Encoding]::ASCII.GetBytes("GGUF"))
        $bw.Write([uint32]$Version)
        $bw.Write([uint64]$TensorCount)

        $kvList = New-Object System.Collections.Generic.List[object]
        if (-not [string]::IsNullOrEmpty($Architecture)) {
            $kvList.Add(@{ Key = "general.architecture"; Type = [uint32]8; Val = $Architecture })
        }
        if (-not [string]::IsNullOrEmpty($ModelName)) {
            $kvList.Add(@{ Key = "general.name"; Type = [uint32]8; Val = $ModelName })
        }
        if ($SplitCount -gt 0) {
            $kvList.Add(@{ Key = "split.count"; Type = [uint32]2; Val = $SplitCount })
        }
        $bw.Write([uint64]$kvList.Count)

        foreach ($kv in $kvList) {
            $kBytes = [System.Text.Encoding]::UTF8.GetBytes([string]$kv.Key)
            $bw.Write([uint64]$kBytes.Length)
            $bw.Write($kBytes)
            $bw.Write([uint32]$kv.Type)
            if ($kv.Type -eq 8) {
                $vBytes = [System.Text.Encoding]::UTF8.GetBytes([string]$kv.Val)
                $bw.Write([uint64]$vBytes.Length)
                $bw.Write($vBytes)
            } elseif ($kv.Type -eq 2) {
                $bw.Write([uint16]$kv.Val)
            }
        }
        $bw.Write([System.Text.Encoding]::UTF8.GetBytes($Tag))
        $bw.Flush()
        [System.IO.File]::WriteAllBytes($Path, $ms.ToArray())
    } finally {
        $bw.Dispose()
        $ms.Dispose()
    }
}

$tmpRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("pb_ps_model_test_" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$prevCatalogEnv = $env:PAPERBRIDGE_MODEL_CATALOG_PATH
try {
    New-Item -ItemType Directory -Path $tmpRoot -Force | Out-Null

    # 1. Encoding (UTF-8 BOM + CRLF) and PS 5.1 syntax verification on all production scripts
    $psScripts = @("launcher.ps1", "model_registry_common.ps1", "model_setup.ps1", "setup_desktop_shortcut.ps1", "setup_wizard.ps1")
    foreach ($name in $psScripts) {
        $p = Join-Path $ScriptsDir $name
        Assert-True (Test-Path -LiteralPath $p) "Script exists: $name"
        $bytes = [System.IO.File]::ReadAllBytes($p)
        Assert-True ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) "$name must have UTF-8 BOM"
        $txt = [System.Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3)
        $bareLf = [regex]::Matches($txt, '(?<!\r)\n').Count
        Assert-True ($bareLf -eq 0) "$name must have zero bare LF lines (got $bareLf)"
        $tokens = $null
        $errors = $null
        $null = [System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$tokens, [ref]$errors)
        Assert-True ($errors.Count -eq 0) "$name must parse cleanly in PS 5.1 (errors: $($errors.Count))"
    }
    Write-Output "[PASS] 1. Encoding (UTF-8 BOM + CRLF) & PS 5.1 syntax check passed for all 5 scripts."

    # 2. Dot-source shared helper & create synthetic GGUF payloads + isolated catalog
    . (Join-Path $ScriptsDir "model_registry_common.ps1")

    $extDir = Join-Path $tmpRoot "日本語 モデル保存先 (外部)"
    New-Item -ItemType Directory -Path $extDir -Force | Out-Null
    $f08 = Join-Path $extDir "plamo-3-translate-0.8b-Q4_K_M.gguf"
    $f2 = Join-Path $extDir "plamo-3-translate-2b-Q4_K_M.gguf"
    $f2Legacy = Join-Path $extDir "plamo-3-translate-2b-legacy.gguf"
    $f8 = Join-Path $extDir "plamo-3-translate-8b-Q4_K_M.gguf"

    New-SyntheticGgufFile -Path $f08 -ModelName "plamo-3-translate-0.8b" -Tag "PS_08B"
    New-SyntheticGgufFile -Path $f2 -ModelName "plamo-3-translate-2b" -Tag "PS_2B_CUR"
    New-SyntheticGgufFile -Path $f2Legacy -ModelName "plamo-3-translate-2b" -Tag "PS_2B_LEG"
    New-SyntheticGgufFile -Path $f8 -ModelName "plamo-3-translate-8b" -Tag "PS_8B"

    $sha08 = (Get-FileHash -LiteralPath $f08 -Algorithm SHA256).Hash.ToLowerInvariant()
    $sha2 = (Get-FileHash -LiteralPath $f2 -Algorithm SHA256).Hash.ToLowerInvariant()
    $sha2Leg = (Get-FileHash -LiteralPath $f2Legacy -Algorithm SHA256).Hash.ToLowerInvariant()
    $sha8 = (Get-FileHash -LiteralPath $f8 -Algorithm SHA256).Hash.ToLowerInvariant()

    $prodCat = [System.IO.File]::ReadAllText((Join-Path $ProjectRoot "backend\model_catalog.json"), [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    foreach ($m in $prodCat.models) {
        if ($m.model_id -eq "plamo-3-translate-0.8b-q4_k_m") {
            $len = (Get-Item -LiteralPath $f08).Length
            $m.known_size_bytes = $len
            $m.known_sha256 = $sha08
            $m.accepted_artifacts = @([pscustomobject]@{ revision = "rev08"; size_bytes = $len; sha256 = $sha08; label = "test-08b" })
        } elseif ($m.model_id -eq "plamo-3-translate-2b-q4_k_m") {
            $lenCur = (Get-Item -LiteralPath $f2).Length
            $lenLeg = (Get-Item -LiteralPath $f2Legacy).Length
            $m.known_size_bytes = $lenCur
            $m.known_sha256 = $sha2
            $m.accepted_artifacts = @(
                [pscustomobject]@{ revision = "rev2cur"; size_bytes = $lenCur; sha256 = $sha2; label = "test-2b-cur" },
                [pscustomobject]@{ revision = "rev2leg"; size_bytes = $lenLeg; sha256 = $sha2Leg; label = "test-2b-leg" }
            )
        } elseif ($m.model_id -eq "plamo-3-translate-8b-q4_k_m") {
            $len8 = (Get-Item -LiteralPath $f8).Length
            $m.known_size_bytes = $len8
            $m.known_sha256 = $sha8
            $m.accepted_artifacts = @([pscustomobject]@{ revision = "rev8"; size_bytes = $len8; sha256 = $sha8; label = "test-8b" })
        }
    }
    $testCatPath = Join-Path $tmpRoot "test_catalog.json"
    [System.IO.File]::WriteAllText($testCatPath, ($prodCat | ConvertTo-Json -Depth 15), [System.Text.Encoding]::UTF8)
    $env:PAPERBRIDGE_MODEL_CATALOG_PATH = $testCatPath

    $rtDir = Join-Path $tmpRoot "runtime_isolated"
    New-Item -ItemType Directory -Path $rtDir -Force | Out-Null

    # 3. Invalid GGUF / Split / Non-plamo / 31B / Unknown SHA rejections
    $badMagic = Join-Path $tmpRoot "bad_magic.gguf"
    [System.IO.File]::WriteAllText($badMagic, "NOT_GGUF_PAYLOAD_1234567890")
    $badInsp = Inspect-GgufFile -FilePath $badMagic
    Assert-True (-not $badInsp.format_valid) "Bad magic must fail Inspect-GgufFile"

    $splitMeta = Join-Path $tmpRoot "meta_split.gguf"
    New-SyntheticGgufFile -Path $splitMeta -SplitCount 2 -Tag "SPLIT"
    $splitInsp = Inspect-GgufFile -FilePath $splitMeta
    Assert-True $splitInsp.is_split_gguf "Split count > 1 in metadata must set is_split_gguf"

    $nonPlamo = Join-Path $tmpRoot "llama3.gguf"
    New-SyntheticGgufFile -Path $nonPlamo -Architecture "llama" -ModelName "llama-3-8b" -Tag "LLAMA"
    Assert-Throws { Register-PaperBridgeLocalModel -UserDataDir $rtDir -ProjectRoot $ProjectRoot -FilePath $nonPlamo -RegisterMode "reference" -LicenseAccepted $true } "Non-plamo3 must throw"

    $p31b = Join-Path $tmpRoot "plamo31b.gguf"
    New-SyntheticGgufFile -Path $p31b -Architecture "plamo3" -ModelName "plamo-3-translate-31b" -Tag "31B"
    Assert-Throws { Register-PaperBridgeLocalModel -UserDataDir $rtDir -ProjectRoot $ProjectRoot -FilePath $p31b -RegisterMode "reference" -LicenseAccepted $true } "31B must throw"

    $unknownPlamo = Join-Path $tmpRoot "unknown_plamo.gguf"
    New-SyntheticGgufFile -Path $unknownPlamo -Architecture "plamo3" -ModelName "plamo-3-translate-2b" -Tag "UNKNOWN_SHA"
    Assert-Throws { Register-PaperBridgeLocalModel -UserDataDir $rtDir -ProjectRoot $ProjectRoot -FilePath $unknownPlamo -RegisterMode "reference" -LicenseAccepted $true } "Unknown SHA must throw without treating as 2B"

    Assert-Throws { Register-PaperBridgeLocalModel -UserDataDir $rtDir -ProjectRoot $ProjectRoot -FilePath $f08 -ExpectedCatalogModelId "plamo-3-translate-2b-q4_k_m" -RegisterMode "reference" -LicenseAccepted $true } "Wrong expected model ID must throw"
    Write-Output "[PASS] 2. Corrupt/split/non-plamo3/31B/unknown-SHA rejections passed."

    # 4. Reference vs Copy + self-copy guard + Japanese+spaces path
    $rUnconf = Resolve-PaperBridgeActiveModelForLaunch -UserDataDir $rtDir -ProjectRoot $ProjectRoot
    Assert-True ($rUnconf.Status -eq "Unconfigured") "Empty registry resolves to Unconfigured"

    $regRef08 = Register-PaperBridgeLocalModel -UserDataDir $rtDir -ProjectRoot $ProjectRoot -FilePath $f08 -RegisterMode "reference" -LicenseAccepted $true -SetActive $true
    Assert-True ($regRef08.Registration.source_type -eq "reference") "Default local registration uses reference"
    Assert-True ($regRef08.Registration.file_path -eq [System.IO.Path]::GetFullPath($f08)) "Reference keeps original path with Japanese/spaces"

    $regCopy2 = Register-PaperBridgeLocalModel -UserDataDir $rtDir -ProjectRoot $ProjectRoot -FilePath $f2 -RegisterMode "copy" -LicenseAccepted $true -SetActive $true
    Assert-True ($regCopy2.Registration.source_type -eq "managed_copy") "Copy sets managed_copy"
    Assert-True (Test-Path -LiteralPath $regCopy2.Registration.file_path) "Managed copy exists"
    Assert-True (Test-Path -LiteralPath $f2) "Original file preserved on copy"

    # Self-copy guard
    $regSelf2 = Register-PaperBridgeLocalModel -UserDataDir $rtDir -ProjectRoot $ProjectRoot -FilePath $regCopy2.Registration.file_path -RegisterMode "copy" -LicenseAccepted $true -SetActive $true
    Assert-True (((Get-FileHash -LiteralPath $regSelf2.Registration.file_path -Algorithm SHA256).Hash.ToLowerInvariant()) -eq $sha2) "Self-copy guard preserves intact file"

    # Launch resolution & args across models and backends
    $rReady2 = Resolve-PaperBridgeActiveModelForLaunch -UserDataDir $rtDir -ProjectRoot $ProjectRoot
    Assert-True ($rReady2.Status -eq "Ready") "2B resolves Ready"
    $argsGpu = Get-PaperBridgeLlamaLaunchArgs -ModelPath $rReady2.Entry.file_path -CatalogModel $rReady2.CatalogModel -LlamaPort 8000
    Assert-True (($argsGpu -contains "-ngl") -and ($argsGpu -contains "99")) "Vulkan args use -ngl 99"
    Assert-True ($argsGpu -contains "plamo-3-translate-2b-q4_k_m") "Alias uses catalog launch_settings.alias"
    $argsCpu = Get-PaperBridgeLlamaLaunchArgs -ModelPath $rReady2.Entry.file_path -CatalogModel $rReady2.CatalogModel -LlamaPort 8000 -UseCpu
    Assert-True ($argsCpu -contains "0") "CPU fallback uses -ngl 0"

    # Missing file detection + re-registration
    Remove-Item -LiteralPath $regCopy2.Registration.file_path -Force
    $rMissing = Resolve-PaperBridgeActiveModelForLaunch -UserDataDir $rtDir -ProjectRoot $ProjectRoot
    Assert-True ($rMissing.Status -eq "Missing") "Deleted active file resolves Missing"
    Write-Output "[PASS] 3. Reference vs Copy, self-copy guard, Missing detection & launch args passed."

    # 5. Legacy 2B automatic migration without re-download
    $legacyRt = Join-Path $tmpRoot "legacy_2b_runtime"
    $legacyDest = Join-Path $legacyRt "models\plamo-3-translate-2b-GGUF\plamo-3-translate-2b-Q4_K_M.gguf"
    New-Item -ItemType Directory -Path (Split-Path -Parent $legacyDest) -Force | Out-Null
    [System.IO.File]::Copy($f2Legacy, $legacyDest, $true)
    $rLegacy = Resolve-PaperBridgeActiveModelForLaunch -UserDataDir $legacyRt -ProjectRoot $ProjectRoot
    Assert-True ($rLegacy.Status -eq "Ready") "Legacy 2B auto-migrated and Ready"
    Assert-True ($rLegacy.Entry.source_type -eq "migrated_legacy") "Legacy migration records migrated_legacy"
    Assert-True ($rLegacy.Entry.measured_sha256 -eq $sha2Leg) "Legacy SHA-256 matched accepted artifact"
    Write-Output "[PASS] 4. Legacy 2B automatic migration passed."

    # 6. CLI invocations of model_setup.ps1 (-PreselectedPath, -Mode Select, -NonInteractive)
    $cliRt = Join-Path $tmpRoot "cli_runtime"
    New-Item -ItemType Directory -Path $cliRt -Force | Out-Null
    $out1 = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptsDir "model_setup.ps1") `
        -UserDataDir $cliRt -PreselectedPath $f08 -AcceptLicense -RegisterMode Reference 2>&1
    Assert-True ($LASTEXITCODE -eq 0) "model_setup CLI register 0.8B exited with 0 (output: $($out1 -join '; '))"
    Assert-True (($out1 -join "`n") -match "MODEL_ID:plamo-3-translate-0\.8b-q4_k_m") "CLI registered 0.8B"

    $out2 = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptsDir "model_setup.ps1") `
        -UserDataDir $cliRt -PreselectedPath $f8 -AcceptLicense -RegisterMode Copy 2>&1
    Assert-True ($LASTEXITCODE -eq 0) "model_setup CLI register 8B copy exited with 0"
    Assert-True (($out2 -join "`n") -match "MODEL_ID:plamo-3-translate-8b-q4_k_m") "CLI registered 8B"

    $out3 = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptsDir "model_setup.ps1") `
        -UserDataDir $cliRt -Mode Select -ModelId "plamo-3-translate-0.8b-q4_k_m" 2>&1
    Assert-True ($LASTEXITCODE -eq 0) "model_setup CLI select 0.8B exited with 0"

    $out4 = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptsDir "model_setup.ps1") `
        -UserDataDir $cliRt -NonInteractive -ModelId "plamo-3-translate-0.8b-q4_k_m" 2>&1
    Assert-True ($LASTEXITCODE -eq 0) "model_setup CLI NonInteractive check passed (exit=$LASTEXITCODE)"
    Assert-True (($out4 -join "`n") -match "VALID_MODEL_PRESENT:") "NonInteractive output confirms VALID_MODEL_PRESENT"
    Write-Output "[PASS] 5. model_setup.ps1 CLI (-PreselectedPath, -Mode Select, -NonInteractive) passed."

    # 7. Four-stage verification lifecycle checks in PowerShell helpers
    $stageRt = Join-Path $tmpRoot "stage_ps_runtime"
    New-Item -ItemType Directory -Path $stageRt -Force | Out-Null
    $f2CopySrc = Join-Path $tmpRoot "stage_2b.gguf"
    [System.IO.File]::Copy($f2, $f2CopySrc, $true)
    $regPs2 = Register-PaperBridgeLocalModel -UserDataDir $stageRt -ProjectRoot $ProjectRoot -FilePath $f2CopySrc -RegisterMode "reference" -LicenseAccepted $true -SetActive $true
    $regPs08 = Register-PaperBridgeLocalModel -UserDataDir $stageRt -ProjectRoot $ProjectRoot -FilePath $f08 -RegisterMode "reference" -LicenseAccepted $true -SetActive $false

    Assert-True (-not [bool]$regPs2.Registration.verification.launch_verified) "Fresh 2B PS registration starts with launch_verified=$false"
    Assert-True (-not [bool]$regPs2.Registration.verification.translation_verified) "Fresh 2B PS registration starts with translation_verified=$false"
    Assert-True ((Get-PaperBridgeVerificationStageLabel -Verification $regPs2.Registration.verification) -eq "ファイル検証済み") "Fresh 2B PS stage label is ファイル検証済み"
    Assert-True ((Get-PaperBridgeVerificationStageLabel -Verification $regPs08.Registration.verification) -eq "ファイル検証済み") "Fresh 0.8B PS stage label is ファイル検証済み"

    $launchedOk = Set-PaperBridgeModelLaunchVerified -UserDataDir $stageRt -RegistrationId $regPs2.Registration.registration_id
    Assert-True $launchedOk "Set-PaperBridgeModelLaunchVerified transitions launch_verified -> $true"
    $regStore = Read-ModelsRegistry -UserDataDir $stageRt
    $ps2Entry = $regStore.models | Where-Object { $_.registration_id -eq $regPs2.Registration.registration_id } | Select-Object -First 1
    Assert-True ([bool]$ps2Entry.verification.launch_verified -and -not [bool]$ps2Entry.verification.translation_verified) "2B after Set-PaperBridgeModelLaunchVerified has launch_verified=$true translation_verified=$false"
    Assert-True ((Get-PaperBridgeVerificationStageLabel -Verification $ps2Entry.verification) -eq "起動確認済み") "2B after launch check renders 起動確認済み"

    $ps2Entry.verification.translation_verified = $true
    Write-ModelsRegistry -UserDataDir $stageRt -Registry $regStore
    $regStore2 = Read-ModelsRegistry -UserDataDir $stageRt
    $ps2Entry2 = $regStore2.models | Where-Object { $_.registration_id -eq $regPs2.Registration.registration_id } | Select-Object -First 1
    Assert-True ((Get-PaperBridgeVerificationStageLabel -Verification $ps2Entry2.verification) -eq "実翻訳確認済み") "2B with translation_verified=$true renders 実翻訳確認済み"

    Remove-Item -LiteralPath $f2CopySrc -Force
    $missingLabel = Get-PaperBridgeVerificationStageLabel -Verification $ps2Entry2.verification -FileExistsOverride (Test-Path -LiteralPath $f2CopySrc)
    Assert-True ($missingLabel -eq "未検証（ファイル欠損）") "Deleted file renders 未検証（ファイル欠損）"
    Write-Output "[PASS] 6. Four-stage verification lifecycle checks in PowerShell passed."

    Write-Output "ALL_PS_MODEL_TESTS_PASSED"
} finally {
    if ($null -eq $prevCatalogEnv) {
        Remove-Item Env:\PAPERBRIDGE_MODEL_CATALOG_PATH -ErrorAction SilentlyContinue
    } else {
        $env:PAPERBRIDGE_MODEL_CATALOG_PATH = $prevCatalogEnv
    }
    if (Test-Path -LiteralPath $tmpRoot) {
        Remove-Item -LiteralPath $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
