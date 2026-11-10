import { capture } from "./exec.js";

export const MARKETPLACE_SOURCE = "pensieve-io/plugins";
export const MARKETPLACE = "pensieve";
export const PLUGIN = "pensieve@pensieve";

const json = (text) => {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
};

/**
 * Install steps per client. `run` is what the repository README documents;
 * `undo` is the inverse each client's own `--help` documents (the README has
 * no uninstall section). `present` checks whether the step's effect already
 * exists, so install never claims, and uninstall never reverses, something
 * that was there before us.
 */
export const CLIENTS = {
  claude: {
    bin: "claude",
    label: "Claude Code",
    steps: [
      {
        id: "marketplace",
        run: ["plugin", "marketplace", "add", MARKETPLACE_SOURCE],
        undo: ["plugin", "marketplace", "remove", MARKETPLACE],
        present: async (io) => {
          const { code, stdout } = await capture(io, "claude", ["plugin", "marketplace", "list", "--json"]);
          return code === 0 && (json(stdout) || []).some((m) => m.name === MARKETPLACE);
        },
      },
      {
        id: "plugin",
        run: ["plugin", "install", PLUGIN],
        undo: ["plugin", "uninstall", PLUGIN],
        present: async (io) => (await installedPensieve(io, "claude"))?.includes(PLUGIN),
      },
    ],
    nextSteps: [
      "Open Claude Code and run /mcp, choose plugin:pensieve:pensieve and authenticate with Pensieve.",
      "Enable the plugin's hooks when prompted, then start a new conversation.",
    ],
  },
  codex: {
    bin: "codex",
    label: "Codex CLI",
    steps: [
      {
        id: "marketplace",
        run: ["plugin", "marketplace", "add", MARKETPLACE_SOURCE],
        undo: ["plugin", "marketplace", "remove", MARKETPLACE],
        present: async (io) => {
          const { code, stdout } = await capture(io, "codex", ["plugin", "marketplace", "list"]);
          return code === 0 && stdout.split("\n").some((line) => line.split(/\s+/)[0] === MARKETPLACE);
        },
      },
      {
        id: "plugin",
        run: ["plugin", "add", PLUGIN],
        undo: ["plugin", "remove", PLUGIN],
        present: async (io) => (await installedPensieve(io, "codex"))?.includes(PLUGIN),
      },
      {
        id: "login",
        run: ["mcp", "login", MARKETPLACE],
        undo: ["mcp", "logout", MARKETPLACE],
        // Signed in already (ours or the member's own): never reopen the browser.
        present: async (io) => {
          const { code, stdout } = await capture(io, "codex", ["mcp", "list", "--json"]);
          const servers = code === 0 ? json(stdout) : null;
          return Array.isArray(servers) && servers.some((s) => s.name === MARKETPLACE && s.auth_status === "o_auth");
        },
      },
    ],
    nextSteps: ["Enable the plugin's hooks if prompted, then start a new chat."],
  },
};

/**
 * Ids of installed plugins named `pensieve@…` (any marketplace), from
 * `claude plugin list --json` or `codex plugin list`; null when unknown.
 */
export async function installedPensieve(io, name) {
  if (name === "claude") {
    const { code, stdout } = await capture(io, "claude", ["plugin", "list", "--json"]);
    if (code !== 0) return null;
    return (json(stdout) || []).map((p) => p.id).filter((id) => /^pensieve@/.test(id || ""));
  }
  const { code, stdout } = await capture(io, "codex", ["plugin", "list"]);
  if (code !== 0) return null;
  return stdout
    .split("\n")
    .map((line) => line.trim().split(/\s{2,}/))
    .filter(([id, status]) => /^pensieve@/.test(id) && /^installed\b/.test(status || ""))
    .map(([id]) => id);
}
