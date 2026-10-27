param(
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$target = Join-Path $env:LOCALAPPDATA "PocketMind"
$userData = Join-Path $env:APPDATA "PocketMind\data"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path $project "backups\desktop-data-$stamp"

Write-Host "PocketMind desktop update" -ForegroundColor Cyan
Write-Host "  program: $target"
Write-Host "  data:    $userData"
Write-Host "  backup:  $backup"

# 1) 只停止安装目录里的 PocketMind，不碰旧 Web 版/DSH。
Get-Process PocketMind -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "$target*" } |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

# 2) 备份桌面版用户数据（真正的数据在 APPDATA，不在安装目录）。
if (Test-Path $userData) {
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    Copy-Item -Path (Join-Path $userData "*") -Destination $backup -Recurse -Force
    Write-Host "[1/3] 用户数据已备份" -ForegroundColor Green
} else {
    Write-Host "[1/3] 还没有桌面版用户数据，跳过备份（首次安装）" -ForegroundColor Yellow
}

# 3) 重建 Python 后端 + Electron 桌面包。
Write-Host "[2/3] 重建桌面包（会调用 build_exe.ps1）..." -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "build_portable.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "构建失败；用户数据备份仍在 $backup"
}

# 4) 替换程序文件到 LocalAppData；不会覆盖 APPDATA 用户数据。
Write-Host "[3/3] 安装新版本..." -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "install_portable.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "安装失败；用户数据备份仍在 $backup"
}

Write-Host ""
Write-Host "[OK] 更新完成" -ForegroundColor Green
Write-Host "     双击桌面 PocketMind 启动" -ForegroundColor Green
Write-Host "     数据备份：$backup" -ForegroundColor DarkGray

if (-not $NoLaunch) {
    $exe = Join-Path $target "PocketMind.exe"
    if (Test-Path $exe) {
        Start-Process -FilePath $exe -WorkingDirectory $target
    }
}
