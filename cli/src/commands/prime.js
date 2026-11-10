import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { git } from "../lib/git.js";
import { upsertPointer } from "../lib/pointer.js";
import { PRIMING_MARKERS, PRIMING_TEXT } from "../lib/priming.js";

const FILES = ["AGENTS.md", "CLAUDE.md"];

async function read(path) {
  try {
    return await readFile(path, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

export default {
  name: "prime",
  summary: "Tell agents here to use Pensieve: its MCP tools, or this CLI without them",
  usage: `Usage: pensieve prime [--write]

Prints the standing instructions that tell an agent when to use Pensieve and
how: its MCP tools when the session has them, this CLI when it has none.
Claude Code and Codex with the plugin are primed by its hooks already; this is
for any other harness that reads AGENTS.md or CLAUDE.md and can run a shell.

Options:
  --write   Add the instructions, between markers, to AGENTS.md and CLAUDE.md
            where they exist at the repository root (else the current
            directory), creating AGENTS.md when neither does. Running it again
            refreshes the block in place.`,
  options: { write: "boolean" },
  async run(io, flags) {
    if (!flags.write) {
      io.stdout.write(`${PRIMING_TEXT}\n`);
      return 0;
    }
    const top = await git(io, ["rev-parse", "--show-toplevel"], { cwd: io.cwd, allowFailure: true });
    const root = top.code === 0 && top.out ? top.out : io.cwd;
    const existing = [];
    for (const name of FILES) {
      const content = await read(join(root, name));
      if (content !== null) existing.push([name, content]);
    }
    if (!existing.length) existing.push([FILES[0], ""]);
    for (const [name, content] of existing) {
      const updated = upsertPointer(content, PRIMING_TEXT, PRIMING_MARKERS);
      if (updated !== content) await writeFile(join(root, name), updated);
      io.stdout.write(`${updated === content ? "Already current" : "Wrote Pensieve instructions to"}: ${join(root, name)}\n`);
    }
    return 0;
  },
};
