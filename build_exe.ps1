# PocketMind EXE 构建脚本（Windows）
# 用法（项目根目录 PowerShell）：
#   .\build_exe.ps1
# 产物：dist\PocketMind\PocketMind.exe
# 注意：构建产物不提交 git（见 .gitignore 的 build/、dist/）。

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[ERROR] .venv not found. Run from PocketMind project root." -ForegroundColor Red
    exit 1
}

$running = Get-Process PocketMind -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "[ERROR] PocketMind is running (PID $($running.Id -join ', '))." -ForegroundColor Red
    Write-Host "        Close the PocketMind window before rebuilding (Windows locks _internal files)." -ForegroundColor Yellow
    exit 1
}

Write-Host "Installing/updating PyInstaller in project .venv..." -ForegroundColor Cyan
$env:UV_CACHE_DIR = Join-Path $PSScriptRoot ".uv-cache"
& uv pip install --python .\.venv\Scripts\python.exe pyinstaller

# 重新打包会重建 dist\PocketMind；用户数据在 dist\PocketMind\data，绝不能被构建静默删掉。
$distRoot = Join-Path $PSScriptRoot "dist\PocketMind"
$backupRoot = Join-Path $PSScriptRoot "backups"
$backupDir = $null
if (Test-Path (Join-Path $distRoot "data")) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupDir = Join-Path $backupRoot "PocketMind-data-$stamp"
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    Copy-Item -Path (Join-Path $distRoot "data") -Destination $backupDir -Recurse -Force
    Write-Host "Backed up existing data to: $backupDir" -ForegroundColor DarkCyan
}

Write-Host "Building PocketMind.exe (onedir)..." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m PyInstaller PocketMind.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] PyInstaller failed." -ForegroundColor Red
    if ($backupDir) {
        Write-Host "Restoring data backup to $distRoot\data ..." -ForegroundColor Yellow
        New-Item -ItemType Directory -Path (Join-Path $distRoot "data") -Force | Out-Null
        Copy-Item -Path (Join-Path $backupDir "data\*") -Destination (Join-Path $distRoot "data") -Recurse -Force
    }
    exit $LASTEXITCODE
}

# 构建成功后恢复用户数据（如果构建过程已经清掉 dist\PocketMind\data）
if ($backupDir -and -not (Test-Path (Join-Path $distRoot "data"))) {
    Write-Host "Restoring data backup to $distRoot\data ..." -ForegroundColor DarkCyan
    Copy-Item -Path (Join-Path $backupDir "data") -Destination (Join-Path $distRoot "data") -Recurse -Force
}

# 把用户可能需要的说明/模板放到 exe 旁边（PyInstaller 的 datas 会进 _internal，顶层再放一份）
Copy-Item -Path ".env.example" -Destination "dist\PocketMind\.env.example" -Force -ErrorAction SilentlyContinue
Copy-Item -Path "使用说明.md" -Destination "dist\PocketMind\使用说明.md" -Force -ErrorAction SilentlyContinue
Copy-Item -Path "打开工作台.cmd" -Destination "dist\PocketMind\打开工作台.cmd" -Force -ErrorAction SilentlyContinue
Copy-Item -Path "诊断PocketMind.cmd" -Destination "dist\PocketMind\诊断PocketMind.cmd" -Force -ErrorAction SilentlyContinue

$exe = Join-Path $PSScriptRoot "dist\PocketMind\PocketMind.exe"
if (Test-Path $exe) {
    $size = [math]::Round((Get-ChildItem (Split-Path $exe) -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
    Write-Host ""
    Write-Host "[OK] $exe" -ForegroundColor Green
    Write-Host "     Folder size: $size MB"
    Write-Host "     Double-click it; data/ and settings will be next to the exe."
} else {
    Write-Host "[ERROR] exe not found." -ForegroundColor Red
    exit 1
}
