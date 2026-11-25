import { defineConfig } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const backendDir = path.resolve(repoRoot, "backend");
const tmpDataDir = path.resolve(__dirname, "e2e", ".tmp-data");
const venvPython = path.resolve(repoRoot, ".venv", "Scripts", "python.exe");

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  workers: 1,
  fullyParallel: false,
  retries: 0,
  globalSetup: "./e2e/global-setup.js",
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:5175",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    acceptDownloads: true,
    viewport: { width: 1440, height: 900 },
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
  webServer: [
    {
      command: `"${venvPython}" -m uvicorn app.main:app --host 127.0.0.1 --port 8010`,
      cwd: backendDir,
      url: "http://127.0.0.1:8010/api/health",
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        ...process.env,
        STRUCTURAL_AI_DATA_DIR: tmpDataDir,
      },
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 5175 --strictPort",
      cwd: __dirname,
      url: "http://127.0.0.1:5175",
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        ...process.env,
        VITE_API_PROXY_TARGET: "http://127.0.0.1:8010",
      },
    },
  ],
});
