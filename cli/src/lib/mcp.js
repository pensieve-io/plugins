import { getAccessToken } from "./credentials.js";
import { CliError } from "./errors.js";
import { serverUrl } from "./paths.js";
import { typesOf } from "./toolargs.js";
import { VERSION } from "./version.js";

const PROTOCOL_VERSION = "2025-06-18";
const MAX_WINDOWS = 200;

/** JSON-RPC messages carried in a `text/event-stream` body. */
export function parseSse(text) {
  const messages = [];
  for (const event of text.replace(/\r\n?/g, "\n").split(/\n\n+/)) {
    const data = event
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""));
    const payload = data.join("\n").trim();
    if (payload) messages.push(JSON.parse(payload));
  }
  return messages;
}

/** The text a tool returned; clients join content blocks with nothing between them. */
export function toolText(result) {
  const texts = (result?.content || []).filter((b) => b.type === "text").map((b) => b.text);
  if (texts.length) return texts.join("");
  return result?.structuredContent ? JSON.stringify(result.structuredContent, null, 2) : "";
}

/**
 * Pensieve windows long results and ends them with the exact call that
 * continues, e.g. ``Continue with `read(identifier="page:1", cursor="abc")` ``.
 * Returns the body without that footer (or the "Continuing at…" lead) and the cursor.
 */
export function splitContinuation(text) {
  let body = text.replace(/^\*Continuing at [^\n]*\*\n\n/, "");
  const footer = body.match(/\n*---\n\n\*[^\n]*Continue with `[^`\n]*`[^\n]*\*\s*$/);
  if (!footer) return { body, cursor: null };
  const cursor = footer[0].match(/cursor=("(?:[^"\\]|\\.)*")\)`/);
  body = body.slice(0, footer.index);
  return { body, cursor: cursor ? JSON.parse(cursor[1]) : null };
}

const ALIASES = {
  context: ["context_id", "context"],
  query: ["query", "q"],
  identifier: ["identifier", "ref", "reference", "id"],
  around: ["around", "offset"],
  tree: ["tree"],
  trees: ["trees"],
  parent: ["parent"],
  depth: ["depth"],
  topK: ["top_k", "limit"],
  includeSummaries: ["include_summaries"],
  cursor: ["cursor"],
};


/**
 * Map CLI values onto the argument names the server's own schema declares
 * (from `tools/list`), so a renamed parameter fails clearly rather than
 * being silently ignored.
 */
export function buildArguments(tool, values) {
  const properties = tool?.inputSchema?.properties || {};
  const args = {};
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined || value === null) continue;
    const name = (ALIASES[key] || [key]).find((candidate) => candidate in properties);
    if (!name) throw new CliError(`The server's ${tool.name} tool does not accept ${key}.`);
    const types = typesOf(properties[name]);
    let v = value;
    if (types.length === 1 && types[0] === "array" && !Array.isArray(v)) v = [v];
    else if (!types.includes("string") && types.includes("integer") && /^\d+$/.test(String(v))) {
      v = Number(v);
    }
    args[name] = v;
  }
  return args;
}

export class McpClient {
  constructor({ fetch, url, token }) {
    Object.assign(this, { fetch, url, token });
    this.sessionId = null;
    this.protocolVersion = null;
    this.nextId = 1;
    this.tools = null;
  }

  async post(message) {
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      Authorization: `Bearer ${this.token}`,
    };
    if (this.sessionId) headers["Mcp-Session-Id"] = this.sessionId;
    if (this.protocolVersion) headers["MCP-Protocol-Version"] = this.protocolVersion;
    let response;
    try {
      response = await this.fetch(this.url, { method: "POST", headers, body: JSON.stringify(message) });
    } catch (error) {
      throw new CliError(`Could not reach the Pensieve MCP server at ${this.url}: ${error.message}`);
    }
    const sessionId = response.headers.get("mcp-session-id");
    if (sessionId) this.sessionId = sessionId;
    if (response.status === 401 || response.status === 403) {
      throw new CliError("Pensieve did not accept your sign-in. Run `pensieve login`.", 2);
    }
    const text = await response.text();
    if (!response.ok) {
      throw new CliError(`Pensieve MCP server returned HTTP ${response.status}${text ? `: ${text.slice(0, 300)}` : ""}`);
    }
    if (message.id === undefined) return null;
    const type = response.headers.get("content-type") || "";
    const messages = (type.includes("text/event-stream") ? parseSse(text) : [JSON.parse(text)]).flat();
    const reply = messages.find((m) => m?.id === message.id && ("result" in m || "error" in m));
    if (!reply) throw new CliError(`No reply to ${message.method} from the Pensieve MCP server.`);
    if (reply.error) throw new CliError(`${message.method} failed: ${reply.error.message}`);
    return reply.result;
  }

  request(method, params = {}) {
    return this.post({ jsonrpc: "2.0", id: this.nextId++, method, params });
  }

  async connect() {
    const result = await this.request("initialize", {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: "pensieve-cli", version: VERSION },
    });
    this.protocolVersion = result.protocolVersion || PROTOCOL_VERSION;
    await this.post({ jsonrpc: "2.0", method: "notifications/initialized" });
    return result;
  }

  /** Every tool the server offers, by name. */
  async listTools() {
    if (!this.tools) {
      const tools = [];
      let cursor;
      do {
        const page = await this.request("tools/list", cursor ? { cursor } : {});
        tools.push(...(page.tools || []));
        cursor = page.nextCursor;
      } while (cursor);
      this.tools = new Map(tools.map((t) => [t.name, t]));
    }
    return this.tools;
  }

  async tool(name) {
    const tool = (await this.listTools()).get(name);
    if (!tool) throw new CliError(`The Pensieve MCP server has no ${name} tool.`);
    return tool;
  }

  /** Call a tool with arguments already in the server's own names. */
  async invoke(name, args = {}) {
    const result = await this.request("tools/call", { name, arguments: args });
    if (result.isError) throw new CliError(toolText(result) || `${name} failed.`);
    return result;
  }

  /** Call a tool with CLI-level values mapped through its schema. */
  async call(name, values = {}) {
    return this.invoke(name, buildArguments(await this.tool(name), values));
  }

  /** Call a windowed tool and follow its continuation cursors to the end. */
  async callAll(name, values = {}) {
    const bodies = [];
    let cursor;
    for (let window = 0; window < MAX_WINDOWS; window++) {
      const text = toolText(await this.call(name, { ...values, cursor }));
      const part = splitContinuation(text);
      bodies.push(part.body);
      if (!part.cursor) return bodies.join("\n");
      cursor = part.cursor;
    }
    throw new CliError(`${name} did not finish within ${MAX_WINDOWS} windows.`);
  }
}

export async function openMcp(io) {
  const client = new McpClient({ fetch: io.fetch, url: serverUrl(io), token: await getAccessToken(io) });
  await client.connect();
  return client;
}
