import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  experimental: {
    proxyClientMaxBodySize: "100mb",
  },
  // LAN内の他端末からもアクセスを許可
  allowedDevOrigins: ["127.0.0.1", "localhost", "192.168.*.*", "10.*.*.*", "172.16.*.*", "100.*.*.*"],
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
