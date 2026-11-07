import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import doctor from "../src/commands/doctor.js";
import pauseCommand from "../src/commands/pause.js";
import resumeCommand from "../src/commands/resume.js";
import { pausePath, readPause } from "../src/lib/pause.js";
import { makeIo, tempDir } from "./helpers.js";

const EFFECT = /saves no transcripts and records no commit or PR anchors[\s\S]*briefings and MCP tools keep working/;

test("pause defaults to ~/.local/state/pensieve/capture and creates it privately", async () => {
  const io = makeIo({ env: {} });
  assert.equal(pausePath(io), join(io.home, ".local/state/pensieve/capture/paused.json"));
  assert.equal(await pauseCommand.run(io), 0);
  assert.deepEqual(JSON.parse(readFileSync(pausePath(io), "utf8")), { paused_at: "2026-11-06T12:00:00.000Z" });
  assert.equal(statSync(pausePath(io)).mode & 0o777, 0o600);
  assert.equal(statSync(dirname(pausePath(io))).mode & 0o777, 0o700);
  assert.match(io.stdout.text, /^Paused since 2026-11-06T12:00:00\.000Z\. /);
  assert.match(io.stdout.text, EFFECT);
  assert.equal(io.stdout.text.trim().split("\n").length, 1);
});

test("PENSIEVE_CAPTURE_STATE overrides the directory", async () => {
  const state = join(tempDir(), "custom-state");
  const io = makeIo({ env: { PENSIEVE_CAPTURE_STATE: state } });
  await pauseCommand.run(io);
  assert.ok(existsSync(join(state, "paused.json")));
  assert.equal(existsSync(join(io.home, ".local")), false);
});

test("pausing twice keeps the original time; resuming twice is a no-op", async () => {
  let now = Date.parse("2026-11-06T12:00:00Z");
  const io = makeIo({ env: {}, now: () => now });
  await pauseCommand.run(io);
  now += 3_600_000;
  io.stdout.text = "";
  await pauseCommand.run(io);
  assert.match(io.stdout.text, /^Already paused since 2026-11-06T12:00:00\.000Z\./);
  assert.equal(JSON.parse(readFileSync(pausePath(io), "utf8")).paused_at, "2026-11-06T12:00:00.000Z");

  io.stdout.text = "";
  assert.equal(await resumeCommand.run(io), 0);
  assert.match(io.stdout.text, /^Resumed: .*nothing from the paused period will be uploaded/);
  assert.equal(existsSync(pausePath(io)), false);
  io.stdout.text = "";
  assert.equal(await resumeCommand.run(io), 0);
  assert.match(io.stdout.text, /^Not paused; nothing to resume\./);
});

test("a malformed pause file reads as paused, shows so in doctor, and pause rewrites it", async () => {
  const io = makeIo({ env: {}, fetch: async () => new Response("{}", { status: 200 }) });
  mkdirSync(dirname(pausePath(io)), { recursive: true, mode: 0o700 });
  for (const content of ["not json", '{"paused_at": 5}', "null", "{}"]) {
    writeFileSync(pausePath(io), content, { mode: 0o600 });
    assert.deepEqual(await readPause(io), { paused: true, since: null }, content);
  }

  await doctor.run(io);
  assert.match(io.stdout.text, /Transcript saving: paused \(the pause file is unreadable/);

  io.stdout.text = "";
  await pauseCommand.run(io);
  assert.match(io.stdout.text, /^Paused since 2026-11-06T12:00:00\.000Z \(replaced an unreadable pause file\)\./);
  assert.deepEqual(await readPause(io), { paused: true, since: "2026-11-06T12:00:00.000Z" });
});

test("doctor reports not paused, then paused since when", async () => {
  const io = makeIo({ env: {}, fetch: async () => new Response("{}", { status: 200 }) });
  await doctor.run(io);
  assert.match(io.stdout.text, /Transcript saving: not paused\n/);
  await pauseCommand.run(io);
  io.stdout.text = "";
  await doctor.run(io);
  assert.match(io.stdout.text, /Transcript saving: paused since 2026-11-06T12:00:00\.000Z/);
});
