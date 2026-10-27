"use strict";

// 开发用启动器：优先使用环境变量或本地安装的 Electron。
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const appDir = path.resolve(__dirname, "..");
const candidates = [
  process.env.ELECTRON_PATH,
  path.join(appDir, "node_modules", "electron", "dist", "electron.exe"),
  path.join(appDir, "..", "node_modules", "electron", "dist", "electron.exe"),
].filter(Boolean);

const electron = candidates.find((p) => fs.existsSync(p));
if (!electron) {
  console.error("未找到 Electron。请设置 ELECTRON_PATH，或在 desktop 目录 npm install electron。");
  process.exit(1);
}
console.log("Electron:", electron);
const env = { ...process.env };
// 某些开发 shell 可能设置 ELECTRON_RUN_AS_NODE=1；这会让 Electron 退化成 Node。
delete env.ELECTRON_RUN_AS_NODE;
const child = spawn(electron, [appDir], { stdio: "inherit", windowsHide: false, env });
child.on("exit", (code, signal) => {
  process.exit(code == null ? (signal ? 1 : 0) : code);
});
