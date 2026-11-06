// Entry for `node --test cli/test` on Node 21+, where the runner no longer
// expands a directory argument: Node resolves the directory through
// test/package.json to this file, which loads every suite. Node 18 and 20
// expand the directory themselves and never load this file.
import { readdirSync } from "node:fs";

const dir = new URL("./test/", import.meta.url);
for (const name of readdirSync(dir).filter((n) => n.endsWith(".test.js")).sort()) {
  await import(new URL(name, dir));
}
