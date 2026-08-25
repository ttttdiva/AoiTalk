// 検証用の Next.js 開発サーバーを、稼働中のサーバーとは別の distDir で起動する。
//
// ユーザー環境の Next.js（ポート3002）は `.next` を配信しているため、
// 同じ distDir を使うと検証ビルドがユーザー環境を壊す。
// このスクリプトは NEXT_DIST_DIR を指定して起動するので、
// 稼働中のサーバーを止めずに実画面検証ができる。
//
//   node scripts/dev-verify.mjs [--port 3012] [--dist-dir .next-dev-verify]

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const args = process.argv.slice(2);

function readOption(name, fallback) {
  const index = args.indexOf(`--${name}`);
  if (index >= 0 && args[index + 1]) return args[index + 1];
  return fallback;
}

const port = readOption("port", process.env.PORT || "3012");
const distDir = readOption("dist-dir", process.env.NEXT_DIST_DIR || ".next-dev-verify");
const hostname = readOption("hostname", "127.0.0.1");

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const nextBin = path.join(frontendDir, "node_modules", "next", "dist", "bin", "next");

const child = spawn(
  process.execPath,
  [nextBin, "dev", "--port", String(port), "--hostname", hostname],
  {
    cwd: frontendDir,
    env: { ...process.env, NEXT_DIST_DIR: distDir },
    stdio: "inherit",
  },
);

child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});
