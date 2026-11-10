import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { Readable } from "node:stream";
import { test } from "node:test";
import { saveCredentials } from "../src/lib/credentials.js";
import { DEFAULT_MCP_URL } from "../src/lib/paths.js";
import { parseToolArgs, toolHelp } from "../src/lib/toolargs.js";
import { main } from "../src/main.js";
import { jsonResponse, makeIo } from "./helpers.js";

const SAVE = {
  name: "save_data",
  description: "Save durable company knowledge.\n\nLonger guidance.",
  inputSchema: {
    required: ["content"],
    properties: {
      content: { type: "string", description: "The text to save." },
      title: { anyOf: [{ type: "string" }, { type: "null" }] },
      context_id: { anyOf: [{ type: "integer" }, { type: "null" }] },
      tags: { anyOf: [{ type: "array", items: { type: "string" } }, { type: "null" }] },
      pinned: { type: "boolean", default: false },
      metadata: { anyOf: [{ type: "object" }, { type: "null" }] },
    },
  },
};
const READ = {
  name: "read",
  inputSchema: {
    required: ["identifier"],
    properties: { identifier: { anyOf: [{ type: "integer" }, { type: "string" }, { type: "array" }] } },
  },
};

/** A signed-in io whose fetch is an MCP server offering `tools`. */
async function signedIn(tools, calls = []) {
  const io = makeIo({
    fetch: async (url, init) => {
      const message = JSON.parse(init.body);
      if (!("id" in message)) return new Response(null, { status: 202 });
      let result = { protocolVersion: "2025-06-18" };
      if (message.method === "tools/list") result = { tools };
      if (message.method === "tools/call") {
        calls.push(message.params);
        result = { content: [{ type: "text", text: "saved as data:9" }] };
      }
      return jsonResponse({ jsonrpc: "2.0", id: message.id, result });
    },
  });
  await saveCredentials(io, { access_token: "t", refresh_token: null, expires_at: null, server: DEFAULT_MCP_URL });
  return io;
}

test("tool arguments follow the server's schema: types, arrays, booleans, context and JSON", async () => {
  const { args, json } = await parseToolArgs(
    SAVE,
    ["--content", "Acme renewed", "--context", "578", "--tags", "a", "--tags=b", "--pinned", "--metadata", '{"k":1}', "--json"],
    {},
  );
  assert.deepEqual(args, {
    content: "Acme renewed",
    context_id: 578,
    tags: ["a", "b"],
    pinned: true,
    metadata: { k: 1 },
  });
  assert.equal(json, true);
  assert.deepEqual((await parseToolArgs(SAVE, ["--tags", '["x","y"]', "--pinned=false", "--content", "c"])).args, {
    tags: ["x", "y"],
    pinned: false,
    content: "c",
  });
});

test("a bare word fills the only required option, and the context defaults from the environment", async () => {
  const parsed = await parseToolArgs(SAVE, ["Acme", "renewed"], { env: { PENSIEVE_CONTEXT_ID: "578" } });
  assert.deepEqual(parsed.args, { content: "Acme renewed", context_id: 578 });
  assert.deepEqual((await parseToolArgs(READ, ["12"])).args, { identifier: "12" }, "a string-capable option stays text");
  assert.deepEqual((await parseToolArgs(SAVE, ["--args", '{"content":"x","title":"T"}'])).args, { content: "x", title: "T" });
});

test("unknown options, bad types and stray words are refused with the tool's own help named", async () => {
  await assert.rejects(parseToolArgs(SAVE, ["--nope", "1"]), /save_data has no --nope option.*pensieve save-data --help/);
  await assert.rejects(parseToolArgs(SAVE, ["--context", "abc", "--content", "c"]), /--context-id must be a number/);
  await assert.rejects(parseToolArgs(SAVE, ["--content", "c", "stray"]), /takes its arguments as options/);
  await assert.rejects(parseToolArgs(SAVE, ["--args", "[1]"]), /--args must be a JSON object/);
  assert.match(toolHelp(SAVE), /Usage: pensieve save-data[\s\S]*--content <string>  \(required\)\n {6}The text to save\.[\s\S]*--pinned\n/);
});

test("any server tool runs as a command, with a value read from a file or stdin", async () => {
  const calls = [];
  const io = await signedIn([SAVE], calls);
  writeFileSync(join(io.cwd, "note.md"), "from a file");
  assert.equal(await main(["save-data", "--content-file", "note.md", "--context", "578"], io), 0);
  io.stdin = Readable.from(["from ", "stdin"]);
  assert.equal(await main(["save_data", "--content-file", "-"], io), 0);
  assert.deepEqual(calls, [
    { name: "save_data", arguments: { content: "from a file", context_id: 578 } },
    { name: "save_data", arguments: { content: "from stdin" } },
  ]);
  assert.match(io.stdout.text, /saved as data:9/);
});

test("tools lists the server's tools as commands; --help comes from the schema", async () => {
  const io = await signedIn([SAVE, READ]);
  assert.equal(await main(["tools"], io), 0);
  assert.match(io.stdout.text, /^save-data  Save durable company knowledge\.$/m);
  assert.equal(await main(["save-data", "--help"], io), 0);
  assert.match(io.stdout.text, /Usage: pensieve save-data/);
});

test("a command the server does not offer is unknown; signed out, it says both possibilities", async () => {
  const io = await signedIn([SAVE]);
  assert.equal(await main(["serach", "x"], io), 1);
  assert.match(io.stderr.text, /Unknown command `serach`/);
  const out = makeIo();
  assert.equal(await main(["save-data", "x"], out), 2);
  assert.match(out.stderr.text, /not a built-in command.*Run `pensieve login` first/);
});
