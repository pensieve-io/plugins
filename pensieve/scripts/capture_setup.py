"""Import a device setup file downloaded from Pensieve personal settings.

Capture consent is managed in the app. This command only installs a account-scoped
upload credential; it never signs in, reads host credentials or prints secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from conversation_capture import (
    CONFIG_PATH,
    MAX_CONFIG_BYTES,
    encoded,
    private_directory,
    private_file,
    profiles,
)


def save_config(path, value):
    private_directory(path.parent)
    raw = encoded(value)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("Capture config exceeds its size limit")
    fd, temporary = tempfile.mkstemp(prefix=".capture-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # Validate all profiles before replacing working credentials.
        profiles(Path(temporary))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(source: Path, destination: Path):
    if source.absolute() == destination.absolute():
        raise ValueError("Choose the downloaded setup file")
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Setup must be an owned regular file")
        raw = handle.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("Setup file exceeds its size limit")
    incoming = json.loads(raw)
    if not isinstance(incoming, dict) or set(incoming) != {"version", "profiles"}:
        raise ValueError("Invalid setup file")
    if (
        incoming["version"] != 2
        or not isinstance(incoming["profiles"], list)
        or len(incoming["profiles"]) != 1
    ):
        raise ValueError("Setup must contain one device credential")
    profile = incoming["profiles"][0]
    if not isinstance(profile, dict) or set(profile) != {"user_id", "upload_key"}:
        raise ValueError("Invalid setup credential")
    # Read and validate the existing file before merging; never loosen its permissions.
    profiles(destination)
    existing = (
        json.loads(private_file(destination, MAX_CONFIG_BYTES))
        if destination.exists()
        else {"version": 2, "profiles": []}
    )
    identity = profile["user_id"]
    existing["profiles"] = [
        item for item in existing["profiles"] if item["user_id"] != identity
    ] + [profile]
    save_config(destination, existing)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("setup_file", type=Path)
    parser.add_argument(
        "--config", type=Path, default=Path(os.environ.get("PENSIEVE_CAPTURE_CONFIG", CONFIG_PATH))
    )
    args = parser.parse_args()
    try:
        install(args.setup_file, args.config)
    except (OSError, ValueError, KeyError, TypeError):
        print(
            "Setup could not be imported. Check the downloaded file and private config permissions.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        "Device configured. Manage capture in Pensieve Settings → Connected clients. Remove the downloaded setup file, then start or resume your work session."
    )


if __name__ == "__main__":
    main()
