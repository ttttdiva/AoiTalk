import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const projectRoot = path.dirname(frontendRoot);
const outputRoot = path.resolve(frontendRoot, process.env.NEXT_DIST_DIR || ".next");

// Verification builds must never update the production build's marker.
if (outputRoot !== path.join(frontendRoot, ".next")) {
  console.log("Skipping production fingerprint for a separate Next.js output directory.");
  process.exit(0);
}

const venvPython = path.join(
  projectRoot,
  "venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
const python = process.env.AOITALK_PYTHON ||
  (existsSync(venvPython) ? venvPython : process.platform === "win32" ? "python" : "python3");
const result = spawnSync(python, ["-c", `
from pathlib import Path
from src.service_manager._frontend_build import (
    _frontend_build_fingerprint,
    _frontend_static_build_invalid_reason,
    _write_frontend_build_fingerprint,
)

frontend = Path.cwd() / "frontend"
reason = _frontend_static_build_invalid_reason(frontend)
if reason:
    raise SystemExit(f"Frontend build remains invalid: {reason}")
_write_frontend_build_fingerprint(frontend, _frontend_build_fingerprint(frontend))
print("Frontend production build fingerprint saved.")
`], {
  cwd: projectRoot,
  env: { ...process.env, PYTHONUTF8: "1" },
  stdio: "inherit",
});

if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
