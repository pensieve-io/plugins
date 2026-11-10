import { credentialsFromTokens, resourceIndicator, saveCredentials } from "../lib/credentials.js";
import { CliError } from "../lib/errors.js";
import { readJson, readStream, writePrivateJson } from "../lib/files.js";
import {
  authorizeUrl,
  createPkce,
  createState,
  discover,
  exchangeCode,
  expiresAt,
  registerClient,
  scopesFor,
  startCallbackServer,
} from "../lib/oauth.js";
import { credentialsPath, oauthClientsPath, serverUrl } from "../lib/paths.js";

/**
 * Start the loopback listener, reusing the cached client's registered port
 * when it is free (servers may match redirect URIs exactly); otherwise take
 * an ephemeral port and register afresh.
 */
async function listen(io, cached, state) {
  if (cached) {
    try {
      const port = Number(new URL(cached.redirect_uri).port);
      return { listener: await startCallbackServer({ port, state }), client: cached };
    } catch (error) {
      if (error.code !== "EADDRINUSE" && error.code !== "EACCES") throw error;
    }
  }
  return { listener: await startCallbackServer({ port: 0, state }), client: null };
}

export async function browserLogin(io, { reregister = false } = {}) {
  const server = serverUrl(io);
  const metadata = await discover(io.fetch, server);
  const issuer = metadata.issuer || new URL(metadata.authorization_endpoint).origin;
  const clients = (await readJson(oauthClientsPath(io))) || {};
  const state = createState();
  let { listener, client } = await listen(io, reregister ? null : clients[issuer], state);
  try {
    const redirectUri = `http://127.0.0.1:${listener.port}/callback`;
    const reused = !!client;
    if (!client) {
      const registered = await registerClient(io.fetch, metadata, redirectUri);
      client = { client_id: registered.client_id, redirect_uri: redirectUri, registered_at: new Date(io.now()).toISOString() };
      await writePrivateJson(oauthClientsPath(io), { ...clients, [issuer]: client });
    }
    const pkce = createPkce();
    const resource = resourceIndicator(io);
    const url = authorizeUrl(metadata, {
      clientId: client.client_id,
      redirectUri,
      pkce,
      state,
      scope: scopesFor(metadata, io.env.PENSIEVE_OAUTH_SCOPE),
      resource,
    });
    io.stdout.write(`Opening your browser to sign in to Pensieve. If it does not open, visit:\n\n  ${url}\n\n`);
    io.openBrowser(url);
    const code = await listener.result;
    const tokens = await exchangeCode(io.fetch, metadata.token_endpoint, {
      code,
      redirectUri,
      clientId: client.client_id,
      verifier: pkce.verifier,
      resource,
    }).catch((error) => {
      if (reused) error.message += " If this persists, run `pensieve login --reregister`.";
      throw error;
    });
    const credentials = credentialsFromTokens(
      tokens,
      { issuer, clientId: client.client_id, tokenEndpoint: metadata.token_endpoint, server },
      io.now(),
    );
    await saveCredentials(io, credentials);
    return credentials;
  } finally {
    listener.close();
  }
}

export default {
  name: "login",
  summary: "Sign in to Pensieve for the read commands (OAuth with PKCE)",
  usage: `Usage: pensieve login [--token-stdin] [--reregister]

Opens your browser to sign in to Pensieve and stores the resulting tokens in
~/.pensieve/credentials.json (mode 0600). Tokens refresh automatically. This
sign-in is used by the CLI's read commands; Claude Code and Codex keep their
own plugin sign-in.

Options:
  --token-stdin   Read an access token from stdin instead (no browser; no refresh)
  --reregister    Register a fresh OAuth client instead of the cached one

Environment:
  PENSIEVE_MCP_URL         MCP server (default https://mcp.pensieve.uk/mcp)
  PENSIEVE_OAUTH_SCOPE     Scopes to request (default: openid email profile offline_access, as advertised)
  PENSIEVE_OAUTH_RESOURCE  Set to 1 to send the RFC 8707 resource parameter`,
  options: { "token-stdin": "boolean", reregister: "boolean" },

  async run(io, flags) {
    if (flags.tokenStdin) {
      const token = (await readStream(io.stdin)).trim();
      if (!token || /\s/.test(token)) throw new CliError("Expected a single access token on stdin.");
      await saveCredentials(io, {
        access_token: token,
        refresh_token: null,
        expires_at: expiresAt({ access_token: token }, io.now()),
        issuer: null,
        client_id: null,
        token_endpoint: null,
        server: serverUrl(io),
      });
      io.stdout.write(`Saved the token to ${credentialsPath(io)}. It will not refresh; run \`pensieve login\` when it expires.\n`);
      return 0;
    }
    const credentials = await browserLogin(io, { reregister: flags.reregister });
    io.stdout.write(
      `Signed in. Credentials saved to ${credentialsPath(io)}${credentials.expires_at ? `; access token expires ${credentials.expires_at}` : ""}.\n`,
    );
    return 0;
  },
};
