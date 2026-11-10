import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import prime from "../src/commands/prime.js";
import setup from "../src/commands/setup.js";
import { CLIENTS } from "../src/lib/clients.js";
import { saveCredentials } from "../src/lib/credentials.js";
import { DEFAULT_MCP_URL, receiptPath } from "../src/lib/paths.js";
import { PRIMING_MARKERS, PRIMING_TEXT } from "../src/lib/priming.js";
import { binDir, fakeSpawn, jsonResponse, makeIo } from "./helpers.js";

const mcpFetch = async (url, init) => {
  const message = JSON.parse(init.body);
  if (!("id" in message)) return new Response(null, { status: 202 });
  let result = { protocolVersion: "2025-06-18" };
  if (message.method === "tools/list") result = { tools: [{ name: "list_contexts", inputSchema: { properties: {} } }] };
  if (message.method === "tools/call") result = { content: [{ type: "text", text: "- **Acme** (id: `7`)" }] };
  return jsonResponse({ jsonrpc: "2.0", id: message.id, result });
};

/** Codex with the plugin installed and signed in; nothing left to do. */
const codexReady = () =>
  fakeSpawn((command, args) => {
    const line = [command, ...args].join(" ");
    if (line === "codex plugin marketplace list") return { stdout: "MARKETPLACE  ROOT\npensieve  /x\n" };
    if (line === "codex plugin list") {
      return { stdout: "PLUGIN             STATUS              VERSION  SOURCE\npensieve@pensieve  installed, enabled  local    /x\n" };
    }
    if (line === "codex mcp list --json") return { stdout: JSON.stringify([{ name: "pensieve", auth_status: "o_auth" }]) };
    return 0;
  });

test("setup on a ready machine changes nothing, and proves the server answers", async () => {
  const io = makeIo({ spawn: codexReady(), fetch: mcpFetch, env: { PATH: binDir(["codex"]) } });
  await saveCredentials(io, { access_token: "t", refresh_token: null, expires_at: null, server: DEFAULT_MCP_URL });
  assert.equal(await setup.run(io, {}), 0);
  assert.deepEqual(io.spawn.calls.filter((c) => !c.includes("list")), [], "nothing is installed or signed in twice");
  assert.equal(existsSync(receiptPath(io)), false);
  assert.match(io.stdout.text, /Already signed in\.[\s\S]*\*\*Acme\*\*[\s\S]*Pensieve answered/);
});

test("Codex is signed in only when its MCP list says it is not", async () => {
  const login = CLIENTS.codex.steps.find((s) => s.id === "login");
  const io = (stdout, code = 0) => makeIo({ spawn: fakeSpawn(() => ({ code, stdout })) });
  assert.equal(await login.present(io(JSON.stringify([{ name: "pensieve", auth_status: "o_auth" }]))), true);
  assert.equal(await login.present(io(JSON.stringify([{ name: "pensieve", auth_status: "not_logged_in" }]))), false);
  assert.equal(await login.present(io(JSON.stringify([{ name: "other", auth_status: "o_auth" }]))), false);
  assert.equal(await login.present(io("", 1)), false);
});

test("setup without a client still signs the CLI in and checks it", async () => {
  const io = makeIo({ fetch: mcpFetch });
  await saveCredentials(io, { access_token: "t", refresh_token: null, expires_at: null, server: DEFAULT_MCP_URL });
  assert.equal(await setup.run(io, {}), 0);
  assert.match(io.stdout.text, /Neither `claude` nor `codex` is on PATH[\s\S]*Pensieve answered/);
});

test("prime prints the instructions, and --write keeps one current block per file", async () => {
  const io = makeIo({ spawn: fakeSpawn(() => 128) });
  assert.equal(await prime.run(io, {}), 0);
  assert.equal(io.stdout.text, `${PRIMING_TEXT}\n`);
  assert.match(PRIMING_TEXT, /MCP tools when this session has them\. Only when it has none,\nrun the `pensieve` CLI/);

  assert.equal(await prime.run(io, { write: true }), 0);
  const created = readFileSync(join(io.cwd, "AGENTS.md"), "utf8");
  assert.equal(created, `${PRIMING_MARKERS[0]}\n${PRIMING_TEXT}\n${PRIMING_MARKERS[1]}\n`);
  assert.equal(existsSync(join(io.cwd, "CLAUDE.md")), false, "only AGENTS.md is created");

  writeFileSync(join(io.cwd, "CLAUDE.md"), "# Mine\n");
  writeFileSync(join(io.cwd, "AGENTS.md"), created.replace("## Pensieve", "## Stale"));
  assert.equal(await prime.run(io, { write: true }), 0);
  assert.equal(readFileSync(join(io.cwd, "AGENTS.md"), "utf8"), created, "a stale block is refreshed in place");
  assert.equal(readFileSync(join(io.cwd, "CLAUDE.md"), "utf8"), `# Mine\n\n${created}`);
});
