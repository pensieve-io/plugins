"""Describe the git work tree a hook ran in. Standard library, Python 3.9+.

Only the repository's public identity leaves the device: the `origin` URL with
any credentials removed, the current branch and the HEAD commit. Every git call
is bounded and any failure means "no workspace", never an error in the hook.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

GIT_TIMEOUT_SECONDS = 0.3
MAX_REPO_URL_CHARS = 500
MAX_BRANCH_CHARS = 255
SHA = re.compile(r"[0-9a-f]{40}")
# `[user@]host:path`, git's scp-like syntax. A Windows drive letter or a local
# path never matches, since the host needs at least two characters.
SCP_REMOTE = re.compile(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]{2,}:(?!/)\S+")
REMOTE_SCHEMES = {"https", "http", "ssh", "git", "git+ssh", "ssh+git"}


def git(cwd: str | Path, *args: str, timeout: float = GIT_TIMEOUT_SECONDS) -> str | None:
    """Run one read-only git command; its trimmed stdout, or None on any failure."""
    env = dict(
        os.environ,
        GIT_TERMINAL_PROMPT="0",
        # Status-style reads must never take the index lock from the agent's git.
        GIT_OPTIONAL_LOCKS="0",
        LC_ALL="C",
    )
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode().strip()
    except UnicodeDecodeError:
        return None


def public_remote(url: str | None) -> str | None:
    """The remote's address without userinfo, or None for a local path."""
    if not url or any(character.isspace() for character in url):
        return None
    if "://" in url:
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        if parts.scheme.lower() not in REMOTE_SCHEMES or not parts.hostname:
            return None
        # `https://x-access-token:secret@github.com/o/r.git` → `https://github.com/o/r.git`.
        # A query or fragment could carry a token too; neither identifies a repository.
        cleaned = urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))
    elif SCP_REMOTE.fullmatch(url):
        # The scp-like user (`git@`) is an account name, never a secret; the
        # server normalises this form.
        cleaned = url
    else:
        return None
    return cleaned if len(cleaned) <= MAX_REPO_URL_CHARS else None


def head_sha(cwd: str | Path) -> str | None:
    value = git(cwd, "rev-parse", "--verify", "-q", "HEAD")
    return value if value and SHA.fullmatch(value) else None


def on_default_branch(cwd: str | Path, branch: str) -> bool:
    """Whether `branch` is the one `origin` names as its default.

    Read from this clone's `origin/HEAD`, with no network call; a clone that
    never recorded one is taken to default to `main` or `master`.
    """
    default = git(cwd, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
    if default and "/" in default:
        return branch == default.split("/", 1)[1]
    return branch in {"main", "master"}


def workspace(cwd: object, *, head: bool = True) -> dict | None:
    """`{repo_url, branch, head_sha}` for a work tree with an `origin` remote.

    `head=False` skips the HEAD probe and omits `head_sha`, for callers that
    only need to know which branch they are on.
    """
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        return None
    if git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    repo_url = public_remote(git(cwd, "remote", "get-url", "origin"))
    if repo_url is None:
        return None
    branch = git(cwd, "symbolic-ref", "--short", "-q", "HEAD") or None
    if branch is not None and len(branch) > MAX_BRANCH_CHARS:
        branch = None
    result = {"repo_url": repo_url, "branch": branch}
    if head:
        result["head_sha"] = head_sha(cwd)
    return result
