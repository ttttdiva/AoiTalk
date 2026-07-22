import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const FRONTEND_ROOT = path.resolve(path.dirname(SCRIPT_PATH), "..");
const DEFAULT_ROOT = path.join(FRONTEND_ROOT, "src");

const WRITE_PATTERNS = [
  { name: "knowledgeNodes insert", regex: /\.insert\s*\(\s*knowledgeNodes\s*\)/ },
  { name: "knowledgeNodes update", regex: /\.update\s*\(\s*knowledgeNodes\s*\)/ },
  { name: "bodyText write", regex: /^\s*bodyText\s*:(?!\s*string\b)/ },
];

function normalize(filePath) {
  return filePath.replace(/\\/g, "/");
}

function isAllowedFile(filePath) {
  const normalized = normalize(path.relative(FRONTEND_ROOT, filePath));
  return normalized === "src/lib/server/docs-node-writer.ts" || normalized === "src/db/schema.ts";
}

function collectFiles(rootDir) {
  if (!fs.existsSync(rootDir)) return [];
  const files = [];
  for (const entry of fs.readdirSync(rootDir, { withFileTypes: true })) {
    const fullPath = path.join(rootDir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
      files.push(...collectFiles(fullPath));
      continue;
    }
    if (/\.(ts|tsx|js|jsx|mjs|cjs)$/.test(entry.name)) files.push(fullPath);
  }
  return files;
}

export function findViolations(rootDir = DEFAULT_ROOT) {
  const violations = [];
  for (const filePath of collectFiles(rootDir)) {
    if (isAllowedFile(filePath)) continue;
    const text = fs.readFileSync(filePath, "utf8");
    const lines = text.split(/\r?\n/);
    for (const [index, line] of lines.entries()) {
      for (const pattern of WRITE_PATTERNS) {
        if (pattern.regex.test(line)) {
          violations.push({
            filePath,
            line: index + 1,
            kind: pattern.name,
            text: line.trim(),
          });
        }
      }
    }
  }
  return violations;
}

if (process.argv[1] && path.resolve(process.argv[1]) === SCRIPT_PATH) {
  const rootDir = process.argv[2] ? path.resolve(process.argv[2]) : DEFAULT_ROOT;
  const violations = findViolations(rootDir);
  if (violations.length > 0) {
    console.error("Docs node write gate failed. Use src/lib/server/docs-node-writer.ts.");
    for (const violation of violations) {
      const relative = normalize(path.relative(FRONTEND_ROOT, violation.filePath));
      console.error(`- ${relative}:${violation.line} ${violation.kind}: ${violation.text}`);
    }
    process.exit(1);
  }
  console.log("Docs node write gate passed.");
}
