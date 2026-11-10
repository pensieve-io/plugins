import { getAccessToken } from "../lib/credentials.js";
import { CliError } from "../lib/errors.js";
import { openMcp, toolText } from "../lib/mcp.js";
import install, { chooseClients } from "./install.js";
import { browserLogin } from "./login.js";

export default {
  name: "setup",
  summary: "Do everything: install the plugin, sign in, and prove Pensieve answers",
  usage: `Usage: pensieve setup [--claude] [--codex] [--no-plugin]

One command for a new machine:
  1. installs the Pensieve plugin (MCP tools, hooks and skills) into each of
     Claude Code and Codex CLI found on PATH, skipping what is already there;
  2. signs this CLI in, opening your browser, unless it already is;
  3. lists your contexts through the server, which proves the connection.
It then prints whatever only you can do. Safe to run again.

Options:
  --claude      Only set up Claude Code
  --codex       Only set up Codex CLI
  --no-plugin   Skip step 1: sign in and verify the CLI alone`,
  options: { claude: "boolean", codex: "boolean", "no-plugin": "boolean" },
  async run(io, flags) {
    const out = (line = "") => io.stdout.write(`${line}\n`);
    let failed = false;

    out("1. Plugin");
    if (flags.noPlugin) {
      out("Skipped (--no-plugin).");
    } else {
      let found = true;
      try {
        chooseClients(io, flags);
      } catch (error) {
        if (!(error instanceof CliError)) throw error;
        found = false;
        out("Neither `claude` nor `codex` is on PATH, so there is no plugin to install. The CLI still works on its own.");
      }
      if (found) failed = (await install.run(io, flags)) !== 0;
    }

    out("\n2. Sign-in");
    let signedIn = true;
    try {
      await getAccessToken(io);
      out("Already signed in.");
    } catch (error) {
      if (!(error instanceof CliError) || error.exitCode !== 2) throw error;
      signedIn = false;
    }
    if (!signedIn) {
      await browserLogin(io);
      out("Signed in.");
    }

    out("\n3. Check");
    const contexts = toolText(await (await openMcp(io)).call("list_contexts"));
    out(contexts.trim());
    out("\nPensieve answered: this shell can read and write your company's context.");
    out("For an agent without the plugin, run `pensieve prime --write` in its repository.");
    if (failed) out("\nA plugin step above did not finish; fix it and run `pensieve setup` again.");
    return failed ? 1 : 0;
  },
};
