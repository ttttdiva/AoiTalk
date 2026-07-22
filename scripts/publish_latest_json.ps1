param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$Repo,
    [Parameter(Mandatory = $true)][string]$ApkName,
    [Parameter(Mandatory = $true)][string]$Today,
    [string]$NotesFile = ""
)

$ErrorActionPreference = "Stop"

# notes は UTF-8 ファイル指定があればそれを、なければ既定文言を使う。
# この .ps1 自体を UTF-8 (BOM付き) で保存することで日本語リテラルの文字化けを防ぐ。
if ($NotesFile -and (Test-Path -LiteralPath $NotesFile)) {
    $notes = (Get-Content -LiteralPath $NotesFile -Raw -Encoding UTF8).Trim()
} else {
    $notes = "v$Version リリース"
}

$payload = @{
    mobile = @{
        version = $Version
        url     = "https://github.com/$Repo/releases/download/v$Version/$ApkName"
        notes   = $notes
        date    = $Today
    }
}
$json = $payload | ConvertTo-Json -Depth 3
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)

# 生成物が正しい UTF-8 であることを自己検証する。
[System.Text.Encoding]::UTF8.GetString($bytes) | Out-Null
$b64 = [Convert]::ToBase64String($bytes)

$sha = ""
try {
    $sha = (& gh api "repos/$Repo/contents/latest.json" --jq ".sha" 2>$null | Out-String).Trim()
} catch {}

$message = "latest.json を v$Version に更新"
if ($sha) {
    & gh api "repos/$Repo/contents/latest.json" --method PUT --field "message=$message" --field "content=$b64" --field "sha=$sha" | Out-Null
} else {
    & gh api "repos/$Repo/contents/latest.json" --method PUT --field "message=$message" --field "content=$b64" | Out-Null
}
if ($LASTEXITCODE -ne 0) {
    throw "latest.json update failed (exit=$LASTEXITCODE)"
}
Write-Output "latest.json updated to v$Version"
