# llama.cppの稼働ログを通常のターミナルで再表示する。
# ログ窓を閉じてもモデル本体は動き続けるため、必要なときにこのスクリプトで確認し直せる。
# 閲覧専用で、モデルの起動・停止には影響しない。
param(
    [string]$LogPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'logs\models\llama_cpp.log')
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $LogPath)) {
    throw "ログが見つかりません: $LogPath"
}
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$Host.UI.RawUI.WindowTitle = 'AoiTalk llama.cpp'
Get-Content -LiteralPath $LogPath -Encoding UTF8 -Tail 300 -Wait
