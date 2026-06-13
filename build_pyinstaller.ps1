<#
  build_pyinstaller.ps1 - 用 PyInstaller(onedir) 打包知识卡片 Studio。

  产物：dist\KnowledgeCard\  (KnowledgeCard.exe + _internal + 同级 resource/config/ms-playwright/storage)
  目标机无需装 Python：拷整个文件夹，双击 start.bat。

  前置：.venv 可正常运行本项目，且已 `python -m playwright install chromium`。
  用法：powershell -ExecutionPolicy Bypass -File build_pyinstaller.ps1
  保持本文件 ASCII。
#>
$ErrorActionPreference = "Stop"
$proj = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $proj

$pyi = Join-Path $proj ".venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $pyi)) { throw "pyinstaller not found in .venv. Run: uv pip install pyinstaller" }

Write-Host "[1/4] PyInstaller build (may take several minutes)..." -ForegroundColor Cyan
& $pyi knowledgecard.spec --noconfirm --distpath dist --workpath build_pyi
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed (exit $LASTEXITCODE)" }

$out = Join-Path $proj "dist\KnowledgeCard"
if (-not (Test-Path (Join-Path $out "KnowledgeCard.exe"))) { throw "exe not produced at $out" }

Write-Host "[2/4] Copy resource / config (next to exe)..." -ForegroundColor Cyan
Copy-Item "resource" (Join-Path $out "resource") -Recurse -Force
if (Test-Path "config.toml") { Copy-Item "config.toml" (Join-Path $out "config.toml") -Force }
else { Copy-Item "config.example.toml" (Join-Path $out "config.toml") -Force }
Copy-Item "config.example.toml" (Join-Path $out "config.example.toml") -Force
New-Item -ItemType Directory -Force -Path (Join-Path $out "storage") | Out-Null

Write-Host "[3/4] Copy Chromium (ms-playwright, large)..." -ForegroundColor Cyan
$pw = $env:PLAYWRIGHT_BROWSERS_PATH
if (-not $pw) { $pw = Join-Path $env:LOCALAPPDATA "ms-playwright" }
if (-not (Test-Path $pw)) { throw "Chromium not found at $pw. Run: python -m playwright install chromium" }
Copy-Item $pw (Join-Path $out "ms-playwright") -Recurse -Force

Write-Host "[4/4] Write start.bat..." -ForegroundColor Cyan
$bat = @"
@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Knowledge Card Studio (PyInstaller)
echo   Opening http://localhost:8501 ...
echo ============================================
start "" http://localhost:8501
"%~dp0KnowledgeCard.exe"
pause
"@
[System.IO.File]::WriteAllText((Join-Path $out "start.bat"), $bat, (New-Object System.Text.UTF8Encoding($false)))

$size = [math]::Round((Get-ChildItem $out -Recurse | Measure-Object Length -Sum).Sum / 1GB, 2)
Write-Host ""
Write-Host "DONE. PyInstaller bundle: $out  (about $size GB)" -ForegroundColor Green
Write-Host "Copy the whole 'KnowledgeCard' folder, double-click start.bat." -ForegroundColor Green
