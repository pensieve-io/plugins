import assert from "node:assert/strict";
import { existsSync, statSync } from "node:fs";
import { test } from "node:test";
import install from "../src/commands/install.js";
import uninstall from "../src/commands/uninstall.js";
import { receiptPath } from "../src/lib/paths.js";
import { loadReceipt } from "../src/lib/receipt.js";
import { binDir, fakeSpawn, makeIo } from "./helpers.js";

/** Fake clients where Claude's marketplace already exists and nothing else does. */
function clients(state = { claudeMarketplace: true }) {
  return fakeSpawn((command, args) => {
    const line = [command, ...args].join(" ");
    if (line === "claude plugin marketplace list --json") {
      return { stdout: JSON.stringify(state.claudeMarketplace ? [{ name: "pensieve" }] : []) };
    }
    if (line === "claude plugin list --json") return { stdout: "[]" };
    if (line === "codex plugin marketplace list") return { stdout: "MARKETPLACE  ROOT\nopenai-bundled  /x\n" };
    if (line === "codex plugin list") return { stdout: "" };
    return state.fail === line ? 1 : 0;
  });
}

const mutating = (calls) => calls.filter((c) => !c.includes("list")).map((c) => c.join(" "));

test("install runs the documented commands, skipping what already exists, and records only its own", async () => {
  const io = makeIo({ spawn: clients(), env: { PATH: binDir(["claude", "codex"]) } });
  assert.equal(await install.run(io, {}), 0);
  assert.deepEqual(mutating(io.spawn.calls), [
    "claude plugin install pensieve@pensieve",
    "codex plugin marketplace add pensieve-io/plugins",
    "codex plugin add pensieve@pensieve",
    "codex mcp login pensieve",
  ]);
  const receipt = await loadReceipt(io);
  assert.deepEqual(receipt.clients.claude.steps.map((s) => s.id), ["plugin"], "the existing marketplace is not ours");
  assert.deepEqual(receipt.clients.codex.steps.map((s) => s.id), ["marketplace", "plugin", "login"]);
  assert.equal(receipt.cli_version, "0.1.0");
  assert.ok(receipt.clients.claude.steps[0].ran_at);
  assert.equal(statSync(receiptPath(io)).mode & 0o777, 0o600);
  assert.match(io.stdout.text, /run \/mcp/);
});

test("uninstall reverses exactly the receipt, newest first, then deletes it", async () => {
  const io = makeIo({ spawn: clients(), env: { PATH: binDir(["claude", "codex"]) } });
  await install.run(io, {});
  io.spawn = clients();
  assert.equal(await uninstall.run(io, {}), 0);
  assert.deepEqual(mutating(io.spawn.calls), [
    "claude plugin uninstall pensieve@pensieve",
    "codex mcp logout pensieve",
    "codex plugin remove pensieve@pensieve",
    "codex plugin marketplace remove pensieve",
  ]);
  assert.equal(existsSync(receiptPath(io)), false);
});

test("a failed reversal stays in the receipt for a retry", async () => {
  const io = makeIo({ spawn: clients(), env: { PATH: binDir(["claude", "codex"]) } });
  await install.run(io, { codex: true });
  io.spawn = clients({ fail: "codex plugin remove pensieve@pensieve" });
  assert.equal(await uninstall.run(io, {}), 1);
  const receipt = await loadReceipt(io);
  assert.deepEqual(receipt.clients.codex.steps.map((s) => s.id), ["plugin"]);
});

test("a failed install step stops that client and is not recorded", async () => {
  const io = makeIo({
    spawn: clients({ claudeMarketplace: false, fail: "claude plugin install pensieve@pensieve" }),
    env: { PATH: binDir(["claude"]) },
  });
  assert.equal(await install.run(io, {}), 1);
  const receipt = await loadReceipt(io);
  assert.deepEqual(receipt.clients.claude.steps.map((s) => s.id), ["marketplace"]);
  assert.deepEqual(Object.keys(receipt.clients), ["claude"]);
});

test("dry runs print commands without running or recording anything", async () => {
  const io = makeIo({ spawn: clients(), env: { PATH: "" } });
  assert.equal(await install.run(io, { claude: true, dryRun: true }), 0);
  assert.equal(io.spawn.calls.length, 0);
  assert.match(io.stdout.text, /\$ claude plugin marketplace add pensieve-io\/plugins\n\$ claude plugin install pensieve@pensieve/);
  assert.equal(existsSync(receiptPath(io)), false);
});

test("uninstall without a receipt does nothing", async () => {
  const io = makeIo();
  assert.equal(await uninstall.run(io, {}), 0);
  assert.equal(io.spawn.calls.length, 0);
});
