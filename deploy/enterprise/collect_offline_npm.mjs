// Runs inside the handoff's pinned Node linux/amd64 container only.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

function bundledDependencyName(installPath) {
  const marker = '/node_modules/';
  const split = installPath.lastIndexOf(marker);
  if (split <= 0) return null;
  const childPath = installPath.slice(split + marker.length);
  if (!childPath) return null;
  const parts = childPath.split('/');
  if (childPath.startsWith('@')) {
    if (parts.length < 2) return null;
    return `${parts[0]}/${parts[1]}`;
  }
  return parts[0] || null;
}

function isParentBundledDependency(lock, installPath) {
  const marker = '/node_modules/';
  const split = installPath.lastIndexOf(marker);
  if (split <= 0) return false;
  const parentPath = installPath.slice(0, split);
  const dependencyName = bundledDependencyName(installPath);
  const parent = lock.packages?.[parentPath];
  if (!dependencyName || !parent) return false;

  const bundled = parent.bundleDependencies ?? parent.bundledDependencies;
  if (bundled === true) {
    return Boolean(parent.dependencies && Object.prototype.hasOwnProperty.call(parent.dependencies, dependencyName));
  }
  return Array.isArray(bundled) && bundled.includes(dependencyName);
}

function packageNameFromInstallPath(installPath) {
  const marker = '/node_modules/';
  let relative;
  const split = installPath.lastIndexOf(marker);
  if (split >= 0) relative = installPath.slice(split + marker.length);
  else if (installPath.startsWith('node_modules/')) relative = installPath.slice('node_modules/'.length);
  else return null;
  const parts = relative.split('/');
  if (relative.startsWith('@')) {
    if (parts.length < 2) return null;
    return `${parts[0]}/${parts[1]}`;
  }
  return parts[0] || null;
}

export function installScriptSpecsFromLock(lock) {
  if (![2, 3].includes(lock.lockfileVersion) || !lock.packages) throw new Error('unsupported npm lock format');
  const specs = new Set();
  for (const [installPath, row] of Object.entries(lock.packages)) {
    if (!installPath || row.hasInstallScript !== true) continue;
    const name = typeof row.name === 'string' && row.name ? row.name : packageNameFromInstallPath(installPath);
    const version = row.version;
    const hasResolved = typeof row.resolved === 'string' && row.resolved.length > 0;
    const hasIntegrity = typeof row.integrity === 'string' && row.integrity.length > 0;
    if (!hasResolved || !hasIntegrity) throw new Error('install-script dependency lacks an integrity-bound registry spec');
    const url = new URL(row.resolved);
    const officialRegistry = url.protocol === 'https:' && url.hostname === 'registry.npmjs.org' &&
      !url.port && !url.username && !url.password && !url.search && !url.hash;
    if (!officialRegistry || !name || !/^(?:@[a-z0-9][a-z0-9._-]*\/)?[a-z0-9][a-z0-9._-]*$/.test(name) ||
        typeof version !== 'string' || !/^[0-9A-Za-z][0-9A-Za-z.+_-]*$/.test(version)) {
      throw new Error('install-script dependency lacks an exact registry spec');
    }
    specs.add(`${name}@${version}`);
  }
  return [...specs].sort();
}

const RUNTIME_NAME = '@aoitalk/seethrough-rig-runtime';
const RUNTIME_PATH = 'resources/seethrough-rig/runtime';
const RUNTIME_LOCK_PATH = '../resources/seethrough-rig/runtime';
const RUNTIME_SPEC = 'file:../resources/seethrough-rig/runtime';

// Source bytes are authenticated by the handoff's source_tree/binding/proof,
// not by a registry tarball. Keep this exception specific to the fixed runtime.
function plainPath(target, kind) {
  let current = path.resolve(target);
  let first = true;
  for (;;) {
    const info = fs.lstatSync(current);
    if (info.isSymbolicLink() || (first && kind === 'file' ? !info.isFile() : !info.isDirectory())) {
      throw new Error('npm source contains a symlink or non-regular path');
    }
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
    first = false;
  }
}

function sourceFile(source, relative) {
  const file = path.join(source, relative);
  plainPath(file, 'file');
  return fs.readFileSync(file);
}

