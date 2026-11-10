import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { CliError } from "../lib/errors.js";
import { readStream } from "../lib/files.js";
import { openMcp } from "../lib/mcp.js";
import { printResult } from "../lib/output.js";
import { commandName, parseToolArgs, toolHelp, toolName, toolSummary } from "../lib/toolargs.js";

/**
 * Run a command that is not built in as the MCP tool of the same name, so the
 * CLI offers whatever the server does — reads and writes alike — with no list
 * here to fall behind. Returns null when the server has no such tool.
 */
export async function runTool(io, command, argv) {
  let mcp;
  try {
    mcp = await openMcp(io);
  } catch (error) {
    // Not signed in: we cannot tell a tool from a typo, so say both.
    if (error instanceof CliError && error.exitCode === 2) {
      throw new CliError(
        `\`${command}\` is not a built-in command, and Pensieve's tools need a sign-in. ${error.message}`,
        2,
      );
    }
    throw error;
  }
  const tool = (await mcp.listTools()).get(toolName(command));
  if (!tool) return null;
  const parsed = await parseToolArgs(tool, argv, {
    env: io.env,
    readText: (path) => (path === "-" ? readStream(io.stdin) : readFile(resolve(io.cwd, path), "utf8")),
  });
  if (parsed.help) {
    io.stdout.write(`${toolHelp(tool)}\n`);
    return 0;
  }
  return printResult(io, await mcp.invoke(tool.name, parsed.args), parsed.json);
}

export default {
  name: "tools",
  summary: "List every Pensieve tool; each one is a command (pensieve <tool> --help)",
  usage: `Usage: pensieve tools [--json]

Lists the tools the Pensieve MCP server offers your account. Each is a command
of the same name with hyphens: \`pensieve save-data …\`, \`pensieve edit-page …\`.
Run \`pensieve <tool> --help\` for its options, which come from the server's own
schema, so the CLI always matches the MCP tools.

Options:
  --json   Print the tools with their full schemas`,
  options: { json: "boolean" },
  async run(io, flags) {
    const tools = [...(await (await openMcp(io)).listTools()).values()];
    if (flags.json) {
      io.stdout.write(`${JSON.stringify(tools, null, 2)}\n`);
      return 0;
    }
    const width = Math.max(...tools.map((t) => t.name.length));
    for (const tool of tools) {
      io.stdout.write(`${commandName(tool.name).padEnd(width)}  ${toolSummary(tool).slice(0, 100)}\n`);
    }
    return 0;
  },
};
