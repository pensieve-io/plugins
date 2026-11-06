import { appendFile, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { contextFlag, intFlag } from "../lib/args.js";
import { CliError } from "../lib/errors.js";
import { git, normaliseRemote } from "../lib/git.js";
import { openMcp, toolText } from "../lib/mcp.js";
import { pointerText, upsertPointer } from "../lib/pointer.js";
import {
  INDEX_MARKER,
  parseOutline,
  parseTreeList,
  planPaths,
  renderIndex,
  sameRevision,
  selectTree,
} from "../lib/tree.js";

export const DEFAULT_OUT = ".pensieve/docs";
const DEFAULT_MAX_PAGES = 500;
const CONCURRENCY = 4;
const MAX_DEPTH = 12;

async function mapLimit(items, limit, fn) {
  const results = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const index = next++;
      results[index] = await fn(items[index], index);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

/** A path inside `root`, refusing anything that would escape it. */
export function inside(root, path) {
  const target = resolve(root, path);
  if (target !== resolve(root) && !target.startsWith(resolve(root) + sep)) {
    throw new CliError(`Refusing to write outside ${root}: ${path}`);
  }
  return target;
}

/**
 * Replace a previous pull and write the Pages. An existing directory is only
 * cleared when its index.md carries our marker, so unrelated files are safe.
 */
export async function writeDocs(outDir, pages, indexText) {
  let previous = null;
  try {
    previous = await readFile(join(outDir, "index.md"), "utf8");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  if (previous !== null && !previous.startsWith(INDEX_MARKER)) {
    throw new CliError(`${outDir} already has an index.md that pensieve did not write; choose another --out.`);
  }
  if (previous !== null) await rm(outDir, { recursive: true, force: true });
  for (const page of pages) {
    const target = inside(outDir, page.path);
    await mkdir(dirname(target), { recursive: true });
    await writeFile(target, page.content);
  }
  await mkdir(outDir, { recursive: true });
  await writeFile(join(outDir, "index.md"), indexText);
}

/** Add the default output to .gitignore unless git already ignores it. */
async function ensureIgnored(io, root, out) {
  const ignored = await git(io, ["check-ignore", "-q", out], { cwd: root, allowFailure: true });
  if (ignored.code === 0) return false;
  const path = join(root, ".gitignore");
  let existing = "";
  try {
    existing = await readFile(path, "utf8");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  await appendFile(path, `${existing && !existing.endsWith("\n") ? "\n" : ""}/${out}/\n`);
  return true;
}

async function addPointers(io, root, out) {
  const touched = [];
  for (const name of ["AGENTS.md", "CLAUDE.md"]) {
    const path = join(root, name);
    let content;
    try {
      content = await readFile(path, "utf8");
    } catch (error) {
      if (error.code === "ENOENT") continue;
      throw error;
    }
    const updated = upsertPointer(content, pointerText(out));
    if (updated !== content) await writeFile(path, updated);
    touched.push(name);
  }
  return touched;
}

async function pull(io, flags) {
  const root = (await git(io, ["rev-parse", "--show-toplevel"]).catch(() => {
    throw new CliError("`pensieve docs pull` must be run inside a git repository.");
  })).out;
  const maxPages = intFlag(flags.maxPages, "max-pages") ?? DEFAULT_MAX_PAGES;
  const context = contextFlag(io, flags);
  const repo = flags.tree
    ? null
    : normaliseRemote((await git(io, ["remote", "get-url", "origin"], { cwd: root, allowFailure: true })).out);
  if (!flags.tree && !repo) throw new CliError("Could not read an `origin` remote; pass --tree.");
  const branch = (await git(io, ["rev-parse", "--abbrev-ref", "HEAD"], { cwd: root, allowFailure: true })).out;
  const head = (await git(io, ["rev-parse", "HEAD"], { cwd: root, allowFailure: true })).out || null;

  const mcp = await openMcp(io);
  const trees = parseTreeList(toolText(await mcp.call("list_trees", { context })));
  const tree = selectTree(trees, { repo, selector: flags.tree, branch });
  io.stdout.write(`Pulling ${tree.selector}${tree.processed ? ` (processed ${tree.processed})` : ""}\n`);

  const outline = await mcp.callAll("get_tree", { context, tree: tree.selector, depth: MAX_DEPTH, includeSummaries: false });
  const { pages: all, truncated } = parseOutline(outline);
  if (!all.length) throw new CliError(`The tree ${tree.selector} has no Pages yet.`);
  const pages = planPaths(all.slice(0, maxPages));
  if (all.length > maxPages) {
    io.stderr.write(`This tree has ${all.length} Pages; pulling the first ${maxPages}. Raise --max-pages for more.\n`);
  }

  let done = 0;
  const pulledAt = new Date(io.now()).toISOString();
  const bodies = await mapLimit(pages, CONCURRENCY, async (page) => {
    const body = await mcp.callAll("read", { identifier: `page:${page.id}`, context, tree: tree.selector });
    if (++done % 25 === 0) io.stderr.write(`  ${done}/${pages.length} Pages\n`);
    return `<!-- pensieve page:${page.id} from ${tree.selector}, pulled ${pulledAt} -->\n${body.trim()}\n`;
  });

  const outFlag = flags.out ?? DEFAULT_OUT;
  const outDir = isAbsolute(outFlag) ? outFlag : resolve(flags.out ? io.cwd : root, outFlag);
  const index = renderIndex({ tree, repo, contextId: context, head, pages, total: all.length, truncated, pulledAt });
  await writeDocs(outDir, pages.map((page, i) => ({ ...page, content: bodies[i] })), index);
  io.stdout.write(`Wrote ${pages.length} Pages to ${outDir}\n`);

  const relativeOut = relative(root, outDir).split(sep).join("/");
  if (!flags.out && (await ensureIgnored(io, root, relativeOut))) io.stdout.write(`Added /${relativeOut}/ to .gitignore\n`);
  if (flags.pointer) {
    const touched = await addPointers(io, root, relativeOut);
    io.stdout.write(touched.length ? `Pointer in ${touched.join(", ")}\n` : "No AGENTS.md or CLAUDE.md to add a pointer to.\n");
  }
  if (head && tree.processed && !sameRevision(head, tree.processed)) {
    io.stderr.write(
      `Warning: Pensieve processed ${tree.processed}, but local HEAD is ${head.slice(0, 12)}. These docs may lag the code.\n`,
    );
  } else if (!tree.processed) {
    io.stderr.write("Warning: Pensieve reports no processed revision for this tree; freshness is unknown.\n");
  }
  return 0;
}

export default {
  name: "docs",
  summary: "Pull this repository's Pensieve docs into Markdown files",
  usage: `Usage: pensieve docs pull [--context N] [--tree SELECTOR] [--out DIR] [--pointer] [--max-pages N]

Inside a git repository: finds the Pensieve repository tree matching the
\`origin\` remote (owner/repo@branch), fetches every Page and writes them as
Markdown mirroring the tree, plus index.md with the revision Pensieve last
processed. Warns when that differs from local HEAD. Local only: this is not
the GitHub mirror.

Options:
  --context N        Context id (default: PENSIEVE_CONTEXT_ID, or your only context)
  --tree SELECTOR    Tree to pull instead of matching the origin remote
  --out DIR          Output directory (default .pensieve/docs at the repository root,
                     which is added to .gitignore if not already ignored)
  --pointer          Add a marked pointer to AGENTS.md / CLAUDE.md where they exist
  --max-pages N      Stop after N Pages (default ${DEFAULT_MAX_PAGES})`,
  options: { context: "string", tree: "string", out: "string", pointer: "boolean", "max-pages": "string" },

  async run(io, flags, positionals) {
    if (positionals[0] !== "pull" || positionals.length !== 1) {
      throw new CliError("Usage: pensieve docs pull [options]. See `pensieve docs --help`.");
    }
    return pull(io, flags);
  },
};
