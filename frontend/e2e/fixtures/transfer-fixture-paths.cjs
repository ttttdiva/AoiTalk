const { createHash } = require("node:crypto");
const { lstatSync, mkdirSync, mkdtempSync, realpathSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { isAbsolute, join, relative, resolve, sep } = require("node:path");

const repositoryRoot = resolve(__dirname, "../../..");
const normalize = (p) => process.platform === "win32" ? resolve(p).toLowerCase() : resolve(p);
function isInsideOrEqual(parent, candidate) {
  const rel = relative(normalize(parent), normalize(candidate));
  return rel === "" || (rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel));
}
function resolveTransferFixturePaths({ repo = repositoryRoot, temp = tmpdir(), realpath = realpathSync } = {}) {
  // Check both names before any creation, then check the resolved aliases.
  if (isInsideOrEqual(repo, temp)) throw new Error("OS temp is inside the repository");
  const canonicalRepo = realpath(repo);
  const canonicalTemp = realpath(temp);
  if (isInsideOrEqual(canonicalRepo, canonicalTemp)) throw new Error("OS temp resolves inside the repository");
  const key = createHash("sha256").update(normalize(canonicalRepo)).digest("hex").slice(0, 16);
  const bundleDir = join(canonicalTemp, `aoitalk-transfer-browser-fixture-${key}`);
  if (isInsideOrEqual(canonicalRepo, bundleDir)) throw new Error("Bundle is inside the repository");
  return { bundleDir, canonicalRepo, canonicalTemp };
}
// Reject links and reparse points even when they resolve outside the checkout.
function assertRegularPath(path, { lstat = lstatSync, realpath = realpathSync } = {}) {
  let stat;
  try { stat = lstat(path); } catch (error) {
    if (error.code === "ENOENT") return;
    throw error;
  }
  if (stat.isSymbolicLink() || normalize(realpath(path)) !== normalize(path))
    throw new Error(`Fixture path is a link or reparse point: ${path}`);
}
function getTransferFixtureBundleFile(name, overrides) {
  if (!["admin-restart-entry.js", "transfer-download-entry.js"].includes(name)) throw new Error("Unknown fixture bundle");
  const { bundleDir } = resolveTransferFixturePaths(overrides);
  assertRegularPath(bundleDir);
  const file = join(bundleDir, name);
  assertRegularPath(file);
  return file;
}
function ensureTransferFixtureBundleDir(overrides) {
  const { bundleDir } = resolveTransferFixturePaths(overrides);
  assertRegularPath(bundleDir);
  try { mkdirSync(bundleDir); } catch (error) { if (error.code !== "EEXIST") throw error; }
  assertRegularPath(bundleDir);
  return bundleDir;
}
function createTransferFixtureOutputDir(overrides) {
  const { canonicalTemp } = resolveTransferFixturePaths(overrides);
  return mkdtempSync(join(canonicalTemp, "aoitalk-playwright-transfer-fixture-"));
}

module.exports = { isInsideOrEqual, resolveTransferFixturePaths, assertRegularPath, getTransferFixtureBundleFile, ensureTransferFixtureBundleDir, createTransferFixtureOutputDir };
