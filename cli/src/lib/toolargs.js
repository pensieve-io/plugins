import { CliError } from "./errors.js";

/** `save-data` → `save_data`: a tool's command is its name with hyphens. */
export const toolName = (command) => command.replace(/-/g, "_");
export const commandName = (tool) => tool.replace(/_/g, "-");

/** A property's JSON types, without `null`. */
export const typesOf = (schema = {}) =>
  [schema.type, ...(schema.anyOf || schema.oneOf || []).map((s) => s.type)]
    .flat()
    .filter((type) => type && type !== "null");

function coerce(name, raw, schema) {
  const types = typesOf(schema);
  const flag = `--${commandName(name)}`;
  if (types.includes("object") || (types.includes("array") && /^\s*\[/.test(raw))) {
    try {
      return JSON.parse(raw);
    } catch {
      if (!types.includes("string")) throw new CliError(`${flag} must be JSON.`);
    }
  }
  if (types.includes("string")) return raw;
  if (types.includes("integer") || types.includes("number")) {
    if (raw.trim() === "" || Number.isNaN(Number(raw))) throw new CliError(`${flag} must be a number.`);
    return Number(raw);
  }
  if (types.includes("boolean")) {
    if (raw !== "true" && raw !== "false") throw new CliError(`${flag} must be true or false.`);
    return raw === "true";
  }
  return raw;
}

const isArray = (schema) => {
  const types = typesOf(schema);
  return types.includes("array") && !types.includes("string");
};
const isBoolean = (schema) => {
  const types = typesOf(schema);
  return types.length === 1 && types[0] === "boolean";
};

/**
 * Turn a tool command's argv into the tool's own arguments, guided only by the
 * schema the server publishes — so a tool the server adds works here unchanged.
 *
 *   --top-k 3            a property, hyphens for underscores
 *   --context 578        shorthand for --context-id
 *   --include-evidence   a boolean property (or --include-evidence=false)
 *   --trees a --trees b  an array property, repeated (or one JSON array)
 *   --body-file page.md  any property read from a file; `-` is stdin
 *   --args '{…}'         a JSON object of arguments, merged first
 *   one bare word        the tool's only required property
 */
export async function parseToolArgs(tool, argv, { env = {}, readText } = {}) {
  const properties = tool.inputSchema?.properties || {};
  const args = {};
  const positionals = [];
  let json = false;
  let help = false;
  const set = (name, value) => {
    if (isArray(properties[name]) && !Array.isArray(value)) args[name] = [...(args[name] || []), value];
    else args[name] = value;
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--") {
      positionals.push(...argv.slice(i + 1));
      break;
    }
    if (arg === "-h" || arg === "--help") {
      help = true;
      continue;
    }
    if (!arg.startsWith("--")) {
      positionals.push(arg);
      continue;
    }
    const eq = arg.indexOf("=");
    const flag = eq < 0 ? arg.slice(2) : arg.slice(2, eq);
    const inline = eq < 0 ? undefined : arg.slice(eq + 1);
    const value = () => {
      const v = inline ?? argv[++i];
      if (v === undefined) throw new CliError(`--${flag} needs a value.`);
      return v;
    };
    let name = toolName(flag);
    if (name === "json" && !("json" in properties)) {
      json = true;
      continue;
    }
    if (name === "args" && !("args" in properties)) {
      let extra;
      try {
        extra = JSON.parse(value());
      } catch {
        extra = null;
      }
      if (!extra || typeof extra !== "object" || Array.isArray(extra)) {
        throw new CliError("--args must be a JSON object.");
      }
      Object.assign(args, extra);
      continue;
    }
    if (name === "context" && !("context" in properties) && "context_id" in properties) name = "context_id";
    if (!(name in properties) && name.endsWith("_file") && name.slice(0, -5) in properties) {
      name = name.slice(0, -5);
      set(name, coerce(name, await readText(value()), properties[name]));
      continue;
    }
    if (!(name in properties)) {
      throw new CliError(`${tool.name} has no --${flag} option. See \`pensieve ${commandName(tool.name)} --help\`.`);
    }
    if (isBoolean(properties[name]) && inline === undefined) set(name, true);
    else set(name, coerce(name, value(), properties[name]));
  }
  if (help) return { help: true };
  if (positionals.length) {
    const open = (tool.inputSchema?.required || []).filter((name) => !(name in args));
    if (open.length !== 1) {
      throw new CliError(
        `${tool.name} takes its arguments as options. See \`pensieve ${commandName(tool.name)} --help\`.`,
      );
    }
    set(open[0], coerce(open[0], positionals.join(" "), properties[open[0]]));
  }
  if ("context_id" in properties && !("context_id" in args) && env.PENSIEVE_CONTEXT_ID) {
    args.context_id = coerce("context_id", env.PENSIEVE_CONTEXT_ID, properties.context_id);
  }
  return { args, json };
}

const firstLine = (text = "") => text.trim().split("\n")[0];

export function toolHelp(tool) {
  const properties = tool.inputSchema?.properties || {};
  const required = new Set(tool.inputSchema?.required || []);
  const lines = [`Usage: pensieve ${commandName(tool.name)} [options]`, "", (tool.description || "").trim(), "", "Options:"];
  for (const [name, schema] of Object.entries(properties)) {
    const types = typesOf(schema).join("|") || "value";
    const label = isBoolean(schema) ? `--${commandName(name)}` : `--${commandName(name)} <${types}>`;
    lines.push(`  ${label}${required.has(name) ? "  (required)" : ""}`);
    if (schema.description) lines.push(`      ${firstLine(schema.description)}`);
  }
  lines.push(
    "  --<option>-file <path>   Read any option's value from a file (`-` for stdin)",
    "  --args <json>            A JSON object of arguments",
    "  --json                   Print the raw MCP tool result",
  );
  return lines.join("\n");
}

export const toolSummary = (tool) => firstLine(tool.description).replace(/\s+/g, " ");
