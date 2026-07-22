import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    // 既定は既存の node 環境ユニットテスト（src/**/__tests__/**/*.test.ts）。
    // コンポーネントの描画テスト（*.test.tsx）は各ファイル先頭の
    // `// @vitest-environment jsdom` コメントで個別に jsdom を選択する。
    environment: "node",
    setupFiles: ["src/__tests__/setup-component-tests.ts"],
    include: [
      "src/**/__tests__/**/*.test.ts",
      "src/**/__tests__/**/*.test.tsx",
    ],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
});
