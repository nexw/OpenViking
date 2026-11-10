#!/usr/bin/env node
// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';

export const shellQuote = value => `'${String(value).replaceAll("'", "'\\''")}'`;
const sourceRoot = fileURLToPath(new URL('..', import.meta.url));
function exists(p) { try { fs.lstatSync(p); return true; } catch (e) { if (e.code === 'ENOENT') return false; throw e; } }

export function installTermux({ source = sourceRoot, env = process.env,
  platform = process.platform, node = process.execPath,
  run = execFileSync, output = process.stdout } = {}) {
  if (platform !== 'android' || !path.isAbsolute(env.PREFIX || '') || !path.isAbsolute(env.HOME || '')) {
    throw new Error('Run this installer inside native Termux with absolute HOME and PREFIX.');
  }
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major < 22 || major === 22 && minor < 19) throw new Error('Node >=22.19.0 is required.');
  const db = new DatabaseSync(':memory:');
  try { db.prepare('SELECT 1').get(); } finally { db.close(); }
  const shell = path.join(env.PREFIX, 'bin', 'sh');
  fs.accessSync(shell, fs.constants.X_OK);
  const lookup = name => (env.PATH || '').split(path.delimiter)
    .map(dir => path.join(dir, name)).find(p => {
      try { fs.accessSync(p, fs.constants.X_OK); return fs.statSync(p).isFile(); } catch { return false; }
    });
  const lark = env.LARK_CLI_BIN || lookup('lark-cli');
  if (!lark || !path.isAbsolute(lark)) throw new Error('Set LARK_CLI_BIN to your native lark-cli absolute path.');
  fs.accessSync(lark, fs.constants.X_OK);
  run(lark, ['--version'], { env, stdio: 'inherit', timeout: 15_000 });
  const npm = lookup('npm');
  if (!npm) throw new Error('npm is required.');
  const base = path.join(env.HOME, '.vika');
  const current = path.join(base, 'current');
  const launcher = path.join(base, 'bin', 'vika');
  if (exists(current) || exists(launcher)) throw new Error('An installation already exists. See docs/termux.md for manual updates.');
  fs.mkdirSync(path.join(base, 'releases'), { recursive: true });
  const release = fs.mkdtempSync(path.join(base, 'releases', 'termux-'));
  let linked = false;
  let launcherCreated = false;
  try {
    // Copy only application inputs; never copy credentials, node_modules or local data.
    for (const name of fs.readdirSync(source)) {
      if (!['packages', 'scripts', 'bin', 'docs', 'package.json', 'package-lock.json',
        'permission.json', 'config.example.json', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'README.md', 'install.md',
        'deployment-config.cjs', 'ecosystem.config.cjs'].includes(name)) continue;
      fs.cpSync(path.join(source, name), path.join(release, name), {
        recursive: true,
        filter: p => !['node_modules', '.env', '.git', 'dist'].includes(path.basename(p)),
      });
    }
    // lark-cli is optional in npm: Android skips its unsupported npm wrapper.
    // Do not execute package download hooks or fetch Linux Node/CLI binaries.
    const childEnv = { ...env, LARK_CLI_BIN: lark, npm_config_script_shell: shell };
    run(npm, ['ci', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund'],
      { cwd: release, env: childEnv, stdio: 'inherit' });
    fs.mkdirSync(path.join(release, 'bin'), { recursive: true });
    fs.writeFileSync(path.join(release, 'bin', 'pm2'),
      `#!${shell}\nexec ${shellQuote(node)} ${shellQuote(path.join(release, 'node_modules/pm2/bin/pm2'))} "$@"\n`, { mode: 0o755 });
    for (const check of ['check-pi.mjs', 'check-durable.mjs']) {
      run(node, [path.join(release, 'packages/daemon/scripts', check)],
        { cwd: release, env: childEnv, stdio: 'inherit', timeout: 60_000 });
    }
    fs.writeFileSync(path.join(release, 'termux-install.json'), JSON.stringify({
      node, shell, larkCli: lark, source: fs.realpathSync(source),
    }, null, 2) + '\n', { mode: 0o600 });
    fs.mkdirSync(path.dirname(launcher), { recursive: true });
    // Link only after the pinned Pi and Durable checks pass.
    fs.symlinkSync(release, current);
    linked = true;
    fs.writeFileSync(launcher, `#!${shell}\n` +
      `if [ -z "\${LARK_CLI_BIN:-}" ]; then LARK_CLI_BIN=${shellQuote(lark)}; fi\nexport LARK_CLI_BIN\n` +
      `exec ${shellQuote(node)} ${shellQuote(path.join(current, 'packages/cli/bin/vika.mjs'))} "$@"\n`,
      { flag: 'wx', mode: 0o755 });
    launcherCreated = true;
    output.write(`Installed ${release}\nRun ${launcher} setup\nNo services have been started.\n`);
    return release;
  } catch (e) {
    if (launcherCreated) fs.rmSync(launcher);
    if (linked) fs.unlinkSync(current);
    fs.rmSync(release, { recursive: true, force: true });
    throw e;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv.length !== 2) throw new Error('usage: node scripts/install-termux.mjs');
    installTermux();
  } catch (e) { console.error(e.message); process.exitCode = 1; }
}
