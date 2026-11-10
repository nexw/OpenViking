// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { installTermux } from '../../../scripts/install-termux.mjs';
import { autostartCommand, autostartEnabled } from '../src/autostart.mjs';
import { runitScript, serviceDirectory } from '../src/termux-autostart.mjs';
import * as android from '../../daemon/src/platform/android.mjs';
import { allowedPm2Environment } from '../../updater/src/pm2-platform.mjs';

function fixture(t, prefix = "vika-termux-' ") {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const env = { HOME: path.join(root, 'home'), PREFIX: path.join(root, 'prefix') };
  env.PATH = path.join(env.PREFIX, 'bin');
  fs.mkdirSync(env.PATH, { recursive: true });
  for (const name of ['sh', 'npm', 'lark-cli']) fs.writeFileSync(path.join(env.PATH, name), '', { mode: 0o755 });
  const source = path.join(root, 'source');
  fs.mkdirSync(source);
  fs.writeFileSync(path.join(source, 'package.json'), '{}');
  fs.writeFileSync(path.join(source, 'permission.json'), '{"scopes":{}}');
  fs.writeFileSync(path.join(source, '.env'), 'private');
  const calls = [];
  return { root, env, source, calls, platform: 'android',
    run: (...args) => calls.push(args), output: { write() {} } };
}

test('Termux installation activates only after native checks; wrappers quote paths', t => {
  const f = fixture(t);
  const release = installTermux(f);
  assert.equal(fs.realpathSync(path.join(f.env.HOME, '.vika/current')), release);
  assert.equal(fs.existsSync(path.join(release, '.env')), false);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(release, 'permission.json'))), { scopes: {} });
  assert.deepEqual(f.calls[1][1], ['ci', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund']);
  assert.match(f.calls[2][1][0], /check-pi.mjs$/);
  assert.match(f.calls[3][1][0], /check-durable.mjs$/);
  const launcher = path.join(f.env.HOME, '.vika/bin/vika');
  execFileSync('sh', ['-n', launcher]);
  execFileSync('sh', ['-n', path.join(release, 'bin/pm2')]);
  assert.throws(() => installTermux(f), /already exists/);
  assert.equal(fs.realpathSync(path.join(f.env.HOME, '.vika/current')), release);
});

test('failed runtime check leaves no active installation or staged release', t => {
  const f = fixture(t);
  f.run = (_cmd, args) => { if (args[0]?.endsWith('check-durable.mjs')) throw new Error('broken durable'); };
  assert.throws(() => installTermux(f), /broken durable/);
  assert.equal(fs.existsSync(path.join(f.env.HOME, '.vika/current')), false);
  assert.equal(fs.existsSync(path.join(f.env.HOME, '.vika/bin/vika')), false);
  assert.deepEqual(fs.readdirSync(path.join(f.env.HOME, '.vika/releases')), []);
});

test('non-Android install is refused before invoking dependencies', t => {
  const f = fixture(t);
  assert.throws(() => installTermux({ ...f, platform: 'linux' }), /native Termux/);
  assert.deepEqual(f.calls, []);
});

test('runit uses foreground PM2, persists down state and supports re-enable', async t => {
  const f = fixture(t);
  installTermux(f);
  const calls = [];
  const options = { env: f.env, homeDir: f.env.HOME, platform: android,
    output: f.output, execFileImpl: async (...args) => { calls.push(args); } };
  const directory = serviceDirectory(f.env);
  assert.equal(await autostartCommand(['enable'], options), 0);
  assert.equal(autostartEnabled(options), true);
  const run = fs.readFileSync(path.join(directory, 'run'), 'utf8');
  assert.match(run, /pm2-runtime/);
  assert.match(run, /PM2_HOME=/);
  assert.doesNotMatch(run, /systemctl|vika['"]? start/);
  execFileSync('sh', ['-n', path.join(directory, 'run')]);
  fs.mkdirSync(path.join(directory, 'supervise'));
  fs.writeFileSync(path.join(directory, 'supervise/ok'), '');
  assert.equal(await autostartCommand(['disable'], options), 0);
  assert.equal(autostartEnabled(options), false);
  assert.deepEqual(calls.at(-1).slice(0, 2), ['sv', ['-w', '45', 'down', directory]]);
  await autostartCommand(['enable'], options);
  assert.equal(autostartEnabled(options), true);
  assert.deepEqual(calls.at(-1).slice(0, 2), ['sv', ['up', directory]]);
});

test('runit environment quoting is literal, and secrets are not persisted', t => {
  const f = fixture(t);
  const script = runitScript({ homeDir: f.env.HOME,
    env: { ...f.env, PATH: "x'$(touch /should-not-exist)", OPENAI_API_KEY: 'secret-token' } });
  const run = path.join(f.root, 'run');
  fs.writeFileSync(run, script);
  execFileSync('sh', ['-n', run]);
  assert.doesNotMatch(script, /secret-token/);
  const values = allowedPm2Environment({ PREFIX: '/prefix', LD_PRELOAD: '/prefix/lib/termux-exec.so',
    LD_LIBRARY_PATH: '/prefix/lib', TMPDIR: '/prefix/tmp', SECRET: 'x' }, [], { implementation: android });
  assert.deepEqual(values, { PREFIX: '/prefix', TMPDIR: '/prefix/tmp',
    LD_PRELOAD: '/prefix/lib/termux-exec.so', LD_LIBRARY_PATH: '/prefix/lib' });
});

test('generated service supervises a real PM2 app and terminates it on SIGTERM', { timeout: 25_000 }, async t => {
  const f = fixture(t, 'vika-termux-service-');
  const release = path.join(f.env.HOME, '.vika/current');
  fs.mkdirSync(path.join(release, 'node_modules'), { recursive: true });
  fs.symlinkSync(fileURLToPath(new URL('../../../node_modules/pm2', import.meta.url)),
    path.join(release, 'node_modules/pm2'), 'dir');
  fs.writeFileSync(path.join(release, 'app.cjs'), 'setInterval(() => {}, 1000);');
  fs.writeFileSync(path.join(release, 'ecosystem.config.cjs'),
    `module.exports={apps:[{name:'termux-fixture',script:__dirname+'/app.cjs',interpreter:${JSON.stringify(process.execPath)}}]};`);
  const env = { ...f.env, PATH: process.env.PATH, TMPDIR: os.tmpdir(),
    PM2_HOME: path.join(f.env.HOME, '.vika/pm2') };
  const run = path.join(f.root, 'run');
  fs.writeFileSync(run, runitScript({ homeDir: f.env.HOME, env }));
  const child = spawn('sh', [run], { env, stdio: ['ignore', 'pipe', 'pipe'] });
  let log = '';
  child.stdout.on('data', data => { log += data; });
  child.stderr.on('data', data => { log += data; });
  const exited = new Promise((resolve, reject) => { child.once('exit', resolve); child.once('error', reject); });
  const pidFile = path.join(env.PM2_HOME, 'pids/termux-fixture-0.pid');
  try {
    const deadline = Date.now() + 12_000;
    while (!fs.existsSync(pidFile)) {
      if (child.exitCode !== null || Date.now() > deadline) throw new Error(`PM2 service did not start: ${log}`);
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    const appPid = Number(fs.readFileSync(pidFile, 'utf8').trim());
    process.kill(appPid, 0);
    child.kill('SIGTERM');
    await exited;
    assert.throws(() => process.kill(appPid, 0), { code: 'ESRCH' });
  } finally {
    if (child.exitCode === null) {
      child.kill('SIGTERM');
      await exited;
    }
  }
});
