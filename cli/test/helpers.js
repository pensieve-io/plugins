import { EventEmitter } from "node:events";
import { chmodSync, mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";

export const tempDir = () => mkdtempSync(join(tmpdir(), "pensieve-cli-test-"));

/** A capturing writable with `.text`. */
function sink() {
  const out = { text: "", write: (chunk) => ((out.text += chunk), true) };
  return out;
}

/**
 * A fake `spawn`: `handler(command, args)` returns `{ code, stdout }` (or a
 * number). Every call is recorded in `spawn.calls`.
 */
export function fakeSpawn(handler = () => 0) {
  const spawn = (command, args) => {
    spawn.calls.push([command, ...args]);
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.unref = () => {};
    const outcome = handler(command, args) ?? 0;
    const { code = 0, stdout = "" } = typeof outcome === "number" ? { code: outcome } : outcome;
    setImmediate(() => {
      child.stdout.end(stdout);
      child.stderr.end();
      setImmediate(() => child.emit("close", code));
    });
    return child;
  };
  spawn.calls = [];
  return spawn;
}

/** Directory containing executable stubs named `names`, for PATH lookups. */
export function binDir(names) {
  const dir = join(tempDir(), "bin");
  mkdirSync(dir);
  for (const name of names) {
    writeFileSync(join(dir, name), "#!/bin/sh\n");
    chmodSync(join(dir, name), 0o755);
  }
  return dir;
}

export function makeIo(overrides = {}) {
  const home = tempDir();
  return {
    fetch: async () => {
      throw new Error("unexpected network call");
    },
    spawn: fakeSpawn(),
    env: { PATH: "" },
    home,
    cwd: home,
    platform: "linux",
    stdin: null,
    stdout: sink(),
    stderr: sink(),
    now: () => Date.parse("2026-11-06T12:00:00Z"),
    openBrowser: () => {},
    ...overrides,
  };
}

export const jsonResponse = (body, status = 200, headers = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
