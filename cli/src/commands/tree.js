import { contextFlag, intFlag } from "../lib/args.js";
import { openMcp } from "../lib/mcp.js";
import { printResult } from "../lib/output.js";

export default {
  name: "tree",
  summary: "Outline a Page tree, or list the trees (--list)",
  usage: `Usage: pensieve tree [--context N] [--tree SELECTOR] [--parent REF] [--depth N] [--json]
       pensieve tree --list [--context N] [--json]

Calls the MCP get_tree tool (or list_trees with --list). Repository and
database documentation lives in its own tree; --list shows each tree's
selector and the revision Pensieve last processed.

Options:
  --context N        Context id (default: PENSIEVE_CONTEXT_ID, or your only context)
  --tree SELECTOR    Tree to outline (default: main)
  --parent REF       Outline beneath this Page (page:ID, path or title)
  --depth N          Levels to show (server default 2)
  --list             List the context's trees instead
  --json             Print the raw MCP tool result`,
  options: { context: "string", tree: "string", parent: "string", depth: "string", list: "boolean", json: "boolean" },

  async run(io, flags) {
    const mcp = await openMcp(io);
    const context = contextFlag(io, flags);
    const result = flags.list
      ? await mcp.call("list_trees", { context })
      : await mcp.call("get_tree", {
          context,
          tree: flags.tree,
          parent: flags.parent,
          depth: intFlag(flags.depth, "depth"),
        });
    return printResult(io, result, flags.json);
  },
};
