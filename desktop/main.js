"use strict";

const { app, BrowserWindow, Menu, dialog, session, shell } = require("electron");
const { spawn, execFile, spawnSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");

// 便携包启动诊断：即使主进程在窗口创建前退出，也留一条记录。
try {
  fs.appendFileSync(path.join(__dirname, "boot.log"), `[${new Date().toISOString()}] main loaded packaged=${require("electron").app.isPackaged}\n`, "utf8");
} catch (_) {}

const APP_TITLE = "PocketMind";
const PROJECT_ROOT = path.resolve(__dirname, "..");
const LOADING_FILE = path.join(__dirname, "loading.html");
const WINDOW_STATE_FILE = () => path.join(app.getPath("userData"), "window-state.json");
const LOG_FILE = () => path.join(app.getPath("userData"), "desktop-shell.log");

app.setName("PocketMind");
if (process.platform === "win32") app.setAppUserModelId("com.pocketmind.desktop");

let mainWindow = null;
let backend = null;
let backendUrl = "";
let backendStopping = false;
let quitting = false;

function log(message) {
  try {
    fs.mkdirSync(path.dirname(LOG_FILE()), { recursive: true });
    fs.appendFileSync(LOG_FILE(), `[${new Date().toISOString()}] ${message}\n`, "utf8");
  } catch (_) {}
}

function loadWindowState() {
  try {
    const raw = fs.readFileSync(WINDOW_STATE_FILE(), "utf8");
    const state = JSON.parse(raw);
    if (Number.isFinite(state.width) && Number.isFinite(state.height)) return state;
  } catch (_) {}
  return { width: 1380, height: 900 };
}

function saveWindowState(win) {
  if (!win || win.isDestroyed()) return;
  try {
    const bounds = win.getBounds();
    fs.mkdirSync(path.dirname(WINDOW_STATE_FILE()), { recursive: true });
    fs.writeFileSync(WINDOW_STATE_FILE(), JSON.stringify({
      x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height,
      maximized: win.isMaximized(),
    }), "utf8");
  } catch (_) {}
}

function resolveBackendCommand() {
  if (app.isPackaged) {
    const exe = path.join(process.resourcesPath, "backend", "PocketMind.exe");
    if (!fs.existsSync(exe)) {
      throw new Error("找不到内置后端：" + exe);
    }
    return {
      command: exe,
      args: ["--desktop"],
      cwd: path.dirname(exe),
      dataDir: path.join(app.getPath("userData"), "data"),
    };
  }
  const python = path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe");
  if (!fs.existsSync(python)) {
    throw new Error("找不到项目 Python 环境：" + python);
  }
  // 开发时直接用源码，保证壳测试的是当前代码；打包时才使用 PyInstaller 后端。
  return {
    command: python,
    args: ["-m", "pocketmind.launcher", "--desktop"],
    cwd: PROJECT_ROOT,
    dataDir: path.join(PROJECT_ROOT, "data"),
  };
}

function prepareDataDir(dataDir) {
  fs.mkdirSync(dataDir, { recursive: true });
  const db = path.join(dataDir, "pocketmind.db");
  if (fs.existsSync(db)) return;
  // 首次运行：把便携包内的种子数据复制到用户目录，避免升级/移动安装目录时丢记忆。
  const seeds = [
    path.join(process.resourcesPath, "backend", "data"),
    path.join(path.dirname(process.execPath), "data"),
  ];
  for (const seed of seeds) {
    if (!fs.existsSync(seed)) continue;
    try {
      fs.cpSync(seed, dataDir, { recursive: true, force: false, errorOnExist: false });
      log(`seeded user data from ${seed}`);
      return;
    } catch (error) {
      log(`seed data failed: ${error.message}`);
    }
  }
}

function waitHealth(url, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const poll = () => {
      if (quitting) return reject(new Error("应用正在退出"));
      const req = http.get(url + "/health", { timeout: 1200 }, (res) => {
        res.resume();
        if (res.statusCode === 200) return resolve();
        retry();
      });
      req.on("timeout", () => { req.destroy(); retry(); });
      req.on("error", retry);
    };
    const retry = () => {
      if (Date.now() > deadline) return reject(new Error("后端在 45 秒内未就绪"));
      setTimeout(poll, 280);
    };
    poll();
  });
}

