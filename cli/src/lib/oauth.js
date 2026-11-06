import { createHash, randomBytes } from "node:crypto";
import { createServer } from "node:http";
import { CliError } from "./errors.js";

export const CLIENT_NAME = "Pensieve CLI";
const WANTED_SCOPES = ["openid", "email", "profile", "offline_access"];
const CALLBACK_TIMEOUT_MS = 5 * 60 * 1000;

export const base64url = (buffer) =>
  buffer.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

export function createPkce() {
  const verifier = base64url(randomBytes(32));
  const challenge = base64url(createHash("sha256").update(verifier).digest());
  return { verifier, challenge, method: "S256" };
}

export const createState = () => base64url(randomBytes(16));

async function getJson(fetch, url) {
  let response;
  try {
    response = await fetch(String(url), { headers: { Accept: "application/json" } });
  } catch (error) {
    throw new CliError(`Could not reach ${url}: ${error.message}`);
  }
  if (!response.ok) return null;
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/**
 * Authorisation server metadata for the MCP server. Tries the MCP origin's
 * RFC 8414 document first, then follows its protected-resource metadata
 * (RFC 9728) to the issuer. Nothing about the issuer is hard-coded.
 */
export async function discover(fetch, mcpUrl) {
  const server = new URL(mcpUrl);
  let metadata = await getJson(fetch, `${server.origin}/.well-known/oauth-authorization-server`);
  if (!metadata) {
    const path = server.pathname.replace(/\/$/, "");
    const resource =
      (await getJson(fetch, `${server.origin}/.well-known/oauth-protected-resource${path}`)) ||
      (await getJson(fetch, `${server.origin}/.well-known/oauth-protected-resource`));
    const issuer = resource?.authorization_servers?.[0];
    if (issuer) {
      const url = new URL(issuer);
      metadata =
        (await getJson(
          fetch,
          `${url.origin}/.well-known/oauth-authorization-server${url.pathname.replace(/\/$/, "")}`,
        )) || (await getJson(fetch, `${issuer.replace(/\/$/, "")}/.well-known/openid-configuration`));
    }
  }
  if (!metadata?.authorization_endpoint || !metadata?.token_endpoint) {
    throw new CliError(`Could not discover Pensieve's sign-in endpoints from ${server.origin}.`);
  }
  const methods = metadata.code_challenge_methods_supported;
  if (Array.isArray(methods) && !methods.includes("S256")) {
    throw new CliError("Pensieve's authorisation server does not support PKCE (S256).");
  }
  return metadata;
}

/** RFC 7591 dynamic registration as a public client (no secret). */
export async function registerClient(fetch, metadata, redirectUri) {
  if (!metadata.registration_endpoint) {
    throw new CliError("Pensieve's authorisation server does not offer dynamic client registration.");
  }
  const response = await fetch(metadata.registration_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({
      client_name: CLIENT_NAME,
      redirect_uris: [redirectUri],
      grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"],
      token_endpoint_auth_method: "none",
    }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.client_id) {
    throw new CliError(
      `Client registration failed (HTTP ${response.status}): ${body.error_description || body.error || "no client_id returned"}`,
    );
  }
  return body;
}

/** Scopes to request: an override, or the wanted ones the server advertises. */
export function scopesFor(metadata, override) {
  if (override !== undefined) return override.trim() || null;
  const supported = metadata.scopes_supported;
  if (!Array.isArray(supported)) return null;
  const scopes = WANTED_SCOPES.filter((scope) => supported.includes(scope));
  return scopes.length ? scopes.join(" ") : null;
}

export function authorizeUrl(metadata, { clientId, redirectUri, pkce, state, scope, resource }) {
  const url = new URL(metadata.authorization_endpoint);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", clientId);
  url.searchParams.set("redirect_uri", redirectUri);
  url.searchParams.set("code_challenge", pkce.challenge);
  url.searchParams.set("code_challenge_method", pkce.method);
  url.searchParams.set("state", state);
  if (scope) url.searchParams.set("scope", scope);
  if (resource) url.searchParams.set("resource", resource);
  return url.toString();
}

const page = (message) =>
  `<!doctype html><meta charset="utf-8"><title>Pensieve</title><p style="font:16px system-ui;margin:3rem">${message}</p>`;

/**
 * A one-shot loopback listener for the authorisation redirect. The first
 * request to /callback settles it: a wrong `state`, an error or a missing
 * code rejects, so a stray or forged redirect can never be accepted later.
 */
export async function startCallbackServer({ port = 0, state, timeoutMs = CALLBACK_TIMEOUT_MS }) {
  let settle;
  const result = new Promise((resolve, reject) => (settle = { resolve, reject }));
  let settled = false;
  const finish = (fn, value) => {
    if (settled) return;
    settled = true;
    fn(value);
  };
  const server = createServer((request, response) => {
    const url = new URL(request.url, "http://127.0.0.1");
    if (url.pathname !== "/callback" || settled) {
      response.writeHead(404, { Connection: "close" }).end();
      return;
    }
    const params = url.searchParams;
    const reply = (status, message) =>
      response
        .writeHead(status, { "Content-Type": "text/html; charset=utf-8", Connection: "close" })
        .end(page(message));
    if (params.get("state") !== state) {
      reply(400, "Sign-in failed: the response did not match this request. Run pensieve login again.");
      return finish(settle.reject, new CliError("Sign-in failed: the state parameter did not match."));
    }
    if (params.get("error")) {
      const detail = params.get("error_description") || params.get("error");
      reply(400, "Sign-in failed. Return to the terminal for details.");
      return finish(settle.reject, new CliError(`Sign-in failed: ${detail}`));
    }
    const code = params.get("code");
    if (!code) {
      reply(400, "Sign-in failed: no authorisation code was returned.");
      return finish(settle.reject, new CliError("Sign-in failed: no authorisation code was returned."));
    }
    reply(200, "Signed in to Pensieve. You can close this window and return to the terminal.");
    finish(settle.resolve, code);
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  const timer = setTimeout(
    () => finish(settle.reject, new CliError("Timed out waiting for the browser sign-in.")),
    timeoutMs,
  );
  const close = () => {
    clearTimeout(timer);
    server.close();
  };
  result.then(close, close);
  return { port: server.address().port, result, close };
}

async function postForm(fetch, url, fields) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
    body: new URLSearchParams(Object.entries(fields).filter(([, v]) => v != null)).toString(),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.access_token) {
    throw new CliError(
      `Token request failed (HTTP ${response.status}): ${body.error_description || body.error || "no access token returned"}`,
    );
  }
  return body;
}

export const exchangeCode = (fetch, tokenEndpoint, { code, redirectUri, clientId, verifier, resource }) =>
  postForm(fetch, tokenEndpoint, {
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri,
    client_id: clientId,
    code_verifier: verifier,
    resource,
  });

export const refreshTokens = (fetch, tokenEndpoint, { refreshToken, clientId, resource }) =>
  postForm(fetch, tokenEndpoint, {
    grant_type: "refresh_token",
    refresh_token: refreshToken,
    client_id: clientId,
    resource,
  });

/** The `exp` claim of a JWT, in milliseconds, or null for an opaque token. */
export function jwtExpiry(token) {
  try {
    const payload = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString("utf8"));
    return typeof payload.exp === "number" ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

export function expiresAt(tokens, now) {
  const ms = tokens.expires_in ? now + Number(tokens.expires_in) * 1000 : jwtExpiry(tokens.access_token);
  return ms ? new Date(ms).toISOString() : null;
}
