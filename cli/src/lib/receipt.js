import { readJson, removeFile, writePrivateJson } from "./files.js";
import { receiptPath } from "./paths.js";

/**
 * `~/.pensieve/install-receipt.json`: exactly the steps this CLI ran, so
 * `uninstall` reverses those and nothing else.
 *
 * { version: 1, cli_version, updated_at,
 *   clients: { claude: { steps: [{ id, command: [...], undo: [...], ran_at }] } } }
 */
export const loadReceipt = (io) => readJson(receiptPath(io));

export async function saveReceipt(io, receipt) {
  const clients = Object.fromEntries(Object.entries(receipt.clients || {}).filter(([, c]) => c.steps.length));
  if (!Object.keys(clients).length) return removeFile(receiptPath(io));
  return writePrivateJson(receiptPath(io), { ...receipt, clients });
}

/** Add newly run steps to a receipt, keeping what an earlier install recorded. */
export function recordSteps(receipt, client, steps, meta) {
  const next = receipt || { version: 1, clients: {} };
  const existing = next.clients[client]?.steps || [];
  const ids = new Set(steps.map((s) => s.id));
  return {
    ...next,
    ...meta,
    clients: { ...next.clients, [client]: { steps: [...existing.filter((s) => !ids.has(s.id)), ...steps] } },
  };
}
