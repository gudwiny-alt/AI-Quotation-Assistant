$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProjectRoot = Split-Path -Parent $ProjectRoot
Set-Location $ProjectRoot

py -m pytest -q
py -m PyInstaller --noconfirm --clean packaging/quotation_app.spec

$PortableDirectory = Join-Path $ProjectRoot "dist\福建移动铺货报价助手"
$Archive = Join-Path $ProjectRoot "dist\福建移动铺货报价助手-Windows-x64.zip"
Compress-Archive -Path "$PortableDirectory\*" -DestinationPath $Archive -Force
