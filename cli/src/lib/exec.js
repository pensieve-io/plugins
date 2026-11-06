import { accessSync, constants, statSync } from "node:fs";
import { delimiter, join } from "node:path";

const isWindows = (io) => io.platform === "win32";

/** Run a command, streaming its output to this terminal. Resolves to the exit code. */
export function run(io, command, args, options = {}) {
  return new Promise((resolve) => {
    const child = io.spawn(command, args, {
      stdio: "inherit",
      cwd: options.cwd || io.cwd,
      shell: isWindows(io),
    });
    child.on("error", (error) => {
      io.stderr.write(`Could not run ${command}: ${error.message}\n`);
      resolve(127);
    });
    child.on("close", (code) => resolve(code ?? 1));
  });
}

/** Run a command and collect its output. Never rejects: a spawn failure is code 127. */
export function capture(io, command, args, options = {}) {
  return new Promise((resolve) => {
    let stdout = "";
    let stderr = "";
    const child = io.spawn(command, args, {
      stdio: ["ignore", "pipe", "pipe"],
      cwd: options.cwd || io.cwd,
      shell: isWindows(io),
    });
    child.stdout?.on("data", (chunk) => (stdout += chunk));
    child.stderr?.on("data", (chunk) => (stderr += chunk));
    child.on("error", (error) => resolve({ code: 127, stdout, stderr: error.message }));
    child.on("close", (code) => resolve({ code: code ?? 1, stdout, stderr }));
  });
}

/** The absolute path of `name` on PATH, or null. */
export function which(io, name) {
  const extensions = isWindows(io)
    ? (io.env.PATHEXT || ".EXE;.CMD;.BAT").split(";").filter(Boolean)
    : [""];
  for (const dir of (io.env.PATH || "").split(delimiter).filter(Boolean)) {
    for (const extension of extensions) {
      const candidate = join(dir, name + extension);
      try {
        if (!statSync(candidate).isFile()) continue;
        if (!isWindows(io)) accessSync(candidate, constants.X_OK);
        return candidate;
      } catch {
        // not here
      }
    }
  }
  return null;
}

/** Best-effort browser launch. The caller always prints the URL as well. */
export function openBrowser(io, url) {
  const [command, args, extra] =
    io.platform === "darwin"
      ? ["open", [url], {}]
      : io.platform === "win32"
        ? ["cmd.exe", ["/c", "start", '""', `"${url}"`], { windowsVerbatimArguments: true }]
        : ["xdg-open", [url], {}];
  try {
    const child = io.spawn(command, args, { stdio: "ignore", detached: true, ...extra });
    child.on("error", () => {});
    child.unref?.();
  } catch {
    // The printed URL is the fallback.
  }
}
