import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "node",
    setupFiles: ["src/__tests__/setup-component-tests.ts"],
    include: [
      "src/components/operations/media/**/*.test.ts",
      "src/components/operations/media/**/*.test.tsx",
      "src/lib/media-operations-*.test.ts",
    ],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
});
