import assert from "node:assert/strict";
import { test } from "node:test";
import { normaliseRemote } from "../src/lib/git.js";

test("remote URLs normalise to owner/repo", () => {
  const cases = {
    "https://github.com/pensieve-io/Pensieve.git": "pensieve-io/Pensieve",
    "https://github.com/pensieve-io/plugins": "pensieve-io/plugins",
    "https://github.com/pensieve-io/plugins/": "pensieve-io/plugins",
    "https://x-access-token:ghs_secret@github.com/pensieve-io/plugins.git": "pensieve-io/plugins",
    "https://user@github.com/pensieve-io/plugins.git": "pensieve-io/plugins",
    "ssh://git@github.com/pensieve-io/plugins.git": "pensieve-io/plugins",
    "ssh://git@github.com:22/pensieve-io/plugins.git": "pensieve-io/plugins",
    "git@github.com:pensieve-io/plugins.git": "pensieve-io/plugins",
    "github.com:pensieve-io/plugins": "pensieve-io/plugins",
    "git://github.com/pensieve-io/plugins.git": "pensieve-io/plugins",
    "git@gitlab.com:group/sub/project.git": "group/sub/project",
    "  git@github.com:pensieve-io/plugins.git\n": "pensieve-io/plugins",
  };
  for (const [remote, expected] of Object.entries(cases)) {
    assert.equal(normaliseRemote(remote), expected, remote);
  }
});

test("unusable remotes normalise to null", () => {
  for (const remote of ["", null, "/srv/git/plugins.git", "https://github.com/", "not a url"]) {
    assert.equal(normaliseRemote(remote), null, String(remote));
  }
});
