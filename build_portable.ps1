<#
  build_portable.ps1  -  Build a portable Windows "image-only" bundle (Knowledge Card Studio).

  Output: a self-contained folder with
    - python\           relocatable Python runtime + all dependencies
    - ms-playwright\    headless Chromium (used to render knowledge cards)
    - app\ webui\ resource\ config.toml
    - start.bat         double-click to launch

  Target machine needs nothing installed: copy the whole folder, double-click start.bat.

  Usage (run from project root, with a working .venv that can already render images):
    powershell -ExecutionPolicy Bypass -File build_portable.ps1
    powershell -ExecutionPolicy Bypass -File build_portable.ps1 -Slim     # drop video-only deps, smaller

  Prerequisite: .venv exists, and "python -m playwright install chromium" was run.

  NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads non-BOM scripts as the
  system codepage and would corrupt non-ASCII characters.
#>
param(
  [string]$OutDir = "dist\KnowledgeCard-Portable",
  [switch]$Slim
)
$ErrorActionPreference = "Stop"
$proj = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $proj

# 1) Resolve the base Python the .venv depends on (uv-managed, relocatable)
if (-not (Test-Path ".venv\pyvenv.cfg")) { throw "No .venv\pyvenv.cfg found. Run the project locally first." }
$cfgLine = (Get-Content ".venv\pyvenv.cfg" | Where-Object { $_ -match "^\s*home\s*=" })
$pyHome  = ($cfgLine -replace "^\s*home\s*=\s*", "").Trim()
if (-not (Test-Path (Join-Path $pyHome "python.exe"))) { throw "Base Python not found: $pyHome" }

# 2) Locate Chromium
$pwPath = $env:PLAYWRIGHT_BROWSERS_PATH
if (-not $pwPath) { $pwPath = Join-Path $env:LOCALAPPDATA "ms-playwright" }
if (-not (Test-Path $pwPath)) { throw "Chromium not found at $pwPath. Run: python -m playwright install chromium" }

# 3) Knowledge card page (emoji filename; locate by glob)
$pageSrc = Get-ChildItem "webui\pages" -Filter "*.py" | Where-Object { $_.Name -notlike "__*" } | Select-Object -First 1
if (-not $pageSrc) { throw "Knowledge card page webui\pages\*.py not found" }

if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$abs = (Resolve-Path $OutDir).Path

Write-Host "[1/6] Copy Python runtime..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path "$OutDir\python" | Out-Null
Copy-Item (Join-Path $pyHome "*") "$OutDir\python\" -Recurse -Force

Write-Host "[2/6] Copy dependencies (site-packages)..." -ForegroundColor Cyan
$dstSP = "$OutDir\python\Lib\site-packages"
New-Item -ItemType Directory -Force -Path $dstSP | Out-Null
Copy-Item ".venv\Lib\site-packages\*" "$dstSP\" -Recurse -Force

Write-Host "[3/6] Copy project code and resources..." -ForegroundColor Cyan
Copy-Item "app" "$OutDir\app" -Recurse -Force
Copy-Item "resource" "$OutDir\resource" -Recurse -Force
New-Item -ItemType Directory -Force -Path "$OutDir\webui\pages" | Out-Null
Copy-Item "webui\.streamlit" "$OutDir\webui\.streamlit" -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item "webui\i18n" "$OutDir\webui\i18n" -Recurse -Force -ErrorAction SilentlyContinue
# Page -> ASCII entry name (avoid emoji path in the bat)
Copy-Item $pageSrc.FullName "$OutDir\webui\pages\KnowledgeCard.py" -Force
if (Test-Path "config.toml") { Copy-Item "config.toml" "$OutDir\config.toml" -Force }
else { Copy-Item "config.example.toml" "$OutDir\config.toml" -Force }
New-Item -ItemType Directory -Force -Path "$OutDir\storage" | Out-Null

Write-Host "[4/6] Copy Chromium (large, please wait)..." -ForegroundColor Cyan
Copy-Item $pwPath "$OutDir\ms-playwright" -Recurse -Force

Write-Host "[5/6] Trim..." -ForegroundColor Cyan
Get-ChildItem "$OutDir" -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
if ($Slim) {
  Write-Host "  -Slim: removing video-only heavy deps (not needed for images)..." -ForegroundColor Yellow
  $heavy = @("moviepy*","edge_tts*","faster_whisper*","ctranslate2*","onnxruntime*","av","av-*","dashscope*","pydub*","redis*","litellm*","tokenizers*","torch*")
  foreach ($p in $heavy) {
    Get-ChildItem $dstSP -Filter $p -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  }
}

Write-Host "[6/6] Write start.bat..." -ForegroundColor Cyan
$bat = @"
@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0ms-playwright"
echo ============================================
echo   Knowledge Card Studio (portable)
echo   Opening http://localhost:8501 ...
echo ============================================
"%~dp0python\python.exe" -m streamlit run "webui\pages\KnowledgeCard.py" --server.port 8501 --server.address 127.0.0.1 --browser.gatherUsageStats false
pause
"@
[System.IO.File]::WriteAllText((Join-Path $abs "start.bat"), $bat, (New-Object System.Text.UTF8Encoding($false)))

$size = [math]::Round((Get-ChildItem $OutDir -Recurse | Measure-Object Length -Sum).Sum / 1GB, 2)
Write-Host ""
Write-Host "DONE. Portable bundle: $abs  (about $size GB)" -ForegroundColor Green
Write-Host "Copy the whole folder to the target Windows machine and double-click start.bat." -ForegroundColor Green
