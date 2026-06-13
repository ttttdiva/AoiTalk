param(
    [ValidateSet("GetOrCreateDataKey", "Status")]
    [string]$Action = "GetOrCreateDataKey",
    [string]$KeyPath = ""
)

$ErrorActionPreference = "Stop"

function Get-DefaultKeyPath {
    $base = $env:AOITALK_FIELD_CRYPTO_KEY_DIR
    if (-not $base) {
        $appData = $env:APPDATA
        if (-not $appData) {
            throw "APPDATA is not available; set AOITALK_FIELD_CRYPTO_KEY_DIR"
        }
        $base = Join-Path $appData "AoiTalk\keys"
    }
    return Join-Path $base "field-crypto-key.dpapi"
}

function Protect-KeyFileAcl([string]$Path) {
    if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
        return
    }
    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls $Path /inheritance:r /grant:r "${identity}:(R,W)" "SYSTEM:(F)" "Administrators:(F)" | Out-Null
    } catch {
        Write-Warning "Failed to harden key file ACL: $($_.Exception.Message)"
    }
}

function New-RandomBytes([int]$Length) {
    $bytes = New-Object byte[] $Length
    $rng = [System.Security.Cryptography.RNGCryptoServiceProvider]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return $bytes
}

function Get-OrCreateDataKey([string]$Path) {
    Add-Type -AssemblyName System.Security
    $entropy = [System.Text.Encoding]::UTF8.GetBytes("AoiTalk.FieldCrypto.v1")

    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    if (Test-Path -LiteralPath $Path) {
        $protected = [Convert]::FromBase64String((Get-Content -Raw -LiteralPath $Path).Trim())
        $plain = [System.Security.Cryptography.ProtectedData]::Unprotect(
            $protected,
            $entropy,
            [System.Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        [Convert]::ToBase64String($plain)
        return
    }

    $plain = New-RandomBytes 32
    $protected = [System.Security.Cryptography.ProtectedData]::Protect(
        $plain,
        $entropy,
        [System.Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    [System.IO.File]::WriteAllText($Path, [Convert]::ToBase64String($protected), [System.Text.Encoding]::ASCII)
    Protect-KeyFileAcl $Path
    [Convert]::ToBase64String($plain)
}

if (-not $KeyPath) {
    $KeyPath = Get-DefaultKeyPath
}

if ($Action -eq "Status") {
    [PSCustomObject]@{
        KeyPath = $KeyPath
        Exists = (Test-Path -LiteralPath $KeyPath)
    } | ConvertTo-Json -Compress
    exit 0
}

Get-OrCreateDataKey $KeyPath
