[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [Alias("Base")]
    [string]$ReleaseBase = "",
    [switch]$PublishOnly,
    [switch]$InternalBuild,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArguments
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

Set-Variable -Name OwnerIdentity -Value "AoiTalk-build_mobile_apk/v1" -Scope Script
Set-Variable -Name Sha40Pattern -Value '^[0-9a-f]{40}$' -Scope Script
Set-Variable -Name ApkName -Value "aoitalk-mobile.apk" -Scope Script
Set-Variable -Name ReleaseRepo -Value "ttttdiva/AoiTalk" -Scope Script
Set-Variable -Name OwnedProcesses -Value ([System.Collections.Generic.List[psobject]]::new()) -Scope Script
Set-Variable -Name JobTrackingReady -Value $false -Scope Script

function Stop-Launcher([string]$Message) {
    throw [System.InvalidOperationException]::new($Message)
}

if ($null -ne $RemainingArguments -and @($RemainingArguments).Count -gt 0) {
    if (@($RemainingArguments).Count -eq 1 -and $RemainingArguments[0] -ieq "--publish-only") {
        $PublishOnly = $true
    } else {
        Stop-Launcher ("Unknown parameter or launcher argument: " + ($RemainingArguments -join " "))
    }
}

function Full-Path([string]$Path) {
    return ([System.IO.Path]::GetFullPath($Path)).TrimEnd([char]92, [char]47)
}

function Is-Under([string]$Child, [string]$Parent) {
    $childFull = Full-Path $Child
    $parentFull = Full-Path $Parent
    return $childFull.Equals($parentFull, [System.StringComparison]::OrdinalIgnoreCase) -or
        $childFull.StartsWith($parentFull + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparseAncestors([string]$Path) {
    $cursor = [System.IO.DirectoryInfo]::new((Full-Path $Path))
    while ($null -ne $cursor) {
        $item = Get-Item -LiteralPath $cursor.FullName -Force -ErrorAction SilentlyContinue
        if ($null -ne $item) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                Stop-Launcher "A launcher path or ancestor is a reparse point: $($cursor.FullName)"
            }
        }
        $cursor = $cursor.Parent
    }
}

function Assert-RegularFile([string]$Path, [switch]$AllowMissing) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        if ($AllowMissing) {
            return
        }
        Stop-Launcher "Required regular file is missing: $Path"
    }
    if ($item.PSIsContainer -or $item -isnot [System.IO.FileInfo]) {
        Stop-Launcher "Expected a regular file but found a directory: $Path"
    }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Stop-Launcher "Expected a regular file but found a reparse point: $Path"
    }
}

function Assert-SafeOwnedPath([string]$Path, [string]$AllowedRoot, [string]$RepositoryRoot) {
    $full = Full-Path $Path
    $allowed = Full-Path $AllowedRoot
    $repo = Full-Path $RepositoryRoot
    $profile = Full-Path ([Environment]::GetFolderPath("UserProfile"))
    $drive = (Get-Item -LiteralPath ([System.IO.Path]::GetPathRoot($full)) -Force).FullName.TrimEnd([char]92, [char]47)
    if (-not (Is-Under $full $allowed) -or $full.Equals($allowed, [System.StringComparison]::OrdinalIgnoreCase) -or
        $full.Equals($repo, [System.StringComparison]::OrdinalIgnoreCase) -or
        $full.Equals($profile, [System.StringComparison]::OrdinalIgnoreCase) -or
        $full.Equals($drive, [System.StringComparison]::OrdinalIgnoreCase) -or
        $full.IndexOfAny([char[]]'*?[]') -ge 0) {
        Stop-Launcher "Unsafe owned launcher path: $full"
    }
    Assert-NoReparseAncestors $full
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -ne $item) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            Stop-Launcher "Owned launcher path is already a reparse point: $full"
        }
    }
}

function Resolve-TrustedTool([string]$Name, [string]$RepositoryRoot) {
    $where = Join-Path $env:SystemRoot "System32\where.exe"
    if (-not (Test-Path -LiteralPath $where)) {
        $whereCommand = Get-Command where.exe -ErrorAction SilentlyContinue
        if ($null -eq $whereCommand) {
            Stop-Launcher "Trusted where.exe was not found while resolving $Name"
        }
        $where = $whereCommand.Source
    }
    $forbiddenRoots = [System.Collections.Generic.List[string]]::new()
    foreach ($root in @($RepositoryRoot, $env:AOITALK_RELEASE_LAUNCH_ROOT)) {
        if ([string]::IsNullOrWhiteSpace([string]$root)) { continue }
        try { [void]$forbiddenRoots.Add((Full-Path $root)) } catch { }
    }
    $toolNames = @("$Name.exe")
    if ($Name -eq "npm" -or $Name -eq "npx") {
        $toolNames += @("$Name.cmd", "$Name.bat")
    }
    foreach ($toolName in $toolNames) {
        $whereResponse = Invoke-Process $where @($toolName)
        if ($whereResponse.ExitCode -ne 0) {
            continue
        }
        $candidates = @($whereResponse.Stdout -split "`r?`n")
        foreach ($candidateLine in $candidates) {
            $candidate = ([string]$candidateLine).Trim()
            if ([string]::IsNullOrWhiteSpace($candidate)) {
                continue
            }
            try {
                $full = Full-Path $candidate
                $underForbiddenRoot = $false
                foreach ($forbiddenRoot in $forbiddenRoots) {
                    if (Is-Under $full $forbiddenRoot) { $underForbiddenRoot = $true; break }
                }
                if ($underForbiddenRoot -or -not (Test-Path -LiteralPath $full -PathType Leaf)) {
                    continue
                }
                return $full
            } catch {
                continue
            }
        }
    }
    Stop-Launcher "Trusted $Name executable was not found outside the launch checkout"
}

