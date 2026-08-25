[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Run
)

<#
    Scheduled Task を作成できない非昇格 Windows 環境向けの DDNS watchdog。

    - HKCU:\Software\Microsoft\Windows\CurrentVersion\Run に自分自身を冪等登録する。
    - ログオン時に hidden PowerShell として起動し、起動直後と 5 分ごとに更新スクリプトを実行する。
    - 認証情報は update_ddns.py が .env から読み込むため、引数・レジストリ値・ログへ渡さない。
    - Local named mutex で二重起動を防ぎ、ログは 1 MiB を上限にローテーションする。
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
}

$watchdogPath = Join-Path $ProjectRoot 'scripts\run_ddns_watchdog.ps1'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName = 'AoiTalk-DDNS'

if ($Uninstall) {
    Remove-ItemProperty -LiteralPath $runKey -Name $runName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*run_ddns_watchdog.ps1*' -and $_.CommandLine -like '* -Run*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Output "DDNS watchdog の自動起動登録を削除しました"
    exit 0
}

$updateScript = Join-Path $ProjectRoot 'scripts\update_ddns.py'
$pythonPath = Join-Path $ProjectRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    $pythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "プロジェクト venv の Python が見つかりません"
}
if (-not (Test-Path -LiteralPath $updateScript -PathType Leaf)) {
    throw "DDNS 更新スクリプトが見つかりません"
}

$runArguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchdogPath`" -ProjectRoot `"$ProjectRoot`" -Run"
$runCommand = "powershell.exe $runArguments"

if ($Install) {
    # HKCU は管理者権限不要。値はスクリプトパスのみで、秘密を含まない。
    New-Item -Path $runKey -Force | Out-Null
    Set-ItemProperty -LiteralPath $runKey -Name $runName -Value $runCommand -Type String
    Write-Output "DDNS watchdog を HKCU Run に登録しました"
    if (-not $Run) {
        Start-Process `
            -FilePath 'powershell.exe' `
            -ArgumentList $runArguments `
            -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden
        Write-Output "DDNS watchdog を hidden 起動しました"
    }
    if (-not $Run) { exit 0 }
}

if (-not $Run) {
    # 引数なしはインストール相当として扱い、手動実行でも安全に登録する。
    New-Item -Path $runKey -Force | Out-Null
    Set-ItemProperty -LiteralPath $runKey -Name $runName -Value $runCommand -Type String
    Start-Process -FilePath 'powershell.exe' -ArgumentList $runArguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden
    exit 0
}

$mutex = New-Object System.Threading.Mutex($false, 'Local\AoiTalk-AoiTalk-DDNS-Watchdog')
$hasMutex = $false
try {
    $hasMutex = $mutex.WaitOne(0, $false)
    if (-not $hasMutex) { exit 0 }

    $logDirectory = Join-Path $ProjectRoot 'logs\ops'
    $logPath = Join-Path $logDirectory 'ddns_update.log'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

    while ($true) {
        if ((Test-Path -LiteralPath $logPath -PathType Leaf) -and ((Get-Item -LiteralPath $logPath).Length -gt 1MB)) {
            $rotated = "$logPath.1"
            Remove-Item -LiteralPath $rotated -Force -ErrorAction SilentlyContinue
            Move-Item -LiteralPath $logPath -Destination $rotated -Force
        }
        Add-Content -LiteralPath $logPath -Encoding UTF8 -Value ("[{0}] DDNS watchdog 実行開始" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
        # PowerShell 5.1 のネイティブ stderr デコードによる文字化けを避けるため、
        # UTF-8 を指定した .NET Process の stdout/stderr から取り込む。
        $process = $null
        try {
            $pythonArguments = '"{0}" --timeout 10 --retries 2' -f $updateScript
            $startInfo = New-Object System.Diagnostics.ProcessStartInfo
            $startInfo.FileName = $pythonPath
            $startInfo.Arguments = $pythonArguments
            $startInfo.WorkingDirectory = $ProjectRoot
            $startInfo.UseShellExecute = $false
            $startInfo.CreateNoWindow = $true
            $startInfo.RedirectStandardOutput = $true
            $startInfo.RedirectStandardError = $true
            $startInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
            $startInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
            $startInfo.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
            $process = New-Object System.Diagnostics.Process
            $process.StartInfo = $startInfo
            $process.Start() | Out-Null
            if (-not $process.WaitForExit(180000)) {
                $process.Kill()
                $process.WaitForExit()
                $exitCode = 124
                Add-Content -LiteralPath $logPath -Encoding UTF8 -Value 'DDNS watchdog の Python 実行が 180 秒でタイムアウトしました'
            } else {
                $exitCode = $process.ExitCode
            }
            $stdout = $process.StandardOutput.ReadToEnd()
            $stderr = $process.StandardError.ReadToEnd()
            foreach ($line in (($stdout + "`n" + $stderr) -split "`r?`n")) {
                if (-not [string]::IsNullOrWhiteSpace($line)) {
                    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value $line
                }
            }
        } catch {
            $exitCode = 1
            Add-Content -LiteralPath $logPath -Encoding UTF8 -Value "DDNS watchdog の Python 起動に失敗しました"
        } finally {
            if ($null -ne $process) { $process.Dispose() }
        }
        Add-Content -LiteralPath $logPath -Encoding UTF8 -Value ("[{0}] DDNS watchdog 終了 exit={1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $exitCode)
        Start-Sleep -Seconds 300
    }
} finally {
    if ($hasMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
