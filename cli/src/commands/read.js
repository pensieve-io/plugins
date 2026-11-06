import { contextFlag, offsetFlag } from "../lib/args.js";
import { CliError } from "../lib/errors.js";
import { openMcp } from "../lib/mcp.js";
import { printResult } from "../lib/output.js";

export default {
  name: "read",
  summary: "Open a Page, source, Decision or transcript",
  usage: `Usage: pensieve read <ref-or-url> [--context N] [--around N] [--tree SELECTOR] [--cursor C] [--json]

Calls the MCP read tool. <ref-or-url> is a Pensieve link
(https://app.pensieve.uk/r/…), page:ID, data:ID, decision:ID, a Page path or title.

Options:
  --context N        Context id (default: PENSIEVE_CONTEXT_ID, or your only context)
  --around N         Return only the passage around this character offset (from a search hit)
  --tree SELECTOR    Resolve a Page title or path in this tree
  --cursor C         Continue a long result (the cursor its footer prints)
  --json             Print the raw MCP tool result`,
  options: { context: "string", around: "string", tree: "string", cursor: "string", json: "boolean" },

  async run(io, flags, positionals) {
    if (positionals.length !== 1) throw new CliError("Give one reference or link to read. See `pensieve read --help`.");
    const mcp = await openMcp(io);
    const result = await mcp.call("read", {
      identifier: positionals[0],
      context: contextFlag(io, flags),
      around: offsetFlag(flags.around, "around"),
      tree: flags.tree,
      cursor: flags.cursor,
    });
    return printResult(io, result, flags.json);
  },
};
