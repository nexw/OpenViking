import assert from "node:assert/strict";
import test from "node:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Config } from "@deepseek-ai/dsh-mcp-client";
import { resolveConfig } from "./config.mjs";
import { apply } from "./index.mjs";
import { buildMcpConfig, mountOpenVikingMcp } from "./mcp.mjs";

/**
 * Run `fn` with credential files in a throwaway directory so the
 * developer's own ~/.openviking never leaks into what the plugin resolves.
 */
function withCredentialFiles({ ovcli = null, ov = null } = {}, fn) {
  const dir = mkdtempSync(join(tmpdir(), "ov-dsh-mcp-"));
  try {
    const env = {
      OPENVIKING_CLI_CONFIG_FILE: join(dir, "ovcli.conf"),
      OPENVIKING_CONFIG_FILE: join(dir, "ov.conf"),
      OPENVIKING_HOME: join(dir, "home"),
    };
    if (ovcli) writeFileSync(env.OPENVIKING_CLI_CONFIG_FILE, JSON.stringify(ovcli));
    if (ov) writeFileSync(env.OPENVIKING_CONFIG_FILE, JSON.stringify(ov));
    return fn({ env, dir });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("the mount config validates against the pinned bridge's own schema", () => {
  const config = resolveConfig({
    endpoint: "http://127.0.0.1:1933",
    apiKey: "secret",
    workspacePeer: false,
  }, {});
  const parsed = Config(buildMcpConfig(config));

  // streamable-http, not stdio — the plugin talks to /mcp from the host
  // process; the stdio child proxy was a workaround for hosts that could
  // only configure MCP servers, and dsh's bridge has streamable-http
  // natively.
  assert.equal(parsed.transport, "streamable-http");
  assert.equal(parsed.serverName, "openviking");
  assert.equal(parsed.url, "http://127.0.0.1:1933/mcp");
  assert.equal(parsed.toolCallTimeoutMs, 60000);
  // Startup must not be fatal: recall, capture, and commit keep working when
  // only the MCP endpoint is down, and the bridge reconnects on its own.
  assert.equal(parsed.failOnStartupError, false);
  assert.equal(parsed.reconnect.enabled, true);
});

test("credentials resolved by the plugin reach the wire as headers", () => {
  const config = resolveConfig({
    endpoint: "http://ov.example.com/",
    apiKey: "secret",
    account: "acme",
    user: "casey",
    peerId: "workspace-peer",
  }, {});
  const headers = buildMcpConfig(config).headers;

  assert.equal(headers.Authorization, "Bearer secret");
  assert.equal(headers["X-OpenViking-Account"], "acme");
  assert.equal(headers["X-OpenViking-User"], "casey");
  // Default is broad recall: the child used to receive the runtime peer for
  // possible actor-scoped use, but the shared mapper must not put it on the
  // wire and hide memories from other workspaces. The bridge's requestInit
  // honors the same decision.
  assert.equal(headers["X-OpenViking-Actor-Peer"], undefined);
});

test("actor scope lifts the actor peer header onto the wire", () => {
  const config = resolveConfig({
    endpoint: "http://ov.example.com/",
    apiKey: "secret",
    account: "acme",
    user: "casey",
    peerId: "workspace-peer",
    recallPeerScope: "actor",
  }, {});
  const headers = buildMcpConfig(config).headers;

  assert.equal(headers["X-OpenViking-Actor-Peer"], "workspace-peer");
});

test("a key the plugin left empty stays empty on the wire", () => {
  // ovcli.conf naming only a url pins the chain to that file, so the
  // runtime sends no key. The credentials under ov.conf must not fill the
  // gap: an empty Authorization means an anonymous request.
  withCredentialFiles({
    ovcli: { url: "https://ov.example.com" },
    ov: { server: { root_api_key: "root-key" } },
  }, ({ env: files, dir }) => {
    const config = resolveConfig({ workspacePeer: false }, files, dir);
    assert.equal(config.apiKey, "");

    const headers = buildMcpConfig(config).headers;
    assert.equal(headers.Authorization, undefined);
  });
});

test("the auth mode and timeout the host named reach the wire too", () => {
  // Host named api_key mode outranks the environment's trusted mode: the
  // identity headers must not go out for an operator that the server is
  // not told to trust.
  const config = resolveConfig({
    endpoint: "http://ov.example.com",
    apiKey: "secret",
    account: "acme",
    user: "casey",
    authMode: "api_key",
    mcpToolCallTimeoutMs: 45000,
  }, {});
  const parsed = Config(buildMcpConfig(config));

  assert.equal(buildMcpConfig(config).headers["X-OpenViking-Account"], undefined);
  assert.equal(buildMcpConfig(config).headers["X-OpenViking-User"], undefined);
  assert.equal(parsed.toolCallTimeoutMs, 45000);
});

test("ambient env cannot fill a gap the runtime left", () => {
  // DSH only scrubs KEY|PASSWORD|SECRET|TOKEN names from inherited env, so
  // these would reach an in-process helper. The runtime is the authority
  // and must not have its empty fields filled in by the ambient env.
  withCredentialFiles({}, ({ env: files, dir }) => {
    mkdirSync(join(dir, "repo", ".git"), { recursive: true });
    writeFileSync(join(dir, "repo", ".git", "HEAD"), "ref: refs/heads/main\n");
    const inherited = {
      ...files,
      OPENVIKING_CREDENTIALS_SOURCE: "cli",
      OPENVIKING_ACCOUNT: "stale-account",
      OPENVIKING_USER: "stale-user",
      OPENVIKING_PEER_ID: "stale-peer",
    };
    const config = resolveConfig({ workspacePeer: false }, inherited, dir);
    const seen = (c) => ({
      account: c.account,
      user: c.user,
      peerId: c.peerId,
      sendIdentityHeaders: c.sendIdentityHeaders,
    });
    assert.deepEqual(seen(config), {
      account: "",
      user: "",
      peerId: "",
      sendIdentityHeaders: false,
    });

    const headers = buildMcpConfig(config).headers;
    assert.equal(headers.Authorization, undefined);
    assert.equal(headers["X-OpenViking-Account"], undefined);
    assert.equal(headers["X-OpenViking-User"], undefined);
    assert.equal(headers["X-OpenViking-Actor-Peer"], undefined);
  });
});

test("an anonymous local server sends no key and no identity", () => {
  withCredentialFiles({}, ({ env: files, dir }) => {
    const headers = buildMcpConfig(
      resolveConfig({ endpoint: "http://127.0.0.1:1933", workspacePeer: false }, files, dir),
    ).headers;

    assert.equal(headers.Authorization, undefined);
    assert.equal(headers["X-OpenViking-Account"], undefined);
    assert.equal(headers["X-OpenViking-User"], undefined);
  });
});

test("mounting passes the bridge plugin and its config to the host context", () => {
  const mounted = [];
  mountOpenVikingMcp(
    { plugin: (plugin, config) => mounted.push({ plugin, config }) },
    resolveConfig({ endpoint: "http://127.0.0.1:1933", workspacePeer: false }, {}),
  );

  assert.equal(mounted.length, 1);
  assert.equal(mounted[0].plugin.name, "mcp-client");
  assert.equal(typeof mounted[0].plugin.apply, "function");
  assert.equal(mounted[0].config.serverName, "openviking");
});

test("apply mounts the tool surface instead of registering tools itself", () => {
  const mounted = [];
  const registered = [];
  apply({
    logger: { debug() {} },
    provide() {},
    effect(execute) {
      execute();
      return async () => {};
    },
    plugin: (plugin, config) => mounted.push({ plugin, config }),
    tools: { register: definition => registered.push(definition) },
    on() {},
  }, { endpoint: "http://127.0.0.1:1933", workspacePeer: false });

  assert.deepEqual(registered, []);
  const bridge = mounted.find((entry) => entry.plugin.name === "mcp-client");
  assert.ok(bridge, "the MCP bridge must be mounted");
  assert.equal(bridge.config.url, "http://127.0.0.1:1933/mcp");
});