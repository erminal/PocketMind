param(
    [string]$ElectronRoot = "",
    [switch]$SkipBackendBuild,
    [switch]$SeedData
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$distRoot = Join-Path $project "dist\PocketMindDesktop"
$backendDist = Join-Path $project "dist\PocketMind"

function Resolve-ElectronRoot {
    if ($ElectronRoot) {
        if (Test-Path (Join-Path $ElectronRoot "electron.exe")) { return (Resolve-Path $ElectronRoot).Path }
        if (Test-Path $ElectronRoot) { return (Split-Path -Parent (Resolve-Path $ElectronRoot).Path) }
        throw "找不到 Electron：$ElectronRoot"
    }
    if ($env:ELECTRON_PATH -and (Test-Path $env:ELECTRON_PATH)) {
        return (Split-Path -Parent (Resolve-Path $env:ELECTRON_PATH).Path)
    }
    $candidates = @(
        (Join-Path $PSScriptRoot "node_modules\electron\dist"),
        (Join-Path $project "node_modules\electron\dist")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path (Join-Path $candidate "electron.exe")) { return (Resolve-Path $candidate).Path }
    }
    throw "找不到 Electron。请设置 ELECTRON_PATH 指向 electron.exe，或先 npm install electron。"
}

if (-not $SkipBackendBuild) {
    Write-Host "Building Python backend..." -ForegroundColor Cyan
    & (Join-Path $project "build_exe.ps1")
    if ($LASTEXITCODE -ne 0) { throw "后端构建失败" }
}

$backendExe = Join-Path $backendDist "PocketMind.exe"
if (-not (Test-Path $backendExe)) { throw "找不到后端：$backendExe" }

$electron = Resolve-ElectronRoot
Write-Host "Electron runtime: $electron" -ForegroundColor Cyan
Write-Host "Destination: $distRoot" -ForegroundColor Cyan

# 只清理我们的桌面产物；不碰 dist\PocketMind 的用户数据。
if (Test-Path $distRoot) { Remove-Item $distRoot -Recurse -Force }
New-Item -ItemType Directory -Path $distRoot -Force | Out-Null

# 1) Electron runtime
Copy-Item -Path (Join-Path $electron "*") -Destination $distRoot -Recurse -Force
$electronExe = Join-Path $distRoot "electron.exe"
if (-not (Test-Path $electronExe)) { throw "Electron runtime 复制不完整：$electronExe" }
Rename-Item -Path $electronExe -NewName "PocketMind.exe"
# 便携包自己提供 app，不再使用 Electron 默认应用提示页。
Remove-Item (Join-Path $distRoot "resources\default_app.asar") -Force -ErrorAction SilentlyContinue

# 2) Electron app
$appDir = Join-Path $distRoot "resources\app"
New-Item -ItemType Directory -Path $appDir -Force | Out-Null
foreach ($name in @("main.js", "loading.html", "package.json")) {
    Copy-Item (Join-Path $PSScriptRoot $name) (Join-Path $appDir $name) -Force
}
Copy-Item (Join-Path $PSScriptRoot "assets") (Join-Path $appDir "assets") -Recurse -Force
# 桌面开发说明不随运行时打包
$appPackage = Get-Content (Join-Path $appDir "package.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$appPackage | Add-Member -NotePropertyName productName -NotePropertyValue "PocketMind" -Force
$appPackage | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $appDir "package.json") -Encoding UTF8

# 3) Python backend
$backendOut = Join-Path $distRoot "resources\backend"
New-Item -ItemType Directory -Path $backendOut -Force | Out-Null
Copy-Item $backendExe (Join-Path $backendOut "PocketMind.exe") -Force
Copy-Item (Join-Path $backendDist "_internal") (Join-Path $backendOut "_internal") -Recurse -Force
foreach ($extra in @(".env.example")) {
    $src = Join-Path $backendDist $extra
    if (Test-Path $src) { Copy-Item $src (Join-Path $backendOut $extra) -Force }
}

# 4) 发布清单（源码哈希 + 构建产物哈希；不是代码签名）
$manifest = Join-Path $distRoot "release_manifest.json"
$python = Join-Path $project ".venv\Scripts\python.exe"
if (Test-Path $python) {
    & $python (Join-Path $PSScriptRoot "make_release_manifest.py") `
        --root $project `
        --backend-exe (Join-Path $backendDist "PocketMind.exe") `
        --desktop-exe (Join-Path $distRoot "PocketMind.exe") `
        --out $manifest | Write-Host
    if ($LASTEXITCODE -ne 0) { throw "发布清单生成失败" }
    Copy-Item $manifest (Join-Path $backendOut "release_manifest.json") -Force
} else {
    Write-Warning "找不到项目 Python，跳过 release_manifest.json"
}

# 5) 可选种子数据：默认**不打包任何用户数据**，保证发布包纯净。
#    - 默认：只带程序，首次启动在 %APPDATA% 创建空数据目录；
#    - 仅个人本地迁移时显式加 -SeedData，且仍排除 settings.json / secrets。
if ($SeedData) {
    $seedData = Join-Path $backendDist "data"
    if (Test-Path $seedData) {
        $seedOut = Join-Path $backendOut "data"
        Copy-Item $seedData $seedOut -Recurse -Force
        Remove-Item (Join-Path $seedOut "settings.json") -Force -ErrorAction SilentlyContinue
        Remove-Item (Join-Path $seedOut "secrets") -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item (Join-Path $seedOut "logs") -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item (Join-Path $seedOut "exports") -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item (Join-Path $seedOut "backups") -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# 5) 启动提示
$readme = @"
PocketMind Desktop
==================

双击 PocketMind.exe 启动。

- 不需要 CMD，不需要浏览器；
- 关闭窗口 = 退出（后端进程一起退出）；
- 本发布包**不包含任何用户数据/记忆/日志/API Key**；
- 首次启动自动创建 %APPDATA%\PocketMind\data；
- 摄像头：只允许本地 127.0.0.1 页面申请。

注意：这是本地便携包，不包含安装器/代码签名。
"@
$readme | Set-Content (Join-Path $distRoot "README-桌面版.txt") -Encoding UTF8

$size = (Get-ChildItem $distRoot -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("[OK] {0}" -f (Join-Path $distRoot "PocketMind.exe")) -ForegroundColor Green
Write-Host ("     Size: {0:N0} MB" -f $size) -ForegroundColor Green
