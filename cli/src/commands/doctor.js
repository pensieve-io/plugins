import { join } from "node:path";
import { CLIENTS, installedPensieve } from "../lib/clients.js";
import { loadCredentials } from "../lib/credentials.js";
import { which } from "../lib/exec.js";
import { readJson } from "../lib/files.js";
import { captureStateDir, credentialsPath, serverUrl } from "../lib/paths.js";
import { pausePath, readPause } from "../lib/pause.js";
import { loadReceipt } from "../lib/receipt.js";
import { VERSION } from "../lib/version.js";

async function reachability(io) {
  const url = `${new URL(serverUrl(io)).origin}/health`;
  try {
    const response = await io.fetch(url, { signal: AbortSignal.timeout(5000) });
    return `${response.ok ? "reachable" : "unhealthy"} (${url} → HTTP ${response.status})`;
  } catch (error) {
    return `unreachable (${url}: ${error.message})`;
  }
}

function describeLogin(credentials, now) {
  if (!credentials?.access_token) return "not signed in (run `pensieve login`)";
  if (!credentials.expires_at) return `signed in to ${credentials.server || "unknown server"} (no expiry recorded)`;
  const expiry = Date.parse(credentials.expires_at);
  if (expiry > now) return `signed in to ${credentials.server}; access token valid until ${credentials.expires_at}`;
  return credentials.refresh_token
    ? `signed in to ${credentials.server}; access token expired, will refresh on next use`
    : "session expired (run `pensieve login`)";
}

/** One line per primitive field of the plugin's capture health file. */
function describeHealth(health) {
  return Object.entries(health)
    .filter(([, value]) => value === null || typeof value !== "object")
    .slice(0, 12)
    .map(([key, value]) => `      ${key}: ${value}`);
}

export default {
  name: "doctor",
  summary: "Check the CLI, clients, plugin, sign-in and server",
  usage: `Usage: pensieve doctor

Reports the CLI and Node versions, which clients are on PATH and whether the
Pensieve plugin is installed in each, the CLI's sign-in, whether the MCP
server answers /health, and the plugin's capture health file per client if
the installed plugin writes one, and whether transcript saving is paused.`,
  options: {},

  async run(io) {
    const out = (line = "") => io.stdout.write(`${line}\n`);
    const receipt = await loadReceipt(io);
    const major = Number(process.versions.node.split(".")[0]);
    out(`pensieve CLI ${VERSION}`);
    out(`Node ${process.versions.node}${major < 18 ? " (unsupported: Node 18 or newer is required)" : ""}`);
    out();
    for (const [name, client] of Object.entries(CLIENTS)) {
      const path = which(io, client.bin);
      out(`${client.label}: ${path ? path : "not found on PATH"}`);
      if (path) {
        const ids = await installedPensieve(io, name);
        out(`  plugin: ${ids === null ? "unknown (could not list plugins)" : ids.length ? `installed as ${ids.join(", ")}` : "not installed"}`);
      }
      const recorded = receipt?.clients?.[name]?.steps?.map((s) => s.id);
      out(`  installed by this CLI: ${recorded?.length ? recorded.join(", ") : "nothing"}`);
      const healthPath = join(captureStateDir(io), `health-${name}.json`);
      let health = null;
      try {
        health = await readJson(healthPath);
      } catch (error) {
        health = undefined;
        out(`  capture health: unreadable (${error.message})`);
      }
      if (health) {
        out(`  capture health (${healthPath}):`);
        for (const line of describeHealth(health)) out(line);
      } else if (health === null) {
        out("  capture health: no health file");
      }
    }
    const paused = await readPause(io);
    out(
      `Transcript saving: ${
        !paused.paused
          ? "not paused"
          : paused.since
            ? `paused since ${paused.since}`
            : "paused (the pause file is unreadable; the plugin treats that as paused)"
      }${paused.paused ? ` — ${pausePath(io)}; \`pensieve resume\` to start again` : ""}`,
    );
    out();
    out(`Sign-in: ${describeLogin(await loadCredentials(io).catch(() => null), io.now())}`);
    out(`  credentials: ${credentialsPath(io)}`);
    out(`MCP server: ${serverUrl(io)} — ${await reachability(io)}`);
    return 0;
  },
};
