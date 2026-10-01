"""Private proof passed alongside native OAuth; this helper never signs in."""

import fcntl
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

HEADER = "X-Pensieve-Plugin"


def header_path(config, client):
    if client not in {"claude", "codex"}:
        raise ValueError("Invalid plugin client")
    return config.with_name(f"mcp-headers-{client}.json")


def prepare_headers(config, client):
    path = header_path(config, client)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = path.parent.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.getuid()
        or stat.S_IMODE(directory.st_mode) != 0o700
    ):
        raise ValueError("Plugin credentials require an owned private directory")
    try:
        return read_headers(path, client)
    except FileNotFoundError:
        pass
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as lock:
        check_private(lock)
        # Startup helpers and lifecycle hooks can race on first installation.
        # This small file-only critical section never performs network I/O.
        import time

        deadline = time.monotonic() + 0.5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        try:
            return read_headers(path, client)
        except FileNotFoundError:
            headers = {HEADER: f"{client} pcap_{secrets.token_urlsafe(32)}"}
            fd, temporary = tempfile.mkstemp(prefix=".mcp-headers-", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as target:
                    json.dump(headers, target)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return headers


def read_headers(path, client):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        check_private(source)
        headers = json.loads(source.read(1025))
    if (
        not isinstance(headers, dict)
        or set(headers) != {HEADER}
        or not isinstance(headers[HEADER], str)
        or not re.fullmatch(rf"{client} pcap_[A-Za-z0-9_-]{{43}}", headers[HEADER])
    ):
        raise ValueError("Invalid private plugin proof")
    return headers


def check_private(handle):
    info = os.fstat(handle.fileno())
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("Plugin credentials require an owned private file")


if __name__ == "__main__":
    try:
        print(
            json.dumps(prepare_headers(Path.home() / ".config/pensieve/capture.json", sys.argv[1]))
        )
    except (OSError, ValueError):
        print(
            "Pensieve private connection is unavailable; check its file permissions.",
            file=sys.stderr,
        )
        raise SystemExit(1)