function startBackend() {
  return new Promise((resolve, reject) => {
    let command;
    try {
      command = resolveBackendCommand();
    } catch (error) {
      return reject(error);
    }
    prepareDataDir(command.dataDir);
    log(`starting backend: ${command.command} ${command.args.join(" ")} data=${command.dataDir}`);
    const env = {
      ...process.env,
      POCKETMIND_DESKTOP: "1",
      POCKETMIND_NO_BROWSER: "1",
      POCKETMIND_DATA_DIR: command.dataDir,
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
    };
    backend = spawn(command.command, command.args, {
      cwd: command.cwd,
      env,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });

    let stdoutBuffer = "";
    let stderrBuffer = "";
    let settled = false;
    let readyTimer = null;

    const fail = (error) => {
      if (settled) return;
      settled = true;
      if (readyTimer) clearTimeout(readyTimer);
      reject(error);
    };

    const handleOutput = (chunk, stream) => {
      const text = chunk.toString("utf8");
      log(`${stream}: ${text.trimEnd()}`);
      if (stream !== "stdout") return;
      stdoutBuffer += text;
      const match = stdoutBuffer.match(/POCKETMIND_DESKTOP_URL=(http:\/\/127\.0\.0\.1:\d+)/);
      if (!match || settled) return;
      backendUrl = match[1];
      waitHealth(backendUrl)
        .then(() => {
          if (settled) return;
          settled = true;
          if (readyTimer) clearTimeout(readyTimer);
          log(`backend ready: ${backendUrl}`);
          resolve(backendUrl);
        })
        .catch(fail);
    };

    backend.stdout.on("data", (chunk) => handleOutput(chunk, "stdout"));
    backend.stderr.on("data", (chunk) => handleOutput(chunk, "stderr"));
    backend.on("error", (error) => fail(error));
    backend.on("exit", (code, signal) => {
      backend = null;
      log(`backend exited: code=${code} signal=${signal}`);
      if (!settled) {
        fail(new Error(`后端提前退出（code=${code}）`));
        return;
      }
      if (!quitting && !backendStopping) {
        dialog.showErrorBox(APP_TITLE, "本地后端已停止，应用将退出。\n请查看 desktop-shell.log。");
        app.quit();
      }
    });
    readyTimer = setTimeout(() => fail(new Error("后端启动超时（45 秒）")), 45000);
  });
}

function stopBackend() {
  if (!backend || backendStopping) return;
  backendStopping = true;
  const pid = backend.pid;
  log(`stopping backend pid=${pid}`);
  try { backend.kill(); } catch (_) {}
  // Windows 下确保子进程树被结束，避免壳退出后后端还占着端口。
  if (process.platform === "win32" && pid) {
    try { spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true, timeout: 4000 }); } catch (_) {}
  }
  backend = null;
}

function isBackendUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "http:" && url.hostname === "127.0.0.1" && url.port === new URL(backendUrl || "http://127.0.0.1:0").port;
  } catch (_) {
    return false;
  }
}

function installPermissions() {
  const ses = session.defaultSession;
  ses.setPermissionRequestHandler((webContents, permission, callback, details) => {
    const requestingUrl = (details && details.requestingUrl) || (webContents && webContents.getURL()) || "";
    const mediaAllowed = permission === "media" && isBackendUrl(requestingUrl);
    if (!mediaAllowed) log(`permission denied: ${permission} ${requestingUrl}`);
    callback(mediaAllowed);
  });
  ses.setPermissionCheckHandler((webContents, permission, requestingOrigin) => {
    return permission === "media" && isBackendUrl(requestingOrigin);
  });
}

const TITLEBAR_HTML = `
<div id="pm-desktop-titlebar" aria-label="PocketMind 标题栏">
  <span class="pm-title-dot"></span>
  <span class="pm-title-brand">PocketMind</span>
  <span class="pm-title-sep">/</span>
  <span class="pm-title-sub">本地桌面版</span>
</div>`;

const TITLEBAR_CSS = `
html.pm-desktop body { padding-top: 42px !important; }
html.pm-desktop body > .app { height: calc(100vh - 42px) !important; min-height: 0 !important; }
html.pm-desktop body > .layout { min-height: calc(100vh - 42px) !important; }
html.pm-desktop[data-pm-page="space"] body > .layout { height: calc(100vh - 42px - 66px) !important; min-height: 0 !important; }
html.pm-desktop[data-pm-page="space"] body { overflow: hidden !important; }
html.pm-desktop[data-pm-page="admin"] body > .layout { min-height: calc(100vh - 42px) !important; }
#pm-desktop-titlebar {
  position: fixed; inset: 0 0 auto 0; height: 42px; z-index: 2147483647;
  display: flex; align-items: center; gap: 9px; box-sizing: border-box;
  padding: 0 154px 0 16px;
  background: rgba(248, 248, 246, 0.82);
  -webkit-backdrop-filter: blur(18px) saturate(1.25);
  backdrop-filter: blur(18px) saturate(1.25);
  border-bottom: 1px solid rgba(32, 33, 36, 0.08);
  -webkit-app-region: drag; user-select: none;
  font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  color: #202124;
}
#pm-desktop-titlebar .pm-title-dot {
  width: 7px; height: 7px; border-radius: 50%; background: #4d5661;
  box-shadow: 0 0 0 3px rgba(77, 86, 97, 0.10); flex: 0 0 auto;
}
#pm-desktop-titlebar .pm-title-brand {
  font-size: 12px; font-weight: 600; letter-spacing: -0.01em; color: #202124;
}
#pm-desktop-titlebar .pm-title-sep { color: #b9b9b4; font-size: 11px; }
#pm-desktop-titlebar .pm-title-sub { color: #8a8c90; font-size: 10px; letter-spacing: 0.02em; }
@media (prefers-color-scheme: dark) {
  #pm-desktop-titlebar { background: rgba(32, 33, 36, 0.84); color: #f2f2ef; border-bottom-color: rgba(255,255,255,.08); }
  #pm-desktop-titlebar .pm-title-brand { color: #f2f2ef; }
  #pm-desktop-titlebar .pm-title-sub, #pm-desktop-titlebar .pm-title-sep { color: #a6a7a3; }
}
`;

