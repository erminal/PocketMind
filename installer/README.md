# 桌面版安装包

本目录用于放**本地构建的干净便携版**：

```text
installer/PocketMindDesktop/
├─ PocketMind.exe
└─ resources/
```

特点：

- 491 MB 左右；
- 不包含任何用户数据、记忆、日记、媒体、日志或 API Key；
- 首次启动自动创建 `%APPDATA%\PocketMind\data`；
- 关闭窗口 = 退出；
- 当前未做代码签名。

## 为什么不在 git 里

GitHub 单文件上限 100 MB，而便携版包含 Electron 运行时，约 491 MB。  
因此本仓库通过 `.gitignore` 排除：

```text
installer/PocketMindDesktop/
```

发布时请：

1. 将 `PocketMindDesktop` 压缩为 zip；
2. 上传到 GitHub **Releases**；
3. 在 Release 说明中附上根目录 `SHA256SUMS.txt` 的哈希。

## 本地构建

```powershell
.\desktop\build_portable.ps1 -ElectronRoot "desktop\node_modules\electron\dist"
```

默认构建不打包用户数据。  
只有个人迁移才使用：

```powershell
.\desktop\build_portable.ps1 -SeedData
```

## 安装到本机

```powershell
.\desktop\install_portable.ps1
```
