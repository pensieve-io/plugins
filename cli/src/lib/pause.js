import { lstat, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { writePrivateJson } from "./files.js";
import { captureStateDir } from "./paths.js";

/**
 * The machine-wide "off the record" switch the plugin's hooks read:
 * `paused.json` in the capture state directory, holding `{"paused_at": ISO}`.
 * Any file there means paused; one that cannot be read is still paused,
 * matching the hooks' fail-closed reading.
 */
export const pausePath = (io) => join(captureStateDir(io), "paused.json");

export const PAUSE_EFFECT =
  "While paused, the Pensieve plugin saves no transcripts and records no commit or PR anchors from any " +
  "Claude Code or Codex conversation on this machine, and nothing from the paused period is uploaded after " +
  "resuming; company briefings and MCP tools keep working.";

/** `{ paused: false }`, or `{ paused: true, since }` where `since` is null if unreadable. */
export async function readPause(io) {
  let raw;
  try {
    raw = await readFile(pausePath(io), "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return { paused: false };
    return { paused: true, since: null };
  }
  try {
    const since = JSON.parse(raw)?.paused_at;
    if (typeof since === "string" && !Number.isNaN(Date.parse(since))) return { paused: true, since };
  } catch {
    // Malformed: still paused.
  }
  return { paused: true, since: null };
}

/** Pause unless already paused with a readable file; returns the state and whether it wrote. */
export async function pause(io) {
  const current = await readPause(io);
  if (current.paused && current.since) return { ...current, written: false };
  const since = new Date(io.now()).toISOString();
  await writePrivateJson(pausePath(io), { paused_at: since });
  return { paused: true, since, written: true, replaced: current.paused };
}

/** Remove the pause file; returns whether there was one. */
export async function resume(io) {
  try {
    await lstat(pausePath(io));
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
  await rm(pausePath(io), { force: true });
  return true;
}
