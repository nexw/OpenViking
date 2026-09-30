// Build the OpenViking MCP mount config for DSH.
//
// DSH's MCP bridge natively supports `streamable-http` against the server's
// /mcp endpoint, and the harness already runs in-process, so we reach that
// endpoint from the plugin instead of spawning a stdio child that would just
// re-bridge the same URL through fetch. The shape produced here is the one
// `@deepseek-ai/dsh-mcp-client`'s zod union validates, so a drift in either
// side fails at mount instead of silently breaking the tool surface.
//
// To keep wire parity with the stdio child this replaces, every value the
// proxy forwarded is derived here through the same shared mappers: URL
// normalization, identity headers, actor-peer gating, and reserved-header
// filtering of OPENVIKING_EXTRA_HEADERS. Recalling those helpers from the
// memory-plugin-shared override is deliberate: it stays the single authority
// for "how a resolved harness config maps to a wire request", so adding a
// new MCP integration later won't drift from this one.

import { buildOvHeaders } from "./shared/ov-http.mjs";
import { toMcpProxyConfig } from "./shared/mcp-proxy-config.mjs";
import { MCP_SERVER_NAME } from "./config.mjs";

export function buildMcpConfig(config) {
  const connection = toMcpProxyConfig(config, {
    env: process.env,
  });

  // `toMcpProxyConfig` runs `resolveMcpActorPeerId` when `peerId` is left
  // undefined: the actor header goes out only when the caller opted into actor
  // recall. `parseExtraHeaders` already dropped any reserved names from
  // `connection.extraHeaders`, so merging opExtras under `built` here can only
  // disagree on X-OpenViking-* or User-Agent — and `built` is the version the
  // stdio child sent on the wire.
  const built = buildOvHeaders(connection, {
    actorPeerId: connection.peerId,
  });
  const opExtras = connection.extraHeaders;
  const headers = opExtras && typeof opExtras === "object"
    ? { ...opExtras, ...built }
    : built;

  return {
    transport: "streamable-http",
    serverName: MCP_SERVER_NAME,
    url: connection.mcpUrl,
    headers,
    toolCallTimeoutMs: config.mcpToolCallTimeoutMs,
  };
}