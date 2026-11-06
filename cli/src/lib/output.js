import { toolText } from "./mcp.js";

/** A tool result as text, or the raw MCP result with --json. */
export function printResult(io, result, json) {
  const text = json ? JSON.stringify(result, null, 2) : toolText(result);
  io.stdout.write(text.endsWith("\n") ? text : `${text}\n`);
  return 0;
}
