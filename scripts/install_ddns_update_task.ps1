[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$TaskName = 'AoiTalk-DDNS',
    [switch]$Unregister
)

<#
    AoiTalk の DDNS 更新を現在のユーザー権限でタスクスケジューラに登録する。

    認証情報は update_ddns.py がプロジェクトの .env から読むため、このスクリプトの
    引数・タスク Action・履歴には秘密を渡さない。既存の同名タスクは -Force で置換し、
    再実行しても重複タスクを作らない。
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
}

$taskPath = '\'

if ($Unregister) {
    $existingTask = Get-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -ErrorAction SilentlyContinue
    if ($null -ne $existingTask) {
        Unregister-ScheduledTask -InputObject $existingTask -Confirm:$false
        Write-Output "DDNS タスクを削除しました: $TaskName"
    } else {
        # タスクが存在しない場合も冪等な削除として成功扱いにする。
        Write-Output "DDNS タスクは存在しません: $TaskName"
    }
    $legacyName = 'AoiTalk-DDNS-Update'
    if ($legacyName -ne $TaskName) {
        $legacyExisting = Get-ScheduledTask -TaskName $legacyName -TaskPath $taskPath -ErrorAction SilentlyContinue
        if ($null -ne $legacyExisting) {
            Unregister-ScheduledTask -InputObject $legacyExisting -Confirm:$false
            Write-Output "旧 DDNS タスクを削除しました: $legacyName"
        }
    }
    $watchdogPath = Join-Path $ProjectRoot 'scripts\run_ddns_watchdog.ps1'
    if (Test-Path -LiteralPath $watchdogPath -PathType Leaf) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $watchdogPath -ProjectRoot $ProjectRoot -Uninstall
        if ($LASTEXITCODE -ne 0) {
            throw "DDNS watchdog の解除に失敗しました (exit=$LASTEXITCODE)"
        }
    }
    exit 0
}

$scriptPath = Join-Path $ProjectRoot 'scripts\update_ddns.py'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "DDNS 更新スクリプトが見つかりません: $scriptPath"
}

# リポジトリで使う venv を優先し、開発環境で .venv を使う場合だけフォールバックする。
$pythonPath = Join-Path $ProjectRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    $pythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "プロジェクト venv の Python が見つかりません (venv\Scripts\python.exe)"
}

# Action の引数には認証情報を含めない。パスは引用して空白を含むルートにも対応する。
$actionArguments = '-u "{0}"' -f $scriptPath
$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument $actionArguments `
    -WorkingDirectory $ProjectRoot

# 起動時・ログオン時・5 分ごとの定期実行を 1 つのタスクにまとめる。
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$periodicTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3)

$task = New-ScheduledTask `
    -Action $action `
    -Trigger @($startupTrigger, $logonTrigger, $periodicTrigger) `
    -Principal $principal `
    -Settings $settings `
    -Description 'AoiTalk f5.si DDNS の IPv4 A レコードを変更時だけ更新'

# 旧版の名前で登録されたタスクが残っている場合は、二重実行を防ぐため削除する。
$legacyTaskName = 'AoiTalk-DDNS-Update'
if ($legacyTaskName -ne $TaskName) {
    $legacyTask = Get-ScheduledTask -TaskName $legacyTaskName -TaskPath $taskPath -ErrorAction SilentlyContinue
    if ($null -ne $legacyTask) {
        Unregister-ScheduledTask -InputObject $legacyTask -Confirm:$false
        Write-Output "旧 DDNS タスクを削除しました: $legacyTaskName"
    }
}

# -Force により、同名タスクがあっても重複せず最新定義へ置き換える。
# Register-ScheduledTask が管理者権限を要求する Windows 構成では、同じ内容を
# schtasks.exe の XML 経由で現在ユーザーとして登録する（認証情報は XML/引数に含めない）。
$registered = $false
$xmlRegistered = $false
try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $taskPath `
        -InputObject $task `
        -Force | Out-Null
    $registered = $true
} catch {
    Write-Warning "ScheduledTasks API で登録できないため、ユーザー権限の schtasks に切り替えます。"
}

if (-not $registered) {
    function ConvertTo-TaskXmlText([string]$Value) {
        return [System.Security.SecurityElement]::Escape($Value)
    }

    $xmlPath = Join-Path ([System.IO.Path]::GetTempPath()) ("AoiTalk-DDNS-{0}.xml" -f ([guid]::NewGuid().ToString('N')))
    $startBoundary = (Get-Date).AddMinutes(1).ToUniversalTime().ToString('s') + 'Z'
    $userXml = ConvertTo-TaskXmlText $currentUser
    $commandXml = ConvertTo-TaskXmlText $pythonPath
    $argumentsXml = ConvertTo-TaskXmlText $actionArguments
    $workingDirectoryXml = ConvertTo-TaskXmlText $ProjectRoot
    $taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>AoiTalk f5.si DDNS の IPv4 A レコードを変更時だけ更新</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
    <LogonTrigger><Enabled>true</Enabled><UserId>$userXml</UserId></LogonTrigger>
    <TimeTrigger>
      <StartBoundary>$startBoundary</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition><Interval>PT5M</Interval><Duration>P3650D</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>$userXml</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT3M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>$commandXml</Command><Arguments>$argumentsXml</Arguments><WorkingDirectory>$workingDirectoryXml</WorkingDirectory></Exec></Actions>
</Task>
"@
    $xmlRegistered = $false
    try {
        Set-Content -LiteralPath $xmlPath -Value $taskXml -Encoding Unicode
        & schtasks.exe /Create /TN $TaskName /XML $xmlPath /F | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "schtasks.exe が終了コード $LASTEXITCODE を返しました"
        }
        $xmlRegistered = $true
    } catch {
        Write-Warning "schtasks の起動時/ログオン時トリガー登録も権限不足のため、HKCU watchdog に切り替えます。"
    } finally {
        Remove-Item -LiteralPath $xmlPath -Force -ErrorAction SilentlyContinue
    }

    if (-not $xmlRegistered) {
        $watchdogPath = Join-Path $ProjectRoot 'scripts\run_ddns_watchdog.ps1'
        if (-not (Test-Path -LiteralPath $watchdogPath -PathType Leaf)) {
            throw "DDNS watchdog が見つかりません: $watchdogPath"
        }
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $watchdogPath -ProjectRoot $ProjectRoot -Install
        if ($LASTEXITCODE -ne 0) {
            throw "DDNS watchdog の HKCU Run 登録に失敗しました (exit=$LASTEXITCODE)"
        }
        Write-Output 'ユーザー権限の HKCU watchdog を登録しました（起動直後 / 5 分ごと）'
    }
}

if ($registered -or $xmlRegistered) {
    # Scheduled Task が実際に登録できた場合だけ、既存の HKCU watchdog を停止・解除する。
    # 登録失敗時は watchdog を残して可用性を維持する。
    $watchdogPath = Join-Path $ProjectRoot 'scripts\run_ddns_watchdog.ps1'
    if (Test-Path -LiteralPath $watchdogPath -PathType Leaf) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $watchdogPath -ProjectRoot $ProjectRoot -Uninstall
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Scheduled Task は登録済みですが、既存 watchdog の解除に失敗しました。"
        }
    }
}

Write-Output "DDNS タスクを登録しました: $TaskName"
Write-Output "  Python: $pythonPath"
Write-Output "  作業ディレクトリ: $ProjectRoot"
Write-Output '  トリガー: 起動時 / ログオン時 / 5 分ごと'
