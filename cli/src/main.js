import { spawn } from "node:child_process";
import { homedir } from "node:os";
import contexts from "./commands/contexts.js";
import docs from "./commands/docs.js";
import doctor from "./commands/doctor.js";
import install from "./commands/install.js";
import login from "./commands/login.js";
import logout from "./commands/logout.js";
import pause from "./commands/pause.js";
import read from "./commands/read.js";
import resume from "./commands/resume.js";
import search from "./commands/search.js";
import tree from "./commands/tree.js";
import uninstall from "./commands/uninstall.js";
import { parseArgs } from "./lib/args.js";
import { CliError } from "./lib/errors.js";
import { openBrowser } from "./lib/exec.js";
import { VERSION } from "./lib/version.js";

export const COMMANDS = [install, uninstall, login, logout, pause, resume, contexts, search, read, tree, docs, doctor];

/** Everything a command touches outside itself, so tests can replace it. */
export function defaultIo() {
  const io = {
    fetch: (...args) => globalThis.fetch(...args),
    spawn,
    env: process.env,
    home: homedir(),
    cwd: process.cwd(),
    platform: process.platform,
    stdin: process.stdin,
    stdout: process.stdout,
    stderr: process.stderr,
    now: () => Date.now(),
  };
  io.openBrowser = (url) => openBrowser(io, url);
  return io;
}

const width = Math.max(...COMMANDS.map((c) => c.name.length));
const HELP = `pensieve ${VERSION} — Pensieve from your terminal

Usage: pensieve <command> [options]

Commands:
${COMMANDS.map((c) => `  ${c.name.padEnd(width)}  ${c.summary}`).join("\n")}

Run \`pensieve <command> --help\` for a command's options.
Set PENSIEVE_MCP_URL to target another MCP server (development).
`;

export async function main(argv, io = defaultIo()) {
  try {
    const [name, ...rest] = argv;
    if (!name || name === "--help" || name === "-h" || name === "help") {
      io.stdout.write(HELP);
      return 0;
    }
    if (name === "--version" || name === "-v" || name === "version") {
      io.stdout.write(`${VERSION}\n`);
      return 0;
    }
    const command = COMMANDS.find((c) => c.name === name);
    if (!command) throw new CliError(`Unknown command \`${name}\`. Run \`pensieve --help\`.`);
    const { flags, positionals } = parseArgs(rest, command.options);
    if (flags.help) {
      io.stdout.write(`${command.usage}\n`);
      return 0;
    }
    return await command.run(io, flags, positionals);
  } catch (error) {
    if (error instanceof CliError) {
      io.stderr.write(`pensieve: ${error.message}\n`);
      return error.exitCode;
    }
    io.stderr.write(`pensieve: ${io.env.PENSIEVE_DEBUG ? error.stack : error.message}\n`);
    return 1;
  }
}
