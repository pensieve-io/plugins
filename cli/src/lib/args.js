import { CliError } from "./errors.js";

const camel = (name) => name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());

/**
 * Parse `--flag`, `--flag value` and `--flag=value` against a spec of
 * `{ "flag-name": "boolean" | "string" }`. `-h`/`--help` is always accepted.
 */
export function parseArgs(argv, spec = {}) {
  const flags = {};
  const positionals = [];
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--") {
      positionals.push(...argv.slice(i + 1));
      break;
    }
    if (arg === "-h" || arg === "--help") {
      flags.help = true;
      continue;
    }
    if (!arg.startsWith("--")) {
      positionals.push(arg);
      continue;
    }
    const eq = arg.indexOf("=");
    const name = eq < 0 ? arg.slice(2) : arg.slice(2, eq);
    const type = spec[name];
    if (!type) throw new CliError(`Unknown option --${name}. See --help.`);
    if (type === "boolean") {
      if (eq >= 0) throw new CliError(`--${name} does not take a value.`);
      flags[camel(name)] = true;
    } else {
      const value = eq >= 0 ? arg.slice(eq + 1) : argv[++i];
      if (value === undefined) throw new CliError(`--${name} needs a value.`);
      flags[camel(name)] = value;
    }
  }
  return { flags, positionals };
}

/** A positive integer flag, or undefined when absent. */
export function intFlag(value, name) {
  if (value === undefined) return undefined;
  if (!/^\d+$/.test(String(value)) || Number(value) <= 0) {
    throw new CliError(`--${name} must be a positive whole number.`);
  }
  return Number(value);
}

/** A non-negative integer flag (offsets may be zero), or undefined. */
export function offsetFlag(value, name) {
  if (value === undefined) return undefined;
  if (!/^\d+$/.test(String(value))) throw new CliError(`--${name} must be a whole number.`);
  return Number(value);
}

/** `--context N`, else `PENSIEVE_CONTEXT_ID`; undefined lets the server use your only context. */
export const contextFlag = (io, flags) => intFlag(flags.context ?? io.env.PENSIEVE_CONTEXT_ID, "context");
