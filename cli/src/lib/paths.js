import { join } from "node:path";

export const DEFAULT_MCP_URL = "https://mcp.pensieve.uk/mcp";

export const serverUrl = (io) => io.env.PENSIEVE_MCP_URL || DEFAULT_MCP_URL;

/** `~/.pensieve`, overridable for tests and unusual setups. */
export const configDir = (io) => io.env.PENSIEVE_CONFIG_DIR || join(io.home, ".pensieve");
export const credentialsPath = (io) => join(configDir(io), "credentials.json");
export const oauthClientsPath = (io) => join(configDir(io), "oauth-clients.json");
export const receiptPath = (io) => join(configDir(io), "install-receipt.json");

/**
 * The plugin's capture state directory, matching `STATE_PATH` and the
 * `PENSIEVE_CAPTURE_STATE` override in `pensieve/scripts/conversation_capture.py`.
 */
export const captureStateDir = (io) =>
  io.env.PENSIEVE_CAPTURE_STATE || join(io.home, ".local", "state", "pensieve", "capture");
