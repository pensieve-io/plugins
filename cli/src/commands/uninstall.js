import { CLIENTS } from "../lib/clients.js";
import { run, which } from "../lib/exec.js";
import { loadReceipt, saveReceipt } from "../lib/receipt.js";
import { shellLine } from "./install.js";

export default {
  name: "uninstall",
  summary: "Reverse exactly what `pensieve install` did",
  usage: `Usage: pensieve uninstall [--dry-run]

Reads ~/.pensieve/install-receipt.json and runs the inverse of each recorded
step, newest first, then deletes the receipt. Anything the receipt does not
list (a plugin or marketplace you installed yourself) is left alone. Steps
that fail stay in the receipt so you can retry.

Options:
  --dry-run   Print the commands without running them`,
  options: { "dry-run": "boolean" },

  async run(io, flags) {
    const receipt = await loadReceipt(io);
    const clients = Object.entries(receipt?.clients || {});
    if (!clients.length) {
      io.stdout.write("Nothing to uninstall: no install receipt was found.\n");
      return 0;
    }
    const remaining = {};
    for (const [name, { steps }] of clients) {
      io.stdout.write(`\n${CLIENTS[name]?.label || name}\n`);
      const kept = [];
      const binary = steps[0]?.undo[0];
      const available = flags.dryRun || (binary && which(io, binary));
      for (const step of [...steps].reverse()) {
        const [bin, ...args] = step.undo;
        io.stdout.write(`${shellLine(bin, args)}\n`);
        if (flags.dryRun) continue;
        if (!available || (await run(io, bin, args)) !== 0) {
          io.stderr.write(`Could not reverse ${step.id}; keeping it in the receipt.\n`);
          kept.unshift(step);
        }
      }
      if (kept.length) remaining[name] = { steps: kept };
    }
    if (flags.dryRun) return 0;
    await saveReceipt(io, { ...receipt, clients: remaining });
    if (Object.keys(remaining).length) return 1;
    io.stdout.write("\nUninstalled. The install receipt has been removed.\n");
    return 0;
  },
};
