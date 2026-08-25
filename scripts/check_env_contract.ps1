param(
    [string]$EnvPath,
    [string]$SamplePath
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $EnvPath) {
    $EnvPath = Join-Path $repoRoot ".env"
}
if (-not $SamplePath) {
    $SamplePath = Join-Path $repoRoot ".env.sample"
}

function Get-EnvMap {
    param([string]$Path)

    $result = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $result
    }

    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $key = $Matches[1]
            $value = $Matches[2].Trim()
            $result[$key] = $value
        }
    }
    return $result
}

function Add-MissingKeys {
    param(
        [hashtable]$Map,
        [string[]]$Keys,
        [string]$Label,
        [System.Collections.Generic.List[string]]$Errors
    )

    foreach ($key in $Keys) {
        if (-not $Map.ContainsKey($key)) {
            $Errors.Add("$Label is missing $key")
        }
    }
}

function Add-ForbiddenKeys {
    param(
        [hashtable]$Map,
        [string[]]$Keys,
        [string]$Label,
        [System.Collections.Generic.List[string]]$Errors
    )

    foreach ($key in $Keys) {
        if ($Map.ContainsKey($key)) {
            $Errors.Add("$Label contains obsolete key $key")
        }
    }
}

function Add-EmptyValues {
    param(
        [hashtable]$Map,
        [string[]]$Keys,
        [string]$Label,
        [System.Collections.Generic.List[string]]$Errors
    )

    foreach ($key in $Keys) {
        if ($Map.ContainsKey($key) -and [string]::IsNullOrWhiteSpace($Map[$key])) {
            $Errors.Add("$Label has empty required value for $key")
        }
    }
}

function Add-DefaultOnlyValues {
    param(
        [hashtable]$Map,
        [hashtable]$Defaults,
        [string]$Label,
        [System.Collections.Generic.List[string]]$Errors
    )

    foreach ($key in $Defaults.Keys) {
        if ($Map.ContainsKey($key)) {
            $expected = $Defaults[$key]
            if ($expected -is [array]) {
                if ($expected -contains $Map[$key]) {
                    $Errors.Add("$Label contains default-only override $key")
                }
            } elseif ($Map[$key] -eq $expected) {
                $Errors.Add("$Label contains default-only override $key")
            }
        }
    }
}

function Add-PlaceholderValues {
    param(
        [hashtable]$Map,
        [string]$Label,
        [System.Collections.Generic.List[string]]$Errors
    )

    foreach ($key in $Map.Keys) {
        $value = [string]$Map[$key]
        if ($value -match '^(your-|change-this-|/path/to/)') {
            $Errors.Add("$Label contains placeholder value for $key")
        }
    }
}

$forbiddenKeys = @(
    "AOITALK_WEB_AUTH_USER",
    "AOITALK_WEB_AUTH_PASSWORD",
    "AOITALK_DISABLE_SEMANTIC_MEMORY"
)

$sampleRequiredKeys = @(
    "NEXTAUTH_SECRET",
    "AOITALK_WEB_AUTH_SECRET",
    "AOITALK_JWT_SECRET",
    "INTERNAL_API_KEY",
    "AOITALK_CADDY_GATE_KEY",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "USE_POSTGRESQL",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "OLLAMA_MODEL"
)

$envRequiredValueKeys = @(
    "NEXTAUTH_SECRET",
    "AOITALK_WEB_AUTH_SECRET",
    "AOITALK_JWT_SECRET",
    "INTERNAL_API_KEY",
    "AOITALK_CADDY_GATE_KEY",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB"
)

$defaultOnlyValues = @{
    "OPENROUTER_BASE_URL" = "https://openrouter.ai/api/v1"
    "OPENROUTER_APP_NAME" = "AoiTalk"
    "PYTHON_API_URL" = "http://127.0.0.1:3000"
    "NEXTAUTH_URL" = @("http://127.0.0.1:3002", "http://localhost:3002")
    "OLLAMA_BASE_URL" = "http://127.0.0.1:11434/v1"
    "OLLAMA_API_KEY" = "ollama"
    "VOICEVOX_HOST" = "127.0.0.1"
    "VOICEVOX_PORT" = "50021"
    "AIVISSPEECH_HOST" = "127.0.0.1"
    "AIVISSPEECH_PORT" = "10101"
    "QDRANT_HOST" = "localhost"
    "QDRANT_PORT" = "6333"
    "FILER_VIDEO_THUMBNAIL_CACHE" = "cache/video_thumbnails"
    "SPOTIFY_REDIRECT_URI" = "http://127.0.0.1:8080/callback"
    "AOITALK_COMMAND_TIMEOUT" = "120"
}

$sampleMap = Get-EnvMap -Path $SamplePath
$envMap = Get-EnvMap -Path $EnvPath
$errors = [System.Collections.Generic.List[string]]::new()

Add-MissingKeys -Map $sampleMap -Keys $sampleRequiredKeys -Label ".env.sample" -Errors $errors
Add-ForbiddenKeys -Map $sampleMap -Keys $forbiddenKeys -Label ".env.sample" -Errors $errors
Add-DefaultOnlyValues -Map $sampleMap -Defaults $defaultOnlyValues -Label ".env.sample" -Errors $errors

if (Test-Path -LiteralPath $EnvPath) {
    Add-MissingKeys -Map $envMap -Keys $envRequiredValueKeys -Label ".env" -Errors $errors
    Add-EmptyValues -Map $envMap -Keys $envRequiredValueKeys -Label ".env" -Errors $errors
    Add-ForbiddenKeys -Map $envMap -Keys $forbiddenKeys -Label ".env" -Errors $errors
    Add-DefaultOnlyValues -Map $envMap -Defaults $defaultOnlyValues -Label ".env" -Errors $errors
    Add-PlaceholderValues -Map $envMap -Label ".env" -Errors $errors

    $hasRemoteProvider =
        ($envMap.ContainsKey("OPENROUTER_API_KEY") -and -not [string]::IsNullOrWhiteSpace($envMap["OPENROUTER_API_KEY"])) -or
        ($envMap.ContainsKey("GEMINI_API_KEY") -and -not [string]::IsNullOrWhiteSpace($envMap["GEMINI_API_KEY"])) -or
        ($envMap.ContainsKey("OPENAI_API_KEY") -and -not [string]::IsNullOrWhiteSpace($envMap["OPENAI_API_KEY"]))
    $hasLocalProvider =
        ($envMap.ContainsKey("OLLAMA_MODEL") -and -not [string]::IsNullOrWhiteSpace($envMap["OLLAMA_MODEL"]))

    if (-not ($hasRemoteProvider -or $hasLocalProvider)) {
        $errors.Add(".env has no configured LLM provider key or local Ollama endpoint")
    }
}

if ($errors.Count -gt 0) {
    Write-Error ("Environment contract check failed:`n- " + ($errors -join "`n- "))
}

Write-Host "Environment contract check passed."
