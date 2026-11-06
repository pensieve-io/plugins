import assert from "node:assert/strict";
import { test } from "node:test";
import { McpClient, buildArguments, parseSse, splitContinuation, toolText } from "../src/lib/mcp.js";

const TOOLS = [
  {
    name: "search",
    inputSchema: {
      properties: {
        query: { type: "string" },
        trees: { anyOf: [{ type: "array", items: { type: "string" } }, { type: "null" }] },
        top_k: { type: "integer" },
        context_id: { anyOf: [{ type: "integer", exclusiveMinimum: 0 }, { type: "null" }] },
      },
    },
  },
  {
    name: "read",
    inputSchema: {
      properties: {
        identifier: { anyOf: [{ type: "integer" }, { type: "string" }, { type: "array" }] },
        around: { anyOf: [{ type: "integer" }, { type: "null" }] },
        cursor: { anyOf: [{ type: "string" }, { type: "null" }] },
        context_id: { anyOf: [{ type: "integer" }, { type: "null" }] },
      },
    },
  },
];

test("parseSse reads JSON-RPC messages from data lines across events", () => {
  const body =
    'event: message\r\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\r\n\r\n' +
    'event: message\ndata: {"jsonrpc":"2.0",\ndata: "id":3,"result":{"ok":true}}\n\n';
  assert.deepEqual(parseSse(body), [
    { jsonrpc: "2.0", method: "notifications/progress" },
    { jsonrpc: "2.0", id: 3, result: { ok: true } },
  ]);
});

test("buildArguments maps flags onto the schema's own names and types", () => {
  assert.deepEqual(buildArguments(TOOLS[0], { query: "q", trees: "acme/app@main", topK: 3, context: "578" }), {
    query: "q",
    trees: ["acme/app@main"],
    top_k: 3,
    context_id: 578,
  });
  assert.deepEqual(buildArguments(TOOLS[1], { identifier: "page:12", around: 40, context: undefined }), {
    identifier: "page:12",
    around: 40,
  });
  assert.throws(() => buildArguments(TOOLS[1], { depth: 2 }), /does not accept depth/);
});

test("splitContinuation strips the footer and returns the cursor", () => {
  const text =
    '*Continuing at character 30,000 of 61,000.*\n\nbody text\n\n---\n\n*Window 2 of 3. Continue with ' +
    '`read(identifier="page:1", cursor="abc.\\"x")`. The cursor is bound to this exact content and will refuse to continue if it changed.*';
  assert.deepEqual(splitContinuation(text), { body: "body text", cursor: 'abc."x' });
  assert.deepEqual(splitContinuation("plain"), { body: "plain", cursor: null });
});

test("the client initialises, echoes the session id, lists tools and follows cursors over SSE", async () => {
  const requests = [];
  const windows = {
    undefined: 'first\n\n---\n\n*Continue with `read(identifier="page:1", cursor="c1")`. The cursor is bound.*',
    c1: "second",
  };
  const fetch = async (url, init) => {
    const message = JSON.parse(init.body);
    requests.push({ headers: init.headers, message });
    if (!("id" in message)) return new Response(null, { status: 202 });
    let result;
    if (message.method === "initialize") result = { protocolVersion: "2025-06-18", capabilities: {} };
    if (message.method === "tools/list") result = { tools: TOOLS };
    if (message.method === "tools/call") {
      result = { content: [{ type: "text", text: windows[message.params.arguments.cursor] }] };
    }
    const sse = `event: message\ndata: ${JSON.stringify({ jsonrpc: "2.0", id: message.id, result })}\n\n`;
    return new Response(sse, {
      headers: { "Content-Type": "text/event-stream", "Mcp-Session-Id": "session-1" },
    });
  };
  const client = new McpClient({ fetch, url: "https://mcp.example.test/mcp", token: "tok" });
  await client.connect();
  const text = await client.callAll("read", { identifier: "page:1", context: 5 });

  assert.equal(text, "first\nsecond");
  assert.deepEqual(
    requests.map((r) => r.message.method),
    ["initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call"],
  );
  assert.equal(requests[0].headers.Accept, "application/json, text/event-stream");
  assert.equal(requests[0].headers.Authorization, "Bearer tok");
  assert.equal(requests[0].headers["Mcp-Session-Id"], undefined);
  assert.equal(requests[3].headers["Mcp-Session-Id"], "session-1");
  assert.deepEqual(requests[4].message.params.arguments, { identifier: "page:1", context_id: 5, cursor: "c1" });
});

test("a plain JSON reply, a tool error and an expired sign-in are all handled", async () => {
  const fetch = async (url, init) => {
    const message = JSON.parse(init.body);
    if (message.params?.name === "search") return new Response("", { status: 401 });
    return new Response(
      JSON.stringify({ jsonrpc: "2.0", id: message.id, result: { isError: true, content: [{ type: "text", text: "Paused" }] } }),
      { headers: { "Content-Type": "application/json" } },
    );
  };
  const client = new McpClient({ fetch, url: "https://mcp.example.test/mcp", token: "tok" });
  client.tools = new Map(TOOLS.map((t) => [t.name, t]));
  await assert.rejects(client.call("read", { identifier: "x" }), /Paused/);
  await assert.rejects(client.call("search", { query: "x" }), /pensieve login/);
});

test("toolText joins text blocks and falls back to structured content", () => {
  assert.equal(toolText({ content: [{ type: "text", text: "a" }, { type: "image" }, { type: "text", text: "b" }] }), "ab");
  assert.equal(toolText({ content: [], structuredContent: { result: 1 } }), '{\n  "result": 1\n}');
});
