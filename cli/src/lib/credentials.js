import { CliError } from "./errors.js";
import { readJson, removeFile, writePrivateJson } from "./files.js";
import { expiresAt, refreshTokens } from "./oauth.js";
import { credentialsPath, serverUrl } from "./paths.js";

const REFRESH_MARGIN_MS = 60 * 1000;

export const loadCredentials = (io) => readJson(credentialsPath(io));
export const saveCredentials = (io, credentials) => writePrivateJson(credentialsPath(io), credentials);
export const deleteCredentials = (io) => removeFile(credentialsPath(io));

/** Opt-in RFC 8707 resource indicator: set PENSIEVE_OAUTH_RESOURCE=1. */
export const resourceIndicator = (io) =>
  io.env.PENSIEVE_OAUTH_RESOURCE === "1" ? serverUrl(io) : undefined;

export function credentialsFromTokens(tokens, { issuer, clientId, tokenEndpoint, server }, now) {
  return {
    access_token: tokens.access_token,
    refresh_token: tokens.refresh_token || null,
    expires_at: expiresAt(tokens, now),
    issuer,
    client_id: clientId,
    token_endpoint: tokenEndpoint,
    server,
  };
}

/** A usable access token, refreshing (and saving the rotated pair) when expired. */
export async function getAccessToken(io) {
  const credentials = await loadCredentials(io);
  if (!credentials?.access_token) throw new CliError("Not signed in. Run `pensieve login` first.", 2);
  const server = serverUrl(io);
  if (credentials.server && credentials.server !== server) {
    throw new CliError(
      `Signed in to ${credentials.server}, but this command targets ${server}. Run \`pensieve login\`.`,
      2,
    );
  }
  const expiry = credentials.expires_at ? Date.parse(credentials.expires_at) : null;
  if (expiry === null || expiry - REFRESH_MARGIN_MS > io.now()) return credentials.access_token;
  if (!credentials.refresh_token || !credentials.token_endpoint) {
    throw new CliError("Your Pensieve session has expired. Run `pensieve login`.", 2);
  }
  let tokens;
  try {
    tokens = await refreshTokens(io.fetch, credentials.token_endpoint, {
      refreshToken: credentials.refresh_token,
      clientId: credentials.client_id,
      resource: resourceIndicator(io),
    });
  } catch (error) {
    throw new CliError(`Could not refresh your Pensieve session (${error.message}). Run \`pensieve login\`.`, 2);
  }
  const refreshed = {
    ...credentials,
    access_token: tokens.access_token,
    refresh_token: tokens.refresh_token || credentials.refresh_token,
    expires_at: expiresAt(tokens, io.now()),
  };
  await saveCredentials(io, refreshed);
  return refreshed.access_token;
}
