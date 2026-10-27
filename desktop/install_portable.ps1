param(
    [string]$Source = "",
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$src = if ($Source) { (Resolve-Path $Source).Path } else { Join-Path $project "dist\PocketMindDesktop" }
if (-not (Test-Path (Join-Path $src "PocketMind.exe"))) {
    throw "找不到桌面包：$src\PocketMind.exe。请先运行 desktop\build_portable.ps1"
}

$target = Join-Path $env:LOCALAPPDATA "PocketMind"
# 只结束后端/壳自身，不碰 DSH 的 Electron。
Get-Process PocketMind -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$target*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 400

if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target -Force | Out-Null }
# 应用运行时用 ADATA 下的用户数据；这里整体替换的是程序文件，不会覆盖 %APPDATA%\PocketMind\data。
# 先备份旧程序目录里的 backend seed data，避免误删（用户数据不应放这里）。
$oldSeed = Join-Path $target "resources\backend\data"
$seedBackup = $null
if (Test-Path $oldSeed) {
    $seedBackup = Join-Path $env:TEMP ("pocketmind-seed-" + [guid]::NewGuid().ToString("N"))
    Copy-Item $oldSeed $seedBackup -Recurse -Force
}
Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $target -Force | Out-Null
Copy-Item -Path (Join-Path $src "*") -Destination $target -Recurse -Force
if ($seedBackup -and -not (Test-Path (Join-Path $target "resources\backend\data"))) {
    Copy-Item $seedBackup (Join-Path $target "resources\backend\data") -Recurse -Force
}
if ($seedBackup) { Remove-Item $seedBackup -Recurse -Force -ErrorAction SilentlyContinue }

$exe = Join-Path $target "PocketMind.exe"
$icon = Join-Path $target "resources\app\assets\icon.ico"

if (-not $NoShortcut) {
    $shell = New-Object -ComObject WScript.Shell
    $desktop = [Environment]::GetFolderPath("Desktop")
    $startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    foreach ($lnk in @((Join-Path $desktop "PocketMind.lnk"), (Join-Path $startMenu "PocketMind.lnk"))) {
        $shortcut = $shell.CreateShortcut($lnk)
        $shortcut.TargetPath = $exe
        $shortcut.WorkingDirectory = $target
        $shortcut.Description = "PocketMind 本地桌面版"
        if (Test-Path $icon) { $shortcut.IconLocation = $icon }
        $shortcut.Save()
    }
}

Write-Host ("[OK] 已安装到 {0}" -f $target) -ForegroundColor Green
Write-Host ("     双击桌面 PocketMind 启动" ) -ForegroundColor Green
Write-Host ("     用户数据：{0}" -f (Join-Path $env:APPDATA "PocketMind\data")) -ForegroundColor DarkGray
