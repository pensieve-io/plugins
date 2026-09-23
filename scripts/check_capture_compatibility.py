#!/usr/bin/env python3
"""Check this candidate against both deployed capture services without uploads."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pensieve/scripts"))

from capture_pairing import API_BASE, checked_base
from capture_protocol import VERSION, check
from conversation_capture import UPLOAD_ENDPOINT, checked_endpoint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", type=checked_base, default=API_BASE)
    parser.add_argument("--upload", type=checked_endpoint, default=UPLOAD_ENDPOINT)
    args = parser.parse_args()
    result = {
        "protocol_version": VERSION,
        "pairing": check(args.api, "pairing", 5, fresh=True),
        "upload": check(args.upload, "upload", 5, fresh=True),
    }
    print(json.dumps(result))
    return 0 if result["pairing"] == result["upload"] == "compatible" else 1


if __name__ == "__main__":
    raise SystemExit(main())
