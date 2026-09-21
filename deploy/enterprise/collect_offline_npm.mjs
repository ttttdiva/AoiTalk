// Runs inside the handoff's pinned Node linux/amd64 container only.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawnSync } from 'node:child_process';

const [source, output] = process.argv.slice(2);
if (!source || !output) throw new Error('source and output paths are required');
const rawLock = fs.readFileSync(path.join(source, 'frontend/package-lock.json'));
const lock = JSON.parse(rawLock);
if (![2, 3].includes(lock.lockfileVersion) || !lock.packages) throw new Error('unsupported npm lock format');
const cache = path.join(output, 'npm-cache');
fs.mkdirSync(cache, { recursive: true });
const work = fs.mkdtempSync('/tmp/enterprise-npm-');
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
try {
  const urls = new Set();
  for (const [name, row] of Object.entries(lock.packages)) {
    if (!name) continue;
    if (!row.resolved || !row.integrity || row.link) throw new Error('lock has a non-registry/unhashed dependency');
    const url = new URL(row.resolved);
    if (url.protocol !== 'https:' || url.hostname !== 'registry.npmjs.org' || url.port || url.username || url.password || url.search || url.hash)
      throw new Error('lock dependency is outside the official credential-free npm registry');
    urls.add(url.href);
  }
  // npm ci on amd64 alone skips other-platform optional packages. The consumer
  // binds every integrity in the original lock, so explicitly fetch all rows.
  for (const url of [...urls].sort()) npm(['cache', 'add', url]);
  fs.copyFileSync(path.join(source, 'frontend/package.json'), path.join(work, 'package.json'));
  fs.writeFileSync(path.join(work, 'package-lock.json'), rawLock);
  npm(['ci', '--offline']);
  if (!fs.readFileSync(path.join(work, 'package-lock.json')).equals(rawLock)) throw new Error('npm modified the source lock');
  for (const name of fs.readdirSync(cache)) {
    if (name !== '_cacache') fs.rmSync(path.join(cache, name), { recursive: true, force: true });
  }
  const policy = { npm: { cache: 'npm-cache', offline: true,
    package_lock_sha256: crypto.createHash('sha256').update(rawLock).digest('hex') } };
  fs.writeFileSync(path.join(output, 'npm-collection.json'), JSON.stringify(policy));
} finally {
  fs.rmSync(work, { recursive: true, force: true });
}
