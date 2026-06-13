param(
    [switch]$Apply,
    [switch]$ApplyFirewall,
    [string]$EnvPath = ".env",
    [string]$PostgresConf = "C:\Program Files\PostgreSQL\16\data\postgresql.conf"
)

$ErrorActionPreference = "Stop"

function Write-Result([string]$Name, [string]$Status, [string]$Detail) {
    [PSCustomObject]@{
        Check = $Name
        Status = $Status
        Detail = $Detail
    }
}

function Protect-EnvAcl([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Result ".env ACL" "missing" "$Path not found"
        return
    }
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $acl = Get-Acl -LiteralPath $resolved
    $broad = $acl.Access | Where-Object {
        $_.IdentityReference -match "BUILTIN\\Users|Authenticated Users|Everyone" -and
        $_.FileSystemRights -match "Read|Modify|FullControl|Write"
    }
    if (-not $broad) {
        Write-Result ".env ACL" "ok" "$resolved has no broad read/write ACE"
        return
    }
    if (-not $Apply) {
        Write-Result ".env ACL" "needs-fix" "$resolved grants broad access; rerun with -Apply"
        return
    }

    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $resolved /inheritance:r /grant:r "${identity}:(R,W)" "SYSTEM:(F)" "Administrators:(F)" | Out-Null
    Write-Result ".env ACL" "fixed" "$resolved restricted to current user, SYSTEM, Administrators"
}

function Set-PostgresLocalListen([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Result "PostgreSQL listen_addresses" "missing" "$Path not found"
        return
    }
    $content = Get-Content -LiteralPath $Path -Encoding UTF8
    $active = $content | Where-Object { $_ -match "^\s*listen_addresses\s*=" } | Select-Object -First 1
    if ($active -match "'localhost,127\.0\.0\.1'") {
        Write-Result "PostgreSQL listen_addresses" "ok" $active
        return
    }
    if (-not $Apply) {
        Write-Result "PostgreSQL listen_addresses" "needs-fix" "current: $active; rerun with -Apply"
        return
    }

    $updated = $false
    $next = foreach ($line in $content) {
        if ($line -match "^\s*listen_addresses\s*=") {
            $updated = $true
            "listen_addresses = 'localhost,127.0.0.1'"
        } else {
            $line
        }
    }
    if (-not $updated) {
        $next = @("listen_addresses = 'localhost,127.0.0.1'") + $content
    }
    Set-Content -LiteralPath $Path -Encoding UTF8 -Value $next
    Write-Result "PostgreSQL listen_addresses" "fixed" "restart PostgreSQL service to apply"
}

function Check-Firewall {
    $rules = Get-NetFirewallRule -Enabled True -Direction Inbound -Action Allow -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match "PostgreSQL|Qdrant" -or $_.Name -match "PostgreSQL|Qdrant" }
    if (-not $rules) {
        Write-Result "Firewall broad DB/vector allow" "ok" "no enabled inbound PostgreSQL/Qdrant allow rule found"
        return
    }

    $broadRules = @()
    foreach ($rule in $rules) {
        $ports = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue
        $addr = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue
        $isBroad = ($addr.RemoteAddress -contains "Any") -or -not $addr
        if ($isBroad) {
            $broadRules += $rule
            Write-Result "Firewall broad DB/vector allow" "needs-fix" "$($rule.DisplayName) allows inbound from Any"
        }
    }

    if ($ApplyFirewall -and $broadRules.Count -gt 0) {
        foreach ($rule in $broadRules) {
            Disable-NetFirewallRule -Name $rule.Name
        }
        Write-Result "Firewall broad DB/vector allow" "fixed" "disabled $($broadRules.Count) broad inbound allow rule(s)"
    } elseif ($broadRules.Count -gt 0) {
        Write-Result "Firewall broad DB/vector allow" "manual" "rerun with -ApplyFirewall to disable broad allow rules"
    }
}

Protect-EnvAcl $EnvPath
Set-PostgresLocalListen $PostgresConf
Check-Firewall
