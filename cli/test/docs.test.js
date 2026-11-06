import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { inside, writeDocs } from "../src/commands/docs.js";
import { POINTER_END, POINTER_START, pointerText, upsertPointer } from "../src/lib/pointer.js";
import {
  INDEX_MARKER,
  parseOutline,
  parseTreeList,
  planPaths,
  renderIndex,
  selectTree,
  slugify,
} from "../src/lib/tree.js";
import { tempDir } from "./helpers.js";

const LISTING = `Available Page trees (3):
- \`main\` — Organisation (main, tree id \`4a4d\`, root \`page:1\`)
- \`pensieve-io/Pensieve@prod\` — Pensieve (repository, tree id \`77aa\`, root \`page:900\`, https://github.com/pensieve-io/Pensieve, status completed, processed 0123456789abcdef0123456789abcdef01234567, detected fedcba9876543210fedcba9876543210fedcba98)
- \`pensieve-io/Pensieve@staging\` — Pensieve (repository, tree id \`88bb\`, root \`page:950\`, status completed, processed abcdef1)`;

const OUTLINE = `# Tree: \`pensieve-io/Pensieve@prod\`

pensieve "Pensieve" (page:900)
├── backend "Backend" (page:901)
│   ├── api "API Routes" (page:902)
│   └── workers "Workers" (page:903) (+2 below: Scheduler, Agents)
├── ../../etc "../../etc/passwd" (page:904)
└── index "Index" (page:905)
    └── backend "Backend" (page:906)

Open any listed \`page:ID\` with \`read\`.`;

test("list_trees text parses into selectors with revisions", () => {
  const trees = parseTreeList(LISTING);
  assert.equal(trees.length, 3);
  assert.deepEqual(trees[1], {
    selector: "pensieve-io/Pensieve@prod",
    rootPageId: 900,
    processed: "0123456789abcdef0123456789abcdef01234567",
    detected: "fedcba9876543210fedcba9876543210fedcba98",
    status: "completed",
  });
  assert.equal(trees[0].processed, null);
});

test("tree selection matches owner/repo, prefers the current branch, and honours --tree", () => {
  const trees = parseTreeList(LISTING);
  assert.equal(selectTree(trees, { repo: "pensieve-io/pensieve", branch: "staging" }).selector, "pensieve-io/Pensieve@staging");
  assert.throws(() => selectTree(trees, { repo: "pensieve-io/Pensieve", branch: "main" }), /Several trees match/);
  assert.throws(() => selectTree(trees, { repo: "acme/other", branch: "main" }), /No Pensieve repository tree for acme\/other/);
  assert.equal(selectTree(trees, { selector: "PENSIEVE-IO/Pensieve@prod" }).rootPageId, 900);
  assert.equal(selectTree(trees.slice(0, 2), { repo: "pensieve-io/Pensieve", branch: "x" }).selector, "pensieve-io/Pensieve@prod");
  assert.throws(() => selectTree(trees, { selector: "nope" }), /No tree named/);
});

test("the outline parses levels and notes truncated branches", () => {
  const { pages, truncated } = parseOutline(OUTLINE);
  assert.equal(truncated, true);
  assert.deepEqual(
    pages.map((p) => [p.level, p.slug, p.id]),
    [
      [0, "pensieve", 900],
      [1, "backend", 901],
      [2, "api", 902],
      [2, "workers", 903],
      [1, "../../etc", 904],
      [1, "index", 905],
      [2, "backend", 906],
    ],
  );
  assert.equal(pages[4].title, "../../etc/passwd");
});

test("paths mirror the hierarchy, are unique, never traverse and never replace index.md", () => {
  const planned = planPaths(parseOutline(OUTLINE).pages).map((p) => p.path);
  assert.deepEqual(planned, [
    "pensieve.md",
    "backend.md",
    "backend/api.md",
    "backend/workers.md",
    "etc.md",
    "index-2.md",
    "index-2/backend.md",
  ]);
  assert.equal(slugify("../../.."), "page");
  assert.equal(slugify("Café / Ünïcode: «Notes»"), "cafe-unicode-notes");
  assert.equal(slugify("x".repeat(200)).length, 80);
  assert.throws(() => inside("/tmp/out", "../escape.md"), /Refusing to write outside/);
});

test("writeDocs writes the tree, replaces its own previous pull and protects foreign directories", async () => {
  const out = join(tempDir(), "docs");
  const pages = planPaths(parseOutline(OUTLINE).pages).map((p) => ({ ...p, content: `# ${p.title}\n` }));
  const index = renderIndex({
    tree: parseTreeList(LISTING)[1],
    repo: "pensieve-io/Pensieve",
    head: "9999999999999999999999999999999999999999",
    pages,
    total: pages.length,
    truncated: false,
    pulledAt: "2026-11-06T12:00:00.000Z",
  });
  await writeDocs(out, pages, index);
  assert.equal(readFileSync(join(out, "backend/api.md"), "utf8"), "# API Routes\n");
  assert.equal(readFileSync(join(out, "etc.md"), "utf8"), "# ../../etc/passwd\n");
  const written = readFileSync(join(out, "index.md"), "utf8");
  assert.ok(written.startsWith(INDEX_MARKER));
  assert.match(written, /Processed revision: `0123456789abcdef0123456789abcdef01234567`/);
  assert.match(written, /\*\*Stale:\*\*/);
  assert.match(written, /^    - \[API Routes\]\(backend\/api\.md\)$/m);

  await writeDocs(out, pages.slice(0, 1), index);
  assert.equal(existsSync(join(out, "backend/api.md")), false, "a re-pull removes Pages that went away");

  const foreign = join(tempDir(), "mine");
  mkdirSync(foreign);
  writeFileSync(join(foreign, "index.md"), "# My notes\n");
  await assert.rejects(writeDocs(foreign, pages, index), /did not write/);
  assert.equal(readFileSync(join(foreign, "index.md"), "utf8"), "# My notes\n");
});

test("index.md is not marked stale when HEAD matches the processed revision", () => {
  const index = renderIndex({
    tree: parseTreeList(LISTING)[2],
    head: "abcdef1234567890abcdef1234567890abcdef12",
    pages: [],
    total: 0,
    truncated: false,
    pulledAt: "now",
  });
  assert.doesNotMatch(index, /Stale/);
});

test("the AGENTS.md pointer is inserted once and refreshed in place", () => {
  const text = pointerText(".pensieve/docs");
  const original = "# Agents\n\nBe kind.\n";
  const once = upsertPointer(original, text);
  assert.equal(once, `${original}\n${POINTER_START}\n${text}\n${POINTER_END}\n`);
  assert.equal(upsertPointer(once, text), once);
  const moved = upsertPointer(once, pointerText("docs/pensieve"));
  assert.equal(moved.split(POINTER_START).length, 2);
  assert.match(moved, /docs\/pensieve/);
  assert.ok(moved.startsWith(original));
  assert.equal(upsertPointer("", text), `${POINTER_START}\n${text}\n${POINTER_END}\n`);
});
