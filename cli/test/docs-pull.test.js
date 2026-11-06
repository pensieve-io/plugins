import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import docs from "../src/commands/docs.js";
import { saveCredentials } from "../src/lib/credentials.js";
import { POINTER_START } from "../src/lib/pointer.js";
import { fakeSpawn, makeIo, tempDir } from "./helpers.js";

const schema = (...names) => ({
  properties: Object.fromEntries(
    names.map((n) => [n, n === "context_id" || n === "depth" ? { anyOf: [{ type: "integer" }, { type: "null" }] } : {}]),
  ),
});
const TOOLS = [
  { name: "list_trees", inputSchema: schema("context_id") },
  { name: "get_tree", inputSchema: schema("tree", "depth", "include_summaries", "cursor", "context_id") },
  { name: "read", inputSchema: { properties: { identifier: { anyOf: [{ type: "string" }, { type: "integer" }] }, tree: {}, cursor: {}, context_id: schema("context_id").properties.context_id } } },
];

/** A stub MCP server answering in JSON; records tool calls. */
function mcpServer() {
  const calls = [];
  const fetch = async (url, init) => {
    const message = JSON.parse(init.body);
    if (!("id" in message)) return new Response(null, { status: 202 });
    let result = {};
    if (message.method === "tools/list") result = { tools: TOOLS };
    if (message.method === "tools/call") {
      const { name, arguments: args } = message.params;
      calls.push([name, args]);
      const text = {
        list_trees: "Available Page trees (2):\n- `main` — Org (main, tree id `a`, root `page:1`)\n- `acme/app@main` — App (repository, tree id `b`, root `page:10`, status completed, processed 1111111)",
        get_tree: '# Tree\n\napp "App" (page:10)\n└── api "API" (page:11)\n',
        read: `# Page ${args.identifier}\n\nBody.`,
      }[name];
      result = { content: [{ type: "text", text }] };
    }
    return new Response(JSON.stringify({ jsonrpc: "2.0", id: message.id, result }), {
      headers: { "Content-Type": "application/json" },
    });
  };
  return { fetch, calls };
}

test("docs pull writes the repository tree, ignores the output, adds the pointer once and flags staleness", async () => {
  const root = tempDir();
  writeFileSync(join(root, "AGENTS.md"), "# Agents\n");
  writeFileSync(join(root, ".gitignore"), "node_modules/");
  const git = {
    "rev-parse --show-toplevel": root,
    "remote get-url origin": "git@github.com:acme/app.git",
    "rev-parse --abbrev-ref HEAD": "main",
    "rev-parse HEAD": "2222222222222222222222222222222222222222",
  };
  const server = mcpServer();
  const io = makeIo({
    cwd: root,
    fetch: server.fetch,
    spawn: fakeSpawn((command, args) => {
      const key = args.join(" ");
      if (key in git) return { stdout: `${git[key]}\n` };
      if (args[0] === "check-ignore") {
        return readFileSync(join(root, ".gitignore"), "utf8").includes(`/${args[2]}/`) ? 0 : 1;
      }
      throw new Error(`unexpected ${command} ${key}`);
    }),
    env: {},
  });
  await saveCredentials(io, { access_token: "t", server: "https://mcp.pensieve.uk/mcp", expires_at: null });

  for (let run = 0; run < 2; run++) {
    assert.equal(await docs.run(io, { pointer: true, context: "7" }, ["pull"]), 0);
  }

  const out = join(root, ".pensieve/docs");
  assert.match(readFileSync(join(out, "app.md"), "utf8"), /# Page page:10/);
  assert.match(readFileSync(join(out, "api.md"), "utf8"), /# Page page:11/);
  assert.match(readFileSync(join(out, "index.md"), "utf8"), /Processed revision: `1111111`[\s\S]*Stale/);
  assert.equal(readFileSync(join(root, ".gitignore"), "utf8"), "node_modules/\n/.pensieve/docs/\n");
  assert.equal(readFileSync(join(root, "AGENTS.md"), "utf8").split(POINTER_START).length, 2);
  assert.match(io.stderr.text, /Pensieve processed 1111111, but local HEAD is 222222222222/);
  assert.deepEqual(server.calls[1], ["get_tree", { context_id: 7, tree: "acme/app@main", depth: 12, include_summaries: false }]);
  assert.deepEqual(server.calls[2][1], { identifier: "page:10", context_id: 7, tree: "acme/app@main" });
});