function validRuntimeVersion(value) {
  if (typeof value !== 'string') return false;
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/.exec(value);
  return Boolean(match && match[0] === value && (!match[4] || match[4].split('.').every((part) => !/^0\d+$/.test(part))));
}

function validatedLocalRuntime(lock, source) {
  if (!source) return null;
  const manifest = JSON.parse(sourceFile(source, 'frontend/package.json'));
  const root = lock.packages[''];
  if (manifest.dependencies?.[RUNTIME_NAME] !== RUNTIME_SPEC ||
      root?.dependencies?.[RUNTIME_NAME] !== RUNTIME_SPEC) {
    throw new Error('unexpected local runtime dependency');
  }
  for (const document of [manifest, root]) {
    for (const group of ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies']) {
      for (const [name, spec] of Object.entries(document[group] ?? {})) {
        if ((name === RUNTIME_NAME && (group !== 'dependencies' || spec !== RUNTIME_SPEC)) ||
            (name !== RUNTIME_NAME && typeof spec === 'string' &&
             /^(?:file:|link:|workspace:|\.{1,2}[\\/]|[\\/]|[A-Za-z]:)/.test(spec))) {
          throw new Error('unexpected local runtime dependency');
        }
      }
    }
  }
  const runtime = path.join(source, RUNTIME_PATH);
  plainPath(runtime, 'directory');
  const pkg = JSON.parse(sourceFile(source, RUNTIME_PATH + '/package.json'));
  if (pkg.name !== RUNTIME_NAME || !validRuntimeVersion(pkg.version)) {
    throw new Error('unexpected local runtime identity/version');
  }
  for (const field of ['scripts', 'dependencies', 'devDependencies', 'optionalDependencies',
    'peerDependencies', 'bundleDependencies', 'bundledDependencies', 'workspaces', 'bin', 'gypfile']) {
    if (Object.hasOwn(pkg, field)) throw new Error('unsupported local runtime scripts/dependencies');
  }
  const localRow = lock.packages[RUNTIME_LOCK_PATH];
  const linkKey = 'node_modules/' + RUNTIME_NAME;
  const linkRow = lock.packages[linkKey];
  if (!localRow || localRow.name !== pkg.name || localRow.version !== pkg.version ||
      Object.keys(localRow).some((key) => !['name', 'version', 'license'].includes(key)) ||
      !linkRow || linkRow.link !== true || linkRow.resolved !== RUNTIME_LOCK_PATH ||
      Object.keys(linkRow).some((key) => !['resolved', 'link'].includes(key))) {
    throw new Error('local runtime lock/source contract mismatch');
  }
  const files = [];
  function visit(directory) {
    for (const entry of fs.readdirSync(directory)) {
      const file = path.join(directory, entry);
      const info = fs.lstatSync(file);
      if (info.isSymbolicLink() || (!info.isDirectory() && !info.isFile())) {
        throw new Error('npm source contains a symlink or non-regular path');
      }
      if (info.isDirectory()) {
        if (entry === 'node_modules') throw new Error('unsupported local runtime dependencies');
        visit(file);
      } else {
        if (['npm-shrinkwrap.json', 'package-lock.json'].includes(entry) ||
            (directory === runtime && entry === 'binding.gyp')) {
          // npm can run node-gyp implicitly even without package.json scripts.
          throw new Error('unsupported local runtime scripts/dependencies');
        }
        files.push(file);
      }
    }
  }
  visit(runtime);
  return { runtime, files, rowKeys: new Set([RUNTIME_LOCK_PATH, linkKey]) };
}

