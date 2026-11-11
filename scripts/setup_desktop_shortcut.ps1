# PaperBridge Local - Desktop Shortcut Creator
param(
    [string]$ShortcutName = "",
    [switch]$CpuMode = $false,
    [switch]$Silent = $false,
    [string]$DesktopDir = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$VbsPath = Join-Path $ScriptDir "run_silent.vbs"
$RuntimeProfilePath = Join-Path $ProjectRoot ".runtime_tools\runtime_profile.json"

if ([string]::IsNullOrWhiteSpace($ShortcutName)) {
    if (Test-Path -LiteralPath $RuntimeProfilePath) {
        try {
            $prof = Get-Content -LiteralPath $RuntimeProfilePath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($prof -and -not [string]::IsNullOrWhiteSpace($prof.ShortcutName)) {
                $ShortcutName = [string]$prof.ShortcutName
            }
        } catch {}
    }
    if ([string]::IsNullOrWhiteSpace($ShortcutName)) {
        $ShortcutName = "PaperBridge Local"
    }
}

if (-not (Test-Path -LiteralPath $VbsPath)) {
    throw "ランチャーVBSが見つかりません: $VbsPath"
}

if ([string]::IsNullOrWhiteSpace($DesktopDir)) {
    $DesktopDir = [Environment]::GetFolderPath("Desktop")
}
if (-not (Test-Path -LiteralPath $DesktopDir)) {
    New-Item -ItemType Directory -Path $DesktopDir -Force | Out-Null
}

$shortcutFileName = if ($CpuMode) { "$ShortcutName (CPUモード).lnk" } else { "$ShortcutName.lnk" }
$shortcutPath = Join-Path $DesktopDir $shortcutFileName

$wscriptExe = Join-Path $env:WINDIR "System32\wscript.exe"
$argStr = "//nologo `"$VbsPath`""
if ($CpuMode) {
    $argStr += " -UseCpuInference"
}

$iconLocation = "%SystemRoot%\System32\shell32.dll,21"
$edgeCandidates = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe"
)
foreach ($edge in $edgeCandidates) {
    if (-not [string]::IsNullOrWhiteSpace($edge) -and (Test-Path -LiteralPath $edge)) {
        $iconLocation = "$edge,0"
        break
    }
}

$wsh = $null
$shortcut = $null
try {
    $wsh = New-Object -ComObject WScript.Shell
    $shortcut = $wsh.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $wscriptExe
    $shortcut.Arguments = $argStr
    $shortcut.WorkingDirectory = $ProjectRoot
    $shortcut.IconLocation = $iconLocation
    $shortcut.Description = "PaperBridge Local - 学術PDFローカル翻訳ワークベンチ"
    $shortcut.Save()
    Write-Output "SHORTCUT_CREATED:$shortcutPath"
    if (-not $Silent) {
        [System.Windows.Forms.MessageBox]::Show("デスクトップにショートカットを作成しました:`n$shortcutPath", "ショートカット作成完了", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
    }
} finally {
    if ($shortcut) { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($shortcut) | Out-Null }
    if ($wsh) { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($wsh) | Out-Null }
}

