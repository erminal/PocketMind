# Desktop shell

PocketMind 的桌面壳采用 Electron + 现有 Python/FastAPI 后端：

```text
PocketMind.exe
→ 启动内置 Python 后端（--desktop）
→ 等 /health
→ 内嵌窗口加载 PocketMind
→ 关闭窗口时结束后端进程
```

## 开发预览

需要 Electron 运行时。

```powershell
cd desktop
node scripts/run-local-electron.js
```

该脚本会优先查找本地 Electron；也可设置环境变量：

```powershell
$env:ELECTRON_PATH = "C:\path\to\electron.exe"
```

## 构建便携版

```powershell
.\desktop\build_portable.ps1 -ElectronRoot "desktop\node_modules\electron\dist"
```

构建默认**不包含任何用户数据**；只有显式 `-SeedData` 才会复制个人数据（并排除 Key/日志）。

输出：

```text
dist\PocketMindDesktop\
```

## 安装到本机

```powershell
.\desktop\install_portable.ps1
```

安装到：

```text
%LOCALAPPDATA%\PocketMind
```

用户数据仍在 `%APPDATA%\PocketMind\data`，升级不覆盖。

## 安全

- 摄像头权限只允许本地 `127.0.0.1`；
- 关闭窗口=退出；
- 单实例；
- 发布包不包含数据和 API Key；
- 当前无代码签名，发布清单仅作完整性校验。
