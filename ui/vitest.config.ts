import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { tmpdir } from "node:os";

// Node's experimental file-backed localStorage (`--localstorage-file=`) only
// exists on Node >= 22 and is REJECTED in NODE_OPTIONS on Node 20 ("not allowed
// in NODE_OPTIONS"), which crashes every vitest worker at startup before a single
// test runs. The jsdom `environment` below already provides `localStorage`, so
// only opt into the Node-level flag where it is actually supported — keeping the
// suite runnable on Node 20 (CI) and Node 22+ alike.
const nodeMajor = Number(process.versions.node.split(".")[0]);
const localStorageFlag = "--localstorage-file=";
if (nodeMajor >= 22 && !process.env.NODE_OPTIONS?.includes(localStorageFlag)) {
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