function Quote-ProcessArgument([string]$Value) {
    if ($null -eq $Value -or $Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append([char]34)
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) {
            $slashes++
            continue
        }
        if ($character -eq [char]34) {
            [void]$builder.Append(('\' * (2 * $slashes + 1)))
            [void]$builder.Append([char]34)
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) {
            [void]$builder.Append(('\' * $slashes))
            $slashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($slashes -gt 0) { [void]$builder.Append(('\' * (2 * $slashes))) }
    [void]$builder.Append([char]34)
    return $builder.ToString()
}

function Ensure-OwnedJobType {
    if ($null -eq ('AoiTalkProcessJob' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class AoiTalkProcessJob
{
    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public IntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimitInformation
    {
        public BasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr securityAttributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    public static IntPtr CreateForProcess(IntPtr process)
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");
        try
        {
            ExtendedLimitInformation limits = new ExtendedLimitInformation();
            limits.BasicLimitInformation.LimitFlags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            int size = Marshal.SizeOf(typeof(ExtendedLimitInformation));
            IntPtr memory = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(limits, memory, false);
                if (!SetInformationJobObject(job, 9, memory, (uint)size))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "SetInformationJobObject failed");
            }
            finally { Marshal.FreeHGlobal(memory); }
            if (!AssignProcessToJobObject(job, process))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "AssignProcessToJobObject failed");
            return job;
        }
        catch
        {
            CloseHandle(job);
            throw;
        }
    }

    public static void CloseKillOnCloseJob(IntPtr job)
    {
        if (job != IntPtr.Zero && !CloseHandle(job))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "CloseHandle(job) failed");
    }
}
'@ -ErrorAction Stop
    }
}

function Read-ProcessSnapshot {
    $rows = [System.Collections.Generic.List[psobject]]::new()
    try {
        foreach ($row in @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)) {
            try {
                if ($row.CreationDate -is [DateTime]) {
                    $created = ([DateTime]$row.CreationDate).ToUniversalTime()
                } else {
                    $created = [System.Management.ManagementDateTimeConverter]::ToDateTime([string]$row.CreationDate).ToUniversalTime()
                }
                [void]$rows.Add([pscustomobject]@{
                    Pid = [int]$row.ProcessId
                    ParentPid = [int]$row.ParentProcessId
                    CreatedUtc = $created
                    Name = [string]$row.Name
                })
            } catch { }
        }
    } catch { }
    return $rows.ToArray()
}

function Capture-ProcessDescendants([psobject]$Owned, [object[]]$Snapshot) {
    $captured = [System.Collections.Generic.List[psobject]]::new()
    if ($null -eq $Snapshot -or $Snapshot.Count -eq 0) { return $captured.ToArray() }
    $rootRows = @($Snapshot | Where-Object { $_.Pid -eq $Owned.Pid })
    if ($rootRows.Count -gt 0) {
        $delta = [Math]::Abs(($rootRows[0].CreatedUtc - $Owned.CreatedUtc).TotalSeconds)
        if ($delta -gt 2) { return $captured.ToArray() }
    }
    $depths = @{}
    $depths[[int]$Owned.Pid] = 0
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($row in $Snapshot) {
            if ($depths.ContainsKey([int]$row.Pid)) { continue }
            if (-not $depths.ContainsKey([int]$row.ParentPid)) { continue }
            if ($row.CreatedUtc -lt $Owned.CreatedUtc.AddSeconds(-2)) { continue }
            $depth = [int]$depths[[int]$row.ParentPid] + 1
            $depths[[int]$row.Pid] = $depth
            [void]$captured.Add([pscustomobject]@{
                Pid = [int]$row.Pid
                ParentPid = [int]$row.ParentPid
                CreatedUtc = $row.CreatedUtc
                Name = $row.Name
                Depth = $depth
            })
            $changed = $true
        }
    }
    return $captured.ToArray()
}

function Stop-ProcessTreeFallback([psobject]$Owned) {
    $errors = [System.Collections.Generic.List[string]]::new()
    $snapshot = Read-ProcessSnapshot
    $captured = [System.Collections.Generic.List[psobject]]::new()
    foreach ($item in @(Capture-ProcessDescendants $Owned $snapshot)) { [void]$captured.Add($item) }
    if ($captured.Count -eq 0 -and ([System.IO.Path]::GetFileName($Owned.Executable) -match '^(?i:cmd|powershell|pwsh)\.exe$')) {
        for ($attempt = 0; $attempt -lt 10 -and $captured.Count -eq 0; $attempt++) {
            Start-Sleep -Milliseconds 100
            foreach ($item in @(Capture-ProcessDescendants $Owned (Read-ProcessSnapshot))) { [void]$captured.Add($item) }
        }
    }
    foreach ($old in @($Owned.Descendants.ToArray())) {
        if (-not (@($captured.ToArray() | Where-Object { $_.Pid -eq $old.Pid -and $_.CreatedUtc -eq $old.CreatedUtc }).Count -gt 0)) {
            [void]$captured.Add($old)
        }
    }
    $targets = @($captured.ToArray() | Sort-Object -Property Depth -Descending)
    foreach ($target in $targets) {
        try {
            $process = Get-Process -Id $target.Pid -ErrorAction SilentlyContinue
            if ($null -eq $process) { continue }
            $created = $process.StartTime.ToUniversalTime()
            if ([Math]::Abs(($created - $target.CreatedUtc).TotalSeconds) -gt 2) { continue }
            $process.Kill()
            $process.WaitForExit()
            $process.Dispose()
        } catch {
            [void]$errors.Add("owned descendant PID $($target.Pid): $($_.Exception.Message)")
        }
    }
    try {
        $root = $Owned.Process
        if ($null -ne $root -and -not $root.HasExited) {
            $root.Kill()
            $root.WaitForExit()
        }
    } catch {
        [void]$errors.Add("owned root PID $($Owned.Pid): $($_.Exception.Message)")
    }
    return $errors.ToArray()
}

function Close-OwnedProcessGroup([psobject]$Owned) {
    $errors = [System.Collections.Generic.List[string]]::new()
    if ($Owned.TreeFallback) {
        foreach ($processError in @(Stop-ProcessTreeFallback $Owned)) {
            [void]$errors.Add([string]$processError)
        }
        if ($errors.Count -eq 0) { $Owned.TreeFallback = $false }
    } elseif ($Owned.Job -ne [IntPtr]::Zero) {
        try {
            [AoiTalkProcessJob]::CloseKillOnCloseJob($Owned.Job)
            $Owned.Job = [IntPtr]::Zero
        } catch {
            [void]$errors.Add("owned process group PID $($Owned.Pid): $($_.Exception.Message)")
        }
    }
    return $errors.ToArray()
}

function New-OwnedProcessJob([System.Diagnostics.Process]$Process, [string]$Executable) {
    Ensure-OwnedJobType
    try {
        $job = [AoiTalkProcessJob]::CreateForProcess($Process.Handle)
        return [pscustomobject]@{
            Process = $Process
            Job = $job
            Pid = $Process.Id
            Executable = $Executable
            CreatedUtc = $Process.StartTime.ToUniversalTime()
            Owner = $script:OwnerIdentity
            GroupId = [Guid]::NewGuid().ToString("N")
            TreeFallback = $false
            Descendants = [System.Collections.Generic.List[psobject]]::new()
        }
    } catch {
        if ($_.Exception.Message -match "AssignProcessToJobObject") {
            try {
                return [pscustomobject]@{
                    Process = $Process
                    Job = [IntPtr]::Zero
                    Pid = $Process.Id
                    Executable = $Executable
                    CreatedUtc = $Process.StartTime.ToUniversalTime()
                    Owner = $script:OwnerIdentity
                    GroupId = [Guid]::NewGuid().ToString("N")
                    TreeFallback = $true
                    Descendants = [System.Collections.Generic.List[psobject]]::new()
                }
            } catch { }
        }
        try { if (-not $Process.HasExited) { $Process.Kill(); $Process.WaitForExit() } } catch { }
        $Process.Dispose()
        Stop-Launcher ("Could not attach the owned process job for ${Executable}: $($_.Exception.Message)")
    }
}

function Invoke-Process([string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory = "", [switch]$LiveOutput) {
    $fileName = $Executable
    $effectiveArguments = @($Arguments)
    $extension = [System.IO.Path]::GetExtension($Executable).ToLowerInvariant()
    $cmdWrapper = $false
    if ($extension -eq ".bat" -or $extension -eq ".cmd") {
        $fileName = $env:ComSpec
        $cmdWrapper = $true
    }
    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $fileName
    if ($cmdWrapper) {
        $commandParts = @((Quote-ProcessArgument $Executable)) + @($Arguments | ForEach-Object { Quote-ProcessArgument ([string]$_) })
        $commandText = $commandParts -join " "
        $start.Arguments = "/d /s /c `"$commandText`""
    } else {
        $start.Arguments = (($effectiveArguments | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join " ")
    }
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $liveOutput = $false
    if ($LiveOutput) {
        $liveOutput = $true
    }
    $start.RedirectStandardOutput = -not $liveOutput
    $start.RedirectStandardError = -not $liveOutput
    if (-not $liveOutput) {
        $start.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
        $start.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
    }
    if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) { $start.WorkingDirectory = $WorkingDirectory }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $start
    if (-not $process.Start()) { Stop-Launcher "Could not start child command" }
    $trackJob = $script:JobTrackingReady
    $ownedProcess = $null
    if ($trackJob) {
        $ownedProcess = New-OwnedProcessJob $process $Executable
        [void]$script:OwnedProcesses.Add($ownedProcess)
    }
    $completed = $false
    try {
        if ($liveOutput) {
            $process.WaitForExit()
            $exitCode = $process.ExitCode
            $ownedCleanupErrors = if ($trackJob) { @(Close-OwnedProcessGroup $ownedProcess) } else { @() }
            $response = [pscustomobject]@{ ExitCode = $exitCode; Stdout = ""; Stderr = ""; DrainComplete = $true; OwnedCleanupErrors = $ownedCleanupErrors }
        } else {
            $stdoutTask = $process.StandardOutput.ReadToEndAsync()
            $stderrTask = $process.StandardError.ReadToEndAsync()
            $process.WaitForExit()
            $exitCode = $process.ExitCode
            $ownedCleanupErrors = if ($trackJob) { @(Close-OwnedProcessGroup $ownedProcess) } else { @() }
            $deadline = [DateTime]::UtcNow.AddSeconds(15)
            while ((-not $stdoutTask.IsCompleted -or -not $stderrTask.IsCompleted) -and [DateTime]::UtcNow -lt $deadline) {
                Start-Sleep -Milliseconds 25
            }
            if (-not $stdoutTask.IsCompleted -or -not $stderrTask.IsCompleted) {
                try { $process.StandardOutput.BaseStream.Close() } catch { }
                try { $process.StandardError.BaseStream.Close() } catch { }
                $drainDeadline = [DateTime]::UtcNow.AddSeconds(2)
                while ((-not $stdoutTask.IsCompleted -or -not $stderrTask.IsCompleted) -and [DateTime]::UtcNow -lt $drainDeadline) {
                    Start-Sleep -Milliseconds 25
                }
            }
            $drainComplete = $stdoutTask.IsCompleted -and $stderrTask.IsCompleted
            $stdout = if ($stdoutTask.IsCompleted) { $stdoutTask.Result } else { "" }
            $stderr = if ($stderrTask.IsCompleted) { $stderrTask.Result } else { "" }
            $response = [pscustomobject]@{ ExitCode = $exitCode; Stdout = $stdout; Stderr = $stderr; DrainComplete = $drainComplete; OwnedCleanupErrors = $ownedCleanupErrors }
        }
        $completed = $true
        return $response
    } finally {
        if ($completed -and -not $trackJob) {
            $process.Dispose()
        } elseif ($completed -and $null -ne $ownedProcess) {
            # Keep the job handle until the launcher finally block closes it.
            # This also catches Kotlin/Gradle/node descendants that outlive the root.
        }
    }
}

function Write-ProcessOutput([psobject]$Response) {
    if (-not [string]::IsNullOrEmpty($Response.Stdout)) { [Console]::Out.Write($Response.Stdout) }
    if (-not [string]::IsNullOrEmpty($Response.Stderr)) { [Console]::Error.Write($Response.Stderr) }
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory = "", [switch]$LiveOutput) {
    $response = Invoke-Process $Executable $Arguments $WorkingDirectory -LiveOutput:$LiveOutput
    Write-ProcessOutput $response
    if (-not $response.DrainComplete) { Stop-Launcher "Child command output drain timed out after its root exited" }
    if (@($response.OwnedCleanupErrors).Count -gt 0) { Stop-Launcher ("Owned child cleanup failed: " + ($response.OwnedCleanupErrors -join "; ")) }
    if ($response.ExitCode -ne 0) {
        Stop-Launcher "Child command failed with exit code $($response.ExitCode)"
    }
}

function Invoke-Captured([string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory = "") {
    $response = Invoke-Process $Executable $Arguments $WorkingDirectory
    if (-not [string]::IsNullOrEmpty($response.Stderr)) { [Console]::Error.Write($response.Stderr) }
    if (-not $response.DrainComplete) { Stop-Launcher "Child command output drain timed out after its root exited" }
    if (@($response.OwnedCleanupErrors).Count -gt 0) { Stop-Launcher ("Owned child cleanup failed: " + ($response.OwnedCleanupErrors -join "; ")) }
    if ($response.ExitCode -ne 0) {
        Stop-Launcher "Child command failed with exit code $($response.ExitCode)"
    }
    return $response.Stdout.Trim()
}

function Invoke-Git([string]$GitExe, [string]$Root, [string[]]$Arguments) {
    return Invoke-Captured $GitExe (@("-C", $Root) + $Arguments)
}

function Invoke-GitUnchecked([string]$GitExe, [string]$Root, [string[]]$Arguments) {
    $response = Invoke-Process $GitExe $Arguments $Root
    return $response.ExitCode
}

function Assert-MobileExpo([psobject]$Expo, [string]$SourceLabel) {
    if ($null -eq $Expo) { Stop-Launcher "Mobile app.json has no expo section: $SourceLabel" }
    $version = [string]$Expo.version
    if ($version -notmatch '^\d+\.\d+\.\d+$') {
        Stop-Launcher "Mobile app.json has an invalid release version: $SourceLabel"
    }
    $versionCodeValue = $Expo.android.versionCode
    $isJsonInteger = $versionCodeValue -is [System.SByte] -or $versionCodeValue -is [System.Byte] -or
        $versionCodeValue -is [System.Int16] -or $versionCodeValue -is [System.UInt16] -or
        $versionCodeValue -is [System.Int32] -or $versionCodeValue -is [System.UInt32] -or
        $versionCodeValue -is [System.Int64] -or $versionCodeValue -is [System.UInt64]
    if (-not $isJsonInteger -or $versionCodeValue -lt 1 -or $versionCodeValue -gt [int32]::MaxValue) {
        Stop-Launcher "Mobile app.json versionCode must be a positive 32-bit JSON integer: $SourceLabel"
    }
    $package = [string]$Expo.android.package
    if ($package -notmatch '^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$') {
        Stop-Launcher "Mobile app.json has an invalid Android package: $SourceLabel"
    }
    return $Expo
}

function Get-AppExpo([string]$GitExe, [string]$Root, [string]$Revision) {
    $spec = "{0}:mobile/app.json" -f $Revision
    $raw = Invoke-Git $GitExe $Root @("show", $spec)
    try { $expo = ($raw | ConvertFrom-Json).expo } catch {
        Stop-Launcher "mobile/app.json at $Revision is invalid"
    }
    return Assert-MobileExpo $expo $Revision
}

function Get-AppExpoFromFile([string]$Root) {
    $path = Join-Path $Root "mobile\app.json"
    try {
        $expo = ((Get-Content -LiteralPath $path -Raw -Encoding UTF8) | ConvertFrom-Json).expo
    } catch {
        Stop-Launcher "Snapshot mobile/app.json is invalid"
    }
    return Assert-MobileExpo $expo $path
}

function Select-AutomaticBase([string]$GitExe, [string]$Root, [string]$Target) {
    $targetExpo = Get-AppExpo $GitExe $Root $Target
    try {
        $targetVersion = [version]$targetExpo.version
        $targetCode = [int]$targetExpo.android.versionCode
    } catch {
        Stop-Launcher "Snapshot mobile/app.json has an invalid release version"
    }
    $revisions = (Invoke-Git $GitExe $Root @("rev-list", "--first-parent", $Target)).Split([Environment]::NewLine, [StringSplitOptions]::RemoveEmptyEntries)
    foreach ($revision in @($revisions | Select-Object -Skip 1)) {
        try {
            $expo = Get-AppExpo $GitExe $Root $revision
            $version = [version]$expo.version
            $code = [int]$expo.android.versionCode
        } catch {
            Stop-Launcher "Could not inspect the first-parent Mobile release base candidate $revision"
        }
        if ($version -eq $targetVersion -and $code -eq $targetCode) {
            continue
        }
        if ($version -lt $targetVersion -and $code -lt $targetCode) {
            return $revision.ToLowerInvariant()
        }
        Stop-Launcher "The first-parent Mobile release bump is incomplete; set AOITALK_MOBILE_RELEASE_BASE explicitly"
    }
    Stop-Launcher "No prior first-parent Mobile version has both a lower version and versionCode; set AOITALK_MOBILE_RELEASE_BASE explicitly"
}

function Resolve-ReleaseBase([string]$GitExe, [string]$Root, [string]$Target, [string]$Requested, [bool]$QaBuild) {
    if ($QaBuild -and [string]::IsNullOrWhiteSpace($Requested)) {
        Stop-Launcher "QA branch-build requires an explicit AOITALK_MOBILE_RELEASE_BASE"
    }
    $candidate = if ([string]::IsNullOrWhiteSpace($Requested)) {
        Select-AutomaticBase $GitExe $Root $Target
    } else {
        (Invoke-Git $GitExe $Root @("rev-parse", "--verify", ("{0}^{{commit}}" -f $Requested))).Trim().ToLowerInvariant()
    }
    if ($candidate -notmatch $script:Sha40Pattern -or $candidate.Equals($Target, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Launcher "Release gate base must be a different complete commit SHA"
    }
    $ancestorCode = Invoke-GitUnchecked $GitExe $Root @("merge-base", "--is-ancestor", $candidate, $Target)
    if ($ancestorCode -ne 0) {
        Stop-Launcher "Release gate base is not an ancestor of the fixed snapshot target"
    }
    return $candidate
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Ensure-Directory([string]$Path) {
    [void][System.IO.Directory]::CreateDirectory($Path)
}

function Get-JsonSha256([string]$Path) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $digest = $algorithm.ComputeHash($stream)
        return ([System.BitConverter]::ToString($digest).Replace('-', '')).ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Resolve-ReparseTarget([System.IO.FileSystemInfo]$Item) {
    try {
        $resolved = @(Resolve-Path -LiteralPath $Item.FullName -ErrorAction Stop)
        if ($resolved.Count -ne 1) { throw "reparse target is ambiguous" }
        return Full-Path ([string]$resolved[0].Path)
    } catch {
        Stop-Launcher "Could not resolve owned reparse target: $($Item.FullName)"
    }
}

function Remove-OwnedTree([string]$Path) {
    $root = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $root) { return }
    if (($root.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Stop-Launcher "Refusing to remove an owned root reparse point: $Path"
    }
    if (-not $root.PSIsContainer) {
        Remove-Item -LiteralPath $root.FullName -Force -ErrorAction Stop
        return
    }
    $stack = [System.Collections.Generic.Stack[psobject]]::new()
    $stack.Push([pscustomobject]@{ Directory = [System.IO.DirectoryInfo]::new($root.FullName); Exit = $false })
    $ownedRoot = Full-Path $Path
    while ($stack.Count -gt 0) {
        $entry = $stack.Pop()
        $directory = $entry.Directory
        if ($entry.Exit) {
            Remove-Item -LiteralPath $directory.FullName -Force -ErrorAction Stop
            continue
        }
        $stack.Push([pscustomobject]@{ Directory = $directory; Exit = $true })
        foreach ($item in @(Get-ChildItem -LiteralPath $directory.FullName -Force -ErrorAction Stop)) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                $target = Resolve-ReparseTarget $item
                if (-not (Is-Under $target $ownedRoot)) {
                    Stop-Launcher "Owned cleanup encountered a reparse point outside its root: $($item.FullName)"
                }
                Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
                continue
            }
            if ($item.PSIsContainer) {
                $stack.Push([pscustomobject]@{ Directory = [System.IO.DirectoryInfo]::new($item.FullName); Exit = $false })
            } else {
                Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
            }
        }
    }
}

function Stop-OwnedProcesses {
    $errors = [System.Collections.Generic.List[string]]::new()
    foreach ($owned in @($script:OwnedProcesses.ToArray())) {
        try {
            if ($owned.Owner -ne $script:OwnerIdentity -or [string]::IsNullOrWhiteSpace($owned.GroupId)) {
                [void]$errors.Add("owned process group identity is invalid for PID $($owned.Pid)")
                continue
            }
            if ($owned.TreeFallback) {
                foreach ($processError in @(Stop-ProcessTreeFallback $owned)) {
                    [void]$errors.Add([string]$processError)
                }
            } elseif ($owned.Job -ne [IntPtr]::Zero) {
                [AoiTalkProcessJob]::CloseKillOnCloseJob($owned.Job)
                $owned.Job = [IntPtr]::Zero
            }
            $process = $owned.Process
            if ($null -ne $process) {
                if (-not $process.HasExited) {
                    $process.WaitForExit(5000)
                    if (-not $process.HasExited) {
                        $process.Kill()
                        $process.WaitForExit()
                    }
                }
                $process.Dispose()
            }
            [void]$script:OwnedProcesses.Remove($owned)
        } catch {
            [void]$errors.Add("owned process group PID $($owned.Pid): $($_.Exception.Message)")
        }
    }
    return $errors
}

function Assert-WorkspaceIdentity([psobject]$Workspace) {
    Assert-RegularFile $Workspace.MarkerPath
    try {
        $marker = Get-Content -LiteralPath $Workspace.MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Stop-Launcher "Owned launcher marker is not valid JSON"
    }
    $pairs = @(
        @($marker.creator, $script:OwnerIdentity),
        @($marker.repository_root, $Workspace.RepositoryRoot),
        @($marker.local_root, $Workspace.LocalRoot),
        @($marker.temp_root, $Workspace.Temp),
        @($marker.gradle_home, $Workspace.Gradle),
        @($marker.npm_cache, $Workspace.Npm),
        @($marker.snapshot_root, $Workspace.Snapshot),
        @($marker.lock_path, $Workspace.LockPath),
        @($marker.owner_marker, $Workspace.MarkerPath),
        @($marker.nonce, $Workspace.Nonce)
    )
    foreach ($pair in $pairs) {
        if ([string]$pair[0] -ne [string]$pair[1]) {
            Stop-Launcher "Owned launcher marker path or nonce does not match the workspace"
        }
    }
    if ($env:AOITALK_RELEASE_OWNER_NONCE -and $env:AOITALK_RELEASE_OWNER_NONCE -ne $Workspace.Nonce) {
        Stop-Launcher "Owned launcher nonce differs from the active workspace"
    }
    if ($env:AOITALK_RELEASE_LOCK_PATH -and (Full-Path $env:AOITALK_RELEASE_LOCK_PATH) -ne $Workspace.LockPath) {
        Stop-Launcher "Owned launcher lock path differs from the active workspace"
    }
    $lockItem = Get-Item -LiteralPath $Workspace.LockPath -Force -ErrorAction SilentlyContinue
    if ($null -eq $lockItem -or $lockItem.PSIsContainer -or ($lockItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Stop-Launcher "Owned launcher lock is missing or unsafe"
    }
    return $marker
}

function New-Workspace([string]$RepositoryRoot) {
    $localRoot = Join-Path $RepositoryRoot ".local"
    Assert-NoReparseAncestors $localRoot
    if (-not (Test-Path -LiteralPath $localRoot -PathType Container)) {
        [void][System.IO.Directory]::CreateDirectory($localRoot)
    }
    $roles = [ordered]@{
        Temp = Join-Path $localRoot "at"
        Gradle = Join-Path $localRoot "ag"
        Npm = Join-Path $localRoot "an"
        Snapshot = Join-Path $localRoot "as"
    }
    $lockPath = Join-Path $localRoot "at.lock"
    foreach ($path in @($roles.Values)) {
        Assert-SafeOwnedPath $path $localRoot $RepositoryRoot
        if ($null -ne (Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue)) {
            Stop-Launcher "Owned Mobile launcher path already exists; refusing to remove an unrelated path: $path"
        }
    }
    Assert-SafeOwnedPath $lockPath $localRoot $RepositoryRoot
    $lock = $null
    try {
        $lock = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    } catch {
        Stop-Launcher "Another Mobile launcher owns the serialized .local workspace"
    }
    $created = [System.Collections.Generic.List[string]]::new()
    try {
        foreach ($path in @($roles.Values)) {
            [void][System.IO.Directory]::CreateDirectory($path)
            [void]$created.Add($path)
            $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                Stop-Launcher "Owned launcher directory became a reparse point: $path"
            }
        }
        $markerPath = Join-Path $roles.Temp ".aoitalk-launcher-owner.json"
        $nonce = [Guid]::NewGuid().ToString("N")
        $marker = [ordered]@{
            creator = $script:OwnerIdentity
            parent_pid = $PID
            repository_root = Full-Path $RepositoryRoot
            local_root = Full-Path $localRoot
            temp_root = Full-Path $roles.Temp
            gradle_home = Full-Path $roles.Gradle
            npm_cache = Full-Path $roles.Npm
            snapshot_root = Full-Path $roles.Snapshot
            lock_path = Full-Path $lockPath
            owner_marker = Full-Path $markerPath
            nonce = $nonce
            cleanup = "exact-owned-roots-only"
        }
        Write-Utf8NoBom $markerPath (($marker | ConvertTo-Json -Depth 4) + [Environment]::NewLine)
        $env:TEMP = Full-Path $roles.Temp
        $env:TMP = Full-Path $roles.Temp
        $script:JobTrackingReady = $true
        return [pscustomobject]@{
            RepositoryRoot = Full-Path $RepositoryRoot
            LocalRoot = Full-Path $localRoot
            Temp = Full-Path $roles.Temp
            Gradle = Full-Path $roles.Gradle
            Npm = Full-Path $roles.Npm
            Snapshot = Full-Path $roles.Snapshot
            LockPath = Full-Path $lockPath
            Lock = $lock
            MarkerPath = Full-Path $markerPath
            Nonce = $nonce
            Created = $created
        }
    } catch {
        $createdPaths = @($created.ToArray())
        [Array]::Reverse($createdPaths)
        foreach ($path in $createdPaths) {
            try { Remove-OwnedTree $path } catch { }
        }
        $script:JobTrackingReady = $false
        if ($null -ne $lock) { $lock.Dispose() }
        $lockItem = Get-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
        if ($null -ne $lockItem) { Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue }
        throw
    }
}

function Remove-Workspace([psobject]$Workspace) {
    $cleanupErrors = [System.Collections.Generic.List[string]]::new()
    try {
        foreach ($processError in @(Stop-OwnedProcesses)) {
            [void]$cleanupErrors.Add([string]$processError)
        }
    } catch {
        [void]$cleanupErrors.Add("owned child process cleanup: $($_.Exception.Message)")
    }
    try { [void](Assert-WorkspaceIdentity $Workspace) } catch { [void]$cleanupErrors.Add($_.Exception.Message) }
    if ($cleanupErrors.Count -gt 0) {
        Stop-Launcher ("Owned Mobile launcher cleanup refused: " + ($cleanupErrors -join "; "))
    }
    foreach ($path in @($Workspace.Snapshot, $Workspace.Npm, $Workspace.Gradle, $Workspace.Temp)) {
        try {
            Remove-OwnedTree $path
        } catch {
            [void]$cleanupErrors.Add("${path}: $($_.Exception.Message)")
        }
    }
    try {
        if ($null -ne $Workspace.Lock) { $Workspace.Lock.Dispose() }
    } catch {
        [void]$cleanupErrors.Add("$($Workspace.LockPath): $($_.Exception.Message)")
    }
    try {
        if ($null -ne (Get-Item -LiteralPath $Workspace.LockPath -Force -ErrorAction SilentlyContinue)) {
            Remove-Item -LiteralPath $Workspace.LockPath -Force -ErrorAction Stop
        }
    } catch {
        [void]$cleanupErrors.Add("$($Workspace.LockPath): $($_.Exception.Message)")
    }
    if ($cleanupErrors.Count -gt 0) {
        Stop-Launcher ("Owned Mobile launcher cleanup failed: " + ($cleanupErrors -join "; "))
    }
}

function Prepare-Snapshot([psobject]$Workspace, [string]$RepositoryRoot, [string]$GitExe, [string]$Origin, [bool]$QaBuild, [string]$QaBranch, [string]$QaRevision) {
    $targetBranch = if ($QaBuild) { $QaBranch } else { "main" }
    if ($QaBuild) {
        if ([string]::IsNullOrWhiteSpace($QaBranch) -or $QaBranch -eq "main") {
            Stop-Launcher "QA candidate branch must be explicit and must not be main"
        }
        $branchCheck = Invoke-Process $GitExe @("check-ref-format", "--branch", $QaBranch)
        if ($branchCheck.ExitCode -ne 0) { Stop-Launcher "QA candidate branch name is invalid" }
        if ($QaRevision -notmatch $script:Sha40Pattern) { Stop-Launcher "QA source revision must be a complete SHA" }
    }
    $cloneArguments = @("clone", "--quiet", "--no-checkout", "--no-tags", "--no-local", "--dissociate", "--single-branch", "--branch", "main")
    if (Test-Path -LiteralPath $RepositoryRoot -PathType Container) {
        $cloneArguments += @("--reference-if-able", $RepositoryRoot)
    }
    if (Test-Path -LiteralPath $Origin -PathType Container) {
        $cloneArguments += @("--reference-if-able", $Origin)
    }
    $cloneArguments += @($Origin, $Workspace.Snapshot)
    Invoke-Checked $GitExe $cloneArguments
    $alternates = Join-Path $Workspace.Snapshot ".git\objects\info\alternates"
    if ($null -ne (Get-Item -LiteralPath $alternates -Force -ErrorAction SilentlyContinue)) {
        Stop-Launcher "Fetched snapshot retained an object alternates file"
    }
    Invoke-Checked $GitExe @("-C", $Workspace.Snapshot, "fetch", "--quiet", "--force", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    $mainSha = (Invoke-Git $GitExe $Workspace.Snapshot @("rev-parse", "--verify", "refs/remotes/origin/main")).Trim().ToLowerInvariant()
    if ($mainSha -notmatch $script:Sha40Pattern) { Stop-Launcher "Fetched origin/main did not resolve to a complete SHA" }
    if ($QaBuild) {
        Invoke-Checked $GitExe @("-C", $Workspace.Snapshot, "fetch", "--quiet", "--force", "--no-tags", "origin", ("+refs/heads/{0}:refs/remotes/origin/{0}" -f $QaBranch))
        $candidateSha = (Invoke-Git $GitExe $Workspace.Snapshot @("rev-parse", "--verify", ("refs/remotes/origin/{0}" -f $QaBranch))).Trim().ToLowerInvariant()
        if ($candidateSha -ne $QaRevision.ToLowerInvariant()) {
            Stop-Launcher "QA candidate branch does not match the requested source revision"
        }
    }
    $sourceSha = if ($QaBuild) { $QaRevision.ToLowerInvariant() } else { $mainSha }
    Invoke-Checked $GitExe @("-C", $Workspace.Snapshot, "checkout", "--quiet", "--detach", $sourceSha)
    if ($QaBuild) {
        Invoke-Checked $GitExe @("-C", $Workspace.Snapshot, "switch", "--quiet", "--create", $QaBranch, $sourceSha)
    }
    $head = (Invoke-Git $GitExe $Workspace.Snapshot @("rev-parse", "--verify", "HEAD")).Trim().ToLowerInvariant()
    if ($head -ne $sourceSha) { Stop-Launcher "Fetched snapshot HEAD does not match the fixed source SHA" }
    $originMain = (Invoke-Git $GitExe $Workspace.Snapshot @("rev-parse", "--verify", "refs/remotes/origin/main")).Trim().ToLowerInvariant()
    if ($originMain -ne $mainSha) { Stop-Launcher "Fetched snapshot origin/main does not match the actual remote SHA" }
    if ($QaBuild -and ((Invoke-Git $GitExe $Workspace.Snapshot @("branch", "--show-current")).Trim() -ne $QaBranch)) {
        Stop-Launcher "QA snapshot did not retain the explicit candidate branch"
    }
    $helperPath = Join-Path $Workspace.Snapshot "scripts\build_mobile_apk.ps1"
    $batchPath = Join-Path $Workspace.Snapshot "scripts\build_apk.bat"
    Assert-RegularFile $helperPath
    Assert-RegularFile $batchPath
    $helperHash = Get-JsonSha256 $helperPath
    $batchHash = Get-JsonSha256 $batchPath
    $sourceMarker = Join-Path $Workspace.Snapshot ".git\aoitalk-snapshot-owner.json"
    $sourcePayload = [ordered]@{
        creator = $script:OwnerIdentity
        source_sha = $sourceSha
        origin_main_sha = $mainSha
        branch = $targetBranch
        helper_sha256 = $helperHash
        batch_sha256 = $batchHash
        owner_nonce = $Workspace.Nonce
        owner_marker = $Workspace.MarkerPath
        owner_lock = $Workspace.LockPath
        source_root = Full-Path $Workspace.Snapshot
        temp_root = $Workspace.Temp
    }
    Write-Utf8NoBom $sourceMarker (($sourcePayload | ConvertTo-Json -Depth 4) + [Environment]::NewLine)
    [Console]::Out.WriteLine(("[INFO] Mobile snapshot pinned: source={0} origin/main={1}" -f $sourceSha, $mainSha))
    return [pscustomobject]@{
        SourceSha = $sourceSha
        OriginMainSha = $mainSha
        Branch = $targetBranch
        HelperHash = $helperHash
        BatchHash = $batchHash
        MarkerPath = $sourceMarker
    }
}

function Set-ChildBuildEnvironment([psobject]$Workspace, [string]$RepositoryRoot, [string]$GitExe, [psobject]$Snapshot, [string]$PythonPath, [string]$ReleaseBase) {
    if ([string]::IsNullOrWhiteSpace($env:JAVA_HOME)) {
        $microsoftRoot = Join-Path $env:ProgramFiles "Microsoft"
        $javaCandidates = @()
        if (Test-Path -LiteralPath $microsoftRoot -PathType Container) {
            $javaCandidates = @(Get-ChildItem -LiteralPath $microsoftRoot -Directory -Filter "jdk-17*" -ErrorAction SilentlyContinue | Sort-Object -Property Name -Descending)
        }
        if ($javaCandidates.Count -gt 0) {
            $env:JAVA_HOME = Full-Path $javaCandidates[0].FullName
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:JAVA_HOME) -or -not (Test-Path -LiteralPath $env:JAVA_HOME -PathType Container)) {
        Stop-Launcher "JAVA_HOME is not set and a Microsoft JDK 17 installation was not found"
    }
    if ([string]::IsNullOrWhiteSpace($env:ANDROID_HOME)) {
        $env:ANDROID_HOME = Join-Path $env:LOCALAPPDATA "Android\Sdk"
    }
    if ([string]::IsNullOrWhiteSpace($env:ANDROID_SDK_ROOT)) {
        $env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
    }
    $windowsPowerShellModules = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\Modules"
    if (Test-Path -LiteralPath $windowsPowerShellModules -PathType Container) {
        $modulePaths = @($env:PSModulePath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and (Full-Path $_) -ne (Full-Path $windowsPowerShellModules) })
        $env:PSModulePath = $windowsPowerShellModules + ';' + ($modulePaths -join ';')
    }
    $env:AOITALK_RELEASE_OWNER = $script:OwnerIdentity
    $env:AOITALK_RELEASE_OWNER_FILE = $Workspace.MarkerPath
    $env:AOITALK_RELEASE_OWNER_NONCE = $Workspace.Nonce
    $env:AOITALK_RELEASE_LOCK_PATH = $Workspace.LockPath
    $env:AOITALK_RELEASE_LAUNCH_ROOT = $Workspace.RepositoryRoot
    $env:AOITALK_RELEASE_SOURCE_COMMIT = $Snapshot.SourceSha
    $env:AOITALK_RELEASE_ORIGIN_MAIN = $Snapshot.OriginMainSha
    $env:AOITALK_RELEASE_SOURCE_ROOT = Full-Path (Join-Path $Workspace.Snapshot ".")
    $env:AOITALK_RELEASE_TEMP_ROOT = $Workspace.Temp
    $env:AOITALK_RELEASE_GIT_EXE = $GitExe
    $env:AOITALK_RELEASE_PYTHON = $PythonPath
    $env:AOITALK_RELEASE_HELPER_SHA256 = $Snapshot.HelperHash
    $env:AOITALK_RELEASE_BATCH_SHA256 = $Snapshot.BatchHash
    $env:AOITALK_BUILD_TEMP_ROOT = $Workspace.Temp
    $env:AOITALK_TEMP_BASE = $Workspace.Temp
    $env:TEMP = $Workspace.Temp
    $env:TMP = $Workspace.Temp
    $env:GRADLE_USER_HOME = $Workspace.Gradle
    $env:npm_config_cache = $Workspace.Npm
    $env:NPM_CONFIG_CACHE = $Workspace.Npm
    $env:AOITALK_MOBILE_RELEASE_BASE = $ReleaseBase
    $env:AOITALK_BUILD_ONLY = "1"
    $env:RELEASE_TARGET = "main"
    if ([string]::IsNullOrWhiteSpace($env:REACT_NATIVE_ARCHITECTURES)) {
        $env:REACT_NATIVE_ARCHITECTURES = "arm64-v8a"
    }
    $env:NODE_ENV = "production"
    $jdkTmp = (Full-Path $Workspace.Temp).Replace('\', '/')
    $jdkTmpOption = '-Djdk.net.unixdomain.tmpdir="' + $jdkTmp + '"'
    $env:_JAVA_OPTIONS = $jdkTmpOption
    $env:GRADLE_OPTS = $jdkTmpOption
}

function Assert-InternalIdentity([string]$SourceRoot, [string]$GitExe) {
    if ($env:AOITALK_RELEASE_OWNER -ne $script:OwnerIdentity) { Stop-Launcher "Internal snapshot owner marker is missing" }
    if ([string]::IsNullOrWhiteSpace($env:AOITALK_RELEASE_SOURCE_COMMIT) -or $env:AOITALK_RELEASE_SOURCE_COMMIT -notmatch $script:Sha40Pattern) {
        Stop-Launcher "Internal snapshot source SHA is invalid"
    }
    $head = (Invoke-Git $GitExe $SourceRoot @("rev-parse", "--verify", "HEAD")).Trim().ToLowerInvariant()
    if ($head -ne $env:AOITALK_RELEASE_SOURCE_COMMIT.ToLowerInvariant()) { Stop-Launcher "Internal helper is not running at the fixed snapshot HEAD" }
    if ([string]::IsNullOrWhiteSpace($env:AOITALK_RELEASE_SOURCE_ROOT) -or (Full-Path $env:AOITALK_RELEASE_SOURCE_ROOT) -ne (Full-Path $SourceRoot)) {
        Stop-Launcher "Internal snapshot source root does not match the owner boundary"
    }
    $ownerMarkerPath = Full-Path ([string]$env:AOITALK_RELEASE_OWNER_FILE)
    Assert-RegularFile $ownerMarkerPath
    try { $ownerMarker = Get-Content -LiteralPath $ownerMarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { Stop-Launcher "Internal workspace owner marker is invalid" }
    foreach ($pair in @(
        @($ownerMarker.creator, $script:OwnerIdentity),
        @($ownerMarker.repository_root, $env:AOITALK_RELEASE_LAUNCH_ROOT),
        @($ownerMarker.temp_root, $env:AOITALK_RELEASE_TEMP_ROOT),
        @($ownerMarker.owner_marker, $ownerMarkerPath),
        @($ownerMarker.lock_path, $env:AOITALK_RELEASE_LOCK_PATH),
        @($ownerMarker.nonce, $env:AOITALK_RELEASE_OWNER_NONCE)
    )) {
        if ([string]$pair[0] -ne [string]$pair[1]) { Stop-Launcher "Internal workspace owner identity does not match" }
    }
    $lockItem = Get-Item -LiteralPath $env:AOITALK_RELEASE_LOCK_PATH -Force -ErrorAction SilentlyContinue
    if ($null -eq $lockItem -or $lockItem.PSIsContainer -or ($lockItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Stop-Launcher "Internal workspace lock is missing or unsafe"
    }
    foreach ($pathPair in @(
        @($env:AOITALK_BUILD_TEMP_ROOT, $ownerMarker.temp_root),
        @($env:GRADLE_USER_HOME, $ownerMarker.gradle_home),
        @($env:npm_config_cache, $ownerMarker.npm_cache)
    )) {
        if ([string]::IsNullOrWhiteSpace([string]$pathPair[0]) -or (Full-Path $pathPair[0]) -ne (Full-Path $pathPair[1])) {
            Stop-Launcher "Internal build workspace path does not match the owner marker"
        }
    }
    $expectedPython = Full-Path (Join-Path ([string]$ownerMarker.repository_root) "venv\Scripts\python.exe")
    if ([string]::IsNullOrWhiteSpace($env:AOITALK_RELEASE_PYTHON) -or (Full-Path $env:AOITALK_RELEASE_PYTHON) -ne $expectedPython) {
        Stop-Launcher "Internal build must reuse the owning repository venv"
    }
    Assert-RegularFile $expectedPython
    $markerPath = Join-Path $SourceRoot ".git\aoitalk-snapshot-owner.json"
    Assert-RegularFile $markerPath
    $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($marker.creator -ne $script:OwnerIdentity -or $marker.source_sha -ne $head -or
        $marker.helper_sha256 -ne (Get-JsonSha256 (Join-Path $SourceRoot "scripts\build_mobile_apk.ps1")) -or
        $marker.batch_sha256 -ne (Get-JsonSha256 (Join-Path $SourceRoot "scripts\build_apk.bat")) -or
        $marker.owner_nonce -ne $env:AOITALK_RELEASE_OWNER_NONCE -or
        (Full-Path $marker.source_root) -ne (Full-Path $SourceRoot) -or
        (Full-Path $marker.temp_root) -ne (Full-Path $env:AOITALK_RELEASE_TEMP_ROOT) -or
        (Full-Path $marker.owner_marker) -ne $ownerMarkerPath -or
        (Full-Path $marker.owner_lock) -ne (Full-Path $env:AOITALK_RELEASE_LOCK_PATH)) {
        Stop-Launcher "Internal helper identity does not match the fixed snapshot owner marker"
    }
    if ($env:AOITALK_RELEASE_HELPER_SHA256 -ne $marker.helper_sha256 -or $env:AOITALK_RELEASE_BATCH_SHA256 -ne $marker.batch_sha256) {
        Stop-Launcher "Internal helper identity differs from the outer snapshot record"
    }
}

function Invoke-CanonicalPython([string]$PythonPath, [string]$ScriptPath, [string[]]$Arguments) {
    Assert-RegularFile $PythonPath
    Invoke-Checked $PythonPath (@($ScriptPath) + $Arguments)
}

function Invoke-StageRuntime([string]$PythonPath, [string]$SourceRoot, [string]$BuildRoot) {
    $scriptPath = Join-Path $SourceRoot "scripts\stage_mobile_runtime.py"
    Invoke-CanonicalPython $PythonPath $scriptPath @("--source-root", (Join-Path $SourceRoot "resources\seethrough-rig\runtime"), "--build-root", $BuildRoot)
}

function Invoke-StageChatBackground([string]$PythonPath, [string]$SourceRoot, [string]$BuildRoot) {
    $scriptPath = Join-Path $SourceRoot "scripts\stage_chat_background.py"
    Invoke-CanonicalPython $PythonPath $scriptPath @("--source-root", (Join-Path $SourceRoot "resources\chat-background"), "--build-root", $BuildRoot)
}

function Replace-TextFile([string]$Path, [scriptblock]$Transform) {
    Assert-RegularFile $Path
    $content = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    $newContent = & $Transform $content
    Write-Utf8NoBom $Path ([string]$newContent)
}

function Invoke-NativeSnapshotBuild([string]$SourceRoot, [psobject]$Workspace, [string]$PythonPath, [string]$GitExe, [psobject]$Snapshot) {
    Assert-InternalIdentity $SourceRoot $GitExe
    $mobileDir = Join-Path $SourceRoot "mobile"
    $buildRoot = Join-Path $Workspace.Temp "m"
    $androidDir = Join-Path $buildRoot "android"
    $stage = Join-Path $Workspace.Temp "k"
    $apkName = $script:ApkName
    $expo = Get-AppExpoFromFile $SourceRoot
    $version = [string]$expo.version
    $versionCode = [string]$expo.android.versionCode
    $artifactDir = Join-Path $SourceRoot ("artifacts\releases\mobile\v{0}" -f $version)
    $apkPath = Join-Path $artifactDir $apkName
    [Console]::Out.WriteLine(("[INFO] Mobile APK stage: version={0} versionCode={1}" -f $version, $versionCode))
    if (-not (Test-Path -LiteralPath $artifactDir -PathType Container)) { Ensure-Directory $artifactDir }
    Invoke-CanonicalPython $PythonPath (Join-Path $SourceRoot "scripts\mobile_release_manifest.py") @("preflight", "--project-root", $SourceRoot, "--apk-path", (Join-Path $artifactDir "__preflight__.apk"))
    Invoke-CanonicalPython $PythonPath (Join-Path $SourceRoot "scripts\mobile_release_manifest.py") @("invalidate", "--project-root", $SourceRoot, "--apk-path", $apkPath)

    $robocopy = Resolve-TrustedTool "robocopy" $SourceRoot
    $exclude = Join-Path $mobileDir "modules\apk-installer\android\build"
    [Console]::Out.WriteLine("[INFO] Mobile APK stage: copy source workspace")
    $copyResponse = Invoke-Process $robocopy @($mobileDir, $buildRoot, "/E", "/XD", "node_modules", ".expo", $exclude) -LiveOutput
    Write-ProcessOutput $copyResponse
    $copyCode = $copyResponse.ExitCode
    if ($copyCode -ge 8) { Stop-Launcher "Mobile workspace copy failed with robocopy exit code $copyCode" }
    Assert-RegularFile (Join-Path $buildRoot "modules\apk-installer\android\src\main\java\expo\modules\apkinstaller\ApkInstallerModule.kt")
    Invoke-StageRuntime $PythonPath $SourceRoot $Workspace.Temp
    Invoke-StageChatBackground $PythonPath $SourceRoot $Workspace.Temp

    $npm = Resolve-TrustedTool "npm" $SourceRoot
    $npx = Resolve-TrustedTool "npx" $SourceRoot
    [Console]::Out.WriteLine("[INFO] Mobile APK stage: install dependencies and generate Android project")
    Invoke-Checked $npm @("ci") $buildRoot -LiveOutput
    Invoke-Checked $npx @("expo", "prebuild", "--platform", "android", "--clean") $buildRoot -LiveOutput

    if ($null -ne (Get-Item -LiteralPath $stage -Force -ErrorAction SilentlyContinue)) { Remove-OwnedTree $stage }
    Ensure-Directory $stage
    $autolinking = Join-Path $buildRoot "node_modules\@react-native\gradle-plugin\react-native-gradle-plugin\src\main\kotlin\com\facebook\react\tasks\GenerateAutolinkingNewArchitecturesFileTask.kt"
    Replace-TextFile $autolinking { param($content) $content.Replace('${libraryName}_autolinked_build', '${libraryName.take(12)}_autolinked_build') }

    $keyboard = Join-Path $buildRoot "node_modules\react-native-keyboard-controller\android\src\main\jni\CMakeLists.txt"
    Replace-TextFile $keyboard {
        param($content)
        $needle = 'file(GLOB LIB_CODEGEN_SRCS CONFIGURE_DEPENDS ${LIB_ANDROID_GENERATED_JNI_DIR}/*.cpp ${LIB_ANDROID_GENERATED_COMPONENTS_DIR}/*.cpp)'
        if (-not $content.Contains($needle)) { Stop-Launcher "Keyboard-controller CMake marker was not found" }
        $stageUnix = $stage.Replace('\', '/')
        $nl = [Environment]::NewLine
        $q = [char]34
        $block = $needle + $nl + $nl + 'file(GLOB LIB_CUSTOM_HEADERS CONFIGURE_DEPENDS ${LIB_COMMON_COMPONENTS_DIR}/*.h)' + $nl + 'file(GLOB LIB_CODEGEN_HEADERS CONFIGURE_DEPENDS ${LIB_ANDROID_GENERATED_JNI_DIR}/*.h ${LIB_ANDROID_GENERATED_COMPONENTS_DIR}/*.h)' + $nl + ('file(MAKE_DIRECTORY ' + $q + $stageUnix + '/custom' + $q + ' ' + $q + $stageUnix + '/codegen' + $q + ')') + $nl + ('file(COPY ${LIB_CUSTOM_SRCS} ${LIB_CUSTOM_HEADERS} DESTINATION ' + $q + $stageUnix + '/custom' + $q + ')') + $nl + ('file(COPY ${LIB_CODEGEN_SRCS} ${LIB_CODEGEN_HEADERS} DESTINATION ' + $q + $stageUnix + '/codegen' + $q + ')') + $nl + ('file(GLOB LIB_CUSTOM_SRCS CONFIGURE_DEPENDS ' + $q + $stageUnix + '/custom/*.cpp' + $q + ')') + $nl + ('file(GLOB LIB_CODEGEN_SRCS CONFIGURE_DEPENDS ' + $q + $stageUnix + '/codegen/*.cpp' + $q + ')')
        return $content.Replace($needle, $block)
    }

    $nodeModules = Join-Path $buildRoot "node_modules"
    foreach ($target in @(
        @((Join-Path $nodeModules "react-native-safe-area-context\android\src\main\jni\CMakeLists.txt"), "safeareacontext"),
        @((Join-Path $nodeModules "react-native-screens\android\src\main\jni\CMakeLists.txt"), "rnscreens")
    )) {
        $path = [string]$target[0]
        $label = [string]$target[1]
        Replace-TextFile $path {
            param($content)
            $needle = if ($label -eq "rnscreens") { 'file(GLOB LIB_CODEGEN_SRCS CONFIGURE_DEPENDS ${LIB_ANDROID_GENERATED_COMPONENTS_DIR}/*.cpp)' } else { 'file(GLOB LIB_CODEGEN_SRCS CONFIGURE_DEPENDS ${LIB_ANDROID_GENERATED_JNI_DIR}/*.cpp ${LIB_ANDROID_GENERATED_COMPONENTS_DIR}/*.cpp)' }
            if (-not $content.Contains($needle)) { Stop-Launcher "Codegen CMake marker was not found: $path" }
            $stageUnix = $stage.Replace('\', '/')
            $custom = $stageUnix + '/' + $label + '-custom'
            $codegen = $stageUnix + '/' + $label + '-codegen'
            $nl = [Environment]::NewLine
            $q = [char]34
            $block = $needle + $nl + $nl + 'file(GLOB LIB_CUSTOM_HEADERS CONFIGURE_DEPENDS *.h ${LIB_COMMON_DIR}/react/renderer/components/${LIB_LITERAL}/*.h ${LIB_COMMON_DIR}/react/renderer/components/${LIB_LITERAL}/utils/*.h)' + $nl + 'file(GLOB LIB_CODEGEN_HEADERS CONFIGURE_DEPENDS ${LIB_ANDROID_GENERATED_JNI_DIR}/*.h ${LIB_ANDROID_GENERATED_COMPONENTS_DIR}/*.h)' + $nl + ('file(MAKE_DIRECTORY ' + $q + $custom + $q + ' ' + $q + $codegen + $q + ')') + $nl + ('file(COPY ${LIB_CUSTOM_SRCS} ${LIB_CUSTOM_HEADERS} DESTINATION ' + $q + $custom + $q + ')') + $nl + ('file(GLOB LIB_CUSTOM_SRCS CONFIGURE_DEPENDS ' + $q + $custom + '/*.cpp' + $q + ')') + $nl + ('include_directories(' + $q + $custom + $q + ')') + $nl + ('file(COPY ${LIB_CODEGEN_SRCS} ${LIB_CODEGEN_HEADERS} DESTINATION ' + $q + $codegen + $q + ')') + $nl + ('file(GLOB LIB_CODEGEN_SRCS CONFIGURE_DEPENDS ' + $q + $codegen + '/*.cpp' + $q + ')')
            return $content.Replace($needle, $block)
        }
    }

    $gradleProperties = Join-Path $androidDir "gradle.properties"
    Replace-TextFile $gradleProperties {
        param($content)
        $jdkTmp = (Full-Path $Workspace.Temp).Replace('\', '/')
        $content = [regex]::Replace($content, '(?m)^org\.gradle\.jvmargs=.*$', ('org.gradle.jvmargs=-Xmx4g -Djdk.net.unixdomain.tmpdir="' + $jdkTmp + '"'))
        $line = 'android.packagingOptions.pickFirsts=**/libreactnative.so'
        if ($content -notmatch '(?m)^android\.packagingOptions\.pickFirsts=\*\*/libreactnative\.so$') {
            if (-not $content.EndsWith([Environment]::NewLine)) { $content += [Environment]::NewLine }
            $content += $line + [Environment]::NewLine
        }
        return $content
    }
    $appGradle = Join-Path $androidDir "app\build.gradle"
    Replace-TextFile $appGradle {
        param($content)
        $content += [Environment]::NewLine + ('android.defaultConfig.externalNativeBuild.cmake.arguments.add(' + [char]34 + '-DCMAKE_OBJECT_PATH_MAX=128' + [char]34 + ')') + [Environment]::NewLine
        return [regex]::Replace($content, '(?m)^\s*cliFile = new File\(\["node", "--print", "require\.resolve\(''@expo/cli'', \{ paths: \[require\.resolve\(''expo/package\.json''\) \]\}\)"\]\.execute\(null, rootDir\)\.text\.trim\(\)\)$', '    cliFile = new File(rootDir, "../node_modules/expo/node_modules/@expo/cli/build/bin/cli")')
    }
    $wrapper = Join-Path $androidDir "gradle\wrapper\gradle-wrapper.properties"
    Replace-TextFile $wrapper {
        param($content)
        $matches = [regex]::Matches($content, '(?m)^distributionUrl=(?<url>[^\r\n]+)\r?$')
        $expected = 'https\://services.gradle.org/distributions/gradle-8.14.3-bin.zip'
        if ($matches.Count -ne 1 -or $matches[0].Groups['url'].Value -cne $expected) { Stop-Launcher "Unexpected generated Gradle wrapper distribution; refusing an unreviewed upgrade" }
        $content = [regex]::Replace($content, '(?m)^distributionSha256Sum=[^\r\n]*(?:\r?\n|$)', '')
        $content = [regex]::Replace($content, '(?m)^distributionUrl=[^\r\n]+', 'distributionUrl=https\://services.gradle.org/distributions/gradle-9.1.0-bin.zip')
        $newline = if ($content.Contains(([string][char]13) + ([string][char]10))) { ([string][char]13) + ([string][char]10) } else { [string][char]10 }
        $content = [regex]::Replace($content, '[\r\n]*$', '') + $newline
        return $content + 'distributionSha256Sum=a17ddd85a26b6a7f5ddb71ff8b05fc5104c0202c6e64782429790c933686c806' + $newline
    }

    $gradlew = Join-Path $androidDir "gradlew.bat"
    $arch = $env:REACT_NATIVE_ARCHITECTURES
    [Console]::Out.WriteLine(("[INFO] Mobile APK stage: Gradle assembleRelease architectures={0}" -f $arch))
    Invoke-Checked $gradlew @("assembleRelease", "--stacktrace", "--no-daemon", "--max-workers=1", "-Pkotlin.incremental=false", "-PreactNativeArchitectures=$arch") $androidDir -LiveOutput
    $builtApk = Join-Path $androidDir "app\build\outputs\apk\release\app-release.apk"
    Assert-RegularFile $builtApk
    $powershell = Resolve-TrustedTool "powershell" $SourceRoot
    Invoke-Checked $powershell @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $SourceRoot "scripts\verify_mobile_apk.ps1"), "-ApkPath", $builtApk, "-ExpectedVersion", $version, "-ExpectedVersionCode", $versionCode)
    if (-not (Test-Path -LiteralPath $artifactDir -PathType Container)) { Ensure-Directory $artifactDir }
    Copy-Item -LiteralPath $builtApk -Destination $apkPath -Force
    Invoke-Checked $powershell @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $SourceRoot "scripts\verify_mobile_apk.ps1"), "-ApkPath", $apkPath, "-ExpectedVersion", $version, "-ExpectedVersionCode", $versionCode)
    Invoke-CanonicalPython $PythonPath (Join-Path $SourceRoot "scripts\mobile_release_manifest.py") @("write", "--project-root", $SourceRoot, "--apk-path", $apkPath)
    return [pscustomobject]@{ Version = $version; VersionCode = [int]$versionCode; ApkPath = $apkPath; ManifestPath = "$apkPath.manifest.json"; QaPath = "$apkPath.qa-approved.json" }
}

function Read-ArtifactManifest([string]$Path, [string]$SourceRoot, [string]$SourceSha) {
    Assert-RegularFile $Path
    try { $manifest = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json } catch { Stop-Launcher "Artifact manifest is not valid JSON: $Path" }
    if ($manifest.artifact.path -ne ((Resolve-Path -LiteralPath $Path).Path.Substring((Full-Path $SourceRoot).Length + 1).Replace('\', '/').Replace('.manifest.json', ''))) { Stop-Launcher "Artifact manifest path does not match the snapshot artifact" }
    if ($manifest.artifact.sha256 -ne (Get-JsonSha256 ($Path -replace '\.manifest\.json$', ''))) { Stop-Launcher "Artifact manifest SHA-256 does not match the APK" }
    if ($manifest.source.revision -ne $SourceSha) { Stop-Launcher "Artifact manifest source revision does not match the fixed snapshot" }
    return $manifest
}

function Copy-ArtifactTransaction([psobject]$Workspace, [string]$OriginalRoot, [string]$SourceRoot, [string]$Version, [string]$SourceSha, [bool]$QaBuild) {
    $sourceDir = Join-Path $SourceRoot ("artifacts\releases\mobile\v{0}" -f $Version)
    $sourceApk = Join-Path $sourceDir $script:ApkName
    $sourceManifest = "$sourceApk.manifest.json"
    $sourceQa = "$sourceApk.qa-approved.json"
    Assert-RegularFile $sourceApk
    $manifest = Read-ArtifactManifest $sourceManifest $SourceRoot $SourceSha
    if ($QaBuild -and $null -ne (Get-Item -LiteralPath $sourceQa -Force -ErrorAction SilentlyContinue)) { Stop-Launcher "QA snapshot artifact unexpectedly has a release approval marker" }
    $stageDir = Join-Path $Workspace.Temp "out"
    if ($null -ne (Get-Item -LiteralPath $stageDir -Force -ErrorAction SilentlyContinue)) { Remove-OwnedTree $stageDir }
    Ensure-Directory $stageDir
    $stageApk = Join-Path $stageDir $script:ApkName
    $stageManifest = "$stageApk.manifest.json"
    Copy-Item -LiteralPath $sourceApk -Destination $stageApk -Force
    Copy-Item -LiteralPath $sourceManifest -Destination $stageManifest -Force
    if ((Get-JsonSha256 $stageApk) -ne (Get-JsonSha256 $sourceApk) -or (Get-JsonSha256 $stageManifest) -ne (Get-JsonSha256 $sourceManifest)) { Stop-Launcher "Staged artifact hash differs from the verified snapshot artifact" }
    $destinationDir = Join-Path $OriginalRoot ("artifacts\releases\mobile\v{0}" -f $Version)
    Assert-NoReparseAncestors $destinationDir
    if (-not (Test-Path -LiteralPath $destinationDir -PathType Container)) { Ensure-Directory $destinationDir }
    $destApk = Join-Path $destinationDir $script:ApkName
    $destManifest = "$destApk.manifest.json"
    $destQa = "$destApk.qa-approved.json"
    $destFiles = @($destApk, $destManifest, $destQa)
    if ($QaBuild -and $null -ne (Get-Item -LiteralPath $destQa -Force -ErrorAction SilentlyContinue)) { Stop-Launcher "QA artifact cannot coexist with an existing root QA approval marker" }
    foreach ($path in $destFiles) { Assert-RegularFile $path -AllowMissing }
    $backup = Join-Path $stageDir "backup"
    Ensure-Directory $backup
    $had = @{}
    $installed = [System.Collections.Generic.List[string]]::new()
    try {
        foreach ($path in $destFiles) {
            if ($null -ne (Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue)) {
                $name = [System.IO.Path]::GetFileName($path)
                Copy-Item -LiteralPath $path -Destination (Join-Path $backup $name) -Force
                $had[$name] = $true
            }
        }
        foreach ($path in @($destApk, $destManifest, $destQa)) { if ($null -ne (Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $path -Force -ErrorAction Stop } }
        Move-Item -LiteralPath $stageApk -Destination $destApk -Force; [void]$installed.Add($destApk)
        Move-Item -LiteralPath $stageManifest -Destination $destManifest -Force; [void]$installed.Add($destManifest)
        if ((Get-JsonSha256 $destApk) -ne (Get-JsonSha256 $sourceApk) -or (Get-JsonSha256 $destManifest) -ne (Get-JsonSha256 $sourceManifest)) { Stop-Launcher "Final artifact hash differs from the verified snapshot artifact" }
    } catch {
        $rollbackErrors = [System.Collections.Generic.List[string]]::new()
        $installedPaths = @($installed.ToArray())
        [Array]::Reverse($installedPaths)
        foreach ($path in $installedPaths) {
            if ($null -ne (Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue)) {
                try { Remove-Item -LiteralPath $path -Force -ErrorAction Stop } catch { [void]$rollbackErrors.Add("remove partial artifact ${path}: $($_.Exception.Message)") }
            }
        }
        foreach ($path in $destFiles) {
            $name = [System.IO.Path]::GetFileName($path)
            $saved = Join-Path $backup $name
            if ($had.ContainsKey($name) -and $null -ne (Get-Item -LiteralPath $saved -Force -ErrorAction SilentlyContinue)) {
                try { Move-Item -LiteralPath $saved -Destination $path -Force -ErrorAction Stop } catch { [void]$rollbackErrors.Add("restore previous artifact ${path}: $($_.Exception.Message)") }
            }
        }
        if ($rollbackErrors.Count -gt 0) {
            $recovery = Join-Path $destinationDir ("aoitalk-mobile-recovery-{0}" -f $Workspace.Nonce)
            try {
                Assert-NoReparseAncestors $recovery
                if ($null -ne (Get-Item -LiteralPath $recovery -Force -ErrorAction SilentlyContinue)) { Stop-Launcher "Artifact rollback recovery path already exists: $recovery" }
                Ensure-Directory $recovery
                foreach ($path in $destFiles) {
                    $name = [System.IO.Path]::GetFileName($path)
                    $saved = Join-Path $backup $name
                    if ($null -ne (Get-Item -LiteralPath $saved -Force -ErrorAction SilentlyContinue)) {
                        Assert-RegularFile $saved
                        Copy-Item -LiteralPath $saved -Destination (Join-Path $recovery $name) -Force -ErrorAction Stop
                    }
                }
                Write-Utf8NoBom (Join-Path $recovery "README.txt") ("Artifact transaction rollback failed; recovery copies are retained.\r\n" + ($rollbackErrors -join "\r\n") + "\r\n")
            } catch {
                [void]$rollbackErrors.Add("write rollback recovery: $($_.Exception.Message)")
            }
            Stop-Launcher ("Artifact transaction rollback failed; previous outputs were not safely restored: " + ($rollbackErrors -join "; "))
        }
        throw
    }
    return [pscustomobject]@{ ApkPath = $destApk; ManifestPath = $destManifest; QaPath = $destQa }
}

function Invoke-PublishOnly([psobject]$Workspace, [string]$OriginalRoot, [string]$GitExe, [string]$PythonPath, [psobject]$Snapshot) {
    if ($env:AOITALK_BUILD_ONLY -eq "1") { Stop-Launcher "-PublishOnly cannot be combined with AOITALK_BUILD_ONLY=1" }
    if ($env:AOITALK_MOBILE_QA_BUILD -eq "1") { Stop-Launcher "QA branch-build cannot be combined with -PublishOnly" }
    $expo = Get-AppExpo $GitExe $Workspace.Snapshot $Snapshot.SourceSha
    $version = [string]$expo.version
    $sourceDir = Join-Path $Workspace.Snapshot ("artifacts\releases\mobile\v{0}" -f $version)
    Ensure-Directory $sourceDir
    $originalDir = Join-Path $OriginalRoot ("artifacts\releases\mobile\v{0}" -f $version)
    Assert-NoReparseAncestors $originalDir
    $originalApk = Join-Path $originalDir $script:ApkName
    $originalManifest = "$originalApk.manifest.json"
    $originalQa = "$originalApk.qa-approved.json"
    foreach ($path in @($originalApk, $originalManifest, $originalQa)) { Assert-RegularFile $path }
    foreach ($sourcePath in @((Join-Path $sourceDir $script:ApkName), (Join-Path $sourceDir "$script:ApkName.manifest.json"), (Join-Path $sourceDir "$script:ApkName.qa-approved.json"))) {
        if ($null -ne (Get-Item -LiteralPath $sourcePath -Force -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $sourcePath -Force }
    }
    Copy-Item -LiteralPath $originalApk -Destination (Join-Path $sourceDir $script:ApkName) -Force
    Copy-Item -LiteralPath $originalManifest -Destination (Join-Path $sourceDir "$script:ApkName.manifest.json") -Force
    Copy-Item -LiteralPath $originalQa -Destination (Join-Path $sourceDir "$script:ApkName.qa-approved.json") -Force
    Invoke-CanonicalPython $PythonPath (Join-Path $Workspace.Snapshot "scripts\mobile_release_manifest.py") @("validate", "--project-root", $Workspace.Snapshot, "--apk-path", (Join-Path $sourceDir $script:ApkName))
    $gh = Resolve-TrustedTool "gh" $OriginalRoot
    Invoke-Checked $gh @("auth", "status", "-h", "github.com")
    $releaseView = Invoke-Process $gh @("release", "view", ("v{0}" -f $version), "--repo", $script:ReleaseRepo)
    Write-ProcessOutput $releaseView
    if (-not $releaseView.DrainComplete) { Stop-Launcher "GitHub release inspection output drain timed out" }
    if (@($releaseView.OwnedCleanupErrors).Count -gt 0) { Stop-Launcher ("Owned GitHub CLI cleanup failed: " + ($releaseView.OwnedCleanupErrors -join "; ")) }
    if ($releaseView.ExitCode -ne 0 -and $releaseView.ExitCode -ne 1) {
        Stop-Launcher "Could not inspect the existing GitHub release"
    }
    $releaseExists = $releaseView.ExitCode -eq 0
    if ($releaseExists -and $env:ALLOW_SAME_VERSION_RELEASE -ne "1") { Stop-Launcher "Release v$version already exists; explicit same-version overwrite is required" }
    $apk = Join-Path $sourceDir $script:ApkName
    if ($releaseExists) {
        Invoke-Checked $gh @("release", "upload", ("v{0}" -f $version), $apk, "--clobber", "--repo", $script:ReleaseRepo)
    } else {
        $releaseLabel = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("44Oq44Oq44O844K5"))
        $releaseNotes = ("v{0} {1}" -f $version, $releaseLabel)
        Invoke-Checked $gh @("release", "create", ("v{0}" -f $version), $apk, "--repo", $script:ReleaseRepo, "--target", "main", "--title", ("v{0}" -f $version), "--notes", $releaseNotes)
    }
    $today = (Get-Date -Format yyyy-MM-dd)
    $publisher = Join-Path $Workspace.Snapshot "scripts\publish_latest_json.ps1"
    $publisherArguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $publisher, "-Version", $version, "-Repo", $script:ReleaseRepo, "-ApkName", $script:ApkName, "-Today", $today)
    if (-not [string]::IsNullOrWhiteSpace($env:NOTES_FILE)) {
        Assert-RegularFile $env:NOTES_FILE
        $publisherArguments += @("-NotesFile", $env:NOTES_FILE)
    }
    $trustedGhDirectory = Split-Path -Parent $gh
    $publisherPowerShell = Resolve-TrustedTool "powershell" $OriginalRoot
    $oldPath = $env:PATH
    try {
        $env:PATH = $trustedGhDirectory + [System.IO.Path]::PathSeparator + $oldPath
        Invoke-Checked $publisherPowerShell $publisherArguments
    } finally {
        $env:PATH = $oldPath
    }
}

function Assert-QALaunchCheckout([string]$GitExe, [string]$RepositoryRoot, [string]$ExpectedBranch, [string]$ExpectedRevision) {
    $branch = (Invoke-Git $GitExe $RepositoryRoot @("--no-optional-locks", "branch", "--show-current")).Trim()
    if ([string]::IsNullOrWhiteSpace($branch) -or $branch -eq "main") {
        Stop-Launcher "QA branch-build requires a non-main original branch"
    }
    if ([string]::IsNullOrWhiteSpace($ExpectedBranch) -or -not $branch.Equals($ExpectedBranch, [StringComparison]::Ordinal)) {
        Stop-Launcher "QA branch-build requires the original checkout branch to equal AOITALK_MOBILE_QA_BRANCH"
    }
    $head = (Invoke-Git $GitExe $RepositoryRoot @("--no-optional-locks", "rev-parse", "--verify", "HEAD")).Trim().ToLowerInvariant()
    if ($head -ne $ExpectedRevision.ToLowerInvariant()) {
        Stop-Launcher "QA branch-build requires the original checkout HEAD to equal the explicit source revision"
    }
    $status = Invoke-Git $GitExe $RepositoryRoot @("--no-optional-locks", "status", "--porcelain", "--untracked-files=all")
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        Stop-Launcher "QA branch-build requires a full-clean original checkout"
    }
}

$originalRoot = Full-Path $ProjectRoot
Assert-NoReparseAncestors $originalRoot
Assert-RegularFile (Join-Path $originalRoot "scripts\build_mobile_apk.ps1")
$script:JobTrackingReady = $InternalBuild -and
    -not [string]::IsNullOrWhiteSpace($env:AOITALK_BUILD_TEMP_ROOT) -and
    -not [string]::IsNullOrWhiteSpace($env:AOITALK_RELEASE_OWNER_NONCE)
$gitExe = Resolve-TrustedTool "git" $originalRoot

if ($InternalBuild) {
    if ([string]::IsNullOrWhiteSpace($env:AOITALK_RELEASE_PYTHON)) {
        Stop-Launcher "Internal build Python path is missing"
    }
    Assert-InternalIdentity $originalRoot $gitExe
    $pythonPath = Full-Path $env:AOITALK_RELEASE_PYTHON
    $internalWorkspace = [pscustomobject]@{
        Temp = Full-Path $env:AOITALK_BUILD_TEMP_ROOT
        Gradle = Full-Path $env:GRADLE_USER_HOME
        Npm = Full-Path $env:npm_config_cache
        Snapshot = $originalRoot
        MarkerPath = Full-Path $env:AOITALK_RELEASE_OWNER_FILE
    }
    $innerSnapshot = [pscustomobject]@{ SourceSha = $env:AOITALK_RELEASE_SOURCE_COMMIT; OriginMainSha = $env:AOITALK_RELEASE_ORIGIN_MAIN; HelperHash = $env:AOITALK_RELEASE_HELPER_SHA256; BatchHash = $env:AOITALK_RELEASE_BATCH_SHA256 }
    Invoke-NativeSnapshotBuild $originalRoot $internalWorkspace $pythonPath $gitExe $innerSnapshot
    exit 0
}

$pythonPath = Join-Path $originalRoot "venv\Scripts\python.exe"
Assert-RegularFile $pythonPath

$workspace = $null
$result = 1
try {
    if ($PublishOnly -and $env:RELEASE_TARGET -and $env:RELEASE_TARGET -ne "main") { Stop-Launcher "RELEASE_TARGET must remain main for the fixed origin/main release" }
    if (-not $PublishOnly -and $env:RELEASE_TARGET -and $env:RELEASE_TARGET -ne "main") { Stop-Launcher "RELEASE_TARGET must remain main for the fixed origin/main release" }
    if ($env:AOITALK_MOBILE_QA_BUILD -and $env:AOITALK_MOBILE_QA_BUILD -ne "1") { Stop-Launcher "AOITALK_MOBILE_QA_BUILD must be 1 or unset" }
    $qaBuild = $env:AOITALK_MOBILE_QA_BUILD -eq "1"
    if (-not $qaBuild -and $env:EXPO_PUBLIC_AOITALK_READ_ONLY_DIAGNOSTICS) { Stop-Launcher "EXPO_PUBLIC_AOITALK_READ_ONLY_DIAGNOSTICS is a QA-only read-only diagnostics variant and must be unset for release builds" }
    if ($qaBuild -and $PublishOnly) { Stop-Launcher "QA branch-build cannot be combined with -PublishOnly" }
    if ($PublishOnly -and $env:AOITALK_BUILD_ONLY -eq "1") { Stop-Launcher "-PublishOnly cannot be combined with AOITALK_BUILD_ONLY=1" }
    if ($qaBuild -and $env:AOITALK_BUILD_ONLY -ne "1") { Stop-Launcher "QA branch-build requires AOITALK_BUILD_ONLY=1" }
    if (-not $qaBuild -and ($env:AOITALK_MOBILE_QA_BRANCH -or $env:AOITALK_MOBILE_QA_SOURCE_REVISION)) { Stop-Launcher "QA branch/source options require AOITALK_MOBILE_QA_BUILD=1" }
    $qaBranch = if ($qaBuild) { [string]$env:AOITALK_MOBILE_QA_BRANCH } else { "" }
    $qaRevision = if ($qaBuild) { [string]$env:AOITALK_MOBILE_QA_SOURCE_REVISION } else { "" }
    if ($qaBuild -and ([string]::IsNullOrWhiteSpace($qaBranch) -or [string]::IsNullOrWhiteSpace($qaRevision))) { Stop-Launcher "QA branch-build requires an explicit branch and source revision" }
    if ($qaBuild) { Assert-QALaunchCheckout $gitExe $originalRoot $qaBranch $qaRevision }
    $workspace = New-Workspace $originalRoot
    $origin = (Invoke-Git $gitExe $originalRoot @("config", "--local", "--get", "remote.origin.url")).Trim()
    if ([string]::IsNullOrWhiteSpace($origin)) { Stop-Launcher "The launch checkout has no canonical origin remote" }
    $snapshot = Prepare-Snapshot $workspace $originalRoot $gitExe $origin $qaBuild $qaBranch $qaRevision
    $requestedBase = if ([string]::IsNullOrWhiteSpace($ReleaseBase)) { [string]$env:AOITALK_MOBILE_RELEASE_BASE } else { $ReleaseBase }
    $resolvedBase = if ($PublishOnly) { "" } else { Resolve-ReleaseBase $gitExe $workspace.Snapshot $snapshot.SourceSha $requestedBase $qaBuild }
    if (-not $PublishOnly) {
        [Console]::Out.WriteLine(("[INFO] Mobile release gate: base={0} target={1}" -f $resolvedBase, $snapshot.SourceSha))
    }
    if ($PublishOnly) {
        Invoke-PublishOnly $workspace $originalRoot $gitExe $pythonPath $snapshot
    } else {
        Set-ChildBuildEnvironment $workspace $originalRoot $gitExe $snapshot $pythonPath $resolvedBase
        $batch = Join-Path $workspace.Snapshot "scripts\build_apk.bat"
        $childResponse = Invoke-Process $env:ComSpec @("/d", "/c", "call", $batch, "__AOITALK_SNAPSHOT_BUILD") -LiveOutput
        Write-ProcessOutput $childResponse
        if (-not $childResponse.DrainComplete) { $result = 1; Stop-Launcher "Fixed snapshot APK build output drain timed out" }
        if (@($childResponse.OwnedCleanupErrors).Count -gt 0) { $result = 1; Stop-Launcher ("Fixed snapshot APK child cleanup failed: " + ($childResponse.OwnedCleanupErrors -join "; ")) }
        $childCode = $childResponse.ExitCode
        if ($childCode -ne 0) { $result = $childCode; Stop-Launcher "Fixed snapshot APK build failed with exit code $childCode" }
        $expo = Get-AppExpoFromFile $workspace.Snapshot
        $artifact = Copy-ArtifactTransaction $workspace $originalRoot $workspace.Snapshot ([string]$expo.version) $snapshot.SourceSha $qaBuild
        Write-Output ("Built and verified local APK: " + $artifact)
    }
    $result = 0
} catch {
    [Console]::Error.WriteLine("[ERROR] " + $_.Exception.Message)
    if ($result -eq 0) { $result = 1 }
} finally {
    if ($null -ne $workspace) {
        try { Remove-Workspace $workspace } catch { [Console]::Error.WriteLine("[ERROR] " + $_.Exception.Message); if ($result -eq 0) { $result = 1 } }
    }
}
exit $result
