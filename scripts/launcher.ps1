# PaperBridge Local - Runtime Launcher & Process Lifecycle Manager
param(
    [switch]$UseCpuInference = $false,
    [int]$BackendPort = 8080,
    [int]$LlamaPort = 8000,
    [string]$UserDataDir = "",
    [switch]$NoBrowser = $false,
    [switch]$ConsoleDiagnostic = $false,
    [string]$ShutdownSignalFile = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$FrontendDistIndex = Join-Path $ProjectRoot "frontend\dist\index.html"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$VulkanServerExe = Join-Path $ProjectRoot "tools\llama.cpp\b12167-vulkan-x64\llama-server.exe"
$CpuServerExe = Join-Path $ProjectRoot "tools\llama.cpp\b12167-cpu-x64\llama-server.exe"
$ModelFileName = "plamo-3-translate-2b-Q4_K_M.gguf"
$ExpectedModelSize = 1807199392L

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
        throw "LOCALAPPDATA 環境変数が取得できませんでした。"
    }
}

$LogsDir = Join-Path $UserDataDir "logs"
$LauncherLogPath = Join-Path $LogsDir "launcher.log"
$EdgeProfileDir = Join-Path $UserDataDir "edge_profile"
$UserModelPath = Join-Path $UserDataDir "models\plamo-3-translate-2b-GGUF\$ModelFileName"
$RepoModelPath = Join-Path $ProjectRoot "models\plamo-3-translate-2b-GGUF\$ModelFileName"

