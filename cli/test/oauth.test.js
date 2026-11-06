import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { statSync } from "node:fs";
import { request } from "node:http";
import { dirname } from "node:path";
import { test } from "node:test";
import { browserLogin } from "../src/commands/login.js";
import { getAccessToken, loadCredentials, saveCredentials } from "../src/lib/credentials.js";
import { base64url, createPkce, startCallbackServer } from "../src/lib/oauth.js";
import { credentialsPath } from "../src/lib/paths.js";
import { jsonResponse, makeIo } from "./helpers.js";

const ISSUER = "https://auth.example.test/auth/v1";
const METADATA = {
  issuer: ISSUER,
  authorization_endpoint: `${ISSUER}/oauth/authorize`,
  token_endpoint: `${ISSUER}/oauth/token`,
  registration_endpoint: `${ISSUER}/oauth/clients/register`,
  scopes_supported: ["openid", "email", "profile", "phone", "offline_access"],
  code_challenge_methods_supported: ["S256", "plain"],
  token_endpoint_auth_methods_supported: ["client_secret_basic", "none"],
};

/** GET a loopback URL, resolving to the status code. */
const visit = (url) =>
  new Promise((resolve, reject) => {
    request(url, (response) => {
      response.resume();
      response.on("end", () => resolve(response.statusCode));
    })
      .on("error", reject)
      .end();
  });

/** An authorisation server stub that records what the CLI sent. */
function authServer() {
  const seen = {};
  const fetch = async (url, init = {}) => {
    url = String(url);
    if (url === "https://mcp.example.test/.well-known/oauth-authorization-server") return jsonResponse(METADATA);
    if (url === METADATA.registration_endpoint) {
      seen.registration = JSON.parse(init.body);
      return jsonResponse({ client_id: "client-123", ...seen.registration }, 201);
    }
    if (url === METADATA.token_endpoint) {
      seen.token = Object.fromEntries(new URLSearchParams(init.body));
      return jsonResponse({ access_token: "access-1", refresh_token: "refresh-1", expires_in: 3600, token_type: "bearer" });
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  return { fetch, seen };
}

test("PKCE challenge is the S256 hash of the verifier", () => {
  const pkce = createPkce();
  assert.equal(pkce.method, "S256");
  assert.match(pkce.verifier, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(pkce.challenge, base64url(createHash("sha256").update(pkce.verifier).digest()));
});

test("browser login registers a public client, verifies state and stores tokens privately", async () => {
  const { fetch, seen } = authServer();
  let authorizeUrl;
  const io = makeIo({
    fetch,
    env: { PENSIEVE_MCP_URL: "https://mcp.example.test/mcp" },
    openBrowser: (url) => {
      authorizeUrl = new URL(url);
      const params = authorizeUrl.searchParams;
      const callback = new URL(params.get("redirect_uri"));
      callback.searchParams.set("code", "code-abc");
      callback.searchParams.set("state", params.get("state"));
      setImmediate(() => visit(callback));
    },
  });

  const credentials = await browserLogin(io);

  assert.deepEqual(seen.registration, {
    client_name: "Pensieve CLI",
    redirect_uris: [seen.registration.redirect_uris[0]],
    grant_types: ["authorization_code", "refresh_token"],
    response_types: ["code"],
    token_endpoint_auth_method: "none",
  });
  assert.match(seen.registration.redirect_uris[0], /^http:\/\/127\.0\.0\.1:\d+\/callback$/);
  const params = authorizeUrl.searchParams;
  assert.equal(params.get("code_challenge_method"), "S256");
  assert.equal(params.get("client_id"), "client-123");
  assert.equal(params.get("scope"), "openid email profile offline_access");
  assert.equal(params.get("resource"), null, "resource indicator is opt-in");
  assert.equal(seen.token.grant_type, "authorization_code");
  assert.equal(seen.token.code, "code-abc");
  assert.equal(seen.token.client_id, "client-123");
  assert.equal(seen.token.redirect_uri, params.get("redirect_uri"));
  assert.equal(
    base64url(createHash("sha256").update(seen.token.code_verifier).digest()),
    params.get("code_challenge"),
  );

  assert.equal(credentials.access_token, "access-1");
  assert.equal(credentials.expires_at, new Date(io.now() + 3600_000).toISOString());
  const stored = await loadCredentials(io);
  assert.deepEqual(
    { ...stored },
    {
      access_token: "access-1",
      refresh_token: "refresh-1",
      expires_at: credentials.expires_at,
      issuer: ISSUER,
      client_id: "client-123",
      token_endpoint: METADATA.token_endpoint,
      server: "https://mcp.example.test/mcp",
    },
  );
  assert.equal(statSync(credentialsPath(io)).mode & 0o777, 0o600);
  assert.equal(statSync(dirname(credentialsPath(io))).mode & 0o777, 0o700);
});

test("the callback rejects a mismatched state and accepts nothing afterwards", async () => {
  const listener = await startCallbackServer({ state: "expected" });
  const rejected = assert.rejects(listener.result, /state parameter did not match/);
  const base = `http://127.0.0.1:${listener.port}/callback`;
  assert.equal(await visit(`${base}?code=stolen&state=forged`), 400);
  await rejected;
});

test("the callback surfaces an authorisation error", async () => {
  const listener = await startCallbackServer({ state: "s" });
  const rejected = assert.rejects(listener.result, /access_denied|denied by user/);
  await visit(`http://127.0.0.1:${listener.port}/callback?state=s&error=access_denied&error_description=denied+by+user`);
  await rejected;
});

test("an expired access token is refreshed and the rotated pair saved", async () => {
  let sent;
  const io = makeIo({
    env: { PENSIEVE_MCP_URL: "https://mcp.example.test/mcp" },
    fetch: async (url, init) => {
      assert.equal(url, METADATA.token_endpoint);
      sent = Object.fromEntries(new URLSearchParams(init.body));
      return jsonResponse({ access_token: "access-2", refresh_token: "refresh-2", expires_in: 60 * 60 });
    },
  });
  await saveCredentials(io, {
    access_token: "access-1",
    refresh_token: "refresh-1",
    expires_at: new Date(io.now() - 1000).toISOString(),
    issuer: ISSUER,
    client_id: "client-123",
    token_endpoint: METADATA.token_endpoint,
    server: "https://mcp.example.test/mcp",
  });
  assert.equal(await getAccessToken(io), "access-2");
  assert.deepEqual(sent, { grant_type: "refresh_token", refresh_token: "refresh-1", client_id: "client-123" });
  const stored = await loadCredentials(io);
  assert.equal(stored.refresh_token, "refresh-2");
  assert.equal(statSync(credentialsPath(io)).mode & 0o777, 0o600);
});

test("a valid token is used without any network call", async () => {
  const io = makeIo({ env: {} });
  await saveCredentials(io, {
    access_token: "still-good",
    refresh_token: "r",
    expires_at: new Date(io.now() + 600_000).toISOString(),
    server: "https://mcp.pensieve.uk/mcp",
  });
  assert.equal(await getAccessToken(io), "still-good");
});
