// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0
import fs from 'node:fs';
import path from 'node:path';

const quote = value => `'${String(value).replaceAll("'", "'\\''")}'`;
export function serviceDirectory(env) {
  const root = env.SVDIR || (env.PREFIX && path.join(env.PREFIX, 'var', 'service'));
  if (!root || !path.isAbsolute(root)) throw new Error('Termux requires absolute PREFIX or SVDIR.');
  return path.join(root, 'vika');
}

export function runitScript({ homeDir, env, node = process.execPath }) {
  if (!path.isAbsolute(env.PREFIX || '')) throw new Error('Termux requires an absolute PREFIX.');
  const release = path.join(homeDir, '.vika', 'current');
  const values = { HOME: homeDir, PREFIX: env.PREFIX,
    PATH: env.PATH || `${env.PREFIX}/bin`, TMPDIR: env.TMPDIR || `${env.PREFIX}/tmp`,
    PM2_HOME: path.join(homeDir, '.vika', 'pm2') };
  for (const key of ['LD_PRELOAD', 'LD_LIBRARY_PATH', 'LARK_CLI_BIN', 'SLACK_CLI_BIN', 'VIKA_CONFIG']) {
    if (env[key]) values[key] = env[key];
  }
  // Keep PM2 in the foreground so runit supervises a live process, not a
  // short-lived `vika start` command. CLI controls use this same PM2_HOME.
  return `#!${env.PREFIX}/bin/sh\n` +
    Object.entries(values).map(([key, value]) => `export ${key}=${quote(value)}\n`).join('') +
    `cd ${quote(release)} || exit 1\n` +
    // `vika stop` stops apps but leaves a background PM2 daemon. Transfer this
    // private PM2 home to foreground supervision instead of attaching to it.
    `if [ -f "$PM2_HOME/pm2.pid" ]; then\n` +
    `  ${quote(node)} ${quote(path.join(release, 'node_modules/pm2/bin/pm2'))} kill || exit 1\nfi\n` +
    `exec ${quote(node)} ${quote(path.join(release, 'node_modules/pm2/bin/pm2-runtime'))}` +
    ` start ${quote(path.join(release, 'ecosystem.config.cjs'))} --no-auto-exit\n`;
}

export async function termuxAutostart(action, { homeDir, env, execFileImpl, output }) {
  const directory = serviceDirectory(env);
  const run = path.join(directory, 'run');
  if (action === 'disable') {
    if (fs.existsSync(run)) {
      // Persist disabled state before signaling; don't remove a supervised directory.
      fs.writeFileSync(path.join(directory, 'down'), '');
      await execFileImpl('sv', ['-w', '45', 'down', directory], { env });
    }
    output.write('Termux runit autostart is off.\n');
    return 0;
  }
  const metadata = JSON.parse(fs.readFileSync(path.join(homeDir, '.vika/current/termux-install.json'), 'utf8'));
  const script = runitScript({ homeDir, env: { ...env, LARK_CLI_BIN: env.LARK_CLI_BIN || metadata.larkCli }, node: metadata.node });
  // Write a disabled service first; runsv may discover it during setup.
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(directory, 'down'), '');
  fs.writeFileSync(run, script, { mode: 0o700 });
  fs.chmodSync(run, 0o700);
  fs.rmSync(path.join(directory, 'down'));
  if (fs.existsSync(path.join(directory, 'supervise', 'ok'))) await execFileImpl('sv', ['up', directory], { env });
  output.write(`Termux runit service enabled at ${directory}.\nStart termux-services (restart your Termux shell if needed).\nDevice boot additionally requires Termux:Boot; Android battery restrictions still apply.\n`);
  return 0;
}
