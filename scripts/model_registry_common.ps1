# PaperBridge Local - Shared Model Catalog, GGUF Validation & Registry Helpers
# Dot-sourced by model_setup.ps1, setup_wizard.ps1, and launcher.ps1.
# Runs on Windows PowerShell 5.1+ without external dependencies.

$script:PB_SPLIT_FILENAME_REGEX = '-\d{4,5}-of-\d{4,5}\.gguf$'
$script:PB_UNIX_EPOCH_TICKS = 621355968000000000L

if (-not ("PaperBridgeGgufFastSkip" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.IO;

public static class PaperBridgeGgufFastSkip {
    public static void SkipStringArray(BinaryReader reader, long count) {
        Stream s = reader.BaseStream;
        long streamLen = s.CanSeek ? s.Length : -1L;
        for (long i = 0; i < count; i++) {
            ulong lenU64 = reader.ReadUInt64();
            if (lenU64 > 1048576UL) {
                throw new InvalidDataException("Invalid GGUF string length: " + lenU64);
            }
            long len = (long)lenU64;
            if (len == 0) {
                continue;
            }
            if (streamLen >= 0) {
                if (s.Position + len > streamLen) {
                    throw new EndOfStreamException("Unexpected EOF while skipping GGUF string");
                }
                s.Seek(len, SeekOrigin.Current);
            } else {
                byte[] skipped = reader.ReadBytes((int)len);
                if (skipped.Length != (int)len) {
                    throw new EndOfStreamException("Unexpected EOF while skipping GGUF string");
                }
            }
        }
    }
}
"@
}

function ConvertTo-SafeSlug {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return "model" }
    return ($Text -replace '\.', '_' -replace '[^A-Za-z0-9_-]', '_')
}

function Format-ModelSizeText {
    param([int64]$Bytes)
    if ($Bytes -le 0) { return "不明" }
    if ($Bytes -ge 500000000L) {
        return ("約 {0:N2} GB" -f ($Bytes / 1000000000.0))
    }
    return ("約 {0:N1} MB" -f ($Bytes / 1000000.0))
}

function Get-NormalizedModelPathKey {
    param([string]$FilePath)
    if ([string]::IsNullOrWhiteSpace($FilePath)) { return "" }
    try {
        $full = [System.IO.Path]::GetFullPath($FilePath.Trim().Trim('"'))
        if ($full.Length -gt 3) {
            $full = $full.TrimEnd('\', '/')
        }
        return $full.ToLowerInvariant()
    } catch {
        return $FilePath.Trim().ToLowerInvariant()
    }
}

function Get-FileMtimeUnixNs {
    param([System.IO.FileInfo]$FileInfo)
    if (-not $FileInfo) { return 0L }
    return ([int64]($FileInfo.LastWriteTimeUtc.Ticks - $script:PB_UNIX_EPOCH_TICKS)) * 100L
}

function Get-PaperBridgeCatalogPath {
    param([string]$ProjectRoot)
    if (-not [string]::IsNullOrWhiteSpace($env:PAPERBRIDGE_MODEL_CATALOG_PATH) -and (Test-Path -LiteralPath $env:PAPERBRIDGE_MODEL_CATALOG_PATH)) {
        return $env:PAPERBRIDGE_MODEL_CATALOG_PATH
    }
    return (Join-Path $ProjectRoot "backend\model_catalog.json")
}

function Get-PaperBridgeModelCatalog {
    param([string]$ProjectRoot)
    $catalogPath = Get-PaperBridgeCatalogPath -ProjectRoot $ProjectRoot
    if (-not (Test-Path -LiteralPath $catalogPath)) {
        throw "モデルカタログが見つかりません: $catalogPath"
    }
    $raw = [System.IO.File]::ReadAllText($catalogPath, [System.Text.Encoding]::UTF8)
    $parsed = $raw | ConvertFrom-Json
    if (-not $parsed -or -not $parsed.models) {
        throw "モデルカタログの形式が不正です: $catalogPath"
    }
    $models = @($parsed.models)
    return [pscustomobject]@{
        schema_version       = $parsed.schema_version
        catalog_id           = $parsed.catalog_id
        models               = $models
        unsupported_policies = $parsed.unsupported_policies
        catalog_path         = $catalogPath
    }
}

function Get-CatalogModelById {
    param(
        $Catalog,
        [string]$ModelId = ""
    )
    if (-not $Catalog -or -not $Catalog.models) { return $null }
    if (-not [string]::IsNullOrWhiteSpace($ModelId)) {
        foreach ($m in $Catalog.models) {
            if ($m.model_id -eq $ModelId) { return $m }
        }
        return $null
    }
    foreach ($m in $Catalog.models) {
        if ($m.is_default -eq $true) { return $m }
    }
    return $Catalog.models[0]
}

function Get-CatalogAcceptedArtifacts {
    param($CatalogModel)
    $list = New-Object System.Collections.Generic.List[object]
    $seen = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)

    if (-not $CatalogModel) { return @() }
    if (-not [string]::IsNullOrWhiteSpace($CatalogModel.known_sha256) -and $seen.Add([string]$CatalogModel.known_sha256)) {
        $list.Add([pscustomobject]@{
            revision   = [string]$CatalogModel.pinned_revision
            size_bytes = [int64]$CatalogModel.known_size_bytes
            sha256     = ([string]$CatalogModel.known_sha256).ToLowerInvariant()
            label      = "pinned"
        })
    }
    if ($CatalogModel.accepted_artifacts) {
        foreach ($art in $CatalogModel.accepted_artifacts) {
            $sha = [string]$art.sha256
            if (-not [string]::IsNullOrWhiteSpace($sha) -and $seen.Add($sha)) {
                $list.Add([pscustomobject]@{
                    revision   = [string]$art.revision
                    size_bytes = [int64]$art.size_bytes
                    sha256     = $sha.ToLowerInvariant()
                    label      = [string]$art.label
                })
            }
        }
    }
    return @($list.ToArray())
}

