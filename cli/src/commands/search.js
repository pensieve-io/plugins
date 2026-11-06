import { contextFlag, intFlag } from "../lib/args.js";
import { CliError } from "../lib/errors.js";
import { openMcp } from "../lib/mcp.js";
import { printResult } from "../lib/output.js";

export default {
  name: "search",
  summary: "Search Pages, Data and Decisions",
  usage: `Usage: pensieve search <query> [--context N] [--tree SELECTOR] [--top-k N] [--json]

Calls the MCP search tool. Double-quote a phrase inside the query for a literal
match, e.g. pensieve search '"refund policy"'.

Options:
  --context N        Context id (default: PENSIEVE_CONTEXT_ID, or your only context)
  --tree SELECTOR    Search a Page tree from \`pensieve tree --list\` (default: main)
  --top-k N          Results per type (server default 5)
  --json             Print the raw MCP tool result`,
  options: { context: "string", tree: "string", "top-k": "string", json: "boolean" },

  async run(io, flags, positionals) {
    const query = positionals.join(" ").trim();
    if (!query) throw new CliError("Give a search query. See `pensieve search --help`.");
    const mcp = await openMcp(io);
    const result = await mcp.call("search", {
      query,
      context: contextFlag(io, flags),
      trees: flags.tree,
      topK: intFlag(flags.topK, "top-k"),
    });
    return printResult(io, result, flags.json);
  },
};