export function registryUrlsFromLock(lock, source) {
  if (![2, 3].includes(lock.lockfileVersion) || !lock.packages) throw new Error('unsupported npm lock format');
  const local = validatedLocalRuntime(lock, source);
  const urls = new Set();

  for (const [name, row] of Object.entries(lock.packages)) {
    if (!name || local?.rowKeys.has(name)) continue;
    if (row.link) throw new Error('lock has a non-registry/unhashed dependency');

    const hasResolved = typeof row.resolved === 'string' && row.resolved.length > 0;
    const hasIntegrity = typeof row.integrity === 'string' && row.integrity.length > 0;
    if (!hasResolved || !hasIntegrity) {
      // npm lockfile v3 can represent dependencies bundled inside a parent
      // registry tarball without their own resolved/integrity fields. Those
      // bytes are covered by the parent's registry URL + integrity and are
      // installed from that cached parent tarball by npm ci --offline.
      if (hasResolved || hasIntegrity || !isParentBundledDependency(lock, name)) {
        throw new Error('lock has a non-registry/unhashed dependency');
      }
      continue;
    }

    const url = new URL(row.resolved);
    if (url.protocol !== 'https:' || url.hostname !== 'registry.npmjs.org' || url.port || url.username || url.password || url.search || url.hash) {
      throw new Error('lock dependency is outside the official credential-free npm registry');
    }
    urls.add(url.href);
  }

  return [...urls].sort();
}

export function stageNpmSource(source, scratch) {
  plainPath(scratch, 'directory');
  if (fs.readdirSync(scratch).length) throw new Error('npm scratch must be private and empty');
  const rawLock = sourceFile(source, 'frontend/package-lock.json');
  const lock = JSON.parse(rawLock);
  const urls = registryUrlsFromLock(lock, source);
  const installScriptSpecs = installScriptSpecsFromLock(lock);
  const local = validatedLocalRuntime(lock, source);
  const work = path.join(scratch, 'frontend');
  fs.mkdirSync(work);
  fs.writeFileSync(path.join(work, 'package.json'), sourceFile(source, 'frontend/package.json'));
  fs.writeFileSync(path.join(work, 'package-lock.json'), rawLock);
  for (const file of local.files) {
    // Recheck immediately before copying, including every ancestor.
    plainPath(file, 'file');
    const target = path.join(scratch, RUNTIME_PATH, path.relative(local.runtime, file));
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(file, target, fs.constants.COPYFILE_EXCL);
  }
  return { work, rawLock, urls, installScriptSpecs };
}

function main() {
  const [source, output] = process.argv.slice(2);
  if (!source || !output) throw new Error('source and output paths are required');
  const cache = path.join(output, 'npm-cache');
  fs.mkdirSync(cache, { recursive: true });
  const scratch = fs.mkdtempSync('/tmp/enterprise-npm-');
  try {
    const { work, rawLock, urls, installScriptSpecs } = stageNpmSource(source, scratch);
    const userConfig = path.join(work, 'user.npmrc');
    const globalConfig = path.join(work, 'global.npmrc');
    fs.writeFileSync(userConfig, '');
    fs.writeFileSync(globalConfig, '');
    const options = ['--registry=https://registry.npmjs.org', '--strict-ssl=true', `--cache=${cache}`,
      `--userconfig=${userConfig}`, `--globalconfig=${globalConfig}`, '--audit=false', '--fund=false', '--update-notifier=false'];
    function npm(args) {
      const child = spawnSync('npm', [...args, ...options], { cwd: work, stdio: 'inherit' });
      if (child.error || child.status !== 0) throw new Error('npm closure acquisition failed');
    }

    // npm ci on amd64 alone skips other-platform optional packages. The consumer
    // binds every integrity in the original lock, so explicitly fetch all
    // registry-backed rows. Bundled rows are covered by their parent tarball.
    for (const url of urls) npm(['cache', 'add', url]);
    // A tarball URL populates cacache content but not the package packument.
    // Install scripts such as esbuild's exact-version fallback can invoke npm
    // during offline ci and require that metadata. Cache the exact locked specs
    // while connected so the later --offline lifecycle remains network-free.
    for (const spec of installScriptSpecs) npm(['cache', 'add', spec]);
    npm(['ci', '--offline']);
    if (!fs.readFileSync(path.join(work, 'package-lock.json')).equals(rawLock)) throw new Error('npm modified the source lock');
    for (const name of fs.readdirSync(cache)) {
      if (name !== '_cacache') fs.rmSync(path.join(cache, name), { recursive: true, force: true });
    }
    const policy = { npm: { cache: 'npm-cache', offline: true,
      package_lock_sha256: crypto.createHash('sha256').update(rawLock).digest('hex') } };
    fs.writeFileSync(path.join(output, 'npm-collection.json'), JSON.stringify(policy));
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main();
}