foreach ($d in @($UserDataDir, $LogsDir, $EdgeProfileDir)) {
    if (-not (Test-Path -LiteralPath $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
}

function Write-LaunchLog {
    param([string]$Message, [string]$Level = "INFO")
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$ts] [$Level] $Message"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $sw = New-Object System.IO.StreamWriter($LauncherLogPath, $true, $utf8NoBom)
    try {
        $sw.WriteLine($line)
    } finally {
        $sw.Close()
    }
    if ($ConsoleDiagnostic -or $NoBrowser) {
        Write-Host $line
    }
}

function Show-UserError {
    param([string]$Title, [string]$Message)
    Write-LaunchLog "$($Title): $Message" "ERROR"
    if (-not $NoBrowser) {
        [System.Windows.Forms.MessageBox]::Show($Message, $Title, [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    } else {
        Write-Error "$($Title): $Message"
    }
}

# Add Win32 Process Group & StdIO Redirection Helper for Graceful Shutdown
Add-Type -TypeDefinition @"
using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class PaperBridgeWin32 {
    public const uint CREATE_NEW_PROCESS_GROUP = 0x00000200;
    public const uint CREATE_NO_WINDOW = 0x08000000;
    public const uint STARTF_USESHOWWINDOW = 0x00000001;
    public const uint STARTF_USESTDHANDLES = 0x00000100;
    public const short SW_HIDE = 0;
    public const int CTRL_BREAK_EVENT = 1;
    public const uint HANDLE_FLAG_INHERIT = 0x00000001;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct STARTUPINFO {
        public uint cb;
        public string lpReserved;
        public string lpDesktop;
        public string lpTitle;
        public uint dwX;
        public uint dwY;
        public uint dwXSize;
        public uint dwYSize;
        public uint dwXCountChars;
        public uint dwYCountChars;
        public uint dwFillAttribute;
        public uint dwFlags;
        public short wShowWindow;
        public short cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PROCESS_INFORMATION {
        public IntPtr hProcess;
        public IntPtr hThread;
        public uint dwProcessId;
        public uint dwThreadId;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct SECURITY_ATTRIBUTES {
        public uint nLength;
        public IntPtr lpSecurityDescriptor;
        public bool bInheritHandle;
    }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool CreateProcessW(
        string lpApplicationName,
        string lpCommandLine,
        IntPtr lpProcessAttributes,
        IntPtr lpThreadAttributes,
        bool bInheritHandles,
        uint dwCreationFlags,
        IntPtr lpEnvironment,
        string lpCurrentDirectory,
        ref STARTUPINFO lpStartupInfo,
        out PROCESS_INFORMATION lpProcessInformation
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetHandleInformation(IntPtr hObject, uint dwMask, uint dwFlags);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr hObject);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GenerateConsoleCtrlEvent(uint dwCtrlEvent, uint dwProcessGroupId);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr GetStdHandle(int nStdHandle);

    public static int StartProcessNewGroup(string exePath, string args, string workingDir, string outLogPath, string errLogPath) {
        SECURITY_ATTRIBUTES sa = new SECURITY_ATTRIBUTES();
        sa.nLength = (uint)Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
        sa.bInheritHandle = true;
        sa.lpSecurityDescriptor = IntPtr.Zero;

        using (FileStream outFs = new FileStream(outLogPath, FileMode.Create, FileAccess.Write, FileShare.ReadWrite))
        using (FileStream errFs = new FileStream(errLogPath, FileMode.Create, FileAccess.Write, FileShare.ReadWrite)) {
            SafeFileHandle outHandle = outFs.SafeFileHandle;
            SafeFileHandle errHandle = errFs.SafeFileHandle;
            SetHandleInformation(outHandle.DangerousGetHandle(), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
            SetHandleInformation(errHandle.DangerousGetHandle(), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);

            STARTUPINFO si = new STARTUPINFO();
            si.cb = (uint)Marshal.SizeOf(typeof(STARTUPINFO));
            si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
            si.wShowWindow = SW_HIDE;
            si.hStdInput = GetStdHandle(-10);
            si.hStdOutput = outHandle.DangerousGetHandle();
            si.hStdError = errHandle.DangerousGetHandle();

            PROCESS_INFORMATION pi = new PROCESS_INFORMATION();
            string cmdLine = "\"" + exePath + "\"";
            if (!string.IsNullOrEmpty(args)) {
                cmdLine += " " + args;
            }

            uint flags = CREATE_NEW_PROCESS_GROUP;
            bool ok = CreateProcessW(
                exePath,
                cmdLine,
                IntPtr.Zero,
                IntPtr.Zero,
                true,
                flags,
                IntPtr.Zero,
                workingDir,
                ref si,
                out pi
            );

            if (!ok) {
                int err = Marshal.GetLastWin32Error();
                throw new System.ComponentModel.Win32Exception(err, "CreateProcessW failed for " + exePath);
            }

            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
            return (int)pi.dwProcessId;
        }
    }
}
"@

function Format-QuotedArgs {
    param([string[]]$ArgList)
    $parts = foreach ($a in $ArgList) {
        if ($a -match '[\s"]') {
            $esc = $a -replace '"', '\"'
            '"' + $esc + '"'
        } else {
            $a
        }
    }
    return ($parts -join " ")
}

function Start-TrackedServer {
    param(
        [string]$Label,
        [string]$ExePath,
        [string[]]$ArgList,
        [string]$WorkingDir,
        [string]$OutLog,
        [string]$ErrLog
    )
    $argStr = Format-QuotedArgs -ArgList $ArgList
    Write-LaunchLog "起動 [$Label]: `"$ExePath`" $argStr"
    $newPid = [PaperBridgeWin32]::StartProcessNewGroup($ExePath, $argStr, $WorkingDir, $OutLog, $ErrLog)
    $proc = [System.Diagnostics.Process]::GetProcessById($newPid)
    $null = $proc.Handle
    $startTime = $proc.StartTime
    $normPath = [System.IO.Path]::GetFullPath($ExePath).ToLowerInvariant()
    Write-LaunchLog "起動済 [$Label]: PID=$newPid, StartTime=$($startTime.ToString('o'))"
    return @{
        Label     = $Label
        Pid       = $newPid
        StartTime = $startTime
        ExePath   = $normPath
        Process   = $proc
        ErrLog    = $ErrLog
        OutLog    = $OutLog
    }
}

function Stop-TrackedServer {
    param([hashtable]$ServerInfo, [int]$GracefulTimeoutMs = 5000)
    if (-not $ServerInfo) { return }
    $procId = $ServerInfo.Pid
    $label = $ServerInfo.Label
    $proc = $null
    try {
        $proc = [System.Diagnostics.Process]::GetProcessById($procId)
    } catch {
        Write-LaunchLog "終了確認 [$label]: PID=$procId は既に終了しています。"
        return
    }

    if ($proc.HasExited) {
        Write-LaunchLog "終了確認 [$label]: PID=$procId は既に終了済み (ExitCode=$($proc.ExitCode))。"
        return
    }

    # Re-verify ownership strictly before sending signals
    try {
        $actualStart = $proc.StartTime
        if ([Math]::Abs(($actualStart - $ServerInfo.StartTime).TotalSeconds) -gt 2) {
            Write-LaunchLog "安全性保護 [$label]: PID=$procId の StartTime が一致しないため操作を中止します。" "WARN"
            return
        }
        $actualPath = $proc.MainModule.FileName
        if (-not [string]::IsNullOrWhiteSpace($actualPath)) {
            $normActual = [System.IO.Path]::GetFullPath($actualPath).ToLowerInvariant()
            if ($normActual -ne $ServerInfo.ExePath) {
                Write-LaunchLog "安全性保護 [$label]: PID=$procId のパス ($normActual) が一致しないため操作を中止します。" "WARN"
                return
            }
        }
    } catch {
        Write-LaunchLog "プロセス照合中にエラーが発生しました [$label]: $($_.Exception.Message)" "WARN"
        return
    }

    Write-LaunchLog "通常終了を要求中 [$label] (CTRL_BREAK_EVENT -> PID=$procId)..."
    $sent = [PaperBridgeWin32]::GenerateConsoleCtrlEvent([PaperBridgeWin32]::CTRL_BREAK_EVENT, [uint32]$procId)
    if ($sent) {
        if ($proc.WaitForExit($GracefulTimeoutMs)) {
            $proc.WaitForExit()
            Write-LaunchLog "通常終了完了 [$label]: PID=$procId (ExitCode=$($proc.ExitCode))"
            return
        }
        Write-LaunchLog "タイムアウト [$label]: CTRL_BREAK_EVENT 後 $($GracefulTimeoutMs)ms 以内に終了しませんでした。" "WARN"
    } else {
        $w32Err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
        Write-LaunchLog "CTRL_BREAK_EVENT 送信失敗 [$label] (Win32Error=$w32Err)。" "WARN"
    }

    # Ownership re-check right before fallback force termination
    try {
        $recheck = [System.Diagnostics.Process]::GetProcessById($procId)
        if (-not $recheck.HasExited -and ([Math]::Abs(($recheck.StartTime - $ServerInfo.StartTime).TotalSeconds) -le 2)) {
            Write-LaunchLog "フォールバック強制終了を実行 [$label]: taskkill /PID $procId /T /F" "WARN"
            & taskkill.exe /PID $procId /T /F | Out-Null
            $recheck.WaitForExit(3000) | Out-Null
        }
    } catch {
        Write-LaunchLog "フォールバック終了確認中に例外 [$label]: $($_.Exception.Message)" "WARN"
    }
}

function Test-PortInUse {
    param([int]$Port)
    $ipProps = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()
    foreach ($ep in $ipProps.GetActiveTcpListeners()) {
        if ($ep.Port -eq $Port) {
            return $true
        }
    }
    return $false
}

function Test-HttpEndpointReady {
    param([string]$Url)
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = "GET"
        $req.Timeout = 1500
        $res = $req.GetResponse()
        $code = [int]$res.StatusCode
        $res.Close()
        return ($code -ge 200 -and $code -lt 300)
    } catch {
        return $false
    }
}

function Wait-EndpointOrCrash {
    param(
        [hashtable]$ServerInfo,
        [string]$HealthUrl,
        [int]$TimeoutSeconds = 45
    )
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if ($ServerInfo.Process.HasExited) {
            $errTail = ""
            if (Test-Path -LiteralPath $ServerInfo.ErrLog) {
                $errTail = (Get-Content -LiteralPath $ServerInfo.ErrLog -Tail 15 -ErrorAction SilentlyContinue) -join "`n"
            }
            return @{ Ready = $false; Exited = $true; ExitCode = $ServerInfo.Process.ExitCode; ErrTail = $errTail }
        }
        if (Test-HttpEndpointReady -Url $HealthUrl) {
            return @{ Ready = $true; Exited = $false; ExitCode = $null; ErrTail = "" }
        }
        Start-Sleep -Milliseconds 350
    }
    $errTailTimeout = ""
    if (Test-Path -LiteralPath $ServerInfo.ErrLog) {
        $errTailTimeout = (Get-Content -LiteralPath $ServerInfo.ErrLog -Tail 15 -ErrorAction SilentlyContinue) -join "`n"
    }
    return @{ Ready = $false; Exited = $false; ExitCode = $null; ErrTail = $errTailTimeout }
}

function Test-ValidModelQuick {
    param([string]$ModelPath)
    if (-not (Test-Path -LiteralPath $ModelPath)) { return $false }
    $info = Get-Item -LiteralPath $ModelPath
    if ($info.Length -ne $ExpectedModelSize) { return $false }
    $fs = $null
    try {
        $fs = [System.IO.File]::OpenRead($ModelPath)
        $buf = New-Object byte[] 4
        $r = $fs.Read($buf, 0, 4)
        return ($r -eq 4 -and $buf[0] -eq 0x47 -and $buf[1] -eq 0x47 -and $buf[2] -eq 0x55 -and $buf[3] -eq 0x46)
    } catch {
        return $false
    } finally {
        if ($fs) { $fs.Close() }
    }
}

function Find-EdgeExecutable {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe"
    )
    foreach ($c in $candidates) {
        if (-not [string]::IsNullOrWhiteSpace($c) -and (Test-Path -LiteralPath $c)) {
            return $c
        }
    }
    try {
        $reg = Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe" -ErrorAction SilentlyContinue
        if ($reg -and $reg.'(default)' -and (Test-Path -LiteralPath $reg.'(default)')) {
            return $reg.'(default)'
        }
    } catch {}
    return $null
}

function Get-RunningProfileEdgeProcesses {
    param([string]$ProfilePath)
    try {
        $procs = Get-CimInstance -ClassName Win32_Process -Filter "Name = 'msedge.exe'" -ErrorAction SilentlyContinue
        $matched = @()
        foreach ($p in $procs) {
            if ($p.CommandLine -and ($p.CommandLine.IndexOf($ProfilePath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)) {
                $matched += $p
            }
        }
        return $matched
    } catch {
        return @()
    }
}

function Open-AppWindowAndWait {
    param([string]$AppUrl)
    $edgeExe = Find-EdgeExecutable
    if ($edgeExe) {
        $edgeArgs = "--user-data-dir=`"$EdgeProfileDir`" --app=`"$AppUrl`" --start-maximized --no-first-run --no-default-browser-check"
        Write-LaunchLog "専用ウィンドウ起動: `"$edgeExe`" $edgeArgs"
        $edgeProc = Start-Process -FilePath $edgeExe -ArgumentList $edgeArgs -PassThru
        Start-Sleep -Milliseconds 1200

        while ($true) {
            if (-not $edgeProc.HasExited) {
                Start-Sleep -Milliseconds 600
                continue
            }
            $profileProcs = Get-RunningProfileEdgeProcesses -ProfilePath $EdgeProfileDir
            if ($profileProcs.Count -gt 0) {
                Start-Sleep -Milliseconds 700
                continue
            }
            break
        }
        Write-LaunchLog "専用 Edge ウィンドウが閉じられました。"
    } else {
        Write-LaunchLog "Microsoft Edge が見つからないため、既定ブラウザで開きます: $AppUrl" "WARN"
        Start-Process $AppUrl
        [System.Windows.Forms.MessageBox]::Show("ブラウザで PaperBridge Local を開きました ($AppUrl)。`n`n利用を終えてサーバーを停止するときは「OK」を押してください。", "PaperBridge Local 実行中", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
    }
}

# --- Single Instance Guard ---
$sha = [System.Security.Cryptography.SHA256]::Create()
$dirBytes = [System.Text.Encoding]::UTF8.GetBytes($UserDataDir.ToLowerInvariant())
$hashBytes = $sha.ComputeHash($dirBytes)
$sha.Dispose()
$shortId = ([BitConverter]::ToString($hashBytes) -replace "-", "").Substring(0, 12)
$mutexName = "Local\PaperBridgeLocal_$shortId"

$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) {
    Write-LaunchLog "既存インスタンスを検出しました (Mutex=$mutexName)。"
    $appUrl = "http://127.0.0.1:$($BackendPort)/"
    if (-not $NoBrowser -and (Test-HttpEndpointReady -Url "http://127.0.0.1:$($BackendPort)/api/auth/session")) {
        Write-LaunchLog "既に起動中のバックエンドへ画面を再表示します: $appUrl"
        $edgeExe = Find-EdgeExecutable
        if ($edgeExe) {
            $edgeArgs = "--user-data-dir=`"$EdgeProfileDir`" --app=`"$appUrl`" --start-maximized"
            Start-Process -FilePath $edgeExe -ArgumentList $edgeArgs | Out-Null
        } else {
            Start-Process $appUrl
        }
        $mutex.Dispose()
        exit 0
    }
    Show-UserError -Title "多重起動の防止" -Message "PaperBridge Local は既にこのユーザーデータ領域 ($UserDataDir) で起動中、または起動・終了処理の途中です。"
    $mutex.Dispose()
    exit 1
}

$llamaServer = $null
$backendServer = $null
$savedEnv = @{
    PAPERBRIDGE_RUNTIME_DIR   = $env:PAPERBRIDGE_RUNTIME_DIR
    PAPERBRIDGE_HOST          = $env:PAPERBRIDGE_HOST
    PAPERBRIDGE_PORT          = $env:PAPERBRIDGE_PORT
    PAPERBRIDGE_LLAMA_BASE_URL= $env:PAPERBRIDGE_LLAMA_BASE_URL
    PYTHONUTF8                = $env:PYTHONUTF8
    PYTHONIOENCODING          = $env:PYTHONIOENCODING
}

try {
    Write-LaunchLog "=== PaperBridge Local 起動開始 (ProjectRoot=$ProjectRoot, UserDataDir=$UserDataDir, UseCpuInference=$UseCpuInference) ==="

    # 1. Verify setup completion
    $missingItems = @()
    if (-not (Test-Path -LiteralPath $VenvPython)) { $missingItems += ".venv (Python実行環境)" }
    if (-not (Test-Path -LiteralPath $FrontendDistIndex)) { $missingItems += "frontend\dist (ビルド済みUI)" }
    if (-not (Test-Path -LiteralPath $VulkanServerExe) -and -not (Test-Path -LiteralPath $CpuServerExe)) {
        $missingItems += "llama.cpp (推論サーバー)"
    }
    if ($missingItems.Count -gt 0) {
        $missingText = $missingItems -join ", "
        Write-LaunchLog "初回セットアップ未完了項目を検出: $missingText" "WARN"
        if (-not $NoBrowser) {
            $ans = [System.Windows.Forms.MessageBox]::Show("初回セットアップが完了していません（不足: $missingText）。`n`n初回自動セットアップを開きますか？", "PaperBridge Local - 初回セットアップが必要です", [System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Question)
            if ($ans -eq [System.Windows.Forms.DialogResult]::Yes) {
                $setupScript = Join-Path $ScriptDir "setup_wizard.ps1"
                $pSetup = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$setupScript`"", "-UserDataDir", "`"$UserDataDir`"") -Wait -PassThru
                if ($pSetup.ExitCode -ne 0) {
                    Write-LaunchLog "呼び出した初回セットアップが終了コード $($pSetup.ExitCode) で終了したため起動を中止します。"
                    exit 1
                }
            } else {
                exit 1
            }
        } else {
            throw "環境が未構築です: $missingText"
        }
    }

    # 2. Verify Model Presence
    $resolvedModelPath = $null
    if (Test-ValidModelQuick -ModelPath $RepoModelPath) {
        $resolvedModelPath = $RepoModelPath
    } elseif (Test-ValidModelQuick -ModelPath $UserModelPath) {
        $resolvedModelPath = $UserModelPath
    }

    if (-not $resolvedModelPath) {
        Write-LaunchLog "検証済み翻訳モデルが見つかりません (確認先: $RepoModelPath / $UserModelPath)" "WARN"
        if (-not $NoBrowser) {
            $modelSetupScript = Join-Path $ScriptDir "model_setup.ps1"
            $userModelDir = Split-Path -Parent $UserModelPath
            $pModel = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$modelSetupScript`"", "-TargetModelDir", "`"$userModelDir`"") -Wait -PassThru
            if ($pModel.ExitCode -ne 0 -or -not (Test-ValidModelQuick -ModelPath $UserModelPath)) {
                Show-UserError -Title "翻訳モデル未設定" -Message "有効な PLaMo 3 Translate 2B モデルが配置されなかったため起動を中止します。"
                exit 1
            }
            $resolvedModelPath = $UserModelPath
        } else {
            throw "有効な翻訳モデルが見つかりません: $UserModelPath"
        }
    }
    Write-LaunchLog "使用モデル確定: $resolvedModelPath"

    # 3. Check Port Availability (Never kill external unknown processes)
    if (Test-PortInUse -Port $LlamaPort) {
        Show-UserError -Title "ポート競合エラー" -Message "推論サーバー用ポート $LlamaPort は既に他のプロセスが使用中です。競合するプログラムを終了してから再試行してください。"
        exit 1
    }
    if (Test-PortInUse -Port $BackendPort) {
        Show-UserError -Title "ポート競合エラー" -Message "バックエンド用ポート $BackendPort は既に他のプロセスが使用中です。競合するプログラムを終了してから再試行してください。"
        exit 1
    }

    # 4. Launch llama-server (with automatic CPU fallback if Vulkan fails)
    $llamaOut = Join-Path $LogsDir "llama-server.out.log"
    $llamaErr = Join-Path $LogsDir "llama-server.err.log"
    $llamaHealthUrl = "http://127.0.0.1:$($LlamaPort)/health"
    $activeModeLabel = ""

    if (-not $UseCpuInference -and (Test-Path -LiteralPath $VulkanServerExe)) {
        $vulkanArgs = @(
            "--model", $resolvedModelPath,
            "--alias", "plamo-3-translate",
            "--host", "127.0.0.1",
            "--port", "$LlamaPort",
            "-c", "8192",
            "-np", "1",
            "-ngl", "99",
            "--jinja",
            "--no-prefill-assistant",
            "--cors-origins", "localhost"
        )
        $llamaServer = Start-TrackedServer -Label "llama-server (Vulkan)" -ExePath $VulkanServerExe -ArgList $vulkanArgs -WorkingDir (Split-Path -Parent $VulkanServerExe) -OutLog $llamaOut -ErrLog $llamaErr
        $waitLlama = Wait-EndpointOrCrash -ServerInfo $llamaServer -HealthUrl $llamaHealthUrl -TimeoutSeconds 45
        if (-not $waitLlama.Ready) {
            Write-LaunchLog "Vulkan 推論サーバーの起動に失敗しました (Exited=$($waitLlama.Exited), Code=$($waitLlama.ExitCode))。ログ末尾:`n$($waitLlama.ErrTail)" "WARN"
            Stop-TrackedServer -ServerInfo $llamaServer
            $llamaServer = $null
            if (Test-Path -LiteralPath $CpuServerExe) {
                Write-LaunchLog "CPU専用推論バイナリ (b12167-cpu-x64, -ngl 0) へ自動フォールバックします..." "WARN"
            } else {
                Show-UserError -Title "推論サーバー起動エラー" -Message "Vulkan 推論サーバーを起動できず、CPU用バイナリも見つかりませんでした。`n`nログ: $llamaErr"
                exit 1
            }
        } else {
            $activeModeLabel = "Vulkan GPU (b12167-vulkan-x64, -ngl 99)"
        }
    }

    if (-not $llamaServer) {
        if (-not (Test-Path -LiteralPath $CpuServerExe)) {
            Show-UserError -Title "推論サーバー起動エラー" -Message "CPU推論バイナリが見つかりません: $CpuServerExe"
            exit 1
        }
        $cpuArgs = @(
            "--model", $resolvedModelPath,
            "--alias", "plamo-3-translate",
            "--host", "127.0.0.1",
            "--port", "$LlamaPort",
            "-c", "8192",
            "-np", "1",
            "-ngl", "0",
            "--jinja",
            "--no-prefill-assistant",
            "--cors-origins", "localhost"
        )
        $llamaServer = Start-TrackedServer -Label "llama-server (CPU)" -ExePath $CpuServerExe -ArgList $cpuArgs -WorkingDir (Split-Path -Parent $CpuServerExe) -OutLog $llamaOut -ErrLog $llamaErr
        $waitCpu = Wait-EndpointOrCrash -ServerInfo $llamaServer -HealthUrl $llamaHealthUrl -TimeoutSeconds 60
        if (-not $waitCpu.Ready) {
            Show-UserError -Title "推論サーバー起動エラー" -Message "CPU 推論サーバーが起動しませんでした。`n`nログ末尾:`n$($waitCpu.ErrTail)`n`n詳細: $llamaErr"
            exit 1
        }
        $activeModeLabel = "CPU専用 (b12167-cpu-x64, -ngl 0)"
    }
    Write-LaunchLog "推論サーバー準備完了: $activeModeLabel"

    # 5. Launch Backend (uvicorn) with isolated runtime environment
    $env:PAPERBRIDGE_RUNTIME_DIR = $UserDataDir
    $env:PAPERBRIDGE_HOST = "127.0.0.1"
    $env:PAPERBRIDGE_PORT = "$BackendPort"
    $env:PAPERBRIDGE_LLAMA_BASE_URL = "http://127.0.0.1:$LlamaPort/v1"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"

    $backendOut = Join-Path $LogsDir "backend.out.log"
    $backendErr = Join-Path $LogsDir "backend.err.log"
    $backendArgs = @("-m", "uvicorn", "backend.api.app:app", "--host", "127.0.0.1", "--port", "$BackendPort")
    $backendServer = Start-TrackedServer -Label "backend (uvicorn)" -ExePath $VenvPython -ArgList $backendArgs -WorkingDir $ProjectRoot -OutLog $backendOut -ErrLog $backendErr

    $backendHealthUrl = "http://127.0.0.1:$($BackendPort)/api/auth/session"
    $waitBackend = Wait-EndpointOrCrash -ServerInfo $backendServer -HealthUrl $backendHealthUrl -TimeoutSeconds 45
    if (-not $waitBackend.Ready) {
        Show-UserError -Title "バックエンド起動エラー" -Message "バックエンドサーバーが起動しませんでした。`n`nログ末尾:`n$($waitBackend.ErrTail)`n`n詳細: $backendErr"
        exit 1
    }
    Write-LaunchLog "バックエンド準備完了: http://127.0.0.1:$BackendPort/ ($activeModeLabel)"

    # 6. Interactive UI Loop with Active Job Guard
    $appUrl = "http://127.0.0.1:$($BackendPort)/"
    if ($NoBrowser) {
        Write-LaunchLog "NoBrowser モードで準備完了しました。"
        Write-Output "READY_BACKEND_URL=$appUrl"
        Write-Output "READY_LLAMA_URL=http://127.0.0.1:$LlamaPort"
        if (-not [string]::IsNullOrWhiteSpace($ShutdownSignalFile)) {
            Write-LaunchLog "停止シグナルファイルを監視中: $ShutdownSignalFile"
            while (-not (Test-Path -LiteralPath $ShutdownSignalFile)) {
                if ($backendServer.Process.HasExited -or $llamaServer.Process.HasExited) {
                    Write-LaunchLog "バックエンドまたは推論サーバーが予期せず終了しました。" "WARN"
                    break
                }
                Start-Sleep -Milliseconds 250
            }
        } else {
            Write-Host "`nEnter キーを押すとサーバーを通常終了します..." -ForegroundColor Cyan
            [Console]::ReadLine() | Out-Null
        }
    } else {
        $checkJobsScript = Join-Path $ScriptDir "check_active_jobs.py"
        while ($true) {
            Open-AppWindowAndWait -AppUrl $appUrl

            $env:PAPERBRIDGE_RUNTIME_DIR = $UserDataDir
            $jobOut = ""
            $jobExit = 3
            try {
                $jobOut = (& $VenvPython $checkJobsScript 2>&1) -join "`n"
                $jobExit = $LASTEXITCODE
            } catch {
                $jobOut = $_.Exception.Message
                $jobExit = 3
            }
            Write-LaunchLog "未完了ジョブ判定 (exit=$jobExit): $jobOut"

            if ($jobExit -eq 0 -or $jobExit -eq 2) {
                # 0 = active jobs none, 2 = DB not created yet
                break
            } elseif ($jobExit -eq 1) {
                $msg = "現在、未完了または確認待ちのジョブがあります:`n`n$jobOut`n`nウィンドウを再表示して作業を続けますか？`n（「いいえ」を選ぶとサーバーを停止して終了します）"
                $reopen = [System.Windows.Forms.MessageBox]::Show($msg, "進行中ジョブの確認", [System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Warning)
                if ($reopen -eq [System.Windows.Forms.DialogResult]::Yes) {
                    continue
                }
                break
            } else {
                $msg = "進行中ジョブのDB確認に失敗したため、状態を確定できませんでした (exit=$jobExit):`n`n$jobOut`n`n念のためウィンドウを再表示しますか？`n（「いいえ」を選ぶとサーバーを停止して終了します）"
                $reopenErr = [System.Windows.Forms.MessageBox]::Show($msg, "ジョブ確認エラー", [System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Warning)
                if ($reopenErr -eq [System.Windows.Forms.DialogResult]::Yes) {
                    continue
                }
                break
            }
        }
    }
    exit 0
} finally {
    Write-LaunchLog "終了処理を開始します..."
    try { Stop-TrackedServer -ServerInfo $backendServer -GracefulTimeoutMs 6000 } catch { Write-LaunchLog "バックエンド終了中に例外: $($_.Exception.Message)" "WARN" }
    try { Stop-TrackedServer -ServerInfo $llamaServer -GracefulTimeoutMs 6000 } catch { Write-LaunchLog "推論サーバー終了中に例外: $($_.Exception.Message)" "WARN" }

    foreach ($kv in $savedEnv.GetEnumerator()) {
        if ($null -eq $kv.Value) {
            Remove-Item -Path "Env:$($kv.Key)" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$($kv.Key)" -Value $kv.Value
        }
    }
    if ($mutex) {
        try { $mutex.ReleaseMutex() } catch {}
        $mutex.Dispose()
    }
    Write-LaunchLog "=== PaperBridge Local 終了 ==="
}
