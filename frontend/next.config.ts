import type { NextConfig } from "next";
import { resolve } from "node:path";
import { normalizeBasePath } from "./src/lib/base-path";

const publicBasePath = normalizeBasePath(
  process.env.AOITALK_PUBLIC_BASE_PATH || process.env.NEXT_PUBLIC_BASE_PATH,
);
const productionLifecycle =
  process.env.npm_lifecycle_event === "build:production" ||
  process.env.npm_lifecycle_event === "start";
const resolvedDistDir =
  process.env.NEXT_DIST_DIR ||
  (productionLifecycle
    ? ".next"
    : process.env.NODE_ENV === "development"
      ? ".next-dev"
      : process.env.npm_lifecycle_event === "build"
        ? ".next-verify"
        : ".next");

const incomingRequestLogIgnore = [
  /\/operations\/media\/oauth\/callback(?:[/?]|$)/,
  /\/api\/auth\/handoff\/(?:start|authorize|callback|requests|exchange|client-authorize)(?:[/?]|$)/,
];

const nextConfig: NextConfig = {
  logging: { incomingRequests: { ignore: incomingRequestLogIgnore } },
  // The fixed runtime is a sibling resource inside this repository, never87.
  turbopack: { root: resolve(/* turbopackIgnore: true */ __dirname, "..") },
  outputFileTracingRoot: resolve(/* turbopackIgnore: true */ __dirname, ".."),
  // Docs attachments resolve user-owned workspace files at runtime after the
  // database ACL and realpath boundary checks. Never copy the whole runtime
  // workspace tree into the route's standalone trace.
  outputFileTracingExcludes: {
    "/api/docs/attachments/*": ["../workspaces/**/*"],
  },
  basePath: publicBasePath || undefined,
  env: {
    NEXT_PUBLIC_BASE_PATH: publicBasePath,
    NEXT_PUBLIC_BROWSER_TITLE:
      process.env.AOITALK_BROWSER_TITLE ||
      process.env.NEXT_PUBLIC_BROWSER_TITLE ||
      "AoiTalk",
    NEXT_PUBLIC_BROWSER_ICON:
      process.env.AOITALK_BROWSER_ICON ||
      process.env.NEXT_PUBLIC_BROWSER_ICON ||
      "/favicon.ico",
    NEXT_PUBLIC_BROWSER_BRANDING_ORIGINS:
      process.env.AOITALK_BROWSER_BRANDING_ORIGINS ||
      process.env.NEXT_PUBLIC_BROWSER_BRANDING_ORIGINS ||
      "",
  },
  // 開発・通常検証が稼働中の本番成果物を上書きしないよう出力先を分離する。
  // next dev は .next-dev、npm run build は .next-verify を使用する。
  // npm run build:production と next start だけが本番の .next を使用する。
  distDir: resolvedDistDir,
  typescript: {
    // Production builds use a dedicated config so TypeScript follows only the
    // stable production shim, never the concurrently rewritten next-env.d.ts.
    tsconfigPath:
      resolvedDistDir === ".next"
        ? "tsconfig.production.json"
        : "tsconfig.json",
  },
  experimental: {
    proxyClientMaxBodySize: "100mb",
  },
  // LAN内の他端末からもアクセスを許可
  allowedDevOrigins: ["127.0.0.1", "localhost", "192.168.*.*", "10.*.*.*", "172.16.*.*", "100.*.*.*"],
  async redirects() {
    if (!publicBasePath) return [];

    // A production build can be mounted at /at while users still open the
    // Next.js listener directly on the LAN (for example http://<LAN-IP>:3002/).
    // Redirect the historical unprefixed browser pages into the configured
    // mount instead of returning the basePath 404 page.
    const directEntryRoots = [
      "apps",
      "calendar",
      "change-password",
      "chat",
      "docs",
      "filer",
      "files",
      "login",
      "operations",
      "projects",
      "reports",
      "reset-password",
      "settings",
      "tasks",
    ];

    return [
      {
        source: "/",
        destination: `${publicBasePath}/chat`,
        permanent: false,
        basePath: false as const,
      },
      ...directEntryRoots.map((root) => ({
        source: `/${root}/:path*`,
        destination: `${publicBasePath}/${root}/:path*`,
        permanent: false,
        basePath: false as const,
      })),
      {
        source: "/favicon.ico",
        destination: `${publicBasePath}/favicon.ico`,
        permanent: false,
        basePath: false as const,
      },
      {
        source: "/notifications-sw.js",
        destination: `${publicBasePath}/notifications-sw.js`,
        permanent: false,
        basePath: false as const,
      },
    ];
  },

  async rewrites() {
    const pythonApi =
      process.env.PYTHON_API_URL || "http://127.0.0.1:3000";
    return [
      // ヘルスチェック（認証不要）
      {
        source: "/api/python/health",
        destination: `${pythonApi}/api/health`,
      },
      // LLM関連
      {
        source: "/api/python/llm/:path*",
        destination: `${pythonApi}/api/llm/:path*`,
      },
      // 音声ステータス
      {
        source: "/api/python/voice_status",
        destination: `${pythonApi}/api/voice_status`,
      },
      // キャラクター
      {
        source: "/api/python/characters/:path*",
        destination: `${pythonApi}/api/characters/:path*`,
      },
      {
        source: "/api/python/characters",
        destination: `${pythonApi}/api/characters`,
      },
      {
        source: "/api/python/character/:path*",
        destination: `${pythonApi}/api/character/:path*`,
      },
      // 設定
      {
        source: "/api/python/settings",
        destination: `${pythonApi}/api/settings`,
      },
      // コンフィグ
      {
        source: "/api/python/config",
        destination: `${pythonApi}/api/config`,
      },
      // Knowledge Workspace
      {
        source: "/api/python/knowledge/:path*",
        destination: `${pythonApi}/api/knowledge/:path*`,
      },
      // クローラー
      {
        source: "/api/python/crawler/:path*",
        destination: `${pythonApi}/api/crawler/:path*`,
      },
      // ファイラー
      {
        source: "/api/python/filer/:path*",
        destination: `${pythonApi}/api/filer/:path*`,
      },
      {
        source: "/api/python/filer",
        destination: `${pythonApi}/api/filer`,
      },
      // モバイルコマンド
      {
        source: "/api/python/mobile/:path*",
        destination: `${pythonApi}/api/mobile/:path*`,
      },
    ];
  },
};

export default nextConfig;
