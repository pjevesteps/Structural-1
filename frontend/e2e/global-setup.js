import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..", "..");
const tmpDataDir = path.resolve(__dirname, ".tmp-data");
const venvPython = path.resolve(repoRoot, ".venv", "Scripts", "python.exe");
const fixtureScript = path.resolve(__dirname, "generate_fixture.py");
const samplePdf = path.resolve(tmpDataDir, "sample_plan.pdf");

export default async function globalSetup() {
  // Clean previous run's isolated data so every E2E run starts fresh
  fs.rmSync(tmpDataDir, { recursive: true, force: true });
  fs.mkdirSync(path.join(tmpDataDir, "projects"), { recursive: true });
  fs.mkdirSync(path.join(tmpDataDir, "imports"), { recursive: true });
  fs.mkdirSync(path.join(tmpDataDir, "exports"), { recursive: true });

  execFileSync(venvPython, [fixtureScript, samplePdf], {
    stdio: "inherit",
  });
}
