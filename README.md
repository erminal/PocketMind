# PocketMind

PocketMind 是一个**本地优先**的个人 / 学术陪伴型 Agent 实验台：长期记忆、人格连续、关系与情绪、日记、睡眠/梦、摄像头视觉输入，以及一个可观察、可调试的本地管理台。

这个目录是 **PocketMind v0.1.0 的干净发布包**，可以直接作为 GitHub 仓库的根目录使用。

> 重要：这里没有开发阶段的使用数据、记忆、聊天记录、日记、媒体、API Key、密钥文件、运行日志或会话快照。安装包也是干净构建，不包含任何用户数据。

---

## 功能概览

- **长期记忆**：事件 → 记忆抽取 → 打分/分类/遗忘 → 多视图检索 → 记忆门；
- **人格与关系**：人格卡、关系状态、好感/信任/边界/心境、临时情绪奖励；
- **陪伴体验**：人格化回复、内心话、工作日志、人格化日记、梦、睡眠/精力；
- **多模态**：手动截图 + 自动 40 秒观察开关，摄像头画面只在用户主动触发时使用；
- **可观察**：本地管理台、记忆星图（3D/2D）、人工标注、数据浏览/CSV、备份/导入；
- **隐私优先**：本地 SQLite；API Key 加密；默认不上传任何项目数据。

---

## 快速开始

### 方式 A：直接运行桌面版

便携版目录：

```text
installer/PocketMindDesktop/
```

双击：

```text
PocketMind.exe
```

特点：

- 不需要 CMD，不需要浏览器；
- 关闭窗口 = 退出（后端一起结束）；
- 首次启动自动创建用户数据目录（见下）；
- 摄像头权限只对本地 `127.0.0.1` 页面开放。

> `installer/PocketMindDesktop/` 约 491 MB，**不进入 git**；GitHub 发布时请上传到 Releases（GitHub 单文件上限 100 MB）。

### 方式 B：从源码运行

环境：Windows 10/11、Python 3.12。

```powershell
uv venv .venv --python 3.12
uv pip install --python .\.venv\Scripts\python.exe -r requirements.txt
.\.venv\Scripts\python.exe -m pocketmind.launcher
```

也可以双击 `启动PocketMind.cmd`（使用项目内 `.venv`）。  
打开后进入管理台配置模型：

```text
http://127.0.0.1:8000/admin
```

第一次没有 API Key 时会自动使用 Mock 模式，可以离线体验记忆/日记/管理台。

---

## 桌面版构建

需要 Electron 运行时（本仓库只包含壳源码，不包含 Electron 二进制）。

```powershell
cd desktop
npm install
cd ..
.\desktop\build_portable.ps1 -ElectronRoot "desktop\node_modules\electron\dist"
```

如 Electron 在自定义位置，设置 `ELECTRON_PATH` 或传 `-ElectronRoot`。  
构建输出：

```text
dist\PocketMindDesktop\
```

安装到本机：

```powershell
.\desktop\install_portable.ps1
```

默认构建**不包含任何用户数据**；只有显式加 `-SeedData` 才会复制个人数据，且会排除 `settings.json` / `secrets/` / `logs/` 等敏感内容。

---

## 数据与隐私

### 用户数据在哪里

桌面版：

```text
%APPDATA%\PocketMind\data
```

源码版：

```text
<项目目录>\data
```

这些目录**不在本发布包内**，也不会进入 git。

### API Key

- 不再明文写 `settings.json`；
- AES-256-GCM（随机 12 字节 IV + 128-bit tag）加密；
- 主密钥由 Windows DPAPI 保护，绑定当前 Windows 用户；
- 文件：

```text
data/secrets/master.key.dpapi
data/secrets/api_keys.enc
```

- 备份里的 `secrets/` 换 Windows 用户/电脑后无法解密，需要重新填 Key。

### 发布哈希

构建会生成：

```text
release_manifest.json
```

包含：

- git commit / dirty 状态；
- 源码树 SHA-256；
- Python 后端 exe、桌面壳 exe、`desktop/main.js`、`loading.html` 的 SHA-256；
- `code_signing.status = unsigned`。

管理台「总览 → 安全与完整性」可查看当前安装版的源码哈希与构建校验。

> **哈希不是签名。** 当前没有代码签名证书，因此它只用于完整性校验，不能证明发布者身份。

---

## 上下文预算

每轮请求由三层组成：

```text
system prompt（人格/关系/核心事实/检索记忆）
+ 最近 72 小时消息（最多 200 条、8000 字符）
+ 当前消息
```

模型上下文窗口虽然很大，但 PocketMind 每轮只注入“当前相关”的内容；更早内容依靠检索记忆与核心事实，不每轮重放全部历史。模型每轮独立调用，回答结束后本轮上下文结束；数据库/长期记忆不会重置。

可在管理台「记忆参数 → 注入」调整：

- `最近消息时间窗(小时)`：默认 72；
- `最近消息最大条数`：默认 200；
- `最近消息字符预算`：默认 8000；
- `注入字符预算`：默认 4000。

---

## 目录结构

```text
PocketMind-v0.1.0/
├─ README.md
├─ 使用说明.md
├─ .gitignore
├─ release_manifest.json       # 本次构建的源码/产物哈希
├─ SHA256SUMS.txt              # 安装包/后端 exe 哈希
├─ pocketmind/                 # Python 后端、模板、Web、记忆/人格逻辑
├─ tests/                      # 111 项测试
├─ config/                     # 评分等配置
├─ desktop/                    # Electron 壳、构建/安装/更新脚本
├─ installer/
│  ├─ README.md                # 安装包说明（二进制不入 git）
│  └─ PocketMindDesktop/       # 本地纯净便携包（被 .gitignore 排除）
├─ requirements.txt
├─ requirements-dev.txt
├─ pyproject.toml
├─ PocketMind.spec
├─ build_exe.ps1
├─ run_exe.py
└─ 启动PocketMind.cmd
```

---

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check pocketmind tests
```

当前：**111 项测试通过**，ruff 通过。

---

## 发布到 GitHub 的建议

1. 在 GitHub 新建空仓库；
2. 只提交源码和 README（不要提交 `installer/PocketMindDesktop/`、`data/`、`secrets/`、日志、备份）；
3. 仓库根目录就是本目录；
4. 将干净的 `PocketMindDesktop` 打包为压缩包，上传到 **GitHub Releases**，不要直接放进 git；
5. 在 Release 说明里附上 `SHA256SUMS.txt` 的哈希。

如果希望严格可复现，建议在发布前再执行一次：

```powershell
.\desktop\build_portable.ps1
```

并把新生成的 `release_manifest.json` 一起提交。

---

## 当前限制

- Windows 专属（DPAPI / Electron 桌面壳）；
- 桌面版约 491 MB，Electron 运行时较大；
- 未做代码签名，Windows 可能提示未知发布者；
- 模型/Embedding 需要用户自己配置 API Key；
- 尚未附带开源许可证，发布前请自行选择并添加 `LICENSE`。

---

## 版本

- 版本：v0.1.0
- 生成日期：2026-10-27
- 代码提交：见 `release_manifest.json` 的 `git_commit`
