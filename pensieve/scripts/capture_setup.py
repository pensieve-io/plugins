"""Connect the installed plugin through browser approval. No secrets on stdout."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from capture_config import CONFIG_PATH, ReconnectRequired, config_lock, load_config
from capture_pairing import API_BASE, RUNTIMES, checked_base, pairing_path, start, wait


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "poll", "status", "sync"))
    parser.add_argument("--client", choices=("codex", "claude"), required=True)
    parser.add_argument("--runtime", choices=sorted(RUNTIMES), default="unknown")
    parser.add_argument("--host-version", default="")
    parser.add_argument("--wait", type=float, default=0)
    parser.add_argument("--restart", action="store_true")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("PENSIEVE_CAPTURE_CONFIG", str(CONFIG_PATH))),
    )
    parser.add_argument("--endpoint", type=checked_base, default=API_BASE)
    args = parser.parse_args()
    try:
        if args.action == "start":
            result = start(
                args.config,
                args.client,
                args.runtime,
                args.host_version,
                args.endpoint,
                args.restart,
            )
        elif args.action == "poll":
            result = wait(args.config, args.client, args.wait)
        elif args.action == "sync":
            from capture_history import sync

            sync(args.config, args.client, seconds=args.wait or 45, base=args.endpoint)
            result = {
                "status": "sync_checked",
                "message": "Check import progress in Pensieve. Pending work resumes on later agent hooks.",
            }
        else:
            if args.config.exists():
                with config_lock(args.config):
                    value = load_config(args.config, args.client)
            else:
                value = {"profiles": []}
            result = {
                "status": "configured"
                if any(p["client"] == args.client for p in value["profiles"])
                else "not_configured",
                "client": args.client,
                "pairing_pending": pairing_path(args.config, args.client).exists(),
            }
    except ReconnectRequired as error:
        result = {"status": "reconnect_required", "message": str(error)}
    except BlockingIOError:
        result = {"status": "busy", "message": "Another hook is finishing setup. Retry shortly."}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError):
        result = {
            "status": "setup_error",
            "message": "Setup could not finish. Check private config permissions and start again.",
        }
    # Deliberately construct safe result objects rather than echoing HTTP/error bodies.
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] in {"setup_error", "unavailable"}:
        sys.exit(1)


if __name__ == "__main__":
    main()
