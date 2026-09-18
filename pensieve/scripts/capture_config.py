"""Private, client-scoped capture credentials. Standard library, Python 3.9+."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

CONFIG_PATH = Path.home() / ".config/pensieve/capture.json"
MAX_CONFIG_BYTES = 65536
CLIENTS = {"codex", "claude"}


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def private_file(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise ValueError("capture config/state must be an owned regular file with mode 0600")
        data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError("capture config exceeds its size limit")
        return data


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("capture state directory must be owned, private (0700), and not a symlink")


@contextmanager
def private_lock(path: Path):
    """Serialise hook/setup writers without waiting inside a host hook deadline."""
    private_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("capture lock must be an owned private file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def save_private_json(path: Path, value: object) -> None:
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        private_file(path, MAX_CONFIG_BYTES)
    raw = encoded(value)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("Capture config exceeds its size limit")
    fd, temporary = tempfile.mkstemp(prefix=".capture-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def validate_profile(profile: object) -> dict:
    if not isinstance(profile, dict) or set(profile) != {
        "user_id",
        "client",
        "upload_key",
        "installation_id",
        "runtime",
        "host_version",
    }:
        raise ValueError("invalid capture profile")
    key = profile["upload_key"]
    if (
        not valid_uuid(profile["user_id"])
        or profile["client"] not in CLIENTS
        or not isinstance(key, str)
        or not 16 <= len(key) <= 4096
        or any(character.isspace() for character in key)
        or (profile["installation_id"] is not None and not valid_uuid(profile["installation_id"]))
        or not isinstance(profile["runtime"], str)
        or len(profile["runtime"]) > 100
        or not isinstance(profile["host_version"], str)
        or len(profile["host_version"]) > 100
    ):
        raise ValueError("invalid capture profile")
    return profile


def load_config(path: Path, client: str) -> dict:
    """Migrate pilot credentials once, to the invoking client only.

    Legacy credentials served all clients. Migration deliberately does not create
    a credential for a different client; it must pair separately. Callers that
    modify profiles hold ``config_lock`` around this read and their write.
    """
    if client not in CLIENTS:
        raise ValueError("invalid capture client")
    try:
        value = json.loads(private_file(path, MAX_CONFIG_BYTES))
    except FileNotFoundError:
        return {"version": 3, "profiles": []}
    if not isinstance(value, dict) or set(value) != {"version", "profiles"}:
        raise ValueError("capture config must contain version and profiles")
    if not isinstance(value["profiles"], list) or value["version"] not in {2, 3}:
        raise ValueError("unsupported capture config")
    legacy = value["version"] == 2
    if legacy:
        migrated = []
        for old in value["profiles"]:
            if not isinstance(old, dict) or set(old) != {"user_id", "upload_key"}:
                raise ValueError("invalid capture profile")
            migrated.append(
                {
                    **old,
                    "client": client,
                    "installation_id": None,
                    "runtime": "unknown",
                    "host_version": "",
                }
            )
        value = {"version": 3, "profiles": migrated}
    seen = set()
    for profile in value["profiles"]:
        validate_profile(profile)
        identity = (profile["user_id"], profile["client"])
        if identity in seen:
            raise ValueError("duplicate capture profile")
        seen.add(identity)
    if legacy:
        save_private_json(path, value)
    return value


def config_lock(path: Path):
    return private_lock(path.with_name(path.name + ".lock"))


def profiles(path: Path, client: str = "codex") -> dict[str, str]:
    # Preserve capture-off as a read-only fast path, without making directories.
    if not path.exists() and not path.is_symlink():
        return {}
    with config_lock(path):
        value = load_config(path, client)
    return {
        item["user_id"]: item["upload_key"]
        for item in value["profiles"]
        if item["client"] == client
    }


def install_profile(path: Path, profile: dict) -> None:
    validate_profile(profile)
    with config_lock(path):
        value = load_config(path, profile["client"])
        identity = (profile["user_id"], profile["client"])
        value["profiles"] = [
            item for item in value["profiles"] if (item["user_id"], item["client"]) != identity
        ] + [profile]
        save_private_json(path, value)