function installTitlebar(win) {
  const page = win.webContents.getURL().includes("/space") ? "space"
    : win.webContents.getURL().includes("/admin") ? "admin" : "chat";
  win.webContents.insertCSS(TITLEBAR_CSS).catch(() => {});
  win.webContents.executeJavaScript(`(() => {
    document.documentElement.classList.add('pm-desktop');
    document.documentElement.dataset.pmPage = ${JSON.stringify(page)};
    if (!document.getElementById('pm-desktop-titlebar')) {
      document.body.insertAdjacentHTML('afterbegin', ${JSON.stringify(TITLEBAR_HTML)});
    }
  })();`, true).catch(() => {});
}

function createWindow() {
  const saved = loadWindowState();
  const win = new BrowserWindow({
    x: Number.isFinite(saved.x) ? saved.x : undefined,
    y: Number.isFinite(saved.y) ? saved.y : undefined,
    width: saved.width || 1380,
    height: saved.height || 900,
    minWidth: 980,
    minHeight: 680,
    show: false,
    backgroundColor: "#f6f6f4",
    title: APP_TITLE,
    icon: path.join(__dirname, "assets", "icon.png"),
    titleBarStyle: "hidden",
    titleBarOverlay: process.platform === "win32" ? {
      color: "#f6f6f4",
      symbolColor: "#202124",
      height: 42,
    } : undefined,
    webPreferences: {
      backgroundThrottling: false,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });
  mainWindow = win;

  win.loadFile(LOADING_FILE);
  win.once("ready-to-show", () => {
    win.show();
    if (saved.maximized) win.maximize();
  });
  win.on("close", () => saveWindowState(win));
  win.on("closed", () => { mainWindow = null; });

  win.webContents.on("page-title-updated", (event) => event.preventDefault());
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url) && !isBackendUrl(url)) shell.openExternal(url).catch(() => {});
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => {
    if (url.startsWith("file://") || isBackendUrl(url)) return;
    event.preventDefault();
    if (/^https?:\/\//.test(url)) shell.openExternal(url).catch(() => {});
  });
  win.webContents.on("did-finish-load", () => installTitlebar(win));
  win.webContents.on("did-navigate-in-page", () => installTitlebar(win));
  win.webContents.on("render-process-gone", (_event, details) => {
    log(`renderer gone: ${JSON.stringify(details)}`);
    dialog.showErrorBox(APP_TITLE, "页面进程异常退出，应用将重启窗口。");
    if (!quitting) {
      win.reload();
    }
  });
  return win;
}

async function boot() {
  Menu.setApplicationMenu(null);
  const win = createWindow();
  try {
    await startBackend();
    log(`loading UI: ${backendUrl}`);
    await win.loadURL(backendUrl + "/");
  } catch (error) {
    log("boot failed: " + (error && error.stack ? error.stack : error));
    dialog.showErrorBox(APP_TITLE, "桌面版启动失败：\n" + (error && error.message ? error.message : String(error)) + "\n\n日志：" + LOG_FILE());
    stopBackend();
    app.quit();
  }
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    installPermissions();
    app.on("activate", () => {
      if (!mainWindow) createWindow();
    });
    boot();
  });

  app.on("window-all-closed", () => {
    // 关闭窗口 = 退出；关闭后端进程，避免页面关了应用还在跑。
    quitting = true;
    stopBackend();
    app.quit();
  });

  app.on("before-quit", (event) => {
    if (!quitting) {
      quitting = true;
      stopBackend();
    }
  });

  process.on("uncaughtException", (error) => {
    log("uncaught: " + (error && error.stack ? error.stack : error));
    try { dialog.showErrorBox(APP_TITLE, "桌面版发生异常：\n" + error.message + "\n\n日志：" + LOG_FILE()); } catch (_) {}
    try { stopBackend(); } catch (_) {}
    app.exit(1);
  });
}