function Get-ModelSha256Hex {
    param([string]$FilePath)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    $fs = $null
    try {
        $fs = [System.IO.File]::Open($FilePath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        $hashBytes = $sha256.ComputeHash($fs)
        $sb = New-Object System.Text.StringBuilder
        foreach ($b in $hashBytes) {
            $sb.Append($b.ToString("x2")) | Out-Null
        }
        return $sb.ToString()
    } finally {
        if ($fs) { $fs.Dispose() }
        if ($sha256) { $sha256.Dispose() }
    }
}

function Read-GgufString {
    param(
        [System.IO.BinaryReader]$Reader,
        [bool]$Store = $true
    )
    $lenU64 = $Reader.ReadUInt64()
    if ($lenU64 -gt [uint64]1048576) {
        throw "Invalid GGUF string length: $lenU64"
    }
    $len = [int]$lenU64
    $bytes = $Reader.ReadBytes($len)
    if ($bytes.Length -ne $len) {
        throw "Unexpected EOF while reading GGUF string"
    }
    if (-not $Store) { return "" }
    return [System.Text.Encoding]::UTF8.GetString($bytes)
}

function Get-GgufScalarSize {
    param([uint32]$ValueType)
    switch ($ValueType) {
        0  { return 1 }  # uint8
        1  { return 1 }  # int8
        2  { return 2 }  # uint16
        3  { return 2 }  # int16
        4  { return 4 }  # uint32
        5  { return 4 }  # int32
        6  { return 4 }  # float32
        7  { return 1 }  # bool
        10 { return 8 }  # uint64
        11 { return 8 }  # int64
        12 { return 8 }  # float64
        default { return -1 }
    }
}

function Read-OrSkipGgufValue {
    param(
        [System.IO.BinaryReader]$Reader,
        [uint32]$ValueType,
        [bool]$Store = $true,
        [int]$Depth = 0
    )
    if ($Depth -gt 8) {
        throw "Exceeded maximum GGUF array nesting depth"
    }
    switch ($ValueType) {
        0  { $v = $Reader.ReadByte(); if ($Store) { return [int64]$v } return $null }
        1  { $v = $Reader.ReadSByte(); if ($Store) { return [int64]$v } return $null }
        2  { $v = $Reader.ReadUInt16(); if ($Store) { return [int64]$v } return $null }
        3  { $v = $Reader.ReadInt16(); if ($Store) { return [int64]$v } return $null }
        4  { $v = $Reader.ReadUInt32(); if ($Store) { return [int64]$v } return $null }
        5  { $v = $Reader.ReadInt32(); if ($Store) { return [int64]$v } return $null }
        6  { $v = $Reader.ReadSingle(); if ($Store) { return [double]$v } return $null }
        7  { $v = ($Reader.ReadByte() -ne 0); if ($Store) { return [bool]$v } return $null }
        8  { return (Read-GgufString -Reader $Reader -Store:$Store) }
        9  {
            $elemType = $Reader.ReadUInt32()
            $arrLenU64 = $Reader.ReadUInt64()
            if ($arrLenU64 -gt [uint64]2000000) {
                throw "Invalid GGUF array length: $arrLenU64"
            }
            $arrLen = [int64]$arrLenU64
            $scalarSize = Get-GgufScalarSize -ValueType $elemType
            if ((-not $Store -or $arrLen -gt 16) -and $scalarSize -gt 0) {
                $skipBytes = [int64]($scalarSize * $arrLen)
                $Reader.BaseStream.Seek($skipBytes, [System.IO.SeekOrigin]::Current) | Out-Null
                return $null
            }
            if ((-not $Store -or $arrLen -gt 16) -and $elemType -eq 8) {
                if ($arrLen -gt 0) {
                    [PaperBridgeGgufFastSkip]::SkipStringArray($Reader, $arrLen)
                }
                return $null
            }
            if (-not $Store -or $arrLen -gt 16) {
                for ($i = 0L; $i -lt $arrLen; $i++) {
                    $null = Read-OrSkipGgufValue -Reader $Reader -ValueType $elemType -Store:$false -Depth ($Depth + 1)
                }
                return $null
            }
            $items = New-Object System.Collections.Generic.List[object]
            for ($i = 0L; $i -lt $arrLen; $i++) {
                $items.Add((Read-OrSkipGgufValue -Reader $Reader -ValueType $elemType -Store:$true -Depth ($Depth + 1)))
            }
            return $items.ToArray()
        }
        10 {
            $v = $Reader.ReadUInt64()
            if ($Store) {
                if ($v -le [uint64][int64]::MaxValue) { return [int64]$v }
                return $v
            }
            return $null
        }
        11 { $v = $Reader.ReadInt64(); if ($Store) { return $v } return $null }
        12 { $v = $Reader.ReadDouble(); if ($Store) { return $v } return $null }
        default { throw "Unsupported GGUF metadata value type: $ValueType" }
    }
}

function Inspect-GgufFile {
    param(
        [string]$FilePath,
        [switch]$ComputeHash = $false
    )
    $cleanPath = if ([string]::IsNullOrWhiteSpace($FilePath)) { "" } else { $FilePath.Trim().Trim('"') }
    $resolvedPath = $cleanPath
    try {
        if (-not [string]::IsNullOrWhiteSpace($cleanPath)) {
            $resolvedPath = [System.IO.Path]::GetFullPath($cleanPath)
        }
    } catch {}

    if ([string]::IsNullOrWhiteSpace($resolvedPath) -or -not (Test-Path -LiteralPath $resolvedPath)) {
        return [pscustomobject]@{
            file_path       = $resolvedPath
            file_exists     = $false
            file_size_bytes = 0L
            file_mtime_ns   = 0L
            format_valid    = $false
            is_split_gguf   = $false
            gguf_version    = $null
            tensor_count    = 0L
            architecture    = $null
            model_name      = $null
            size_label      = $null
            file_type       = $null
            split_count     = $null
            split_no        = $null
            sha256          = ""
            error           = "指定されたモデルファイルが見つかりません。"
        }
    }

    $fi = Get-Item -LiteralPath $resolvedPath
    if ($fi.PSIsContainer) {
        return [pscustomobject]@{
            file_path       = $resolvedPath
            file_exists     = $false
            file_size_bytes = 0L
            file_mtime_ns   = 0L
            format_valid    = $false
            is_split_gguf   = $false
            gguf_version    = $null
            tensor_count    = 0L
            architecture    = $null
            model_name      = $null
            size_label      = $null
            file_type       = $null
            split_count     = $null
            split_no        = $null
            sha256          = ""
            error           = "指定されたパスは通常のファイルではありません。"
        }
    }

    $sizeBytes = [int64]$fi.Length
    $mtimeNs = Get-FileMtimeUnixNs -FileInfo $fi
    $splitByName = ($fi.Name -match $script:PB_SPLIT_FILENAME_REGEX)

    if ($sizeBytes -lt 24) {
        return [pscustomobject]@{
            file_path       = $resolvedPath
            file_exists     = $true
            file_size_bytes = $sizeBytes
            file_mtime_ns   = $mtimeNs
            format_valid    = $false
            is_split_gguf   = $splitByName
            gguf_version    = $null
            tensor_count    = 0L
            architecture    = $null
            model_name      = $null
            size_label      = $null
            file_type       = $null
            split_count     = $null
            split_no        = $null
            sha256          = ""
            error           = "ファイルサイズが小さすぎるため有効なGGUFファイルではありません。"
        }
    }

    $wanted = @{
        "general.architecture" = $true
        "general.name"         = $true
        "general.size_label"   = $true
        "general.file_type"    = $true
        "split.count"          = $true
        "split.no"             = $true
    }
    $kvs = @{}
    $version = $null
    $tensorCount = 0L

    $fs = $null
    $br = $null
    try {
        $fs = [System.IO.File]::Open($resolvedPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        $br = New-Object System.IO.BinaryReader($fs, [System.Text.Encoding]::UTF8, $true)
        $magic = $br.ReadBytes(4)
        if ($magic.Length -ne 4 -or $magic[0] -ne 0x47 -or $magic[1] -ne 0x47 -or $magic[2] -ne 0x55 -or $magic[3] -ne 0x46) {
            return [pscustomobject]@{
                file_path       = $resolvedPath
                file_exists     = $true
                file_size_bytes = $sizeBytes
                file_mtime_ns   = $mtimeNs
                format_valid    = $false
                is_split_gguf   = $splitByName
                gguf_version    = $null
                tensor_count    = 0L
                architecture    = $null
                model_name      = $null
                size_label      = $null
                file_type       = $null
                split_count     = $null
                split_no        = $null
                sha256          = ""
                error           = "先頭マジックが GGUF ではありません（対応外のファイル形式または破損ファイル）。"
            }
        }
        $version = [int]$br.ReadUInt32()
        $tCountU64 = $br.ReadUInt64()
        $kvCountU64 = $br.ReadUInt64()
        $tensorCount = if ($tCountU64 -le [uint64][int64]::MaxValue) { [int64]$tCountU64 } else { [int64]::MaxValue }

        if ($version -ne 2 -and $version -ne 3) {
            return [pscustomobject]@{
                file_path       = $resolvedPath
                file_exists     = $true
                file_size_bytes = $sizeBytes
                file_mtime_ns   = $mtimeNs
                format_valid    = $false
                is_split_gguf   = $splitByName
                gguf_version    = $version
                tensor_count    = $tensorCount
                architecture    = $null
                model_name      = $null
                size_label      = $null
                file_type       = $null
                split_count     = $null
                split_no        = $null
                sha256          = ""
                error           = "未対応の GGUF バージョン（v$version）です（v2/v3 のみ対応）。"
            }
        }

        if ($kvCountU64 -gt [uint64]65536 -or $tCountU64 -eq [uint64]0) {
            return [pscustomobject]@{
                file_path       = $resolvedPath
                file_exists     = $true
                file_size_bytes = $sizeBytes
                file_mtime_ns   = $mtimeNs
                format_valid    = $false
                is_split_gguf   = $splitByName
                gguf_version    = $version
                tensor_count    = $tensorCount
                architecture    = $null
                model_name      = $null
                size_label      = $null
                file_type       = $null
                split_count     = $null
                split_no        = $null
                sha256          = ""
                error           = "GGUF ヘッダー（テンソル数またはメタデータ数）が不正または破損しています。"
            }
        }

        $kvCount = [int]$kvCountU64
        for ($i = 0; $i -lt $kvCount; $i++) {
            $key = Read-GgufString -Reader $br -Store:$true
            $vType = $br.ReadUInt32()
            $shouldStore = $wanted.ContainsKey($key)
            $val = Read-OrSkipGgufValue -Reader $br -ValueType $vType -Store:$shouldStore
            if ($shouldStore) {
                $kvs[$key] = $val
            }
        }
    } catch {
        return [pscustomobject]@{
            file_path       = $resolvedPath
            file_exists     = $true
            file_size_bytes = $sizeBytes
            file_mtime_ns   = $mtimeNs
            format_valid    = $false
            is_split_gguf   = $splitByName
            gguf_version    = $version
            tensor_count    = $tensorCount
            architecture    = $null
            model_name      = $null
            size_label      = $null
            file_type       = $null
            split_count     = $null
            split_no        = $null
            sha256          = ""
            error           = "GGUF ヘッダー解析エラー（ファイル破損の可能性）: $($_.Exception.Message)"
        }
    } finally {
        if ($br) { $br.Dispose() }
        if ($fs) { $fs.Dispose() }
    }

    $splitCount = $null
    if ($kvs.ContainsKey("split.count") -and $null -ne $kvs["split.count"]) {
        $splitCount = [int64]$kvs["split.count"]
    }
    $splitNo = $null
    if ($kvs.ContainsKey("split.no") -and $null -ne $kvs["split.no"]) {
        $splitNo = [int64]$kvs["split.no"]
    }
    $isSplit = $splitByName -or ($null -ne $splitCount -and $splitCount -gt 1)

    $shaHex = ""
    if ($ComputeHash) {
        $shaHex = Get-ModelSha256Hex -FilePath $resolvedPath
    }

    return [pscustomobject]@{
        file_path       = $resolvedPath
        file_exists     = $true
        file_size_bytes = $sizeBytes
        file_mtime_ns   = $mtimeNs
        format_valid    = $true
        is_split_gguf   = $isSplit
        gguf_version    = $version
        tensor_count    = $tensorCount
        architecture    = if ($kvs.ContainsKey("general.architecture")) { [string]$kvs["general.architecture"] } else { $null }
        model_name      = if ($kvs.ContainsKey("general.name")) { [string]$kvs["general.name"] } else { $null }
        size_label      = if ($kvs.ContainsKey("general.size_label")) { [string]$kvs["general.size_label"] } else { $null }
        file_type       = if ($kvs.ContainsKey("general.file_type") -and $null -ne $kvs["general.file_type"]) { [int64]$kvs["general.file_type"] } else { $null }
        split_count     = $splitCount
        split_no        = $splitNo
        sha256          = $shaHex
        error           = $null
    }
}

function Find-CatalogMatchBySha256 {
    param(
        $Catalog,
        [string]$Sha256
    )
    if (-not $Catalog -or [string]::IsNullOrWhiteSpace($Sha256)) { return $null }
    $needle = $Sha256.Trim().ToLowerInvariant()
    foreach ($catModel in $Catalog.models) {
        foreach ($art in (Get-CatalogAcceptedArtifacts -CatalogModel $catModel)) {
            if ($art.sha256 -eq $needle) {
                return [pscustomobject]@{
                    CatalogModel = $catModel
                    Artifact     = $art
                }
            }
        }
    }
    return $null
}

function Test-ModelMatchesCatalog {
    param(
        $Catalog,
        $Inspection,
        [string]$ExpectedCatalogModelId = ""
    )
    if (-not $Inspection.file_exists) {
        throw $(if ($Inspection.error) { $Inspection.error } else { "指定されたモデルファイルが見つかりません。" })
    }
    if ($Inspection.is_split_gguf) {
        $msg = if ($Catalog.unsupported_policies -and $Catalog.unsupported_policies.multi_file_split_gguf) {
            [string]$Catalog.unsupported_policies.multi_file_split_gguf
        } else {
            "分割 GGUF（multi-file）は初期版の対象外です。単一 GGUF ファイルを使用してください。"
        }
        throw $msg
    }
    if (-not $Inspection.format_valid) {
        throw $(if ($Inspection.error) { $Inspection.error } else { "有効な GGUF ファイルではありません。" })
    }

    $arch = if ($Inspection.architecture) { $Inspection.architecture.Trim().ToLowerInvariant() } else { "" }
    if ($arch -ne "plamo3") {
        $archShow = if ($Inspection.architecture) { $Inspection.architecture } else { "不明" }
        throw "非対応のモデルアーキテクチャです（general.architecture='$archShow'）。初期版は PLaMo 3 Translate（plamo3 / PFML3形式）の単一 GGUF のみ対応しており、他モデルを 2B 等として誤認登録することはできません。"
    }

    $gname = if ($Inspection.model_name) { $Inspection.model_name.ToLowerInvariant() } else { "" }
    if ($gname.Contains("31b") -or $Inspection.file_size_bytes -gt 10000000000L) {
        $msg31 = if ($Catalog.unsupported_policies -and $Catalog.unsupported_policies.plamo_31b) {
            [string]$Catalog.unsupported_policies.plamo_31b
        } else {
            "PLaMo 3 Translate 31B 等の超大容量モデルは初期版の導入対象外です。"
        }
        throw $msg31
    }

    if ([string]::IsNullOrWhiteSpace($Inspection.sha256)) {
        $Inspection = Inspect-GgufFile -FilePath $Inspection.file_path -ComputeHash
    }
    $match = Find-CatalogMatchBySha256 -Catalog $Catalog -Sha256 $Inspection.sha256
    if (-not $match) {
        $fn = [System.IO.Path]::GetFileName($Inspection.file_path)
        $prefix = if ($Inspection.sha256.Length -ge 12) { $Inspection.sha256.Substring(0, 12) } else { $Inspection.sha256 }
        throw "検証済みモデルカタログの既知 SHA-256 と一致しないため登録できません（実測ファイル: $fn, サイズ: $($Inspection.file_size_bytes) bytes, SHA-256先頭: $prefix）。破損ファイルまたはカタログ未検証・未対応量子化モデルを誤って既存モデル扱いすることはできません。"
    }

    if ($Inspection.file_size_bytes -ne $match.Artifact.size_bytes) {
        throw "ファイルサイズが一致しません（期待: $($match.Artifact.size_bytes) bytes / 実測: $($Inspection.file_size_bytes) bytes）。"
    }

    if (-not [string]::IsNullOrWhiteSpace($ExpectedCatalogModelId) -and $match.CatalogModel.model_id -ne $ExpectedCatalogModelId) {
        $expModel = Get-CatalogModelById -Catalog $Catalog -ModelId $ExpectedCatalogModelId
        $expName = if ($expModel) { $expModel.display_name } else { $ExpectedCatalogModelId }
        throw "選択されたファイルは「$($match.CatalogModel.display_name)」ですが、指定された導入先モデル「$expName」と一致しません。正しいファイルを選択するか、該当モデルを選んで登録してください。"
    }

    $now = [DateTime]::UtcNow.ToString("o")
    $ver = [ordered]@{
        file_exists          = $true
        format_valid         = $true
        hash_status          = "matched_known"
        backend_compatible   = (@($match.CatalogModel.supported_backends) -contains "llama.cpp")
        launch_verified      = $false
        translation_verified = $false
        matched_revision     = [string]$match.Artifact.revision
        matched_label        = [string]$match.Artifact.label
        validation_error     = $null
        validated_at         = $now
    }

    return [pscustomobject]@{
        CatalogModel = $match.CatalogModel
        Artifact     = $match.Artifact
        Inspection   = $Inspection
        Verification = $ver
    }
}

function Read-ModelsRegistry {
    param([string]$UserDataDir)
    $regPath = Join-Path $UserDataDir "models_registry.json"
    if (-not (Test-Path -LiteralPath $regPath)) {
        return [ordered]@{
            schema_version  = 1
            active_model_id = $null
            models          = [object[]]@()
        }
    }
    $raw = [System.IO.File]::ReadAllText($regPath, [System.Text.Encoding]::UTF8)
    $parsed = $raw | ConvertFrom-Json
    $list = New-Object System.Collections.Generic.List[object]
    if ($parsed -and $parsed.models) {
        foreach ($m in $parsed.models) {
            if ($null -ne $m) {
                $list.Add($m)
            }
        }
    }
    return [ordered]@{
        schema_version  = if ($parsed.schema_version) { [int]$parsed.schema_version } else { 1 }
        active_model_id = if ([string]::IsNullOrWhiteSpace($parsed.active_model_id)) { $null } else { [string]$parsed.active_model_id }
        models          = [object[]]$list.ToArray()
    }
}

function Write-ModelsRegistry {
    param(
        [string]$UserDataDir,
        [System.Collections.IDictionary]$Registry
    )
    if (-not (Test-Path -LiteralPath $UserDataDir)) {
        New-Item -ItemType Directory -Path $UserDataDir -Force | Out-Null
    }
    $regPath = Join-Path $UserDataDir "models_registry.json"
    $tmpPath = Join-Path $UserDataDir (".models_registry.json.{0}.tmp" -f ([guid]::NewGuid().ToString("N").Substring(0, 8)))

    $modelsArr = New-Object System.Collections.Generic.List[object]
    if ($Registry.models) {
        foreach ($item in $Registry.models) {
            if ($null -ne $item) { $modelsArr.Add($item) }
        }
    }
    $payloadObj = [ordered]@{
        schema_version  = 1
        active_model_id = if ([string]::IsNullOrWhiteSpace($Registry.active_model_id)) { $null } else { [string]$Registry.active_model_id }
        models          = [object[]]$modelsArr.ToArray()
    }
    $json = $payloadObj | ConvertTo-Json -Depth 12
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tmpPath, $json, $utf8NoBom)
    Move-Item -LiteralPath $tmpPath -Destination $regPath -Force
}

function New-RegisteredModelObject {
    param(
        [string]$RegistrationId,
        $CatalogModel,
        [string]$SourceType,
        $Inspection,
        $Verification,
        [bool]$LicenseAccepted,
        [string]$CreatedAt = ""
    )
    $now = [DateTime]::UtcNow.ToString("o")
    $cAt = if ([string]::IsNullOrWhiteSpace($CreatedAt)) { $now } else { $CreatedAt }
    return [ordered]@{
        registration_id     = $RegistrationId
        catalog_model_id    = [string]$CatalogModel.model_id
        display_name        = [string]$CatalogModel.display_name
        short_name          = [string]$CatalogModel.short_name
        provider            = [string]$CatalogModel.provider
        parameter_size      = [string]$CatalogModel.parameter_size
        quantization        = [string]$CatalogModel.quantization
        source_type         = $SourceType
        file_path           = [string]$Inspection.file_path
        file_name           = [System.IO.Path]::GetFileName($Inspection.file_path)
        file_size_bytes     = [int64]$Inspection.file_size_bytes
        file_mtime_ns       = [int64]$Inspection.file_mtime_ns
        measured_sha256     = [string]$Inspection.sha256
        gguf_architecture   = $Inspection.architecture
        gguf_model_name     = $Inspection.model_name
        verification        = $Verification
        license_accepted    = [bool]$LicenseAccepted
        license_accepted_at = if ($LicenseAccepted) { $now } else { $null }
        created_at          = $cAt
        updated_at          = $now
    }
}

function Get-PaperBridgeVerificationStageLabel {
    param(
        $Verification,
        [bool]$FileExistsOverride = $true
    )
    if (-not $FileExistsOverride -or -not $Verification -or -not [bool]$Verification.file_exists -or ([string]$Verification.hash_status -eq "missing_file")) {
        return "未検証（ファイル欠損）"
    }
    if (-not [bool]$Verification.format_valid -or ([string]$Verification.hash_status -eq "mismatch")) {
        return "未検証（整合性エラー）"
    }
    if (([string]$Verification.hash_status -ne "matched_known") -or -not [bool]$Verification.backend_compatible) {
        return "未検証"
    }
    if ([bool]$Verification.translation_verified) {
        return "実翻訳確認済み"
    }
    if ([bool]$Verification.launch_verified) {
        return "起動確認済み"
    }
    return "ファイル検証済み"
}

function Set-PaperBridgeModelLaunchVerified {
    param(
        [string]$UserDataDir,
        [string]$RegistrationId
    )
    if ([string]::IsNullOrWhiteSpace($UserDataDir) -or [string]::IsNullOrWhiteSpace($RegistrationId)) {
        return $false
    }
    $reg = Read-ModelsRegistry -UserDataDir $UserDataDir
    $changed = $false
    $now = [DateTime]::UtcNow.ToString("o")
    foreach ($m in $reg.models) {
        if ($m.registration_id -eq $RegistrationId -and $m.verification) {
            $fileOk = ([bool]$m.verification.file_exists -and [bool]$m.verification.format_valid -and ([string]$m.verification.hash_status -eq "matched_known") -and [bool]$m.verification.backend_compatible)
            if ($fileOk -and -not [bool]$m.verification.launch_verified) {
                $m.verification.launch_verified = $true
                $m.updated_at = $now
                $changed = $true
            }
            break
        }
    }
    if ($changed) {
        Write-ModelsRegistry -UserDataDir $UserDataDir -Registry $reg
    }
    return $changed
}

function Sync-ModelsRegistryWithLegacy {
    param(
        [string]$UserDataDir,
        [string]$ProjectRoot,
        $Catalog = $null
    )
    if (-not $Catalog) {
        $Catalog = Get-PaperBridgeModelCatalog -ProjectRoot $ProjectRoot
    }
    $reg = Read-ModelsRegistry -UserDataDir $UserDataDir
    $default2b = Get-CatalogModelById -Catalog $Catalog -ModelId "plamo-3-translate-2b-q4_k_m"
    $dirty = $false

    $modelsList = New-Object System.Collections.Generic.List[object]
    $knownPathKeys = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($m in $reg.models) {
        $modelsList.Add($m)
        $k = Get-NormalizedModelPathKey -FilePath ([string]$m.file_path)
        if (-not [string]::IsNullOrWhiteSpace($k)) {
            $null = $knownPathKeys.Add($k)
        }
    }

    if ($default2b) {
        $acceptedSizes = New-Object System.Collections.Generic.HashSet[int64]
        foreach ($art in (Get-CatalogAcceptedArtifacts -CatalogModel $default2b)) {
            $null = $acceptedSizes.Add([int64]$art.size_bytes)
        }
        $searchRoots = @($UserDataDir, $ProjectRoot)
        foreach ($root in $searchRoots) {
            if ([string]::IsNullOrWhiteSpace($root)) { continue }
            $cand = Join-Path (Join-Path (Join-Path $root "models") $default2b.relative_subdir) $default2b.filename
            if (-not (Test-Path -LiteralPath $cand -PathType Leaf)) { continue }
            $candKey = Get-NormalizedModelPathKey -FilePath $cand
            if ($knownPathKeys.Contains($candKey)) { continue }

            $fi = Get-Item -LiteralPath $cand
            if (-not $acceptedSizes.Contains([int64]$fi.Length)) { continue }

            $insp = Inspect-GgufFile -FilePath $cand -ComputeHash
            try {
                $res = Test-ModelMatchesCatalog -Catalog $Catalog -Inspection $insp -ExpectedCatalogModelId $default2b.model_id
            } catch {
                continue
            }

            $slug = ConvertTo-SafeSlug -Text $res.CatalogModel.model_id
            $regId = "reg-$slug-legacy"
            $suffix = 0
            while ($true) {
                $collision = $false
                foreach ($existing in $modelsList) {
                    if ($existing.registration_id -eq $regId) { $collision = $true; break }
                }
                if (-not $collision) { break }
                $suffix++
                $regId = "reg-$slug-legacy-$suffix"
            }

            $entry = New-RegisteredModelObject `
                -RegistrationId $regId `
                -CatalogModel $res.CatalogModel `
                -SourceType "migrated_legacy" `
                -Inspection $res.Inspection `
                -Verification $res.Verification `
                -LicenseAccepted $true
            $modelsList.Add($entry)
            $null = $knownPathKeys.Add($candKey)
            if ([string]::IsNullOrWhiteSpace($reg.active_model_id)) {
                $reg.active_model_id = $regId
            }
            $dirty = $true
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($reg.active_model_id)) {
        $foundActive = $false
        foreach ($m in $modelsList) {
            if ($m.registration_id -eq $reg.active_model_id) { $foundActive = $true; break }
        }
        if (-not $foundActive) {
            $reg.active_model_id = $null
            $dirty = $true
        }
    }

    $reg.models = [object[]]$modelsList.ToArray()
    if ($dirty) {
        Write-ModelsRegistry -UserDataDir $UserDataDir -Registry $reg
    }
    return [pscustomobject]@{
        Catalog  = $Catalog
        Registry = $reg
    }
}

function Register-PaperBridgeLocalModel {
    param(
        [string]$UserDataDir,
        [string]$ProjectRoot,
        [string]$FilePath,
        [string]$ExpectedCatalogModelId = "",
        [ValidateSet("reference", "copy")]
        [string]$RegisterMode = "reference",
        [bool]$LicenseAccepted = $false,
        [bool]$SetActive = $true,
        [string]$ReplacesRegistrationId = "",
        [string]$SourceTypeOverride = ""
    )
    $sync = Sync-ModelsRegistryWithLegacy -UserDataDir $UserDataDir -ProjectRoot $ProjectRoot
    $catalog = $sync.Catalog
    $reg = $sync.Registry

    $insp = Inspect-GgufFile -FilePath $FilePath -ComputeHash
    $validated = Test-ModelMatchesCatalog -Catalog $catalog -Inspection $insp -ExpectedCatalogModelId $ExpectedCatalogModelId
    $actualCat = $validated.CatalogModel
    $ver = $validated.Verification

    if ($actualCat.license.requires_agreement -and -not $LicenseAccepted) {
        throw "「$($actualCat.display_name)」を登録するには、モデル利用条件（$($actualCat.license.name)）への同意が必要です。"
    }

    $finalPath = $insp.file_path
    $finalSourceType = if (-not [string]::IsNullOrWhiteSpace($SourceTypeOverride)) {
        $SourceTypeOverride
    } elseif ($RegisterMode -eq "copy") {
        $targetDir = Join-Path (Join-Path $UserDataDir "models") $actualCat.relative_subdir
        $targetPath = [System.IO.Path]::GetFullPath((Join-Path $targetDir $actualCat.filename))
        if ((Get-NormalizedModelPathKey -FilePath $insp.file_path) -eq (Get-NormalizedModelPathKey -FilePath $targetPath)) {
            # Self-copy guard: never copy a file onto itself
            $finalPath = $insp.file_path
            "managed_copy"
        } else {
            if (-not (Test-Path -LiteralPath $targetDir)) {
                New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
            }
            $tmpCopy = Join-Path $targetDir ("{0}.{1}.copying" -f $actualCat.filename, ([guid]::NewGuid().ToString("N").Substring(0, 8)))
            try {
                Copy-Item -LiteralPath $insp.file_path -Destination $tmpCopy -Force
                $copiedSha = Get-ModelSha256Hex -FilePath $tmpCopy
                $copiedLen = (Get-Item -LiteralPath $tmpCopy).Length
                if ($copiedSha -ne $insp.sha256 -or $copiedLen -ne $insp.file_size_bytes) {
                    throw "管理ディレクトリへのコピー中にファイル内容が一致しなくなりました（コピー中止）。"
                }
                Move-Item -LiteralPath $tmpCopy -Destination $targetPath -Force
            } finally {
                if (Test-Path -LiteralPath $tmpCopy) {
                    Remove-Item -LiteralPath $tmpCopy -Force -ErrorAction SilentlyContinue
                }
            }
            $finalPath = $targetPath
            $insp = Inspect-GgufFile -FilePath $finalPath -ComputeHash
            "managed_copy"
        }
    } else {
        "reference"
    }

    $finalKey = Get-NormalizedModelPathKey -FilePath $finalPath
    $modelsList = New-Object System.Collections.Generic.List[object]
    $existingEntry = $null
    if (-not [string]::IsNullOrWhiteSpace($ReplacesRegistrationId)) {
        foreach ($m in $reg.models) {
            if ($m.registration_id -eq $ReplacesRegistrationId) {
                $existingEntry = $m
                break
            }
        }
    }
    foreach ($m in $reg.models) {
        if ($existingEntry -and $m.registration_id -eq $existingEntry.registration_id) {
            continue
        }
        if ((Get-NormalizedModelPathKey -FilePath ([string]$m.file_path)) -eq $finalKey) {
            if (-not $existingEntry) {
                $existingEntry = $m
            }
            continue
        }
        $modelsList.Add($m)
    }

    if ($existingEntry) {
        $regId = [string]$existingEntry.registration_id
        $createdAt = [string]$existingEntry.created_at
        $samePathAndArtifact = (
            ((Get-NormalizedModelPathKey -FilePath ([string]$existingEntry.file_path)) -eq $finalKey) -and
            (-not [string]::IsNullOrWhiteSpace([string]$existingEntry.measured_sha256)) -and
            ([string]$existingEntry.measured_sha256).ToLowerInvariant() -eq ([string]$insp.sha256).ToLowerInvariant() -and
            ([string]$existingEntry.catalog_model_id -eq [string]$actualCat.model_id)
        )
        if ($samePathAndArtifact -and $existingEntry.verification) {
            if ([bool]$existingEntry.verification.launch_verified) { $ver.launch_verified = $true }
            if ([bool]$existingEntry.verification.translation_verified) {
                $ver.launch_verified = $true
                $ver.translation_verified = $true
            }
        }
    } else {
        $slug = ConvertTo-SafeSlug -Text $actualCat.model_id
        $regId = "reg-$slug-{0}" -f ([guid]::NewGuid().ToString("N").Substring(0, 8))
        $createdAt = ""
    }

    $entry = New-RegisteredModelObject `
        -RegistrationId $regId `
        -CatalogModel $actualCat `
        -SourceType $finalSourceType `
        -Inspection $insp `
        -Verification $ver `
        -LicenseAccepted $LicenseAccepted `
        -CreatedAt $createdAt
    $modelsList.Add($entry)

    $reg.models = [object[]]$modelsList.ToArray()
    if ($SetActive) {
        $reg.active_model_id = $regId
    }
    Write-ModelsRegistry -UserDataDir $UserDataDir -Registry $reg

    return [pscustomobject]@{
        Registration = $entry
        CatalogModel = $actualCat
        Registry     = $reg
    }
}

function Set-PaperBridgeActiveModel {
    param(
        [string]$UserDataDir,
        [string]$ProjectRoot,
        [string]$RegistrationId
    )
    $sync = Sync-ModelsRegistryWithLegacy -UserDataDir $UserDataDir -ProjectRoot $ProjectRoot
    $reg = $sync.Registry
    $target = $null
    foreach ($m in $reg.models) {
        if ($m.registration_id -eq $RegistrationId) {
            $target = $m
            break
        }
    }
    if (-not $target) {
        throw "指定された登録IDが見つかりません: $RegistrationId"
    }
    $insp = Inspect-GgufFile -FilePath ([string]$target.file_path) -ComputeHash
    $null = Test-ModelMatchesCatalog -Catalog $sync.Catalog -Inspection $insp -ExpectedCatalogModelId ([string]$target.catalog_model_id)

    $reg.active_model_id = $RegistrationId
    Write-ModelsRegistry -UserDataDir $UserDataDir -Registry $reg
    return $target
}

function Resolve-PaperBridgeActiveModelForLaunch {
    param(
        [string]$UserDataDir,
        [string]$ProjectRoot,
        [switch]$VerifyHashOnChange = $true
    )
    $sync = Sync-ModelsRegistryWithLegacy -UserDataDir $UserDataDir -ProjectRoot $ProjectRoot
    $catalog = $sync.Catalog
    $reg = $sync.Registry

    if ([string]::IsNullOrWhiteSpace($reg.active_model_id)) {
        return [pscustomobject]@{
            Status       = "Unconfigured"
            Entry        = $null
            CatalogModel = $null
            Reason       = "アクティブな翻訳モデルが設定されていません。"
        }
    }

    $entry = $null
    foreach ($m in $reg.models) {
        if ($m.registration_id -eq $reg.active_model_id) {
            $entry = $m
            break
        }
    }
    if (-not $entry) {
        return [pscustomobject]@{
            Status       = "Unconfigured"
            Entry        = $null
            CatalogModel = $null
            Reason       = "選択されていた登録ID ($($reg.active_model_id)) が見つかりません。"
        }
    }

    $catModel = Get-CatalogModelById -Catalog $catalog -ModelId ([string]$entry.catalog_model_id)
    $filePath = [string]$entry.file_path
    if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
        return [pscustomobject]@{
            Status       = "Missing"
            Entry        = $entry
            CatalogModel = $catModel
            Reason       = "登録済みモデルファイルが見つかりません: $filePath"
        }
    }

    $fi = Get-Item -LiteralPath $filePath
    $curSize = [int64]$fi.Length
    $curMtime = Get-FileMtimeUnixNs -FileInfo $fi
    $statsChanged = ($curSize -ne [int64]$entry.file_size_bytes) -or ($curMtime -ne [int64]$entry.file_mtime_ns) -or [string]::IsNullOrWhiteSpace([string]$entry.measured_sha256)

    if (-not $statsChanged) {
        $headerOnly = Inspect-GgufFile -FilePath $filePath
        if (-not $headerOnly.format_valid -or $headerOnly.is_split_gguf) {
            return [pscustomobject]@{
                Status       = "Invalid"
                Entry        = $entry
                CatalogModel = $catModel
                Reason       = $(if ($headerOnly.error) { $headerOnly.error } else { "GGUF ファイルヘッダーが無効です。" })
            }
        }
        $arch = if ($headerOnly.architecture) { $headerOnly.architecture.Trim().ToLowerInvariant() } else { "" }
        if ($arch -ne "plamo3") {
            return [pscustomobject]@{
                Status       = "Invalid"
                Entry        = $entry
                CatalogModel = $catModel
                Reason       = "非対応アーキテクチャです: $arch"
            }
        }
        if ($entry.verification -and $entry.verification.hash_status -ne "matched_known") {
            return [pscustomobject]@{
                Status       = "Invalid"
                Entry        = $entry
                CatalogModel = $catModel
                Reason       = "登録済みモデルの SHA-256 検証が完了していません。"
            }
        }
        return [pscustomobject]@{
            Status       = "Ready"
            Entry        = $entry
            CatalogModel = $catModel
            Reason       = ""
        }
    }

    if (-not $VerifyHashOnChange) {
        return [pscustomobject]@{
            Status       = "Invalid"
            Entry        = $entry
            CatalogModel = $catModel
            Reason       = "登録時とファイルサイズまたは更新日時が異なります。再検証が必要です。"
        }
    }

    $fullInsp = Inspect-GgufFile -FilePath $filePath -ComputeHash
    try {
        $validated = Test-ModelMatchesCatalog -Catalog $catalog -Inspection $fullInsp -ExpectedCatalogModelId ([string]$entry.catalog_model_id)
        return [pscustomobject]@{
            Status       = "Ready"
            Entry        = $entry
            CatalogModel = $validated.CatalogModel
            Reason       = ""
        }
    } catch {
        return [pscustomobject]@{
            Status       = "Invalid"
            Entry        = $entry
            CatalogModel = $catModel
            Reason       = $_.Exception.Message
        }
    }
}

function Invoke-PaperBridgeModelDownload {
    param(
        $Catalog,
        $CatalogModel,
        [string]$DestPath,
        [scriptblock]$ProgressCallback = $null,
        [scriptblock]$CancelCheck = $null
    )
    $destDir = Split-Path -Parent $DestPath
    if (-not (Test-Path -LiteralPath $destDir)) {
        New-Item -ItemType Directory -Path $destDir -Force | Out-Null
    }
    $tmpDl = "$DestPath.downloading"
    if (Test-Path -LiteralPath $tmpDl) {
        Remove-Item -LiteralPath $tmpDl -Force -ErrorAction SilentlyContinue
    }

    $response = $null
    $resStream = $null
    $fs = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create([string]$CatalogModel.download_url)
        $request.Method = "GET"
        $request.Timeout = 30000
        $request.ReadWriteTimeout = 30000
        $request.AllowAutoRedirect = $true
        $request.UserAgent = "PaperBridgeLocal-Setup/2.0"

        $response = $request.GetResponse()
        $totalBytes = [int64]$response.ContentLength
        if ($totalBytes -le 0) { $totalBytes = [int64]$CatalogModel.known_size_bytes }
        $resStream = $response.GetResponseStream()
        $fs = [System.IO.File]::Create($tmpDl)

        $buffer = New-Object byte[] 131072
        $downloaded = 0L
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        while (($readBytes = $resStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            if ($CancelCheck -and (& $CancelCheck)) {
                throw [System.OperationCanceledException]::new("ユーザーによってキャンセルされました。")
            }
            $fs.Write($buffer, 0, $readBytes)
            $downloaded += $readBytes
            if ($ProgressCallback -and $sw.ElapsedMilliseconds -ge 120) {
                $pct = if ($totalBytes -gt 0) { [int][Math]::Min(100, ($downloaded * 100.0) / $totalBytes) } else { 0 }
                & $ProgressCallback $downloaded $totalBytes $pct
                $sw.Restart()
            }
        }
        $fs.Close(); $fs = $null
        $resStream.Close(); $resStream = $null
        $response.Close(); $response = $null

        if ($CancelCheck -and (& $CancelCheck)) {
            throw [System.OperationCanceledException]::new("ユーザーによってキャンセルされました。")
        }

        $insp = Inspect-GgufFile -FilePath $tmpDl -ComputeHash
        $null = Test-ModelMatchesCatalog -Catalog $Catalog -Inspection $insp -ExpectedCatalogModelId ([string]$CatalogModel.model_id)

        if (Test-Path -LiteralPath $DestPath) {
            Remove-Item -LiteralPath $DestPath -Force
        }
        Move-Item -LiteralPath $tmpDl -Destination $DestPath -Force
        return $DestPath
    } finally {
        if ($fs) { try { $fs.Dispose() } catch {} }
        if ($resStream) { try { $resStream.Dispose() } catch {} }
        if ($response) { try { $response.Close() } catch {} }
        if (Test-Path -LiteralPath $tmpDl) {
            Remove-Item -LiteralPath $tmpDl -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-PaperBridgeLlamaLaunchArgs {
    param(
        [string]$ModelPath,
        $CatalogModel = $null,
        [int]$LlamaPort = 8000,
        [switch]$UseCpu = $false
    )
    $alias = "plamo-3-translate"
    $contextLen = 8192
    $parallelSlots = 1
    $gpuLayers = 99
    $extraArgs = @("--jinja", "--no-prefill-assistant")

    if ($CatalogModel) {
        $ls = $CatalogModel.launch_settings
        if ($ls) {
            if (-not [string]::IsNullOrWhiteSpace([string]$ls.alias)) { $alias = [string]$ls.alias }
            elseif (-not [string]::IsNullOrWhiteSpace([string]$CatalogModel.model_id)) { $alias = [string]$CatalogModel.model_id }
            if ($ls.context_length -and [int]$ls.context_length -gt 0) { $contextLen = [int]$ls.context_length }
            if ($ls.parallel_slots -and [int]$ls.parallel_slots -gt 0) { $parallelSlots = [int]$ls.parallel_slots }
            if ($null -ne $ls.gpu_layers_auto) { $gpuLayers = [int]$ls.gpu_layers_auto }
            if ($ls.extra_args) { $extraArgs = @($ls.extra_args | ForEach-Object { [string]$_ }) }
        } elseif (-not [string]::IsNullOrWhiteSpace([string]$CatalogModel.model_id)) {
            $alias = [string]$CatalogModel.model_id
        }
    }

    $nglVal = if ($UseCpu) { "0" } else { "$gpuLayers" }
    $argList = New-Object System.Collections.Generic.List[string]
    $argList.Add("--model"); $argList.Add($ModelPath)
    $argList.Add("--alias"); $argList.Add($alias)
    $argList.Add("--host"); $argList.Add("127.0.0.1")
    $argList.Add("--port"); $argList.Add("$LlamaPort")
    $argList.Add("-c"); $argList.Add("$contextLen")
    $argList.Add("-np"); $argList.Add("$parallelSlots")
    $argList.Add("-ngl"); $argList.Add($nglVal)
    foreach ($ea in $extraArgs) {
        if (-not [string]::IsNullOrWhiteSpace($ea)) {
            $argList.Add($ea)
        }
    }
    $argList.Add("--cors-origins"); $argList.Add("localhost")
    return [string[]]$argList.ToArray()
}

