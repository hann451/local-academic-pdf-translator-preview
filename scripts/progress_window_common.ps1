# PaperBridge Local - Shared WinForms Progress Window & Progress Telemetry Helpers
# Dot-sourced by scripts/apply_update.ps1 and scripts/launcher.ps1.
# Runs on Windows PowerShell 5.1+ without external dependencies.

function Write-PaperBridgeProgressFile {
    param(
        [string]$FilePath,
        [int]$Percent,
        [string]$Stage,
        [string]$Status,
        [string]$Message,
        [string]$Detail = "",
        [string]$ErrorMessage = "",
        [string]$LogPath = ""
    )
    if ([string]::IsNullOrWhiteSpace($FilePath)) { return }
    try {
        $dir = Split-Path -Parent $FilePath
        if (-not [string]::IsNullOrWhiteSpace($dir) -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        $clamped = [Math]::Max(0, [Math]::Min(100, $Percent))
        $obj = [ordered]@{
            percent    = $clamped
            stage      = [string]$Stage
            status     = [string]$Status
            message    = [string]$Message
            detail     = [string]$Detail
            error      = [string]$ErrorMessage
            log_path   = [string]$LogPath
            updated_at = ([DateTime]::UtcNow.ToString("o"))
        }
        $json = $obj | ConvertTo-Json -Depth 4
        $tmp = "$FilePath." + ([guid]::NewGuid().ToString("N").Substring(0, 8)) + ".tmp"
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($tmp, $json, $utf8NoBom)
        Move-Item -LiteralPath $tmp -Destination $FilePath -Force
    } catch {
        # Best-effort telemetry; never abort launcher/updater on file contention
    }
}

function Start-PaperBridgeProgressWindow {
    param(
        [string]$Title = "PaperBridge Local",
        [string]$HeaderText = "処理を実行しています",
        [string]$InitialMessage = "準備しています...",
        [string]$InitialDetail = "",
        [int]$InitialPercent = 0,
        [string]$LogPath = "",
        [bool]$TopMost = $true
    )

    $clampedInit = [Math]::Max(0, [Math]::Min(100, $InitialPercent))
    $state = [hashtable]::Synchronized(@{
        Title            = $Title
        HeaderText       = $HeaderText
        Percent          = $clampedInit
        Message          = $InitialMessage
        Detail           = $InitialDetail
        Status           = "running"
        ErrorMessage     = ""
        LogPath          = $LogPath
        TopMost          = $TopMost
        StartedAt        = [DateTime]::UtcNow
        CloseRequested   = $false
        UserAcknowledged = $false
        FormShown        = $false
        InitError        = ""
    })

    $rs = $null
    $ps = $null
    try {
        $rs = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace()
        $rs.ApartmentState = [System.Threading.ApartmentState]::STA
        $rs.ThreadOptions = [System.Management.Automation.Runspaces.PSThreadOptions]::ReuseThread
        $rs.Open()
        $rs.SessionStateProxy.SetVariable("ProgressState", $state)

        $ps = [powershell]::Create()
        $ps.Runspace = $rs
        $null = $ps.AddScript({
            $s = $ProgressState
            try {
                Add-Type -AssemblyName System.Windows.Forms
                Add-Type -AssemblyName System.Drawing
                try { [System.Windows.Forms.Application]::EnableVisualStyles() } catch {}

                $form = New-Object System.Windows.Forms.Form
                $form.Text = [string]$s.Title
                $form.ClientSize = New-Object System.Drawing.Size(500, 225)
                $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
                $form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
                $form.MaximizeBox = $false
                $form.MinimizeBox = $true
                $form.ControlBox = $false
                $form.TopMost = [bool]$s.TopMost
                $form.BackColor = [System.Drawing.Color]::FromArgb(248, 250, 252)

                $accent = New-Object System.Windows.Forms.Panel
                $accent.Dock = [System.Windows.Forms.DockStyle]::Top
                $accent.Height = 5
                $accent.BackColor = [System.Drawing.Color]::FromArgb(37, 99, 235)
                $form.Controls.Add($accent)

                $lblHeader = New-Object System.Windows.Forms.Label
                $lblHeader.Location = New-Object System.Drawing.Point(22, 18)
                $lblHeader.Size = New-Object System.Drawing.Size(374, 28)
                $lblHeader.Font = New-Object System.Drawing.Font("Yu Gothic UI", 12, [System.Drawing.FontStyle]::Bold)
                $lblHeader.ForeColor = [System.Drawing.Color]::FromArgb(15, 23, 42)
                $lblHeader.Text = [string]$s.HeaderText
                $form.Controls.Add($lblHeader)

                $lblPercent = New-Object System.Windows.Forms.Label
                $lblPercent.Location = New-Object System.Drawing.Point(398, 13)
                $lblPercent.Size = New-Object System.Drawing.Size(80, 34)
                $lblPercent.TextAlign = [System.Drawing.ContentAlignment]::TopRight
                $lblPercent.Font = New-Object System.Drawing.Font("Yu Gothic UI", 17, [System.Drawing.FontStyle]::Bold)
                $lblPercent.ForeColor = [System.Drawing.Color]::FromArgb(37, 99, 235)
                $lblPercent.Text = ("{0}%" -f [int]$s.Percent)
                $form.Controls.Add($lblPercent)

                $lblMessage = New-Object System.Windows.Forms.Label
                $lblMessage.Location = New-Object System.Drawing.Point(22, 54)
                $lblMessage.Size = New-Object System.Drawing.Size(456, 24)
                $lblMessage.Font = New-Object System.Drawing.Font("Yu Gothic UI", 10, [System.Drawing.FontStyle]::Bold)
                $lblMessage.ForeColor = [System.Drawing.Color]::FromArgb(30, 41, 59)
                $lblMessage.AutoEllipsis = $true
                $lblMessage.Text = [string]$s.Message
                $form.Controls.Add($lblMessage)

                $progressBar = New-Object System.Windows.Forms.ProgressBar
                $progressBar.Location = New-Object System.Drawing.Point(22, 84)
                $progressBar.Size = New-Object System.Drawing.Size(456, 22)
                $progressBar.Minimum = 0
                $progressBar.Maximum = 100
                $progressBar.Style = [System.Windows.Forms.ProgressBarStyle]::Continuous
                $progressBar.Value = [Math]::Max(0, [Math]::Min(100, [int]$s.Percent))
                $form.Controls.Add($progressBar)

                $lblDetail = New-Object System.Windows.Forms.Label
                $lblDetail.Location = New-Object System.Drawing.Point(22, 114)
                $lblDetail.Size = New-Object System.Drawing.Size(456, 62)
                $lblDetail.Font = New-Object System.Drawing.Font("Yu Gothic UI", 9, [System.Drawing.FontStyle]::Regular)
                $lblDetail.ForeColor = [System.Drawing.Color]::FromArgb(71, 85, 105)
                $lblDetail.AutoEllipsis = $true
                $lblDetail.Text = [string]$s.Detail
                $form.Controls.Add($lblDetail)

                $lblElapsed = New-Object System.Windows.Forms.Label
                $lblElapsed.Location = New-Object System.Drawing.Point(22, 187)
                $lblElapsed.Size = New-Object System.Drawing.Size(330, 22)
                $lblElapsed.Font = New-Object System.Drawing.Font("Yu Gothic UI", 9, [System.Drawing.FontStyle]::Regular)
                $lblElapsed.ForeColor = [System.Drawing.Color]::FromArgb(100, 116, 139)
                $lblElapsed.Text = "経過時間: 0秒"
                $form.Controls.Add($lblElapsed)

                $btnClose = New-Object System.Windows.Forms.Button
                $btnClose.Location = New-Object System.Drawing.Point(378, 182)
                $btnClose.Size = New-Object System.Drawing.Size(100, 28)
                $btnClose.Font = New-Object System.Drawing.Font("Yu Gothic UI", 9, [System.Drawing.FontStyle]::Regular)
                $btnClose.Text = "閉じる"
                $btnClose.Visible = $false
                $btnClose.Enabled = $false
                $btnClose.Add_Click({
                    $s.UserAcknowledged = $true
                    try { $form.Close() } catch {}
                })
                $form.Controls.Add($btnClose)

                $form.Add_Shown({
                    $s.FormShown = $true
                    try { $form.Activate() } catch {}
                })
                $form.Add_FormClosing({
                    param($sender, $e)
                    if (-not $s.CloseRequested -and ([string]$s.Status -ne "failed")) {
                        $e.Cancel = $true
                        return
                    }
                    $s.UserAcknowledged = $true
                })

                $lastPct = -1
                $lastMsg = $null
                $lastDet = $null
                $lastSt = ""
                $lastErr = ""
                $lastSec = -1

                $timer = New-Object System.Windows.Forms.Timer
                $timer.Interval = 100
                $timer.Add_Tick({
                    if ($s.CloseRequested) {
                        $timer.Stop()
                        try { $form.Close() } catch {}
                        return
                    }

                    $curSec = [int][Math]::Floor(([DateTime]::UtcNow - [DateTime]$s.StartedAt).TotalSeconds)
                    if ($curSec -ne $lastSec) {
                        $lastSec = $curSec
                        if ($curSec -ge 60) {
                            $mins = [int][Math]::Floor($curSec / 60)
                            $rem = $curSec % 60
                            $lblElapsed.Text = ("経過時間: {0}分{1:00}秒" -f $mins, $rem)
                        } else {
                            $lblElapsed.Text = ("経過時間: {0}秒" -f $curSec)
                        }
                    }

                    $curPct = [Math]::Max(0, [Math]::Min(100, [int]$s.Percent))
                    $curMsg = [string]$s.Message
                    $curDet = [string]$s.Detail
                    $curSt = [string]$s.Status
                    $curErr = [string]$s.ErrorMessage

                    if ($curPct -ne $lastPct) {
                        $lastPct = $curPct
                        $progressBar.Value = $curPct
                        $lblPercent.Text = ("{0}%" -f $curPct)
                    }
                    if ($curMsg -ne $lastMsg) {
                        $lastMsg = $curMsg
                        $lblMessage.Text = $curMsg
                    }
                    if ($curSt -ne $lastSt -or $curDet -ne $lastDet -or $curErr -ne $lastErr) {
                        $lastSt = $curSt
                        $lastDet = $curDet
                        $lastErr = $curErr
                        if ($curSt -eq "failed") {
                            $accent.BackColor = [System.Drawing.Color]::FromArgb(220, 38, 38)
                            $lblHeader.ForeColor = [System.Drawing.Color]::FromArgb(153, 27, 27)
                            $lblPercent.ForeColor = [System.Drawing.Color]::FromArgb(220, 38, 38)
                            $detailText = $curErr
                            if (-not [string]::IsNullOrWhiteSpace([string]$s.LogPath)) {
                                $detailText = "$detailText`r`nログ: $($s.LogPath)"
                            }
                            $lblDetail.ForeColor = [System.Drawing.Color]::FromArgb(127, 29, 29)
                            $lblDetail.Text = $detailText
                            $form.ControlBox = $true
                            $btnClose.Visible = $true
                            $btnClose.Enabled = $true
                            $form.AcceptButton = $btnClose
                            $form.CancelButton = $btnClose
                            try { $form.Activate() } catch {}
                        } elseif ($curSt -eq "completed" -or $curSt -eq "ready") {
                            $accent.BackColor = [System.Drawing.Color]::FromArgb(22, 163, 74)
                            $lblPercent.ForeColor = [System.Drawing.Color]::FromArgb(22, 163, 74)
                            $lblDetail.ForeColor = [System.Drawing.Color]::FromArgb(22, 101, 52)
                            $lblDetail.Text = $curDet
                        } else {
                            $lblDetail.Text = $curDet
                        }
                    }
                })
                $timer.Start()

                try {
                    [System.Windows.Forms.Application]::Run($form)
                } finally {
                    try { $timer.Stop(); $timer.Dispose() } catch {}
                    try { $form.Dispose() } catch {}
                }
            } catch {
                $s.InitError = $_.Exception.Message
            }
        })

        $async = $ps.BeginInvoke()

        # Brief wait so the initial frame renders smoothly without noticeably delaying startup
        $waitSw = [System.Diagnostics.Stopwatch]::StartNew()
        while ($waitSw.ElapsedMilliseconds -lt 500) {
            if ($state.FormShown -or (-not [string]::IsNullOrEmpty([string]$state.InitError)) -or $async.IsCompleted) {
                break
            }
            Start-Sleep -Milliseconds 25
        }

        if (-not [string]::IsNullOrEmpty([string]$state.InitError)) {
            try { $ps.Dispose() } catch {}
            try { $rs.Close(); $rs.Dispose() } catch {}
            return @{ IsActive = $false }
        }

        return @{
            IsActive    = $true
            State       = $state
            Runspace    = $rs
            PowerShell  = $ps
            AsyncResult = $async
        }
    } catch {
        if ($ps) { try { $ps.Dispose() } catch {} }
        if ($rs) { try { $rs.Close(); $rs.Dispose() } catch {} }
        return @{ IsActive = $false }
    }
}

function Update-PaperBridgeProgressWindow {
    param(
        $Handle,
        [int]$Percent,
        [string]$Message = "",
        [string]$Detail = "",
        [string]$Status = "running"
    )
    if ($null -eq $Handle -or (-not $Handle.IsActive) -or $null -eq $Handle.State) {
        return
    }
    try {
        $s = $Handle.State
        $s.Percent = [Math]::Max(0, [Math]::Min(100, $Percent))
        if (-not [string]::IsNullOrWhiteSpace($Message)) {
            $s.Message = $Message
        }
        $s.Detail = $Detail
        $s.Status = $Status
    } catch {}
}

function Close-PaperBridgeProgressWindow {
    param(
        $Handle,
        [int]$WaitMs = 2500
    )
    if ($null -eq $Handle -or (-not $Handle.IsActive)) {
        return
    }
    try {
        if ($Handle.State) {
            $Handle.State.CloseRequested = $true
        }
        if ($Handle.AsyncResult) {
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            while ((-not $Handle.AsyncResult.IsCompleted) -and ($sw.ElapsedMilliseconds -lt $WaitMs)) {
                Start-Sleep -Milliseconds 40
            }
            if ($Handle.AsyncResult.IsCompleted -and $Handle.PowerShell) {
                try { $null = $Handle.PowerShell.EndInvoke($Handle.AsyncResult) } catch {}
            } elseif ($Handle.PowerShell) {
                try { $Handle.PowerShell.Stop() } catch {}
            }
        }
    } catch {}
    finally {
        if ($Handle.PowerShell) { try { $Handle.PowerShell.Dispose() } catch {} }
        if ($Handle.Runspace) {
            try { $Handle.Runspace.Close() } catch {}
            try { $Handle.Runspace.Dispose() } catch {}
        }
        $Handle.IsActive = $false
    }
}

function Show-PaperBridgeProgressErrorAndWait {
    param(
        $Handle,
        [string]$ErrorTitle = "エラーが発生しました",
        [string]$ErrorMessage = "",
        [string]$LogPath = ""
    )
    if ($null -eq $Handle -or (-not $Handle.IsActive) -or $null -eq $Handle.State) {
        return
    }
    try {
        $s = $Handle.State
        if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
            $s.LogPath = $LogPath
        }
        $s.Message = $ErrorTitle
        $s.ErrorMessage = $ErrorMessage
        $s.Status = "failed"

        while ((-not $s.UserAcknowledged) -and $Handle.AsyncResult -and (-not $Handle.AsyncResult.IsCompleted)) {
            Start-Sleep -Milliseconds 100
        }
    } finally {
        Close-PaperBridgeProgressWindow -Handle $Handle
    }
}
