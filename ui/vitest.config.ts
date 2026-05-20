import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { tmpdir } from "node:os";

const localStorageFlag = "--localstorage-file=";
if (!process.env.NODE_OPTIONS?.includes(localStorageFlag)) {
  process.env.NODE_OPTIONS = [
    process.env.NODE_OPTIONS,
    `${localStorageFlag}${path.join(tmpdir(), "corlinman-vitest-localstorage")}`,
  ]
    .filter(Boolean)
    .join(" ");
}

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["node_modules", ".next", "out", "playwright/**", "tests/e2e/**"],
  },
});
