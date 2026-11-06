import { openMcp } from "../lib/mcp.js";
import { printResult } from "../lib/output.js";

export default {
  name: "contexts",
  summary: "List the Pensieve contexts you can access",
  usage: `Usage: pensieve contexts [--json]

Calls the MCP list_contexts tool. Pass an id from here as --context N (or set
PENSIEVE_CONTEXT_ID) on the other read commands.

Options:
  --json   Print the raw MCP tool result`,
  options: { json: "boolean" },

  async run(io, flags) {
    const mcp = await openMcp(io);
    return printResult(io, await mcp.call("list_contexts"), flags.json);
  },
};
