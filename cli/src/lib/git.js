import { CliError } from "./errors.js";
import { capture } from "./exec.js";

/**
 * `owner/repo` (or `group/sub/repo`) from a git remote URL: https with or
 * without credentials, ssh://, git://, and scp-style `git@host:owner/repo.git`.
 */
export function normaliseRemote(remote) {
  const value = String(remote || "").trim();
  if (!value) return null;
  let path;
  const scp = value.match(/^(?:[^@/\s]+@)?[^:/\s]+:(?!\/\/)(.+)$/);
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) {
    try {
      path = new URL(value).pathname;
    } catch {
      return null;
    }
  } else if (scp) {
    path = scp[1];
  } else {
    return null;
  }
  const normalised = decodeURIComponent(path)
    .replace(/^\/+|\/+$/g, "")
    .replace(/\.git$/i, "")
    .replace(/^~[^/]*\//, "");
  return normalised.includes("/") ? normalised : null;
}

export async function git(io, args, { cwd, allowFailure = false } = {}) {
  const result = await capture(io, "git", args, { cwd });
  if (result.code !== 0 && !allowFailure) {
    throw new CliError(`git ${args.join(" ")} failed: ${(result.stderr || result.stdout).trim()}`);
  }
  return { ...result, out: result.stdout.trim() };
}
