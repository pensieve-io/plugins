import { CLIENTS, PLUGIN, installedPensieve } from "../lib/clients.js";
import { CliError } from "../lib/errors.js";
import { run, which } from "../lib/exec.js";
import { receiptPath } from "../lib/paths.js";
import { loadReceipt, recordSteps, saveReceipt } from "../lib/receipt.js";
import { VERSION } from "../lib/version.js";

export const shellLine = (bin, args) => `$ ${[bin, ...args].join(" ")}`;

/** Clients named by flags, else every supported client found on PATH. */
export function chooseClients(io, flags) {
  const requested = Object.keys(CLIENTS).filter((name) => flags[name]);
  const names = requested.length ? requested : Object.keys(CLIENTS).filter((name) => which(io, CLIENTS[name].bin));
  if (!names.length) {
    throw new CliError("Neither `claude` nor `codex` is on PATH. Install one, or pass --claude / --codex.");
  }
  return names;
}

export default {
  name: "install",
  summary: "Install the Pensieve plugin into Claude Code and/or Codex CLI",
  usage: `Usage: pensieve install [--claude] [--codex] [--dry-run]

Runs the plugin installation commands documented in the plugin README for each
client found on PATH (or only those named), and records what it ran in
~/.pensieve/install-receipt.json so \`pensieve uninstall\` reverses exactly that.
Steps whose effect already exists (the marketplace or plugin was already
installed) are skipped and not recorded.

Options:
  --claude    Install into Claude Code
  --codex     Install into Codex CLI (includes \`codex mcp login pensieve\`)
  --dry-run   Print the commands without running them`,
  options: { claude: "boolean", codex: "boolean", "dry-run": "boolean" },

  async run(io, flags) {
    const names = chooseClients(io, flags);
    let receipt = flags.dryRun ? null : await loadReceipt(io);
    let failed = false;
    for (const name of names) {
      const client = CLIENTS[name];
      io.stdout.write(`\n${client.label}\n`);
      if (flags.dryRun) {
        for (const step of client.steps) io.stdout.write(`${shellLine(client.bin, step.run)}\n`);
        continue;
      }
      if (!which(io, client.bin)) {
        io.stderr.write(`\`${client.bin}\` is not on PATH; skipping ${client.label}.\n`);
        failed = true;
        continue;
      }
      const others = ((await installedPensieve(io, name)) || []).filter((id) => id !== PLUGIN);
      if (others.length) {
        io.stderr.write(`Note: Pensieve is already installed as ${others.join(", ")}; ${PLUGIN} would be a second copy.\n`);
      }
      const ran = [];
      let stopped = false;
      for (const step of client.steps) {
        if (await step.present(io)) {
          io.stdout.write(`Already present, leaving as is: ${step.id}\n`);
          continue;
        }
        io.stdout.write(`${shellLine(client.bin, step.run)}\n`);
        const code = await run(io, client.bin, step.run);
        if (code !== 0) {
          io.stderr.write(`${client.bin} exited with ${code}; stopping ${client.label} installation.\n`);
          stopped = true;
          break;
        }
        ran.push({ id: step.id, command: [client.bin, ...step.run], undo: [client.bin, ...step.undo], ran_at: new Date(io.now()).toISOString() });
      }
      if (ran.length) {
        receipt = recordSteps(receipt, name, ran, { cli_version: VERSION, updated_at: new Date(io.now()).toISOString() });
        await saveReceipt(io, receipt);
      }
      failed ||= stopped;
      if (!stopped) {
        io.stdout.write("Remaining steps:\n");
        for (const line of client.nextSteps) io.stdout.write(`  - ${line}\n`);
      }
    }
    if (!flags.dryRun && receipt) io.stdout.write(`\nReceipt: ${receiptPath(io)}\n`);
    io.stdout.write(
      "\nTo use Pensieve from a shell as well, run `pensieve login` (separate from the plugin's sign-in).\n",
    );
    return failed ? 1 : 0;
  },
};
